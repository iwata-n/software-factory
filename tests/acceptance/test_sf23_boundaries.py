"""Run console foundation. Full SF-23 AC01 also needs SF-19 live evidence."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from software_factory import cli
from software_factory.api import create_app
from software_factory.models import AuditEvent, Principal, Project, Run, RunEvent
from software_factory.service import issue_token, token_hash
from tests.conftest import example_spec


def test_console_shell_loads_only_packaged_assets(world):
    page = world.client.get("/operations")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Run カンバン" in page.text
    for asset in ("board.css", "board.js"):
        response = world.client.get(f"/operations/{asset}")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
    assert world.client.get("/operations/index.html").status_code == 404


def test_run_index_requires_user_and_scopes_rows_and_cursor(world):
    assert world.client.get("/v1/runs").status_code == 401
    assert world.get("/v1/runs", token=world.worker_token).status_code == 403
    assert world.get("/v1/runs", token=world.other_user_token).json() == {
        "runs": [],
        "next_before": None,
    }
    assert (
        world.get(f"/v1/runs?before={world.run_id}", token=world.other_user_token).status_code
        == 404
    )
    assert world.get("/v1/runs?limit=101").status_code == 422


def test_run_index_paginates_timestamp_ties_and_matches_run_details(world):
    spec = example_spec(world.repository_id)
    spec["task"]["title"] = "Second Run"
    task = world.post(f"/v1/projects/{world.project_id}/tasks", spec)
    started = world.post(f"/v1/tasks/{task.json()['task_id']}/runs", {"spec_version": 1})
    assert started.status_code == 202
    second = started.json()["run_id"]
    with world.database.transaction() as session:
        session.get(Run, second).created_at = session.get(Run, world.run_id).created_at

    first_id, second_id = sorted([world.run_id, second], reverse=True)
    first = world.get("/v1/runs?limit=1")
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["next_before"] == first_id
    next_page = world.get(f"/v1/runs?limit=1&before={first_id}").json()
    assert next_page["next_before"] is None
    for response, expected_id in ((first.json(), first_id), (next_page, second_id)):
        (card,) = response["runs"]
        assert card["run_id"] == expected_id
        assert card["task_title"] == ("Second Run" if expected_id == second else "Add empty state")
        run = world.get(f"/v1/runs/{expected_id}").json()
        assert {key: run[key] for key in card if key != "task_title"} == {
            key: value for key, value in card.items() if key != "task_title"
        }

    assert world.claim().status_code == 200
    cards = world.get("/v1/runs").json()["runs"]
    assert {card["status"] for card in cards} == {"QUEUED", "RUNNING"}


def test_event_stream_resumes_from_last_run_revision_after_restart(world):
    history = f"/v1/runs/{world.run_id}/event-history"
    path = f"/v1/runs/{world.run_id}/events?wait_seconds=0"
    before = world.get(history).json()["events"]
    first = world.get(path)
    assert first.status_code == 200 and first.headers["content-type"].startswith(
        "text/event-stream"
    )
    assert first.headers["cache-control"] == "no-store"
    assert sse_revisions(first) == [(world.run_id, item["revision"]) for item in before]
    assert world.claim().status_code == 200

    last = f"{world.run_id}:{before[-1]['revision']}"
    headers = {"Authorization": f"Bearer {world.user_token}", "Last-Event-ID": last}
    resumed = TestClient(create_app(world.database)).get(path, headers=headers)
    after = world.get(history).json()["events"][len(before) :]
    assert after
    assert sse_revisions(resumed) == [(world.run_id, item["revision"]) for item in after]
    headers["Last-Event-ID"] = f"{world.run_id}:{after[-1]['revision']}"
    assert not sse_revisions(world.client.get(path, headers=headers))


def test_event_stream_scopes_cursor_and_reports_retained_history_gap(world):
    path = f"/v1/runs/{world.run_id}/events?wait_seconds=0"
    headers = {"Authorization": f"Bearer {world.user_token}"}
    assert world.get(path, token=world.other_user_token).status_code == 404
    assert world.get(path, token=world.worker_token).status_code == 403
    assert (
        world.client.get(path, headers=headers | {"Last-Event-ID": f"{uuid4()}:2"}).status_code
        == 404
    )
    for cursor in ("garbage", f"{world.run_id}:-1", f"{world.run_id}:999999"):
        assert (
            world.client.get(path, headers=headers | {"Last-Event-ID": cursor}).status_code == 422
        )
    assert world.claim().status_code == 200
    latest = world.run()["state_revision"]
    with world.database.transaction() as session:
        session.execute(
            delete(RunEvent).where(RunEvent.run_id == world.run_id, RunEvent.revision < latest)
        )
    expired = world.client.get(path, headers=headers | {"Last-Event-ID": f"{world.run_id}:1"})
    assert expired.status_code == 410
    assert expired.json()["history"] == f"/v1/runs/{world.run_id}/event-history"
    assert sse_revisions(world.get(path)) == [(world.run_id, latest)]
    boundary = world.client.get(
        path, headers=headers | {"Last-Event-ID": f"{world.run_id}:{latest - 1}"}
    )
    assert sse_revisions(boundary) == [(world.run_id, latest)]


def sse_revisions(response):
    assert response.status_code == 200
    records = []
    for frame in response.text.split("\n\n"):
        if frame.startswith("id: "):
            identifier, data = frame.split("\n", 1)
            run_id, revision = identifier.removeprefix("id: ").split(":")
            assert json.loads(data.removeprefix("data: "))["revision"] == int(revision)
            records.append((run_id, int(revision)))
    return records


def test_rotation_preserves_worker_slot_and_fences_old_token_at_grace(world, clock, monkeypatch):
    claimed = world.claim().json()
    lease = claimed["lease"]
    path = f"/v1/worker/leases/{lease['id']}/heartbeat"
    rotate = "/v1/worker/credentials/rotate"
    assert world.post(rotate, {}).status_code == 403
    first = world.post(rotate, {}, token=world.worker_token, key="first-rotation")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    payload = first.json()
    assert payload["worker_id"] == world.worker_id
    assert payload["token_expires_at"] == clock.value + 86400
    assert payload["previous_token_expires_at"] == clock.value + 60
    new_token = payload["worker_token"]
    assert new_token != world.worker_token
    replay = world.post(rotate, {}, token=world.worker_token, key="first-rotation")
    assert replay.status_code == 200 and replay.json() == payload
    assert world.claim(token=new_token).status_code == 204
    assert world.post(rotate, {}, token=world.worker_token).status_code == 409
    assert world.post(rotate, {}, token=new_token).status_code == 409
    clock.advance(59)
    assert world.post(path, {"fence": lease["fence"]}, token=world.worker_token).status_code == 200
    clock.advance(1)
    assert world.post(path, {"fence": lease["fence"]}, token=world.worker_token).status_code == 401
    assert world.post(path, {"fence": lease["fence"]}, token=new_token).status_code == 200
    latest = world.post(rotate, {}, token=new_token).json()["worker_token"]

    monkeypatch.setattr(cli, "Database", lambda: world.database)
    monkeypatch.setattr(
        sys, "argv", ["factory", "revoke", "--principal-id", world.worker_id, "--reason", "test"]
    )
    cli.main()
    for token in (new_token, latest):
        assert world.post(path, {"fence": lease["fence"]}, token=token).status_code == 401
    with world.database.transaction() as session:
        worker = session.get(Principal, world.worker_id)
        assert worker.token_hash == token_hash(latest)
        assert worker.previous_token_hash == token_hash(new_token)
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.operation.in_(["worker.credential.rotate", "principal.revoke"]))
                .order_by(AuditEvent.created_at, AuditEvent.operation.desc())
            )
        )
        assert [item.operation for item in events].count("worker.credential.rotate") == 2
        revoke = next(item for item in events if item.operation == "principal.revoke")
        assert revoke.body["reason"] == "test"
        assert all(
            token not in json.dumps([item.body for item in events]) for token in (new_token, latest)
        )
    with pytest.raises(IntegrityError, match="EVIDENCE_IMMUTABLE"):
        with world.database.transaction() as session:
            session.execute(update(AuditEvent).values(operation="overwrite"))


def test_unrotated_worker_token_expires_after_twenty_four_hours(world, clock):
    worker = world.make_worker()
    token = worker["worker_token"]
    assert worker["token_expires_at"] == clock.value + 86400
    clock.advance(86399)
    assert world.claim(token=token).status_code == 200
    clock.advance(1)
    assert world.claim(token=token).status_code == 401
    assert world.post("/v1/worker/credentials/rotate", {}, token=token).status_code == 401


def test_concurrent_rotation_issues_only_one_new_token(world):
    barrier = Barrier(2)

    def rotate(_):
        barrier.wait(timeout=5)
        return world.post("/v1/worker/credentials/rotate", {}, token=world.worker_token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(rotate, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_workspace_user_access_separates_read_run_operations_and_worker_administration(world):
    viewer, operator = issue_token(), issue_token()
    with world.database.transaction() as session:
        for token, access in ((viewer, "VIEWER"), (operator, "OPERATOR")):
            session.add(
                Principal(
                    workspace_id=world.workspace_id,
                    role="USER",
                    token_hash=token_hash(token),
                    user_access=access,
                )
            )
        policy = session.get(Project, world.project_id).policy
    root = f"/v1/workspaces/{world.workspace_id}"
    run_path = f"/v1/runs/{world.run_id}"
    revision = world.run()["state_revision"]
    action = {"expected_run_revision": revision, "reason": "permission test"}
    spec = example_spec(world.repository_id)
    ordinary_mutations = [
        (f"/v1/projects/{world.project_id}/tasks", spec),
        (f"/v1/tasks/{world.task_id}/spec-versions", spec),
        (f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}),
        (f"{run_path}/cancel", action),
        (f"{run_path}/replan", action),
        (f"{run_path}/interventions", {**action, "action": "cancel", "subject_id": world.run_id}),
        (
            f"{run_path}/budget-requests",
            {**action, "current_budget_microusd": 1, "new_budget_microusd": 2},
        ),
        (
            f"/v1/approvals/{uuid4()}/decisions",
            {**action, "target_hash": "a" * 64, "decision": "approve"},
        ),
    ]
    enrollment_path = f"{root}/worker-enrollments"
    revoke_path = f"{root}/workers/{world.worker_id}/revoke"
    administrative_mutations = [
        (enrollment_path, {"kinds": ["PLAN"]}),
        (revoke_path, {"reason": "permission test"}),
        (f"{root}/projects", {"name": "restricted", "policy": policy}),
        (
            f"/v1/projects/{world.project_id}/repositories",
            {"name": "restricted", "base_sha": "a" * 40},
        ),
    ]
    for token, denied in (
        (viewer, ordinary_mutations + administrative_mutations),
        (operator, administrative_mutations),
        (world.worker_token, ordinary_mutations + administrative_mutations),
    ):
        for path, body in denied:
            assert world.post(path, body, token=token).status_code == 403, (path, body)
    for token, access in ((viewer, "VIEWER"), (operator, "OPERATOR"), (world.user_token, "ADMIN")):
        context = world.get("/v1/operations/context", token=token)
        assert context.headers["cache-control"] == "no-store"
        assert context.json() == {
            "access": access,
            "execution": {"workspace_id": world.workspace_id, "stopped": False, "revision": 0},
        }
        assert world.get(run_path, token=token).status_code == 200
        assert world.get(f"{root}/workers", token=token).status_code == 200
    assert world.run()["state_revision"] == revision
    assert (
        world.post(enrollment_path, {"kinds": ["PLAN"]}, token=world.other_user_token).status_code
        == 404
    )
    assert (
        world.post(revoke_path, {"reason": "foreign"}, token=world.other_user_token).status_code
        == 404
    )
    assert world.post(enrollment_path, {"kinds": ["VERIFY"]}).status_code == 422
    enrolled = world.post(enrollment_path, {"kinds": ["PLAN"]})
    assert enrolled.status_code == 201 and enrolled.headers["cache-control"] == "no-store"
    new_worker = world.client.post(
        "/v1/worker/enroll", json={"token": enrolled.json()["enrollment_token"], "name": "scoped"}
    )
    assert new_worker.status_code == 201
    assert enrolled.json()["enrollment_token"] not in world.get(f"{root}/workers").text
    assert new_worker.json()["worker_token"] not in world.get(f"{root}/workers").text
    assert world.post(f"{run_path}/cancel", action, token=operator).status_code == 202
    assert world.post(revoke_path, {"reason": "evaluation"}).status_code == 200
    assert world.claim().status_code == 401
    assert world.claim(token=new_worker.json()["worker_token"]).status_code == 204


def test_emergency_stop_survives_restart_and_requires_current_workspace_revision(world):
    path = f"/v1/workspaces/{world.workspace_id}/execution-control"
    stop = {"stopped": True, "expected_revision": 0, "reason": "exercise stop"}
    assert world.post(path, stop, token=world.other_user_token).status_code == 404
    operator = issue_token()
    with world.database.transaction() as session:
        session.add(
            Principal(
                workspace_id=world.workspace_id,
                role="USER",
                user_access="OPERATOR",
                token_hash=token_hash(operator),
            )
        )
    assert world.post(path, stop, token=operator).status_code == 403
    assert world.post(path, stop, key="stop").json()["revision"] == 1
    assert world.post(path, stop, key="stop").json()["revision"] == 1
    client = TestClient(create_app(world.database))
    headers = {"Authorization": f"Bearer {world.user_token}"}
    assert client.get(path, headers=headers).json()["stopped"] is True
    assert world.claim().status_code == 204
    release = {"stopped": False, "expected_revision": 0, "reason": "release rehearsal"}
    assert world.post(path, release).status_code == 409
    assert world.post(path, release | {"expected_revision": 1}).json()["revision"] == 2
    assert world.claim().status_code == 200
    with world.database.transaction() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.operation.like("workspace.execution.%"))
            )
        )
        assert {item.operation for item in events} == {
            "workspace.execution.stop",
            "workspace.execution.release",
        }
        assert sorted(
            (item.body["before_revision"], item.body["after_revision"]) for item in events
        ) == [(0, 1), (1, 2)]


def test_emergency_stop_requests_running_worker_stop_and_preserves_outcome(world):
    assignment = world.claim().json()
    lease = assignment["lease"]
    path = f"/v1/workspaces/{world.workspace_id}/execution-control"
    stop = {"stopped": True, "expected_revision": 0, "reason": "active worker rehearsal"}
    assert world.post(path, stop).status_code == 200
    assert world.run()["status"] == "CANCELLING"
    lease_root = f"/v1/worker/leases/{lease['id']}"
    response = world.post(
        f"{lease_root}/heartbeat", {"fence": lease["fence"]}, token=world.worker_token
    )
    assert response.json()["cancel_requested"] is True
    completed = world.post(
        f"{lease_root}/complete",
        {"fence": lease["fence"], "outcome": "CANCELLED"},
        token=world.worker_token,
    )
    assert completed.status_code == 200
    assert world.run()["status"] == "CANCELLED"
    assert world.run()["stop_reason"] == "WORKSPACE_EMERGENCY_STOP"


def test_claim_racing_workspace_stop_is_either_denied_or_cancelled(world):
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return world.claim()

    def stop():
        barrier.wait(timeout=5)
        return world.post(
            f"/v1/workspaces/{world.workspace_id}/execution-control",
            {"stopped": True, "expected_revision": 0, "reason": "concurrent claim"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_result = pool.submit(claim)
        stop_result = pool.submit(stop)
        assert stop_result.result(timeout=10).status_code == 200
        result = claim_result.result(timeout=10)
    assert world.claim().status_code == 204
    if result.status_code == 200:
        lease = result.json()["lease"]
        heartbeat = world.post(
            f"/v1/worker/leases/{lease['id']}/heartbeat",
            {"fence": lease["fence"]},
            token=world.worker_token,
        )
        assert heartbeat.json()["cancel_requested"] is True
    else:
        assert result.status_code == 204
        assert world.run()["status"] == "QUEUED"
