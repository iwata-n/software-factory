import copy
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from sqlalchemy import func, select

from software_factory.models import Run, WorkerEvent
from software_factory.service import Service, event
from tests.conftest import example_policy, example_spec
from tests.test_control_plane import completion, submit
from tests.test_processes import start_control_plane

from .helpers import current_attempt, dump, jobs


@pytest.mark.ac("SF-07-AC01", "C-03")
@pytest.mark.parametrize("state", ["QUEUED", "RETRY_WAIT"])
def test_cancel_queued_or_retrying_run_is_terminal_and_replayable(world, evidence, state):
    if state == "RETRY_WAIT":
        assignment = world.claim().json()
        assert (
            world.post(
                f"/v1/worker/leases/{assignment['lease']['id']}/fail",
                {
                    "fence": assignment["lease"]["fence"],
                    "category": "INFRASTRUCTURE",
                    "reason": "retry",
                },
                token=world.worker_token,
            ).status_code
            == 200
        )
    body = {"expected_run_revision": world.run()["state_revision"], "reason": "cancel waiting work"}
    first = world.post(f"/v1/runs/{world.run_id}/cancel", body, key="cancel-waiting")
    assert first.status_code == 202 and first.json()["status"] == "CANCELLED"
    assert (
        world.post(f"/v1/runs/{world.run_id}/cancel", body, key="cancel-waiting").json()
        == first.json()
    )
    assert all(job.state in {"CANCELLED", "FAILED"} for job in jobs(world))
    assert world.claim().status_code == 204 and world.run()["reserved_microusd"] == 0
    evidence(
        "202 CANCELLED; no active reservation/claim; replay exact",
        first.json(),
        run_id=world.run_id,
    )


@pytest.mark.ac("SF-07-AC02")
@pytest.mark.parametrize("iteration", range(20))
@pytest.mark.parametrize("outcome", ["EXECUTED", "CANCELLED"])
def test_active_cancellation_heartbeat_and_completion_race(
    world, clock, evidence, iteration, outcome
):
    assignment = world.claim().json()
    before = current_attempt(world)
    response = world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "stop active worker"},
    )
    assert response.json()["status"] == "CANCELLING"
    barrier = Barrier(2)
    lease = assignment["lease"]

    def heartbeat():
        barrier.wait(timeout=5)
        return world.post(
            f"/v1/worker/leases/{lease['id']}/heartbeat",
            {"fence": lease["fence"]},
            token=world.worker_token,
        )

    def finish():
        barrier.wait(timeout=5)
        return submit(world, assignment, body=completion(assignment) | {"outcome": outcome})

    with ThreadPoolExecutor(max_workers=2) as pool:
        hb = pool.submit(heartbeat)
        done = pool.submit(finish)
        heartbeat_response = hb.result(timeout=5)
        completed = done.result(timeout=5)
    assert completed.status_code == 200 and world.run()["status"] == "CANCELLED"
    assert heartbeat_response.status_code in {200, 409}
    if heartbeat_response.status_code == 200:
        assert heartbeat_response.json()["cancel_requested"] is True
    assert current_attempt(world).expires_at == before.expires_at
    assert current_attempt(world).stop_confirmation == "WORKER_REPORTED_STOP"
    assert len(jobs(world)) == 1 and world.run()["reserved_microusd"] == 0
    evidence(
        "completion ends cancellation, heartbeat never extends, next job=0",
        {
            "heartbeat_status": heartbeat_response.status_code,
            "run_status": world.run()["status"],
            "iteration": iteration,
            "reported_outcome": outcome,
        },
        attempt_id=before.id,
    )


