from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from software_factory.db import db_now
from software_factory.models import (
    Gate,
    Job,
    Principal,
    Project,
    PublicationOperation,
    Publisher,
    Repository,
    Run,
    new_id,
)
from software_factory.publisher_backend import PublicationError
from software_factory.service import issue_token, token_hash
from tests.test_assembler import runner as runner
from tests.test_publications import publication as publication
from tests.test_publisher_backend import backend as backend

from .helpers import dump
from .test_sf10 import pipeline as pipeline
from .test_sf11 import source_bundle as source_bundle


@pytest.mark.ac("SF-17-AC01", "SF-17-AC02")
def test_candidate_objects_and_one_draft_are_exact_in_the_git_provider_contract(backend, evidence):
    pub, api, inputs, trace, remote_head = backend
    first = api.publish(inputs)
    assert api.publish(inputs) == first
    assert trace["pushes"] == 1 and len(trace["pulls"]) == 1
    assert (
        first["head_sha"] == remote_head() == inputs["operation"]["target"]["candidate"]["head_sha"]
    )
    assert first["draft"] is True
    evidence(
        "exact Candidate head and one Draft in contract; real GitHub evidence is separate",
        {"head_sha": remote_head(), "pushes": trace["pushes"], "draft_count": 1},
        run_id=pub.world.run_id,
        candidate_id=inputs["operation"]["candidate_id"],
    )


