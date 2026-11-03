"""Run console foundation. Full SF-23 AC01 also needs SF-19 live evidence."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete

from software_factory.api import create_app
from software_factory.models import Run, RunEvent
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