@pytest.mark.ac("SF-07-AC03")
def test_unreachable_worker_is_logically_cancelled_without_false_physical_confirmation(
    world, clock, evidence
):
    assert world.claim().status_code == 200
    attempt = current_attempt(world)
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "worker unreachable"},
    )
    clock.value = attempt.expires_at
    assert Service(world.database).reconcile() == 1
    assert Service(world.database).reconcile() == 0
    assert world.run()["status"] == "CANCELLED"
    graph = world.get(f"/v1/runs/{world.run_id}/graph").json()
    assert graph["jobs"][0]["attempts"][0]["stop_confirmation"] == "EXECUTION_RIGHT_EXPIRED"
    history = world.get(f"/v1/runs/{world.run_id}/event-history").json()["events"]
    expiry = next(item for item in history if item["kind"] == "LEASE_EXPIRED")
    assert expiry["body"]["physical_stop_confirmed"] is False
    evidence("CANCELLED but physical_stop_confirmed=false", expiry, attempt_id=attempt.id)


@pytest.mark.ac("SF-07-AC04", "C-02", "C-03")
def test_cancel_revision_tenant_terminal_and_replay_rules(world, evidence):
    revision = world.run()["state_revision"]
    valid = {"expected_run_revision": revision, "reason": "valid cancellation"}
    before = dump(world.database)
    assert (
        world.post(
            f"/v1/runs/{world.run_id}/cancel", valid | {"expected_run_revision": revision + 1}
        ).status_code
        == 409
    )
    assert (
        world.post(
            f"/v1/runs/{world.run_id}/cancel", valid, token=world.other_user_token
        ).status_code
        == 404
    )
    assert dump(world.database) == before
    accepted = world.post(f"/v1/runs/{world.run_id}/cancel", valid, key="terminal-replay")
    after = dump(world.database)
    assert (
        world.post(f"/v1/runs/{world.run_id}/cancel", valid, key="terminal-replay").json()
        == accepted.json()
    )
    assert (
        world.post(
            f"/v1/runs/{world.run_id}/cancel",
            valid | {"expected_run_revision": world.run()["state_revision"]},
        ).status_code
        == 409
    )
    assert dump(world.database) == after
    evidence("stale 409, tenant 404, terminal 409, accepted replay exact", accepted.json())


@pytest.mark.ac("SF-07-AC05")
@pytest.mark.parametrize("stage", ["RUNNING", "RETRY_WAIT", "BLOCKED", "APPROVAL_WAIT", "CI_WAIT"])
def test_process_restart_preserves_every_persisted_stage(world, clock, evidence, stage):
    if stage in {"RUNNING", "RETRY_WAIT"}:
        assignment = world.claim().json()
        if stage == "RETRY_WAIT":
            world.post(
                f"/v1/worker/leases/{assignment['lease']['id']}/fail",
                {
                    "fence": assignment["lease"]["fence"],
                    "category": "INFRASTRUCTURE",
                    "reason": "restart retry",
                },
                token=world.worker_token,
            )
    else:
        with world.database.transaction() as session:
            run = session.get(Run, world.run_id)
            if stage == "BLOCKED":
                run.status = "BLOCKED"
                run.stop_reason = "contract-fake unresolved decision"
            else:
                run.status = "WAITING_EXTERNAL"
                run.phase = "WAIT_PLAN_APPROVAL" if stage == "APPROVAL_WAIT" else "WAIT_CI"
                run.wait_context = {
                    "origin": "contract-fake",
                    "subject_id": "future-subject",
                    "candidate_id": "candidate-contract-fake",
                    "deadline_at": clock() + 1800,
                }
            event(
                session, run, "CONTRACT_FAKE_WAIT_SET", {"origin": "contract-fake", "stage": stage}
            )
    headers = {"Authorization": "Bearer " + world.user_token}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = start_control_plane(port)
    api = f"http://127.0.0.1:{port}/v1/runs/{world.run_id}"
    try:
        before = {
            suffix: httpx.get(api + suffix, headers=headers).json()
            for suffix in ("", "/graph", "/event-history")
        }
        process.kill()
        process.communicate(timeout=5)
        process = start_control_plane(port)
        after = {suffix: httpx.get(api + suffix, headers=headers).json() for suffix in before}
        assert after == before
        if stage != "RUNNING":
            assert before[""]["reserved_microusd"] == 0
        evidence(
            "all state, reason, deadline, references, budget, graph, history survive process kill",
            after,
            run_id=world.run_id,
            fixture_stage=stage,
            external_wait_origin="contract-fake",
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)