@pytest.mark.ac("SF-17-AC02", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_two_dispatch_requests_create_one_publication_operation(publication, trial, evidence):
    barrier = Barrier(2)

    def begin(index):
        barrier.wait(5)
        return publication.begin(key="dispatch-race-" + str(index))

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(begin, [0, 1]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r.json() for r in responses if r.status_code == 200)
    with publication.world.database.transaction() as session:
        assert len(list(session.scalars(select(PublicationOperation)))) == 1
    assert winner["target"]["head_ref"] == "codex/factory/" + publication.world.run_id
    evidence(
        "20 concurrent dispatch trials authorize one immutable operation identity",
        {"trial": trial, "statuses": [r.status_code for r in responses], "operations": 1},
    )


@pytest.mark.ac("SF-17-AC03")
def test_racing_human_commit_is_preserved_and_recorded_as_blocked(backend, evidence):
    pub, api, inputs, trace, remote_head = backend
    trace["race"] = True
    with pytest.raises(PublicationError) as denied:
        api.publish(inputs)
    assert denied.value.reason == "REMOTE_HEAD_CONFLICT"
    assert remote_head() == inputs["operation"]["target"]["candidate"]["base_sha"]
    reply = pub.report(
        inputs["operation"],
        {
            "delivery_id": "conflicting-head",
            "target_hash": inputs["operation"]["target_hash"],
            "status": "CONFLICT",
            "reason": denied.value.reason,
            "observed_head_sha": remote_head(),
        },
    )
    assert reply.status_code == 200 and pub.world.run()["status"] == "BLOCKED"
    assert not trace["pulls"]
    evidence(
        "an unexpected commit wins the explicit remote lease and blocks publication",
        {"reason": denied.value.reason, "preserved_head": remote_head(), "drafts_created": 0},
    )


@pytest.mark.ac("SF-17-AC04", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_cancel_dispatch_race_has_a_single_authorization_order(publication, trial, evidence):
    pub, world = publication, publication.world
    revision = world.run()["state_revision"]
    barrier = Barrier(2)

    def invoke(index):
        barrier.wait(5)
        if index == 0:
            return pub.begin(key="dispatch-cancel-race")
        return world.post(
            f"/v1/runs/{world.run_id}/cancel",
            {"expected_run_revision": revision, "reason": "cancel at the dispatch boundary"},
        )

    with ThreadPoolExecutor(2) as pool:
        dispatch, cancel = list(pool.map(invoke, [0, 1]))
    assert sorted([dispatch.status_code, cancel.status_code]) in ([200, 409], [202, 409])
    sends = 0
    if dispatch.status_code == 200:
        assert (
            world.post(
                f"/v1/runs/{world.run_id}/cancel",
                {
                    "expected_run_revision": world.run()["state_revision"],
                    "reason": "cancel after dispatch",
                },
            ).status_code
            == 202
        )
        sends = 1
        assert pub.report(dispatch.json()).status_code == 200
    else:
        assert not dump(world.database)["publication_operations"]
    assert world.run()["status"] == "CANCELLED"
    evidence(
        "20 cancellation races send nothing before authorization and retain late external history",
        {
            "trial": trial,
            "dispatch": dispatch.status_code,
            "cancel": cancel.status_code,
            "simulated_external_sends": sends,
            "run_status": world.run()["status"],
        },
    )


@pytest.mark.ac("SF-17-AC05", "C-02", "C-06")
def test_publisher_inputs_results_and_credentials_are_not_worker_or_user_capabilities(
    publication, evidence
):
    pub, world = publication, publication.world
    operation = pub.begin().json()
    prefix = f"/v1/publisher/operations/{operation['operation_id']}"
    body = pub.success(operation)
    before = dump(world.database)
    for token in (world.user_token, world.worker_token, world.other_user_token):
        assert world.get(prefix + "/inputs", token=token).status_code == 403
        assert world.post(prefix + "/reports", body, token=token).status_code == 403
    assert (
        pub.report(operation, body | {"delivery_id": "ghp_PRIVATE_PUBLISHER_CANARY"}).status_code
        == 422
    )
    assert dump(world.database) == before
    with world.database.transaction() as session:
        session.get(Principal, pub.identity).active = False
    assert world.get(prefix + "/inputs", token=pub.secret).status_code == 401
    evidence(
        "active Publisher identity is required; credential-like report fields are rejected",
        {"wrong_role": 403, "revoked": 401, "secret_input": 422, "negative_effects": 0},
    )


@pytest.mark.ac("SF-17-AC06")
@pytest.mark.parametrize(
    "change", ["candidate", "profile", "policy", "base", "repository", "gate", "hash"]
)
def test_invalid_or_retired_publication_target_never_enters_dispatch(publication, change, evidence):
    pub, world = publication, publication.world
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        if change == "candidate":
            run.current_candidate_id = None
        elif change == "profile":
            run.snapshot = run.snapshot | {"verification_profile_hash": "0" * 64}
        elif change == "policy":
            project = session.get(Project, world.project_id)
            project.policy = project.policy | {"job_reservation_microusd": 200000}
        elif change == "base":
            run.base_sha = "b" * 40
        elif change == "repository":
            session.get(Repository, world.repository_id).active = False
        elif change == "gate":
            forged = Gate(
                workspace_id=world.workspace_id,
                run_id=world.run_id,
                candidate_id=pub.gate.candidate_id,
                stage="LOCAL",
                status="FAIL",
                reason="fixture",
                target=pub.gate.target,
                evidence_ids=[],
                evidence_digest="e" * 64,
                policy_version=1,
                metadata_body={},
                created_at=db_now(session),
            )
            session.add(forged)
            session.flush()
            job = session.get(Job, pub.pending["job_id"])
            job.payload = job.payload | {
                "gate_id": forged.id,
                "evidence_digest": forged.evidence_digest,
            }
    before = dump(world.database)
    response = pub.begin(
        body={
            "expected_run_revision": world.run()["state_revision"],
            "target_hash": "0" * 64 if change == "hash" else pub.pending["target_hash"],
        }
    )
    assert response.status_code == 409 and dump(world.database) == before
    evidence(
        "stale Candidate/Profile/Policy/base/permission/hash cannot authorize any external call",
        {"change": change, "status": 409, "operations_created": 0},
    )


@pytest.mark.ac("SF-17-AC04", "SF-17-AC06", "C-04")
@pytest.mark.parametrize("point", ["publication.in_flight", "publication.reported"])
def test_dispatch_and_result_audit_share_the_atomic_commit(publication, point, evidence):
    pub, world = publication, publication.world
    operation = pub.begin().json() if point == "publication.reported" else None
    before = dump(world.database)
    service = world.client.app.state.service

    def fail(name):
        if name == point:
            raise RuntimeError("injected publication commit failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            pub.report(operation) if operation else pub.begin()
    finally:
        service.checkpoint = lambda name: None
    assert dump(world.database) == before
    result = pub.report(operation) if operation else pub.begin()
    assert result.status_code == 200
    evidence(
        "operation/job/Run/audit/Outbox/replay roll back together before commit",
        {"checkpoint": point, "rollback_equal": True},
    )


@pytest.mark.ac("SF-17-AC05", "C-02")
def test_machine_workspace_assignment_and_user_publication_view_are_scoped(publication, evidence):
    pub, world = publication, publication.world
    operation = pub.begin().json()
    prefix = f"/v1/publisher/operations/{operation['operation_id']}"
    secrets = []
    for workspace in (world.workspace_id, world.other_workspace_id):
        identity, secret = new_id(), issue_token()
        with world.database.transaction() as session:
            session.add(
                Principal(
                    id=identity,
                    workspace_id=workspace,
                    role="PUBLISHER",
                    token_hash=token_hash(secret),
                )
            )
            session.flush()
            session.add(Publisher(id=identity, workspace_id=workspace, name="unassigned publisher"))
        secrets.append(secret)
    before = dump(world.database)
    for secret in secrets:
        assert world.get(prefix + "/inputs", token=secret).status_code == 404
        assert (
            world.post(prefix + "/reports", pub.success(operation), token=secret).status_code == 404
        )
        assert world.get("/v1/publisher/queue", token=secret).json() == {"operations": []}
    path = f"/v1/runs/{world.run_id}/publication"
    assert world.client.get(path).status_code == 401
    assert world.get(path, token=world.worker_token).status_code == 403
    assert world.get(path, token=world.other_user_token).status_code == 404
    assert world.get(path).status_code == 200
    assert dump(world.database) == before
    evidence(
        "different Workspace or unassigned machine identity cannot access an operation",
        {"machine_denied": 404, "user_view_statuses": [401, 403, 404, 200], "effects": 0},
    )


@pytest.mark.ac("SF-17-AC02", "SF-17-AC06")
def test_publication_identity_is_immutable_in_the_database(publication, evidence):
    operation = publication.begin().json()
    with (
        pytest.raises(IntegrityError, match="PUBLICATION_IDENTITY_IMMUTABLE"),
        publication.world.database.transaction() as session,
    ):
        session.execute(
            text("UPDATE publication_operations SET target='{}' WHERE id=:id"),
            {"id": operation["operation_id"]},
        )
    evidence(
        "SQL cannot rebind an operation to another target after authorization",
        {"operation_id": operation["operation_id"], "identity_rebinding": "rejected"},
    )
