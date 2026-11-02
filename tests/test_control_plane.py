from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from software_factory.adapters import FakeAgent
from software_factory.api import create_app
from software_factory.contracts import Complete
from software_factory.db import db_now
from software_factory.models import (
    Attempt,
    Enrollment,
    Job,
    OutboxEvent,
    Principal,
    Project,
    Repository,
    Run,
)
from software_factory.service import Service, token_hash

from .conftest import example_spec

pytestmark = pytest.mark.postgres


def completion(assignment):
    agent = FakeAgent()
    agent.start(assignment["job"])
    return {
        "fence": assignment["lease"]["fence"],
        "outcome": "EXECUTED",
        "artifact_ids": [],
        "result": agent.result().result,
    }


def submit(world, assignment, *, key=None, body=None, token=None):
    return world.post(
        f"/v1/worker/leases/{assignment['lease']['id']}/complete",
        body or completion(assignment),
        token=token or world.worker_token,
        key=key,
    )


def expire(world, assignment):
    with world.database.transaction() as session:
        attempt = session.scalar(
            select(Attempt).where(Attempt.lease_id == assignment["lease"]["id"])
        )
        attempt.expires_at = db_now(session) - 1


def ready_retries(world):
    with world.database.transaction() as session:
        for job in session.scalars(select(Job).where(Job.state == "RETRY_WAIT")):
            job.available_at = db_now(session) - 1


def test_run_snapshot_and_spec_version_are_fixed(world):
    original = world.run()
    spec = example_spec(world.repository_id)
    spec["task"]["objective"] = "A different objective"
    assert world.post(f"/v1/tasks/{world.task_id}/spec-versions", spec).status_code == 201
    assert world.run()["snapshot"] == original["snapshot"]
    assert world.run()["spec_version"] == 1
    reply = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 2}, key="new-run")
    assert reply.status_code == 202
    assert reply.headers["location"] == f"/v1/runs/{reply.json()['run_id']}"
    assert reply.json()["snapshot"]["task_spec"]["task"]["objective"] == "A different objective"
    replay = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 2}, key="new-run")
    assert replay.json() == reply.json()
    conflict = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key="new-run")
    assert conflict.status_code == 409


