import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from software_factory.api import create_app
from software_factory.db import db_now
from software_factory.models import (
    AuditEvent,
    Principal,
    Run,
    SpecVersion,
    Task,
    TaskChatTurn,
    Worker,
)
from software_factory.service import issue_token, token_hash
from tests.conftest import example_spec

ORIGIN = "https://factory.example"


def delete(world, task_id=None, token=None):
    with TestClient(create_app(world.database), base_url=ORIGIN) as client:
        return client.delete(
            f"/v1/tasks/{task_id or world.task_id}",
            headers={"Origin": ORIGIN, "Authorization": "Bearer " + (token or world.user_token)},
        )


def stop(world, status="BLOCKED"):
    with world.database.transaction() as session:
        session.get(Run, world.run_id).status = status


def deletion_events(session, task_id):
    return session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.operation == "task.delete", AuditEvent.body["task_id"].as_string() == task_id
        )
    )


@pytest.mark.parametrize("access,expected", [("VIEWER", 403), ("OPERATOR", 200), ("ADMIN", 200)])
def test_delete_permissions_and_workspace(world, access, expected):
    stop(world)
    credential = issue_token()
    with world.database.transaction() as session:
        session.add(
            Principal(
                workspace_id=world.workspace_id,
                role="USER",
                user_access=access,
                token_hash=token_hash(credential),
            )
        )
    assert delete(world, token=world.other_user_token).status_code == 404
    assert delete(world, token=world.worker_token).status_code == 403
    response = delete(world, token=credential)
    assert response.status_code == expected
    with world.database.transaction() as session:
        assert (session.get(Task, world.task_id).deleted_at is not None) == (expected == 200)
        assert deletion_events(session, world.task_id) == (1 if expected == 200 else 0)


def test_delete_requires_browser_write_protection(world):
    stop(world)
    headers = {"Authorization": "Bearer " + world.user_token}
    with TestClient(create_app(world.database), base_url=ORIGIN) as client:
        assert client.delete(f"/v1/tasks/{world.task_id}", headers=headers).status_code == 403
    with TestClient(create_app(world.database), base_url="http://192.0.2.1") as client:
        assert (
            client.delete(
                f"/v1/tasks/{world.task_id}", headers=headers | {"Origin": "http://192.0.2.1"}
            ).status_code
            == 403
        )
    with world.database.transaction() as session:
        assert session.get(Task, world.task_id).deleted_at is None
        assert deletion_events(session, world.task_id) == 0


@pytest.mark.parametrize(
    "status,expected",
    [
        ("QUEUED", 409),
        ("RUNNING", 409),
        ("WAITING_EXTERNAL", 409),
        ("CANCELLING", 409),
        ("BLOCKED", 200),
        ("SUCCEEDED", 200),
        ("FAILED", 200),
        ("CANCELLED", 200),
    ],
)
def test_every_run_status(world, status, expected):
    stop(world, status)
    response = delete(world)
    assert response.status_code == expected
    if expected == 409:
        assert response.json()["code"] == "TASK_DELETE_ACTIVE_RUN"
    with world.database.transaction() as session:
        assert session.get(Run, world.run_id).status == status
        assert deletion_events(session, world.task_id) == (expected == 200)


def test_checks_older_runs_and_keeps_unrelated_tasks(world):
    newer = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()["run_id"]
    with world.database.transaction() as session:
        session.get(Run, newer).status = "SUCCEEDED"
    assert delete(world).json()["code"] == "TASK_DELETE_ACTIVE_RUN"
    untouched = world.post(
        f"/v1/projects/{world.project_id}/tasks", example_spec(world.repository_id)
    ).json()["task_id"]
    stop(world)
    assert delete(world).status_code == 200
    for _ in range(2):
        with TestClient(create_app(world.database)) as client:
            headers = {"Authorization": "Bearer " + world.user_token}
            tasks = client.get("/v1/operations/tasks", headers=headers).json()["tasks"]
            assert world.task_id not in {row["task_id"] for row in tasks}
            assert untouched in {row["task_id"] for row in tasks}
            for latest in (False, True):
                rows = client.get(
                    "/v1/runs", params={"latest_per_task": latest}, headers=headers
                ).json()["runs"]
                assert not {world.run_id, newer}.intersection(row["run_id"] for row in rows)
    with world.database.transaction() as session:
        assert session.get(Run, world.run_id) is not None
        assert session.get(Run, newer) is not None
        assert session.scalar(select(SpecVersion).where(SpecVersion.task_id == world.task_id))
        assert session.get(Task, untouched).deleted_at is None


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "ANSWERED", "FAILED"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_chat_deadline_boundary_preserves_history(world, status, offset):
    stop(world)
    with world.database.transaction() as session:
        now = db_now(session)
    world.database.sessions.configure(info={"clock": lambda: now})
    try:
        with world.database.transaction() as session:
            turn = TaskChatTurn(
                workspace_id=world.workspace_id,
                run_id=world.run_id,
                question="Clarify the task",
                status=status,
                created_at=now - 10,
                deadline_at=now + offset,
            )
            session.add(turn)
            session.flush()
            identifier = turn.id
        response = delete(world)
        blocked = status in {"QUEUED", "RUNNING"} and offset > 0
        assert response.status_code == (409 if blocked else 200)
        if blocked:
            assert response.json()["code"] == "TASK_DELETE_ACTIVE_CHAT"
        with world.database.transaction() as session:
            assert session.get(TaskChatTurn, identifier).status == status
    finally:
        world.database.sessions.configure(info={"clock": None})