@pytest.mark.ac("SF-07-AC06", "C-04")
def test_worker_sequence_replay_conflicts_and_unknown_kind(world, evidence):
    assignment = world.claim().json()
    lease = assignment["lease"]
    path = f"/v1/worker/leases/{lease['id']}/events"
    entries = [
        {"sequence": i, "kind": "LOG", "body": {"message": f"event {i}"}} for i in [1, 3, 2, 2]
    ]
    body = {"fence": lease["fence"], "events": entries}
    first = world.post(path, body, token=world.worker_token)
    assert first.status_code == 200
    before = dump(world.database)
    assert world.post(path, body, token=world.worker_token).json() == first.json()
    assert dump(world.database) == before
    conflict = body | {
        "events": [
            {"sequence": 4, "kind": "LOG", "body": {}},
            entries[2] | {"body": {"message": "changed"}},
        ]
    }
    assert world.post(path, conflict, token=world.worker_token).status_code == 409
    assert dump(world.database) == before
    assert (
        world.post(
            path, body | {"events": [entries[0] | {"kind": "UNKNOWN"}]}, token=world.worker_token
        ).status_code
        == 422
    )
    with world.database.transaction() as session:
        saved = list(session.scalars(select(WorkerEvent).order_by(WorkerEvent.run_revision)))
        assert [item.sequence for item in saved] == [1, 3, 2]
        assert len({item.run_revision for item in saved}) == 3
    history = world.get(f"/v1/runs/{world.run_id}/event-history").json()["events"]
    assert [item["revision"] for item in history] == sorted({item["revision"] for item in history})
    evidence(
        "three unique worker events; same sequence different payload rolls whole batch back",
        first.json(),
        attempt_id=assignment["job"]["attempt_id"],
    )