def test_fake_plan_and_implementation_stop_before_unimplemented_candidate(world):
    first = world.claim()
    assert first.status_code == 200, first.text
    assert first.json()["job"]["kind"] == "PLAN"
    assert submit(world, first.json()).status_code == 200
    assert world.run()["phase"] == "IMPLEMENT"
    second = world.claim().json()
    assert second["job"]["kind"] == "IMPLEMENT"
    assert second["job"]["plan"]["units"][0]["acceptance_ids"] == ["AC-1"]
    assert submit(world, second).status_code == 200
    run = world.run()
    assert run["status"] == "BLOCKED"
    assert run["phase"] == "ASSEMBLE_CANDIDATE"
    assert run["stop_reason"] == "CHANGE_ARTIFACT_MISSING"
    assert run["reserved_microusd"] == 0
    assert world.claim().status_code == 204
    graph = world.get(f"/v1/runs/{world.run_id}/graph").json()
    assert [job["state"] for job in graph["jobs"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert len(graph["edges"]) == 1


def test_at02_completion_replay_even_after_lease_expiry(world):
    assignment = world.claim().json()
    first = submit(world, assignment, key="lost-response")
    expire(world, assignment)
    replay = submit(world, assignment, key="lost-response")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    changed = completion(assignment) | {"usage_microusd": 1}
    assert submit(world, assignment, key="lost-response", body=changed).status_code == 409
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 3


def test_at01_at03_expired_lease_cannot_report_then_reassignment_increases_fence(world):
    old = world.claim().json()
    expire(world, old)
    # Expiry is enforced before the reconciler runs.
    assert submit(world, old).status_code == 409
    heartbeat = world.post(
        f"/v1/worker/leases/{old['lease']['id']}/heartbeat",
        {"fence": old["lease"]["fence"]},
        token=world.worker_token,
    )
    assert heartbeat.status_code == 409
    assert Service(world.database).reconcile() == 1
    ready_retries(world)
    current = world.claim().json()
    assert current["job"]["id"] == old["job"]["id"]
    assert current["lease"]["fence"] == old["lease"]["fence"] + 1
    assert submit(world, old).json()["code"] == "STALE_FENCE"
    assert world.run()["reserved_microusd"] == 100_000
    # Unknown consumption is conservatively charged at the reserved amount.
    assert world.run()["spent_microusd"] == 100_000


def test_infra_attempt_limit_is_not_a_code_repair_loop(world):
    for attempt_no in range(1, 4):
        assignment = world.claim().json()
        reply = world.post(
            f"/v1/worker/leases/{assignment['lease']['id']}/fail",
            {
                "fence": assignment["lease"]["fence"],
                "category": "INFRASTRUCTURE",
                "reason": "worker process crashed",
            },
            token=world.worker_token,
        )
        assert reply.status_code == 200
        ready_retries(world)
        if attempt_no < 3:
            assert world.run()["status"] == "QUEUED"
    assert world.run()["status"] == "FAILED"
    assert world.run()["stop_reason"] == "INFRA_ATTEMPTS_EXHAUSTED"
    graph = world.get(f"/v1/runs/{world.run_id}/graph").json()
    assert len(graph["jobs"]) == 1
    assert len(graph["jobs"][0]["attempts"]) == 3
    assert world.claim().status_code == 204


def test_inconclusive_result_blocks_without_retry(world):
    assignment = world.claim().json()
    reply = world.post(
        f"/v1/worker/leases/{assignment['lease']['id']}/fail",
        {
            "fence": assignment["lease"]["fence"],
            "category": "INCONCLUSIVE",
            "reason": "Acceptance condition needs a human decision",
        },
        token=world.worker_token,
    )
    assert reply.status_code == 200
    assert world.run()["status"] == "BLOCKED"
    assert world.claim().status_code == 204


@pytest.mark.parametrize(
    "result",
    [
        {"summary": "I finished planning"},
        {
            "units": [
                {
                    "id": "unit",
                    "objective": "x",
                    "acceptance_ids": ["wrong"],
                    "proposed_write_paths": ["src"],
                }
            ]
        },
        {
            "units": [
                {
                    "id": "unit",
                    "objective": "x",
                    "acceptance_ids": ["AC-1"],
                    "proposed_write_paths": ["../secret"],
                }
            ]
        },
    ],
)
def test_invalid_plan_job_completion_does_not_create_implementation(world, result):
    assignment = world.claim().json()
    reply = submit(world, assignment, body=completion(assignment) | {"result": result})
    assert reply.status_code == 200
    assert world.run()["status"] == "BLOCKED"
    assert world.claim().status_code == 204


def test_cancel_active_job_does_not_renew_lease_and_completion_cannot_progress(world):
    assignment = world.claim().json()
    with world.database.transaction() as session:
        before = session.scalar(select(Attempt.expires_at))
    reply = world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "Stop this run",
        },
    )
    assert reply.json()["status"] == "CANCELLING"
    heartbeat = world.post(
        f"/v1/worker/leases/{assignment['lease']['id']}/heartbeat",
        {
            "fence": assignment["lease"]["fence"],
        },
        token=world.worker_token,
    )
    assert heartbeat.json()["cancel_requested"] is True
    with world.database.transaction() as session:
        assert session.scalar(select(Attempt.expires_at)) == before
    assert submit(world, assignment).json()["run"]["status"] == "CANCELLED"
    assert world.claim().status_code == 204
    assert world.run()["reserved_microusd"] == 0


def test_cancel_without_active_job_is_terminal_and_idempotent(world):
    body = {"expected_run_revision": world.run()["state_revision"], "reason": "Stop queued run"}
    first = world.post(f"/v1/runs/{world.run_id}/cancel", body, key="cancel")
    assert first.json()["status"] == "CANCELLED"
    assert world.post(f"/v1/runs/{world.run_id}/cancel", body, key="cancel").json() == first.json()
    assert world.post(f"/v1/runs/{world.run_id}/cancel", body).status_code == 409


def test_cancel_and_lease_expiry_distinguish_logical_stop_from_physical_stop(world):
    assignment = world.claim().json()
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "Stop unreachable worker",
        },
    )
    expire(world, assignment)
    Service(world.database).reconcile()
    assert world.run()["status"] == "CANCELLED"
    graph = world.get(f"/v1/runs/{world.run_id}/graph").json()
    assert graph["jobs"][0]["attempts"][0]["stop_confirmation"] == "EXECUTION_RIGHT_EXPIRED"
    history = world.get(f"/v1/runs/{world.run_id}/event-history").json()["events"]
    expiry = next(e for e in history if e["kind"] == "LEASE_EXPIRED")
    assert expiry["body"]["physical_stop_confirmed"] is False