def test_no_runs_and_duplicate_deletion_are_idempotent(world):
    task_id = world.post(
        f"/v1/projects/{world.project_id}/tasks", example_spec(world.repository_id)
    ).json()["task_id"]
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: delete(world, task_id), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert delete(world, task_id).json() == responses[0].json()
    with world.database.transaction() as session:
        task = session.get(Task, task_id)
        actor = session.scalar(
            select(Principal).where(Principal.token_hash == token_hash(world.user_token))
        )
        assert task.deleted_by == actor.id
        assert deletion_events(session, task_id) == 1


def test_deleted_task_rejects_new_operations_and_old_start_receipt(world):
    key = str(uuid4())
    body = {"spec_version": 1, "reuse_existing": True}
    assert world.post(f"/v1/tasks/{world.task_id}/runs", body, key=key).status_code == 202
    stop(world)
    assert delete(world).status_code == 200
    assert world.get(f"/v1/tasks/{world.task_id}").status_code == 404
    assert world.get(f"/v1/runs/{world.run_id}").status_code == 404
    assert world.post(f"/v1/tasks/{world.task_id}/runs", body, key=key).status_code == 404
    assert world.post(f"/v1/tasks/{world.task_id}/runs", body).status_code == 404
    assert (
        world.post(
            f"/v1/tasks/{world.task_id}/spec-versions", example_spec(world.repository_id)
        ).status_code
        == 404
    )
    assert world.get(f"/v1/runs/{world.run_id}/chat").status_code == 404
    assert world.get(f"/v1/runs/{world.run_id}/attention").status_code == 404
    with TestClient(create_app(world.database), base_url=ORIGIN) as client:
        assert (
            client.post(
                f"/v1/runs/{world.run_id}/chat",
                json={"message": "Explain the task"},
                headers={
                    "Authorization": "Bearer " + world.user_token,
                    "Origin": ORIGIN,
                    "Idempotency-Key": str(uuid4()),
                },
            ).status_code
            == 404
        )
    assert world.claim().status_code == 204


def test_delete_rolls_back_on_commit_failure(world):
    stop(world)
    service = world.client.app.state.service

    def fail(point):
        if point == "task.deleted":
            raise RuntimeError("injected deletion failure")

    service.checkpoint = fail
    with pytest.raises(RuntimeError, match="injected"):
        service.delete_task_request(world.user_token, world.task_id)
    with world.database.transaction() as session:
        assert session.get(Task, world.task_id).deleted_at is None
        assert deletion_events(session, world.task_id) == 0
    service.checkpoint = lambda point: None
    assert service.delete_task_request(world.user_token, world.task_id)["task_id"] == world.task_id


def test_delete_during_source_resolution_prevents_start(world):
    stop(world)
    entered, release = Event(), Event()
    service = world.client.app.state.service

    class Resolver:
        def resolve(self, reference):
            entered.set()
            assert release.wait(10)
            return reference.registered_sha

    service.resolver = Resolver()
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(world.post, f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
        try:
            assert entered.wait(10)
            assert delete(world).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 404
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 1


def test_expired_chat_usage_can_arrive_after_deletion_without_reviving_task(
    world, monkeypatch, tmp_path
):
    monkeypatch.setenv("FACTORY_CODEX_WORKSPACE_ID", world.workspace_id)
    monkeypatch.setenv("FACTORY_CODEX_AUTH_ROOT", str(tmp_path / "auth"))
    monkeypatch.setenv("FACTORY_CODEX_AUTH_FILE", str(tmp_path / "existing.json"))
    monkeypatch.setenv("FACTORY_CODEX_LOGIN_BINARY", "/usr/bin/false")
    stop(world)
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        run.snapshot = run.snapshot | {
            "policy": run.snapshot["policy"]
            | {"pricing_manifest": json.loads(Path("config/codex_pricing.json").read_text())}
        }
        identity = session.get(Worker, world.worker_id)
        identity.managed = True
        identity.capabilities = ["PLAN", "adapter:codex_cli", "usage:trusted"]
        turn = TaskChatTurn(
            workspace_id=world.workspace_id,
            run_id=world.run_id,
            worker_id=world.worker_id,
            question="Clarify the task",
            status="RUNNING",
            created_at=db_now(session) - 20,
            deadline_at=db_now(session) - 1,
        )
        session.add(turn)
        session.flush()
        identifier = turn.id
    assert delete(world).status_code == 200
    body = {
        "answer": "This reply is too late.",
        "usage": {"call_id": "late", "input_tokens": 25, "output_tokens": 12},
    }
    with TestClient(create_app(world.database)) as client:
        for _ in range(2):
            assert (
                client.put(
                    f"/v1/worker/task-chat/{identifier}",
                    json=body,
                    headers={"Authorization": "Bearer " + world.worker_token},
                ).status_code
                == 204
            )
    with world.database.transaction() as session:
        turn = session.get(TaskChatTurn, identifier)
        assert turn.usage["input_tokens"] == 25
        assert turn.status == "FAILED" and turn.error == "CHAT_TIMEOUT"
        assert turn.answer is None
        assert session.get(Task, world.task_id).deleted_at is not None
        assert deletion_events(session, world.task_id) == 1
