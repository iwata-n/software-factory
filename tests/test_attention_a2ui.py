import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from software_factory.api import create_app
from software_factory.contracts import ModelCallUsage, digest
from software_factory.models import PlanVersion, Run, Task, TaskChatTurn, Worker
from software_factory.task_chat_worker import TaskAnswerOptions, claim_chat

ORIGIN = "https://factory.example"
MANIFEST = "c" * 64
QUESTION = "What should the empty state say?"


def test_blocked_question_surface_proposes_automatically_and_accepts_other(
    world, monkeypatch, tmp_path
):
    for name, value in {
        "WORKSPACE_ID": world.workspace_id,
        "AUTH_ROOT": str(tmp_path / "codex"),
        "AUTH_FILE": str(tmp_path / "existing.json"),
        "LOGIN_BINARY": "/usr/bin/false",
    }.items():
        monkeypatch.setenv("FACTORY_CODEX_" + name, value)
    with world.database.transaction() as db:
        run = db.get(Run, world.run_id)
        run.status = "BLOCKED"
        run.stop_reason = "PLAN_HAS_OPEN_QUESTIONS"
        policy = run.snapshot["policy"] | {
            "agent_adapter": "codex_cli",
            "allow_external_model": True,
            "agent_configuration": {"engine_manifest_hash": MANIFEST},
            "pricing_manifest": json.loads(Path("config/codex_pricing.json").read_text()),
        }
        run.snapshot = run.snapshot | {"policy": policy}
        plan = {"open_questions": [QUESTION]}
        db.add(
            PlanVersion(
                workspace_id=world.workspace_id,
                run_id=run.id,
                version=1,
                body=plan,
                body_hash=digest(plan),
                validation_error="PLAN_HAS_OPEN_QUESTIONS",
            )
        )
        worker = db.get(Worker, world.worker_id)
        worker.managed = True
        worker.capabilities = ["PLAN", "adapter:codex_cli", "usage:trusted"]

    app = create_app(world.database)
    path = f"/v1/runs/{world.run_id}/attention"
    http = TestClient(app, base_url=ORIGIN)

    def auth(token, key="test-key"):
        return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Idempotency-Key": key}

    user = auth(world.user_token)
    initial = http.get(path, headers=user).raise_for_status().json()
    surface = initial["messages"]
    assert initial["pending"]
    components = surface[1]["updateComponents"]["components"]
    assert [c["id"] for c in components if c["component"] == "Button"] == ["draft_answers"]
    assert next(c for c in components if c["component"] == "ChoicePicker")["options"] == [
        {"label": "その他", "value": "other"}
    ]
    assert [m["version"] for m in surface] == ["v0.9.1"] * 3
    assert surface[2]["updateDataModel"]["value"]["questions"] == [QUESTION]
    assert http.get(path, headers=auth(world.other_user_token)).status_code == 404

    def envelope(messages, name, **values):
        surface_id = messages[0]["createSurface"]["surfaceId"]
        component = next(
            c for c in messages[1]["updateComponents"]["components"] if c["id"] == name
        )
        context = component["action"]["event"]["context"] | values
        return {
            "version": "v0.9.1",
            "action": {
                "name": name,
                "surfaceId": surface_id,
                "sourceComponentId": name,
                "timestamp": datetime.now(UTC).isoformat(),
                "context": context,
            },
        }

    empty = envelope(surface, "draft_answers", answers={"1": {"choice": ["other"], "text": ""}})
    assert http.post(path + "/actions", json=empty, headers=user).status_code == 422
    free = envelope(
        surface, "draft_answers", answers={"1": {"choice": ["other"], "text": "No items yet"}}
    )
    assert (
        "No items yet" in http.post(path + "/actions", json=free, headers=user).json()["addition"]
    )
    with world.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(TaskChatTurn)) == 0

    def model(context, schema, *_args, **_kwargs):
        assert schema is TaskAnswerOptions
        assert context["run"]["open_questions"] == [QUESTION]
        return SimpleNamespace(
            value={"questions": [{"question_number": 1, "options": ["Show No items yet."]}]},
            usage=ModelCallUsage(call_id="proposal", input_tokens=20, output_tokens=11),
        )

    with TestClient(app, base_url=ORIGIN, headers=auth(world.worker_token)) as worker:
        engine = SimpleNamespace(manifest_hash=MANIFEST, run=model)
        assert claim_chat(worker, engine)
        assert not claim_chat(worker, engine)

    result = http.get(path, headers=user).raise_for_status().json()
    assert not result["pending"]
    proposed = result["messages"]
    choices = next(
        c for c in proposed[1]["updateComponents"]["components"] if c["component"] == "ChoicePicker"
    )
    assert choices["options"] == [
        {"label": "Show No items yet.", "value": "1"},
        {"label": "その他", "value": "other"},
    ]
    draft = envelope(
        proposed, "draft_answers", answers={"1": {"choice": ["1"], "text": "Use Japanese."}}
    )
    result = http.post(path + "/actions", json=draft, headers=user)
    assert result.status_code == 200
    assert "Show No items yet.\nUse Japanese." in result.json()["addition"]
    draft["action"]["context"]["revision"] += 1
    assert (
        http.post(path + "/actions", json=draft, headers=user).json()["code"]
        == "ATTENTION_SURFACE_STALE"
    )
    chat = http.get(f"/v1/runs/{world.run_id}/chat", headers=user).raise_for_status()
    assert chat.json()["turns"] == []
    with world.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(TaskChatTurn)) == 1
        assert db.get(Task, world.task_id).latest_spec_version == 1
        assert db.get(Run, world.run_id).status == "BLOCKED"