def test_at10_restart_restores_run_lease_snapshot_and_event_history(world):
    assignment = world.claim().json()
    previous = world.run()
    fresh_service = Service(world.database)
    assert fresh_service.reconcile() == 0
    with TestClient(create_app(world.database, reconcile_interval=3600)) as restarted:
        headers = {"Authorization": f"Bearer {world.user_token}"}
        assert restarted.get(f"/v1/runs/{world.run_id}", headers=headers).json() == previous
        replay = restarted.post(
            f"/v1/worker/leases/{assignment['lease']['id']}/complete",
            json=completion(assignment),
            headers={
                "Authorization": f"Bearer {world.worker_token}",
                "Idempotency-Key": "after-restart",
            },
        )
        assert replay.status_code == 200
        history = restarted.get(f"/v1/runs/{world.run_id}/event-history", headers=headers).json()
        revisions = [e["revision"] for e in history["events"]]
        assert revisions == sorted(set(revisions))


@pytest.mark.parametrize("suffix", ["", "/plan", "/graph", "/event-history"])
def test_at11_cross_workspace_run_and_log_access_is_hidden(world, suffix):
    assert (
        world.get(f"/v1/runs/{world.run_id}{suffix}", token=world.other_user_token).status_code
        == 404
    )


def test_at11_cross_workspace_task_reference_and_worker_lease_are_rejected(world):
    assert world.get(f"/v1/tasks/{world.task_id}", token=world.other_user_token).status_code == 404
    assert (
        world.post(
            f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, token=world.other_user_token
        ).status_code
        == 404
    )
    assignment = world.claim().json()
    other_worker = world.make_worker()
    assert submit(world, assignment, token=other_worker["worker_token"]).status_code == 404


def test_worker_user_separation_revocation_and_server_capabilities(world):
    assert world.claim(token=world.user_token).status_code == 403
    assert world.get(f"/v1/runs/{world.run_id}", token=world.worker_token).status_code == 403
    incapable = world.make_worker(capabilities=["IMPLEMENT", "adapter:fake"])
    assert world.claim(token=incapable["worker_token"]).status_code == 204
    with world.database.transaction() as session:
        principal = session.get(Principal, world.worker_id)
        principal.active = False
    assert world.claim().status_code == 401
    assert world.client.get(f"/v1/runs/{world.run_id}").status_code == 401


def test_enrollment_is_single_use_expiring_and_server_authorized(world):
    token = "test-enrollment-token-long-enough"
    with world.database.transaction() as session:
        session.add(
            Enrollment(
                workspace_id=world.workspace_id,
                token_hash=token_hash(token),
                capabilities=["PLAN", "adapter:fake"],
                expires_at=db_now(session) + 600,
            )
        )
    body = {"token": token, "name": "single-use"}
    assert (
        world.client.post("/v1/worker/enroll", json=body | {"workspace_id": "other"}).status_code
        == 422
    )
    assert world.client.post("/v1/worker/enroll", json=body).status_code == 201
    assert world.client.post("/v1/worker/enroll", json=body).status_code == 401


def test_at12_concurrent_claims_do_not_double_allocate_slot_or_budget(world):
    # Two ready runs allow this to expose a worker-slot race, not just a single-job race.
    world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return Service(world.database).claim(world.worker_token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1
        assert session.scalar(select(func.sum(Run.reserved_microusd))) == 100_000


def test_concurrent_workers_cannot_claim_same_job(world):
    other = world.make_worker()
    barrier = Barrier(2)

    def claim(token):
        barrier.wait(timeout=5)
        return Service(world.database).claim(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [world.worker_token, other["worker_token"]]))
    assert sum(result is not None for result in results) == 1


def test_concurrent_identical_completions_create_only_one_next_job(world):
    assignment = world.claim().json()
    body = completion(assignment)
    barrier = Barrier(2)

    def finish(_):
        barrier.wait(timeout=5)
        service = Service(world.database)
        return service.write(
            world.worker_token,
            "WORKER",
            "test-complete",
            "same",
            body,
            lambda s, a: service.complete(
                s,
                a,
                assignment["lease"]["id"],
                Complete.model_validate(body),
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, range(2)))
    assert results[0] == results[1]
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


