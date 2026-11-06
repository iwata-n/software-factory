from sqlalchemy import select

from software_factory.models import Job, Run
from tests.conftest import example_spec
from tests.test_control_plane import submit


def board(world, **params):
    return world.client.get(
        "/v1/operations/tasks",
        params=params,
        headers={
            "Authorization": f"Bearer {world.user_token}",
        },
    )


def test_saved_task_clarifies_then_waits_for_explicit_execution(world):
    created = world.post(
        f"/v1/projects/{world.project_id}/tasks", example_spec(world.repository_id)
    )
    task_id = created.json()["task_id"]
    path = f"/v1/tasks/{task_id}"
    row = next(t for t in board(world).json()["tasks"] if t["task_id"] == task_id)
    assert row["task_status"] == "DRAFT" and row["run_id"] is None
    assert world.get(path).json()["task_status"] == "DRAFT"
    started = world.post(
        path + "/runs",
        {
            "spec_version": 1,
            "clarification_only": True,
            "reuse_existing": True,
        },
    ).json()
    assert (
        world.post(
            path + "/runs",
            {
                "spec_version": 1,
                "clarification_only": True,
                "reuse_existing": True,
            },
        ).json()["run_id"]
        == started["run_id"]
    )
    assert world.get(path).json()["task_status"] == "CLARIFYING"
    # Cancel the independent fixture Run so the worker claims this task's PLAN.
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "isolate test task",
        },
    )
    assignment = world.claim().json()
    assert assignment["job"]["run_id"] == started["run_id"]
    assert submit(world, assignment).status_code == 200
    result = world.get(f"/v1/runs/{started['run_id']}").json()
    assert (result["status"], result["phase"]) == ("WAITING_EXTERNAL", "WAIT_PLAN_APPROVAL")
    assert result["task_status"] == "READY"
    assert world.get(path).json()["task_status"] == "READY"
    assert world.claim().status_code == 204
    with world.database.transaction() as session:
        assert not session.scalars(
            select(Job).where(
                Job.run_id == started["run_id"],
                Job.kind == "IMPLEMENT",
            )
        ).all()
    snapshot = result["snapshot"]
    request = world.get(f"/v1/runs/{started['run_id']}/approvals").json()[-1]
    response = world.post(
        f"/v1/approvals/{request['approval_id']}/decisions",
        {
            "expected_run_revision": result["state_revision"],
            "target_hash": request["target_hash"],
            "decision": "approve",
            "reason": "start implementation after clarification",
        },
        key="execute-clarified-task",
    )
    assert response.status_code == 200, response.text
    assert world.get(f"/v1/runs/{started['run_id']}").json()["snapshot"] == snapshot
    assert world.get(path).json()["task_status"] == "RUNNING"
    assert world.get(f"/v1/runs/{started['run_id']}").json()["task_status"] == "RUNNING"
    assert world.claim().json()["job"]["kind"] == "IMPLEMENT"


def test_task_board_is_scoped_paginated_and_includes_unstarted_tasks(world):
    ids = [
        world.post(
            f"/v1/projects/{world.project_id}/tasks", example_spec(world.repository_id)
        ).json()["task_id"]
        for _ in range(3)
    ]
    seen = []
    before = None
    while True:
        response = board(world, limit=1, **({"before": before} if before else {}))
        assert response.headers["Cache-Control"] == "no-store"
        page = response.json()
        seen.extend(t["task_id"] for t in page["tasks"])
        before = page["next_before"]
        if not before:
            break
    assert len(seen) == len(set(seen)) == 4
    assert set(ids).issubset(seen)
    assert (
        world.client.get(
            "/v1/operations/tasks",
            headers={
                "Authorization": f"Bearer {world.other_user_token}",
            },
        ).json()["tasks"]
        == []
    )
    assert (
        world.client.get(
            "/v1/operations/tasks",
            params={"before": ids[0]},
            headers={
                "Authorization": f"Bearer {world.other_user_token}",
            },
        ).status_code
        == 404
    )


def test_clarification_and_execution_reuse_do_not_confuse_different_run_modes(world):
    path = f"/v1/tasks/{world.task_id}/runs"
    clarification = world.post(
        path,
        {
            "spec_version": 1,
            "clarification_only": True,
            "reuse_existing": True,
        },
    ).json()
    assert clarification["run_id"] != world.run_id
    execution = world.post(path, {"spec_version": 1, "reuse_existing": True}).json()
    assert execution["run_id"] == world.run_id
    with world.database.transaction() as session:
        assert len(session.scalars(select(Run).where(Run.task_id == world.task_id)).all()) == 2