@pytest.mark.ac("SF-07-AC06")
def test_event_receipts_survive_actual_process_restart(world, evidence):
    assignment = world.claim().json()
    lease = assignment["lease"]
    headers = {"Authorization": "Bearer " + world.worker_token}
    body = {
        "fence": lease["fence"],
        "events": [{"sequence": 1, "kind": "PROGRESS", "body": {"phase": "fixture"}}],
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = start_control_plane(port)
    url = f"http://127.0.0.1:{port}/v1/worker/leases/{lease['id']}/events"
    try:
        first = httpx.post(url, json=body, headers=headers)
        assert first.status_code == 200
        process.kill()
        process.communicate(timeout=5)
        process = start_control_plane(port)
        replay = httpx.post(url, json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == first.json()
        with world.database.transaction() as session:
            assert session.scalar(select(func.count()).select_from(WorkerEvent)) == 1
        evidence(
            "restart and replay returns original receipt without new event",
            replay.json(),
            attempt_id=assignment["job"]["attempt_id"],
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)


@pytest.mark.ac("C-03")
@pytest.mark.parametrize(
    "operation", ["project", "repository", "task", "spec", "run", "cancel", "complete", "fail"]
)
def test_all_mutations_require_keys_and_replay_exactly(world, evidence, operation):
    token = world.user_token
    if operation == "project":
        path = f"/v1/workspaces/{world.workspace_id}/projects"
        body = {"name": "new", "policy": example_policy()}
    elif operation == "repository":
        path = f"/v1/projects/{world.project_id}/repositories"
        body = {"name": "new", "base_sha": "b" * 40}
    elif operation == "task":
        path = f"/v1/projects/{world.project_id}/tasks"
        body = example_spec(world.repository_id)
    elif operation == "spec":
        path = f"/v1/tasks/{world.task_id}/spec-versions"
        body = example_spec(world.repository_id)
    elif operation == "run":
        path = f"/v1/tasks/{world.task_id}/runs"
        body = {"spec_version": 1}
    elif operation == "cancel":
        path = f"/v1/runs/{world.run_id}/cancel"
        body = {"expected_run_revision": world.run()["state_revision"], "reason": "request"}
    else:
        assignment = world.claim().json()
        path = f"/v1/worker/leases/{assignment['lease']['id']}/{operation}"
        token = world.worker_token
        body = (
            completion(assignment)
            if operation == "complete"
            else {
                "fence": assignment["lease"]["fence"],
                "category": "INFRASTRUCTURE",
                "reason": "request",
            }
        )
    assert (
        world.client.post(path, json=body, headers={"Authorization": "Bearer " + token}).status_code
        == 422
    )
    first = world.post(path, body, token=token, key="same-mutation")
    assert first.status_code < 300
    after = dump(world.database)
    repeated = world.post(path, body, token=token, key="same-mutation")
    assert (repeated.status_code, repeated.json(), repeated.headers.get("location")) == (
        first.status_code,
        first.json(),
        first.headers.get("location"),
    )
    changed = body | (
        {"name": "different"}
        if operation in {"project", "repository"}
        else {"spec_version": 2}
        if operation == "run"
        else {"usage_microusd": 1}
        if operation == "complete"
        else {"reason": "different"}
    )
    if operation in {"task", "spec"}:
        changed = copy.deepcopy(body)
        changed["task"]["objective"] = "different valid objective"
    conflict = world.post(path, changed, token=token, key="same-mutation")
    assert conflict.status_code == 409
    assert dump(world.database) == after
    evidence(
        "key required, stored response exact, different request rejected with no mutation",
        {
            "operation": operation,
            "status": first.status_code,
            "conflict_status": conflict.status_code,
        },
    )


@pytest.mark.ac("C-04")
def test_write_event_outbox_idempotency_and_audit_commit_atomically(world, evidence):
    service = world.client.app.state.service
    before = dump(world.database)

    def fail(point):
        if point == "write.before_commit":
            raise RuntimeError("precommit loss")

    service.checkpoint = fail
    with pytest.raises(RuntimeError):
        world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key="atomic-start")
    assert dump(world.database) == before
    service.checkpoint = lambda _: None
    first = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key="atomic-start")
    assert first.status_code == 202
    assert (
        world.post(
            f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key="atomic-start"
        ).json()
        == first.json()
    )
    evidence(
        "all run/job/event/outbox/audit/idempotency rows rollback before commit",
        first.json()["run_id"],
    )


@pytest.mark.ac("C-06")
def test_untrusted_secrets_never_persist_or_echo_in_errors(world, evidence):
    secret = "ghp_acceptance_canary_9f88c2d1"
    assignment = world.claim().json()
    lease = assignment["lease"]
    body = {
        "fence": lease["fence"],
        "events": [
            {
                "sequence": 1,
                "kind": "LOG",
                "body": {
                    "access_token": secret,
                    "message": "Bearer " + world.worker_token,
                    secret: "secret as key",
                },
            }
        ],
    }
    assert (
        world.post(
            f"/v1/worker/leases/{lease['id']}/events", body, token=world.worker_token
        ).status_code
        == 200
    )
    failed = world.post(
        f"/v1/worker/leases/{lease['id']}/fail",
        {
            "fence": lease["fence"],
            "category": "INCONCLUSIVE",
            "reason": "Authorization: Bearer " + secret,
        },
        token=world.worker_token,
    )
    assert failed.status_code == 200
    stored = json.dumps(dump(world.database), default=str)
    assert secret not in stored and world.worker_token not in stored
    invalid = world.client.post(
        "/v1/worker/enroll", json={"token": secret, "name": "bad", secret: "unknown"}
    )
    assert invalid.status_code == 422 and secret not in invalid.text
    user_secret = example_spec(world.repository_id)
    user_secret["task"]["objective"] = world.user_token
    assert world.post(f"/v1/projects/{world.project_id}/tasks", user_secret).status_code == 422
    assert (
        world.post(
            f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}, key=world.user_token
        ).status_code
        == 422
    )
    assert secret not in world.get(f"/v1/runs/{world.run_id}/event-history").text
    evidence(
        "credential values/keys absent in persisted data, history, validation response",
        {"secret_occurrences": 0, "validation_status": 422},
    )
