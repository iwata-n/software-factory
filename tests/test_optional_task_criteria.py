import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from software_factory.api import create_app
from software_factory.contracts import ModelCallUsage
from software_factory.models import Job, Project, Run, TaskChatTurn, Worker
from software_factory.task_chat_worker import InterviewReply, claim_chat
from tests.conftest import example_spec


@pytest.mark.parametrize("criteria", [None, []])
def test_task_can_be_saved_with_objective_and_no_title_or_criteria(world, criteria):
    spec = example_spec(world.repository_id)
    spec["task"].pop("title")
    spec["task"].pop("constraints")
    spec["task"].pop("acceptance_criteria")
    spec["task"]["objective"] = "目的だけでタスクを保存する\n詳細はAIと詰める"
    if criteria is not None:
        spec["task"]["acceptance_criteria"] = criteria
    result = world.post(f"/v1/projects/{world.project_id}/tasks", spec)
    assert result.status_code == 201, result.text
    path = f"/v1/tasks/{result.json()['task_id']}"
    task = world.get(path).json()
    assert task["title"] == "目的だけでタスクを保存する"
    assert task["task_status"] == "DRAFT" and task["runs"] == []
    assert task["spec_versions"][0]["body"]["task"]["acceptance_criteria"] == []
    assert world.post(path + "/runs", {"spec_version": 1}).json()["code"] == (
        "SPEC_ACCEPTANCE_CRITERIA_REQUIRED"
    )
    assert world.get(path).json()["runs"] == []
    run = world.post(path + "/runs", {"spec_version": 1, "clarification_only": True}).json()
    assert run["task_status"] == "CLARIFYING"
    assert run["stop_reason"] == "PLAN_HAS_OPEN_QUESTIONS"
    with world.database.transaction() as db:
        assert not db.scalars(select(Job).where(Job.run_id == run["run_id"])).all()


def test_empty_criteria_bootstraps_codex_interview_and_becomes_an_executable_spec(
    world,
    monkeypatch,
    tmp_path,
):
    manifest = "c" * 64
    for name, value in {
        "WORKSPACE_ID": world.workspace_id,
        "AUTH_ROOT": str(tmp_path / "codex"),
        "AUTH_FILE": str(tmp_path / "existing.json"),
        "LOGIN_BINARY": "/usr/bin/false",
    }.items():
        monkeypatch.setenv("FACTORY_CODEX_" + name, value)
    with world.database.transaction() as db:
        project = db.get(Project, world.project_id)
        project.policy = project.policy | {
            "agent_adapter": "codex_cli",
            "allow_external_model": True,
            "agent_configuration": {"engine_manifest_hash": manifest},
            "pricing_manifest": json.loads(Path("config/codex_pricing.json").read_text()),
        }
        worker = db.get(Worker, world.worker_id)
        worker.managed = True
        worker.capabilities = ["PLAN", "adapter:codex_cli", "usage:trusted"]
    spec = example_spec(world.repository_id)
    spec["task"].pop("acceptance_criteria")
    task_id = world.post(f"/v1/projects/{world.project_id}/tasks", spec).json()["task_id"]
    run_id = world.post(
        f"/v1/tasks/{task_id}/runs",
        {
            "spec_version": 1,
            "clarification_only": True,
        },
    ).json()["run_id"]
    origin = "https://factory.example"
    app = create_app(world.database)
    headers = {"Authorization": f"Bearer {world.user_token}", "Origin": origin}
    path = f"/v1/runs/{run_id}/attention"
    with TestClient(app, base_url=origin, headers=headers) as user:
        initial = user.get(path).json()
        assert initial["pending"] and initial["messages"] == [] and initial["history"] == []
        calls = []

        def model(context, schema, *_args, **_kwargs):
            assert schema is InterviewReply
            assert context["task"]["acceptance_criteria"] == []
            assert context["history"] == []
            calls.append(context)
            return SimpleNamespace(
                value={
                    "follow_up": None,
                    "specification": {
                        "objective": "Show the empty state",
                        "acceptance_criteria": [
                            {"id": "AC-1", "description": "Show No items when empty"}
                        ],
                    },
                },
                usage=ModelCallUsage(call_id="bootstrap", input_tokens=20, output_tokens=20),
            )

        with TestClient(
            app,
            base_url=origin,
            headers={
                "Authorization": f"Bearer {world.worker_token}",
            },
        ) as worker:
            assert claim_chat(worker, SimpleNamespace(manifest_hash=manifest, run=model))
        assert len(calls) == 1
        state = user.get(path).json()
        assert state["specification"]["acceptance_criteria"] and state["history"] == []
        button = next(
            c
            for c in state["messages"][1]["updateComponents"]["components"]
            if c["id"] == "adopt_specification"
        )
        envelope = {
            "version": "v0.9.1",
            "action": {
                "name": "adopt_specification",
                "surfaceId": state["surface_id"],
                "sourceComponentId": "adopt_specification",
                "timestamp": datetime.now(UTC).isoformat(),
                "context": button["action"]["event"]["context"],
            },
        }
        adopted = user.post(
            path + "/actions", json=envelope, headers={"Idempotency-Key": "adopt-bootstrap"}
        )
        assert adopted.status_code == 200, adopted.text
        task = user.get(f"/v1/tasks/{task_id}").json()
        assert task["task_status"] == "READY" and len(task["runs"]) == 1
        assert len(task["spec_versions"]) == 2
        assert task["spec_versions"][0]["body"]["task"]["acceptance_criteria"] == []
        assert task["spec_versions"][1]["body"]["task"]["acceptance_criteria"]
        launched = world.post(f"/v1/tasks/{task_id}/runs", {"spec_version": 2}).json()
        assert launched["task_status"] == "RUNNING"
    with world.database.transaction() as db:
        turns = db.scalars(select(TaskChatTurn).where(TaskChatTurn.run_id == run_id)).all()
        assert len(turns) == 1 and turns[0].usage["estimated_microusd"] > 0
        assert db.get(Run, run_id).snapshot["task_spec"]["task"]["acceptance_criteria"] == []
