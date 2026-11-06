import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from software_factory.api import create_app
from software_factory.contracts import ModelCallUsage, digest
from software_factory.models import PlanVersion, Run, Task, TaskChatTurn, Worker
from software_factory.task_chat_worker import InterviewReply, TaskAnswerOptions, claim_chat

ORIGIN = "https://factory.example"
MANIFEST = "c" * 64
QUESTION = "What should the empty state say?"


def test_specification_interview_repeats_questions_until_the_operator_adopts(
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
    assert [c["id"] for c in components if c["component"] == "Button"] == ["continue_interview"]
    assert next(c for c in components if c["component"] == "ChoicePicker")["options"] == [
        {"label": "その他", "value": "other"}
    ]
    assert [m["version"] for m in surface] == ["v0.9.1"] * 3
    assert next(c["text"] for c in components if c["id"] == "question") == QUESTION
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

    def submit(messages, name, key, *, choice=None, text=""):
        values = {"answers": {"1": {"choice": [choice], "text": text}}} if choice else {}
        return http.post(
            path + "/actions",
            json=envelope(messages, name, **values),
            headers=auth(world.user_token, key),
        )

    def get_state():
        return http.get(path, headers=user).raise_for_status().json()

    assert submit(surface, "continue_interview", "empty", choice="other").status_code == 422

    follow_up = {"question": "Which language?", "options": ["Japanese", "English"]}
    spec = {
        "objective": "Show an empty state in Japanese.",
        "acceptance_criteria": [
            {"id": "AC-1", "description": "Show 「データがありません」 when empty."}
        ],
    }

    def model(context, schema, *_args, **_kwargs):
        if schema is TaskAnswerOptions:
            assert context["run"]["open_questions"] == [QUESTION]
            value = {"questions": [{"question_number": 1, "options": ["Show No items yet."]}]}
        else:
            assert schema is InterviewReply
            assert context["role"] == "SPECIFICATION_INTERVIEW"
            assert context["open_questions"] == [QUESTION]
            assert context["history"][0] == {"question": QUESTION, "answer": "Use an empty state."}
            if len(context["history"]) == 1:
                value = {"follow_up": follow_up, "specification": None}
            else:
                assert context["history"][1] == {
                    "question": "Which language?",
                    "answer": "Japanese",
                }
                if len(context["history"]) == 3:
                    assert context["previous_draft"] == spec
                    assert context["history"][2]["answer"] == "Specify that there are zero records."
                    value = {
                        "follow_up": None,
                        "specification": spec
                        | {"objective": "Show a Japanese empty state with zero records."},
                    }
                else:
                    value = {"follow_up": None, "specification": spec}
        return SimpleNamespace(
            value=value,
            usage=ModelCallUsage(call_id="fixture", input_tokens=20, output_tokens=11),
        )

    with TestClient(app, base_url=ORIGIN, headers=auth(world.worker_token)) as worker:
        engine = SimpleNamespace(manifest_hash=MANIFEST, run=model)
        assert claim_chat(worker, engine)
        assert not claim_chat(worker, engine)

        proposed = get_state()["messages"]
        choices = next(
            c
            for c in proposed[1]["updateComponents"]["components"]
            if c["component"] == "ChoicePicker"
        )
        assert choices["options"] == [
            {"label": "Show No items yet.（推奨）", "value": "1"},
            {"label": "その他", "value": "other"},
        ]
        first = submit(
            proposed, "continue_interview", "first", choice="other", text="Use an empty state."
        )
        assert first.status_code == 200
        assert (
            submit(
                proposed, "continue_interview", "first", choice="other", text="Use an empty state."
            ).json()
            == first.json()
        )
        assert (
            submit(
                proposed, "continue_interview", "stale", choice="other", text="different answer"
            ).status_code
            == 409
        )
        assert get_state()["pending"] and not get_state()["messages"]

        assert claim_chat(worker, engine)
        state = get_state()
        assert state["specification"] is None
        question = state["messages"][1]["updateComponents"]["components"]
        assert next(c["text"] for c in question if c["id"] == "question") == "Which language?"
        assert (
            submit(state["messages"], "continue_interview", "second", choice="1").status_code == 200
        )

        assert claim_chat(worker, engine)
        state = get_state()
        assert state["specification"] == spec
        assert len(state["history"]) == 2
        assert (
            submit(
                state["messages"],
                "refine_specification",
                "feedback",
                choice="other",
                text="Specify that there are zero records.",
            ).status_code
            == 200
        )
        assert claim_chat(worker, engine)
        state = get_state()
        assert "zero records" in state["specification"]["objective"]

    with world.database.transaction() as db:
        assert db.get(Task, world.task_id).latest_spec_version == 1
        assert db.get(Run, world.run_id).status == "BLOCKED"
        assert len(db.scalars(select(Run)).all()) == 1

    saved = submit(state["messages"], "adopt_specification", "adopt")
    assert saved.json() == {"spec_version": 2}
    assert submit(state["messages"], "adopt_specification", "adopt-again").json() == saved.json()
    assert get_state()["adopted_version"] == 2
    assert get_state()["resumed_run_id"] is None
    task = world.get(f"/v1/tasks/{world.task_id}").raise_for_status().json()
    assert (
        task["spec_versions"][-1]["body"]["task"]["objective"]
        == state["specification"]["objective"]
    )
    assert len(task["runs"]) == 1
    assert task["task_status"] == "READY"
    assert task["clarified_spec_version"] == 2
    launch = f"/v1/tasks/{world.task_id}/runs"
    start = {"spec_version": 2, "reuse_existing": True}
    run_id = world.post(launch, start).raise_for_status().json()["run_id"]
    assert world.post(launch, start).raise_for_status().json()["run_id"] == run_id
    assert get_state()["resumed_run_id"] == run_id
    assert world.get(f"/v1/tasks/{world.task_id}").json()["task_status"] == "RUNNING"

    assert http.get(f"/v1/runs/{world.run_id}/chat", headers=user).json()["turns"] == []
    with world.database.transaction() as db:
        turns = db.scalars(select(TaskChatTurn)).all()
        assert len(turns) == 4
        assert all(turn.usage["estimated_microusd"] > 0 for turn in turns)
