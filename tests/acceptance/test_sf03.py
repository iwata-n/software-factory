import copy
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from software_factory.contracts import ProjectPolicy, TaskSpec, digest
from software_factory.models import AuditEvent, Job, Principal, Project, Repository, Run
from software_factory.resolver import ReferenceNotFound, ResolutionUnavailable
from tests.conftest import example_spec


@pytest.mark.ac("SF-03-AC01")
def test_compiler_pins_every_contract_field(world, evidence):
    response = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
    assert response.status_code == 202
    data = response.json()
    snapshot = data["snapshot"]
    assert response.headers["location"] == f"/v1/runs/{data['run_id']}"
    with world.database.transaction() as session:
        policy = session.get(Project, world.project_id).policy
        assert (
            session.scalar(
                select(func.count()).select_from(Job).where(Job.run_id == data["run_id"])
            )
            == 1
        )
    spec = TaskSpec.model_validate(example_spec(world.repository_id)).model_dump()
    expected = {
        "task_spec": spec,
        "task_spec_hash": digest(spec),
        "repository_id": world.repository_id,
        "base_sha": "a" * 40,
        "workflow_type": "implement_and_review",
        "workflow_version": 1,
        "policy": policy,
        "policy_hash": digest(policy),
        "agent_adapter": "fake",
        "agent_configuration": ProjectPolicy.model_validate(policy).agent_configuration,
        "execution_image_digest": policy["execution_image_digest"],
        "verification_profile": spec["verification"],
        "verification_profile_hash": digest(spec["verification"]),
    }
    assert {key: snapshot[key] for key in expected} == expected
    assert snapshot["execution"] == spec["execution"]
    evidence(expected, snapshot, run_id=data["run_id"], workspace_id=world.workspace_id)


@pytest.mark.ac("SF-03-AC02")
def test_new_spec_policy_ref_never_mutate_started_run(world, evidence):
    original = copy.deepcopy(world.run()["snapshot"])
    spec = example_spec(world.repository_id)
    spec["task"]["objective"] = "new objective"
    assert world.post(f"/v1/tasks/{world.task_id}/spec-versions", spec).status_code == 201
    with world.database.transaction() as session:
        project = session.get(Project, world.project_id)
        project.policy = project.policy | {
            "job_reservation_microusd": 50000,
            "agent_configuration": {"version": "2"},
        }
        session.get(Repository, world.repository_id).base_sha = "b" * 40
    new = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 2}).json()
    assert world.run()["snapshot"] == original
    assert new["snapshot"]["base_sha"] == "b" * 40
    assert new["snapshot"]["agent_configuration"] == {"version": "2"}
    assert new["snapshot"]["task_spec"]["task"]["objective"] == "new objective"
    evidence(
        "old snapshot byte-equivalent, new run uses updated settings",
        {
            "old_hash": digest(world.run()["snapshot"]),
            "initial_hash": digest(original),
            "new_hash": digest(new["snapshot"]),
        },
    )


@pytest.mark.ac("SF-03-AC03", "C-03")
@pytest.mark.parametrize("iteration", range(20))
def test_run_start_concurrent_replay_and_location(world, evidence, iteration):
    barrier = Barrier(2)
    key = f"same-run-{iteration}"

    def start(_):
        barrier.wait(timeout=5)
        return world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(start, range(2)))
    assert all(r.status_code == 202 for r in responses)
    assert responses[0].json() == responses[1].json()
    assert responses[0].headers["location"] == responses[1].headers["location"]
    result = responses[0].json()
    replay = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key=key)
    assert replay.json() == result
    assert (
        world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 99}, key=key).status_code
        == 409
    )
    with world.database.transaction() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Job).where(Job.run_id == result["run_id"])
            )
            == 1
        )
    evidence(
        "one run/job; identical status/body/Location; different request 409",
        {
            "run_id": result["run_id"],
            "location": replay.headers["location"],
            "iteration": iteration,
        },
    )


