import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from software_factory.api import create_app
from software_factory.contracts import ModelCallUsage
from software_factory.models import Principal, Run, TaskChatTurn, Worker
from software_factory.service import issue_token, token_hash
from software_factory.task_chat_worker import claim_chat

ORIGIN = "https://factory.example"
MANIFEST = "b" * 64


def test_task_chat_context_permissions_and_measured_reply(world, monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_CODEX_WORKSPACE_ID", world.workspace_id)
    monkeypatch.setenv("FACTORY_CODEX_AUTH_ROOT", str(tmp_path / "auth"))
    monkeypatch.setenv("FACTORY_CODEX_AUTH_FILE", str(tmp_path / "existing.json"))
    monkeypatch.setenv("FACTORY_CODEX_LOGIN_BINARY", "/usr/bin/false")
    viewer = issue_token()
    fake_worker = world.make_worker()["worker_token"]
    pricing = json.loads(Path("config/codex_pricing.json").read_text())
    with world.database.transaction() as db:
        run = db.get(Run, world.run_id)
        policy = run.snapshot["policy"] | {
            "allow_external_model": True,
            "agent_adapter": "codex_cli",
            "agent_configuration": {"engine_manifest_hash": MANIFEST},
            "pricing_manifest": pricing,
        }
        run.snapshot = run.snapshot | {"policy": policy}
        run.status, run.stop_reason = "BLOCKED", "PLAN_HAS_OPEN_QUESTIONS"
        worker = db.get(Worker, world.worker_id)
        worker.managed = True
        worker.capabilities = ["PLAN", "adapter:codex_cli", "usage:trusted"]
        db.add(
            Principal(
                workspace_id=world.workspace_id,
                role="USER",
                user_access="VIEWER",
                token_hash=token_hash(viewer),
            )
        )

    http = TestClient(create_app(world.database), base_url=ORIGIN)
    path = f"/v1/runs/{world.run_id}/chat"

    def headers(token=world.user_token, key="question"):
        return {"Origin": ORIGIN, "Authorization": f"Bearer {token}", "Idempotency-Key": key}

    message = {"message": "What should I clarify?"}
    assert http.get(path, headers=headers(world.other_user_token)).status_code == 404
    assert http.post(path, json=message, headers=headers(viewer)).status_code == 403
    assert http.post("/v1/worker/task-chat/claim", headers=headers(fake_worker)).status_code == 403
    first = http.post(path, json=message, headers=headers())
    assert first.status_code == 202
    assert http.post(path, json=message, headers=headers()).json() == first.json()
    assert (
        http.post(path, json=message, headers=headers(key="other")).json()["code"]
        == "TASK_CHAT_BUSY"
    )
    assert http.get(path, headers=headers()).json()["turns"][0]["status"] == "QUEUED"

    def respond(context, output_model, cancelled, *, timeout):
        assert context["task"]["title"] == "Add empty state"
        assert context["run"]["stop_reason"] == "PLAN_HAS_OPEN_QUESTIONS"
        assert context["message"] == message["message"]
        assert "source_files" not in context
        return SimpleNamespace(
            value={"answer": "Clarify what to display when there are no items."},
            usage=ModelCallUsage(call_id="fixture", input_tokens=25, output_tokens=12),
        )

    with TestClient(
        create_app(world.database), base_url=ORIGIN, headers=headers(world.worker_token)
    ) as worker_http:
        assert claim_chat(worker_http, SimpleNamespace(manifest_hash=MANIFEST, run=respond))
    result = http.get(path, headers=headers(viewer)).json()["turns"][0]
    assert result["status"] == "ANSWERED"
    assert result["answer"].startswith("Clarify")
    with world.database.transaction() as db:
        turn = db.scalar(select(TaskChatTurn))
        assert turn.usage["input_tokens"] == 25
        assert db.get(Run, world.run_id).stop_reason == "PLAN_HAS_OPEN_QUESTIONS"
