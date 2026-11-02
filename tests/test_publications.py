from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.models import (
    Principal,
    Project,
    PublicationObservation,
    PublicationOperation,
    Publisher,
    PublisherGrant,
    Repository,
    Run,
    new_id,
)
from software_factory.service import issue_token, token_hash
from tests.acceptance.helpers import dump
from tests.acceptance.test_sf10 import pipeline as pipeline
from tests.acceptance.test_sf11 import source_bundle as source_bundle
from tests.acceptance.test_sf12_integration import pass_local
from tests.test_assembler import runner as runner


@pytest.fixture
def publication(pipeline):
    world = pipeline.world
    remote = "https://github.com/iwata-n/software-factory.git"
    secret, identity = issue_token(), new_id()
    with world.database.transaction() as s:
        s.get(Repository, world.repository_id).remote_url = remote
        run = s.get(Run, world.run_id)
        run.snapshot = run.snapshot | {"source": run.snapshot["source"] | {"remote_url": remote}}
        s.add(
            Principal(
                id=identity,
                workspace_id=world.workspace_id,
                role="PUBLISHER",
                token_hash=token_hash(secret),
            )
        )
        s.flush()
        s.add(
            Publisher(id=identity, workspace_id=world.workspace_id, name="trusted test publisher")
        )
        s.flush()
        s.add(
            PublisherGrant(
                workspace_id=world.workspace_id,
                publisher_id=identity,
                repository_id=world.repository_id,
            )
        )
    gate = pass_local(pipeline)

    class Publication:
        def __init__(self):
            self.world, self.pipeline, self.secret, self.identity, self.gate = (
                world,
                pipeline,
                secret,
                identity,
                gate,
            )
            queued = world.get("/v1/publisher/queue", token=secret)
            assert queued.status_code == 200, queued.text
            self.pending = queued.json()["operations"][0]

        def begin(self, *, key="begin", body=None, token=None):
            data = {
                "expected_run_revision": self.pending["target"]["run_revision"],
                "target_hash": self.pending["target_hash"],
            }
            return world.post(
                f"/v1/publisher/jobs/{self.pending['job_id']}/begin",
                data if body is None else body,
                token=token or secret,
                key=key,
            )

        def success(self, operation):
            target = operation["target"]
            return {
                "delivery_id": "provider-result",
                "target_hash": operation["target_hash"],
                "status": "SUCCEEDED",
                "pull_request": {
                    "repository": target["repository"],
                    "number": 123,
                    "external_id": "provider-123",
                    "url": "https://github.com/iwata-n/software-factory/pull/123",
                    "base_ref": target["base_ref"],
                    "base_sha": target["candidate"]["base_sha"],
                    "head_ref": target["head_ref"],
                    "head_sha": target["candidate"]["head_sha"],
                    "draft": True,
                    "state": "OPEN",
                },
            }

        def report(self, operation, body=None, *, key="report"):
            return world.post(
                f"/v1/publisher/operations/{operation['operation_id']}/reports",
                body or self.success(operation),
                token=secret,
                key=key,
            )

    return Publication()


def test_publication_intent_begin_result_and_replay_are_scoped_and_atomic(publication):
    pub, world = publication, publication.world
    response = pub.begin()
    assert response.status_code == 200, response.text
    operation = response.json()
    before = dump(world.database)
    assert pub.begin().json() == operation
    assert pub.begin(key="another-begin").status_code == 409
    assert dump(world.database) == before
    inputs = world.get(
        f"/v1/publisher/operations/{operation['operation_id']}/inputs", token=pub.secret
    )
    assert inputs.status_code == 200 and len(inputs.json()["sources"]) == 1
    assert (
        inputs.json()["sources"][0]["candidate"]["head_sha"]
        == operation["target"]["candidate"]["head_sha"]
    )
    response = pub.report(operation)
    assert response.status_code == 200, response.text
    assert (world.run()["status"], world.run()["phase"]) == ("WAITING_EXTERNAL", "WAIT_CI")
    after = dump(world.database)
    assert pub.report(operation).json() == response.json()
    assert dump(world.database) == after
    result = world.get(f"/v1/runs/{world.run_id}/publication").json()
    assert result["pull_request"]["head_sha"] == operation["target"]["candidate"]["head_sha"]
    assert len(result["operations"]) == 1
    assert len(after["publication_observations"]) == 1
    assert world.run()["reserved_microusd"] == 0


@pytest.mark.parametrize("order", ["before", "after"])
def test_cancel_before_dispatch_sends_nothing_after_dispatch_preserves_external_history(
    publication, order
):
    pub, world = publication, publication.world
    operation = pub.begin().json() if order == "after" else None
    assert (
        world.post(
            f"/v1/runs/{world.run_id}/cancel",
            {
                "expected_run_revision": world.run()["state_revision"],
                "reason": "cancel publication",
            },
        ).status_code
        == 202
    )
    if operation:
        assert pub.report(operation).status_code == 200
        with world.database.transaction() as s:
            assert s.get(PublicationOperation, operation["operation_id"]).state == "SUCCEEDED"
            assert s.scalar(
                select(PublicationObservation).where(PublicationObservation.run_id == world.run_id)
            )
    else:
        assert pub.begin().status_code == 409
        assert not dump(world.database)["publication_operations"]
    assert world.run()["status"] == "CANCELLED"


@pytest.mark.parametrize("change", ["repository", "policy", "candidate"])
def test_late_external_success_survives_revocation_and_stale_context_without_successful_run(
    publication, change
):
    pub, world = publication, publication.world
    operation = pub.begin().json()
    with world.database.transaction() as s:
        if change == "repository":
            s.get(Repository, world.repository_id).active = False
        elif change == "policy":
            project = s.get(Project, world.project_id)
            project.policy = project.policy | {"job_reservation_microusd": 200000}
        else:
            s.get(Run, world.run_id).current_candidate_id = None
    response = pub.report(operation)
    assert response.status_code == 200, response.text
    result = world.get(f"/v1/runs/{world.run_id}/publication").json()
    assert result["operations"][0]["state"] == "SUCCEEDED" and result["pull_request"]
    assert world.run()["status"] != "SUCCEEDED" and world.run()["phase"] != "WAIT_CI"


def test_user_worker_other_tenant_and_ungranted_publisher_cannot_dispatch(publication):
    pub, world = publication, publication.world
    for token in (world.user_token, world.worker_token):
        before = dump(world.database)
        assert world.get("/v1/publisher/queue", token=token).status_code == 403
        assert pub.begin(token=token, key=str(uuid4())).status_code == 403
        assert dump(world.database) == before
    assert pub.begin(token=world.other_user_token).status_code == 403
    with world.database.transaction() as s:
        grant = s.scalar(select(PublisherGrant).where(PublisherGrant.publisher_id == pub.identity))
        s.delete(grant)
    before = dump(world.database)
    assert pub.begin().status_code == 404
    assert dump(world.database) == before