def test_budget_blocks_before_claim_and_usage_overrun_cannot_progress(world):
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        run.spent_microusd = 950_000
    assert world.claim().status_code == 204
    assert world.run()["stop_reason"] == "BUDGET_EXHAUSTED"


def test_usage_overrun_is_recorded_and_stops_next_job(world):
    assignment = world.claim().json()
    reply = submit(world, assignment, body=completion(assignment) | {"usage_microusd": 1_000_001})
    assert reply.status_code == 200
    assert world.run()["stop_reason"] == "BUDGET_EXCEEDED"
    assert world.run()["spent_microusd"] == 1_000_001
    assert world.run()["reserved_microusd"] == 0
    assert world.claim().status_code == 204


def test_task_cannot_remove_project_required_checks_or_choose_unregistered_ref(world):
    spec = example_spec(world.repository_id)
    spec["verification"]["required_checks"] = ["unit_tests"]
    assert world.post(f"/v1/projects/{world.project_id}/tasks", spec).status_code == 422
    spec = example_spec(world.repository_id)
    spec["task"]["base_ref"] = "unknown-branch"
    task = world.post(f"/v1/projects/{world.project_id}/tasks", spec).json()
    assert world.post(f"/v1/tasks/{task['task_id']}/runs", {"spec_version": 1}).status_code == 422


def test_effective_limits_are_capped_by_server_policy(world):
    with world.database.transaction() as session:
        project = session.get(Project, world.project_id)
        project.policy = project.policy | {
            "max_infra_attempts_per_job": 1,
            "max_job_execution_seconds": 20,
        }
    run = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()
    assert run["snapshot"]["execution"]["max_infra_attempts_per_job"] == 1
    assert run["snapshot"]["execution"]["max_job_execution_seconds"] == 20
    assert world.run()["snapshot"]["execution"]["max_job_execution_seconds"] == 2700


def test_heartbeat_cannot_extend_job_deadline_or_accept_wrong_fence(world):
    assignment = world.claim().json()
    with world.database.transaction() as session:
        attempt = session.scalar(select(Attempt))
        attempt.deadline_at = db_now(session) + 10
    path = f"/v1/worker/leases/{assignment['lease']['id']}/heartbeat"
    assert (
        world.post(
            path, {"fence": assignment["lease"]["fence"] + 1}, token=world.worker_token
        ).status_code
        == 409
    )
    assert (
        world.post(path, {"fence": assignment["lease"]["fence"]}, token=world.worker_token).json()[
            "lease_expires_in_seconds"
        ]
        <= 10
    )


def test_incomplete_artifact_reference_is_not_accepted(world):
    from software_factory.storage import content_hash

    assignment = world.claim().json()
    reply = submit(world, assignment, body=completion(assignment) | {"artifact_ids": ["unknown"]})
    assert reply.status_code == 404
    assert world.run()["reserved_microusd"] == 100_000
    reserved = world.post(
        f"/v1/worker/leases/{assignment['lease']['id']}/artifacts",
        {
            "fence": assignment["lease"]["fence"],
            "kind": "LOG",
            "size_bytes": 0,
            "sha256": content_hash(b""),
        },
        token=world.worker_token,
    )
    assert reserved.status_code == 201
    reply = submit(
        world, assignment, body=completion(assignment) | {"artifact_ids": [reserved.json()["id"]]}
    )
    assert reply.status_code == 409
    assert world.run()["reserved_microusd"] == 100_000


def test_database_itself_rejects_cross_workspace_parent_reference(world):
    with pytest.raises(IntegrityError), world.database.transaction() as session:
        session.add(
            Repository(
                workspace_id=world.other_workspace_id,
                project_id=world.project_id,
                name="invalid",
                base_ref="main",
                base_sha="b" * 40,
            )
        )
        session.flush()


def test_transition_and_idempotency_rollback_together_on_error(world):
    assignment = world.claim().json()
    service = Service(world.database)

    def interrupted(session, actor):
        run = session.get(Run, world.run_id)
        run.status = "FAILED"
        session.flush()
        raise RuntimeError("Simulated CP crash before commit")

    with pytest.raises(RuntimeError):
        service.write(world.worker_token, "WORKER", "interrupt", "same", {}, interrupted)
    assert world.run()["status"] == "RUNNING"
    assert submit(world, assignment).status_code == 200


def test_migration_matches_models(database):
    command.check(Config("alembic.ini"))
    with database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        )