@pytest.mark.ac("SF-03-AC04")
@pytest.mark.parametrize(("attempts", "seconds"), [(1, 19), (2, 20), (3, 2700)])
def test_effective_limits_and_invalid_verification(world, evidence, attempts, seconds):
    with world.database.transaction() as session:
        project = session.get(Project, world.project_id)
        project.policy = project.policy | {
            "max_infra_attempts_per_job": 2,
            "max_job_execution_seconds": 20,
        }
    spec = example_spec(world.repository_id)
    spec["execution"] = {
        "max_infra_attempts_per_job": attempts,
        "max_job_execution_seconds": seconds,
    }
    version = world.post(f"/v1/tasks/{world.task_id}/spec-versions", spec).json()["spec_version"]
    response = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": version})
    assert response.status_code == 202
    execution = response.json()["snapshot"]["execution"]
    assert execution["max_infra_attempts_per_job"] == min(attempts, 2)
    assert execution["max_job_execution_seconds"] == min(seconds, 20)
    for verification in [
        {"profile_id": "other", "required_checks": ["unit_tests", "review"]},
        {"profile_id": "default", "required_checks": ["unit_tests"]},
    ]:
        invalid = example_spec(world.repository_id) | {"verification": verification}
        assert world.post(f"/v1/tasks/{world.task_id}/spec-versions", invalid).status_code == 422
    assert world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 999}).status_code == 404
    evidence({"attempts": min(attempts, 2), "seconds": min(seconds, 20)}, execution)


@pytest.mark.ac("SF-03-AC05")
@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [
        (ReferenceNotFound, 422, "BASE_REF_NOT_REGISTERED"),
        (ResolutionUnavailable, 503, "SOURCE_RESOLUTION_UNAVAILABLE"),
        ("invalid-sha", 422, "INVALID_BASE_SHA"),
    ],
)
def test_resolver_errors_create_no_run_or_job(world, evidence, fault, status, code):
    service = world.client.app.state.service

    class Resolver:
        def resolve(self, reference):
            if isinstance(fault, type):
                raise fault("untrusted provider failure")
            return fault

    service.resolver = Resolver()
    with world.database.transaction() as session:
        before = (
            session.scalar(select(func.count()).select_from(Run)),
            session.scalar(select(func.count()).select_from(Job)),
        )
    response = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
    assert response.status_code == status and response.json()["code"] == code
    with world.database.transaction() as session:
        after = (
            session.scalar(select(func.count()).select_from(Run)),
            session.scalar(select(func.count()).select_from(Job)),
        )
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.operation == "source.resolve.failed")
        )
        assert audit.body["code"] == code
    assert before == after
    evidence(
        {"status": status, "code": code, "run_job_counts": before},
        {"status": response.status_code, "code": response.json()["code"], "run_job_counts": after},
    )


@pytest.mark.ac("SF-03-AC05", "C-03")
def test_accepted_start_replay_does_not_resolve_again(world, evidence):
    first = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key="accepted")
    resolver = Mock()
    resolver.resolve.side_effect = ResolutionUnavailable
    world.client.app.state.service.resolver = resolver
    second = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key="accepted")
    assert second.json() == first.json() and second.headers["location"] == first.headers["location"]
    resolver.resolve.assert_not_called()
    evidence(
        "stored accepted response without resolving mutable reference", second.json()["run_id"]
    )


@pytest.mark.ac("SF-03-AC05", "SF-03-AC06")
def test_resolution_has_no_db_lock_and_revocation_wins(world, evidence):
    entered = Event()
    release = Event()

    class SlowResolver:
        def resolve(self, reference):
            entered.set()
            assert release.wait(5)
            return reference.registered_sha

    world.client.app.state.service.resolver = SlowResolver()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            lambda: world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
        )
        assert entered.wait(5)
        started = time.monotonic()
        with world.database.transaction() as session:
            session.get(Repository, world.repository_id).active = False
        assert time.monotonic() - started < 1
        release.set()
        response = request.result(timeout=5)
    assert response.status_code == 403
    assert world.claim().status_code == 204
    assert world.run()["status"] == "BLOCKED"
    assert world.run()["stop_reason"] == "REPOSITORY_ACCESS_REVOKED"
    evidence(
        "no lock across resolve; revocation prevents compilation and claim",
        {"status": response.status_code, "run_status": world.run()["status"]},
    )


@pytest.mark.ac("SF-03-AC06", "C-06")
def test_current_permissions_override_snapshot_and_publication_boundary(world, evidence):
    original = world.run()["snapshot"]
    with world.database.transaction() as session:
        session.get(Repository, world.repository_id).active = False
    assert world.claim().status_code == 204
    publisher = Mock()
    service = world.client.app.state.service
    with world.database.transaction() as session:
        service.lock_workspace(session, world.workspace_id)
        run = session.get(Run, world.run_id)
        if service.repository_allowed(session, run):
            publisher.publish(run)
    publisher.publish.assert_not_called()
    assert world.run()["snapshot"] == original
    with world.database.transaction() as session:
        session.get(Principal, world.worker_id).active = False
    assert world.claim().status_code == 401
    evidence(
        "snapshot unchanged, revoked repository blocked, revoked worker 401, publisher calls 0",
        {"reason": world.run()["stop_reason"], "publisher_calls": publisher.publish.call_count},
    )
