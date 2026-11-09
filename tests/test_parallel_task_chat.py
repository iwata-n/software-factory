import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from software_factory.api import create_app
from software_factory.contracts import ModelCallUsage, digest
from software_factory.db import db_now
from software_factory.models import PlanVersion, Run, TaskChatTurn, Worker
from software_factory.task_chat import automatic_proposal
from software_factory.task_chat_worker import claim_chat
from tests.conftest import example_spec

ORIGIN = "https://factory.example"
MANIFEST = "b" * 64


@pytest.fixture
def parallel_chat(world, monkeypatch, tmp_path):
    for key, value in {
        "WORKSPACE_ID": world.workspace_id,
        "AUTH_ROOT": str(tmp_path / "auth"),
        "AUTH_FILE": str(tmp_path / "existing.json"),
        "LOGIN_BINARY": "/usr/bin/false",
    }.items():
        monkeypatch.setenv("FACTORY_CODEX_" + key, value)
    runs = [world.run_id]
    for _ in range(2):
        task = (
            world.post(f"/v1/projects/{world.project_id}/tasks", example_spec(world.repository_id))
            .raise_for_status()
            .json()
        )
        runs.append(
            world.post(f"/v1/tasks/{task['task_id']}/runs", {"spec_version": 1})
            .raise_for_status()
            .json()["run_id"]
        )
    workers = [
        {"worker_id": world.worker_id, "worker_token": world.worker_token},
        world.make_worker(),
    ]
    with world.database.transaction() as session:
        for index, identifier in enumerate(runs):
            run = session.get(Run, identifier)
            run.status, run.stop_reason = "BLOCKED", "PLAN_HAS_OPEN_QUESTIONS"
            run.created_at = index
            run.snapshot = run.snapshot | {
                "policy": run.snapshot["policy"]
                | {
                    "agent_adapter": "codex_cli",
                    "allow_external_model": True,
                    "agent_configuration": {"engine_manifest_hash": MANIFEST},
                    "pricing_manifest": json.loads(Path("config/codex_pricing.json").read_text()),
                }
            }
        for worker in workers:
            row = session.get(Worker, worker["worker_id"])
            row.managed = True
            row.capabilities = ["PLAN", "adapter:codex_cli", "usage:trusted"]
    app = create_app(world.database, reconcile_interval=3600)
    with TestClient(app, base_url=ORIGIN) as client:
        yield SimpleNamespace(world=world, app=app, client=client, runs=runs, workers=workers)


def queue(fixture, index, *, key=None):
    return fixture.client.post(
        f"/v1/runs/{fixture.runs[index]}/chat",
        json={"message": f"question-{index}"},
        headers={
            "Authorization": f"Bearer {fixture.world.user_token}",
            "Origin": ORIGIN,
            "Idempotency-Key": key or f"question-{index}",
        },
    )


def worker_headers(worker):
    return {"Authorization": f"Bearer {worker['worker_token']}"}


def test_other_runs_are_accepted_but_duplicate_and_concurrent_same_run_are_serialized(
    parallel_chat,
):
    f = parallel_chat
    with ThreadPoolExecutor(2) as executor:
        replies = list(executor.map(lambda key: queue(f, 0, key=key), ["first", "second"]))
    assert sorted(reply.status_code for reply in replies) == [202, 409]
    assert (
        next(reply for reply in replies if reply.status_code == 409).json()["code"]
        == "TASK_CHAT_BUSY"
    )
    successful = next(
        (key, reply)
        for key, reply in zip(["first", "second"], replies, strict=True)
        if reply.status_code == 202
    )
    assert queue(f, 0, key=successful[0]).json() == successful[1].json()
    assert queue(f, 1).status_code == 202
    assert queue(f, 2).status_code == 202


def test_two_workers_execute_models_simultaneously_and_keep_replies_and_usage_separate(
    parallel_chat,
):
    f = parallel_chat
    assert queue(f, 0).status_code == queue(f, 1).status_code == 202
    entered = Barrier(3)
    finish = Event()

    def model(context, schema, *_args, **_kwargs):
        message = context["message"]
        entered.wait(timeout=10)
        assert finish.wait(timeout=10)
        return SimpleNamespace(
            value={"answer": "reply to " + message},
            usage=ModelCallUsage(call_id=message, input_tokens=20, output_tokens=5),
        )

    def process(worker):
        with TestClient(f.app, base_url=ORIGIN, headers=worker_headers(worker)) as client:
            return claim_chat(client, SimpleNamespace(manifest_hash=MANIFEST, run=model))

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(process, worker) for worker in f.workers]
        try:
            entered.wait(timeout=10)
            with f.world.database.transaction() as session:
                turns = session.scalars(select(TaskChatTurn)).all()
                assert len(turns) == 2
                assert {turn.status for turn in turns} == {"RUNNING"}
                assert len({turn.worker_id for turn in turns}) == 2
        finally:
            finish.set()
        assert all(future.result(timeout=10) for future in futures)
    with f.world.database.transaction() as session:
        for turn in session.scalars(select(TaskChatTurn)):
            assert turn.status == "ANSWERED"
            assert turn.answer == "reply to " + turn.question
            assert turn.usage["call_id"] == turn.question


def test_one_worker_cannot_claim_two_chats_and_another_worker_can_take_the_next(parallel_chat):
    f = parallel_chat
    assert queue(f, 0).status_code == queue(f, 1).status_code == 202
    path = "/v1/worker/task-chat/claim"
    first = f.client.post(path, headers=worker_headers(f.workers[0]))
    assert first.status_code == 200
    assert f.client.post(path, headers=worker_headers(f.workers[0])).status_code == 204
    second = f.client.post(path, headers=worker_headers(f.workers[1]))
    assert second.status_code == 200 and second.json()["id"] != first.json()["id"]


def test_failed_or_expired_chat_releases_only_its_run(parallel_chat):
    f = parallel_chat
    expired = queue(f, 0).raise_for_status().json()["id"]
    active = queue(f, 1).raise_for_status().json()["id"]
    with f.world.database.transaction() as session:
        session.get(TaskChatTurn, expired).deadline_at = db_now(session) - 1
    assert queue(f, 0, key="retry-expired").status_code == 202
    assert queue(f, 1, key="still-busy").json()["code"] == "TASK_CHAT_BUSY"
    with f.world.database.transaction() as session:
        assert session.get(TaskChatTurn, expired).error == "CHAT_TIMEOUT"
        assert session.get(TaskChatTurn, active).status == "QUEUED"


def test_automatic_proposal_skips_busy_run_without_starving_another_run(parallel_chat):
    f = parallel_chat
    busy_id = queue(f, 2).raise_for_status().json()["id"]
    with f.world.database.transaction() as session:
        turn = session.get(TaskChatTurn, busy_id)
        turn.status, turn.worker_id = "RUNNING", f.workers[0]["worker_id"]
        for identifier in f.runs[1:]:
            body = {"open_questions": ["Which wording?"]}
            session.add(
                PlanVersion(
                    workspace_id=f.world.workspace_id,
                    run_id=identifier,
                    version=1,
                    body=body,
                    body_hash=digest(body),
                    validation_error="PLAN_HAS_OPEN_QUESTIONS",
                )
            )
    with f.world.database.transaction() as session:
        f.app.state.service.lock_workspace(session, f.world.workspace_id)
        proposal = automatic_proposal(session, f.world.workspace_id)
        assert proposal.run_id == f.runs[1]
