import base64
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


def test_chat_reads_pinned_project_source_and_records_each_model_call(world, monkeypatch, tmp_path):
    from runner import entry
    from software_factory.codex_engine import CodexResponse
    from software_factory.db import db_now

    monkeypatch.setenv("FACTORY_CODEX_WORKSPACE_ID", world.workspace_id)
    monkeypatch.setenv("FACTORY_CODEX_AUTH_ROOT", str(tmp_path / "auth"))
    monkeypatch.setenv("FACTORY_CODEX_AUTH_FILE", str(tmp_path / "existing.json"))
    monkeypatch.setenv("FACTORY_CODEX_LOGIN_BINARY", "/usr/bin/false")

    source_digest, archive, _ = entry.pack(
        [
            ("src/api.py", "100644", b"value = 1\n"),
            ("docs/spec.md", "100644", b"Existing behavior\n"),
        ]
    )
    source = {
        "sha": "a" * 40,
        "tree": "b" * 40,
        "source_digest": source_digest,
        "image_digest": "sha256:" + "0" * 64,
        "archive": base64.b64encode(archive).decode(),
    }
    with world.database.transaction() as db:
        run = db.get(Run, world.run_id)
        run.status, run.stop_reason = "BLOCKED", "PLAN_HAS_OPEN_QUESTIONS"
        policy = run.snapshot["policy"] | {
            "allow_external_model": True,
            "agent_adapter": "codex_cli",
            "agent_configuration": {"engine_manifest_hash": MANIFEST},
            "pricing_manifest": json.loads(Path("config/codex_pricing.json").read_text()),
            "allowed_read_paths": ["."],
            "allowed_write_paths": ["docs"],
        }
        run.snapshot = run.snapshot | {
            "policy": policy,
            "source": source | {"remote_url": "https://github.com/example/project.git"},
        }
        worker = db.get(Worker, world.worker_id)
        worker.managed = True
        worker.capabilities = ["PLAN", "adapter:codex_cli", "usage:trusted"]

    fetched = []

    class Resolver:
        def fetch(self, remote, sha):
            fetched.append((remote, sha))
            return source

    prompts = []

    def respond(context, schema, cancelled, *, timeout):
        prompts.append(context)
        if len(prompts) == 1:
            assert {item["path"] for item in context["project_file_index"]} == {
                "src/api.py",
                "docs/spec.md",
            }
            output = {
                "read_requests": [{"path": "src/api.py", "start_line": 1, "line_count": 10}],
                "result": None,
            }
        else:
            assert context["project_file_excerpts"][0]["content"] == "value = 1\n"
            assert context["project_access"]["mode"] == "read_only"
            output = {"read_requests": [], "result": {"answer": "api.py currently returns 1"}}
        schema.model_validate(output)
        return CodexResponse(
            output,
            ModelCallUsage(call_id=f"call-{len(prompts)}", input_tokens=10, output_tokens=2),
            "thread",
        )

    path = f"/v1/runs/{world.run_id}/chat"
    headers = {
        "Origin": ORIGIN,
        "Authorization": f"Bearer {world.user_token}",
        "Idempotency-Key": "source-question",
    }
    with TestClient(create_app(world.database, resolver=Resolver()), base_url=ORIGIN) as http:
        turn = http.post(path, json={"message": "What does api.py do?"}, headers=headers)
        assert turn.status_code == 202, turn.text
        source_path = f"/v1/worker/task-chat/{turn.json()['id']}/source"
        assert http.get(source_path, headers=headers).status_code == 403
        with TestClient(
            create_app(world.database, resolver=Resolver()),
            base_url=ORIGIN,
            headers={"Authorization": f"Bearer {world.worker_token}"},
        ) as worker_http:
            assert claim_chat(
                worker_http,
                SimpleNamespace(
                    manifest_hash=MANIFEST,
                    manifest=SimpleNamespace(max_input_bytes=262144),
                    run=respond,
                ),
            )
            assert worker_http.get(source_path).status_code == 409
        result = http.get(path, headers=headers).json()["turns"][0]
        assert result["status"] == "ANSWERED" and "returns 1" in result["answer"]
    assert fetched == [("https://github.com/example/project.git", "a" * 40)]
    with world.database.transaction() as db:
        turn = db.get(TaskChatTurn, turn.json()["id"])
        assert turn.usage["complete"] is True
        assert len(turn.usage["calls"]) == 2 and turn.usage["input_tokens"] == 20
        # An expired claim cannot retrieve the source archive.
        turn.status, turn.deadline_at = "RUNNING", db_now(db) - 1
    with TestClient(
        create_app(world.database, resolver=Resolver()),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {world.worker_token}"},
    ) as worker_http:
        assert worker_http.get(source_path).status_code == 409
    assert len(fetched) == 1
