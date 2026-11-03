"""Acceptance checks for retained Run and approval state."""

import pytest
from sqlalchemy import select

from scripts.factory_maintenance import DAY, collect
from software_factory.models import AuditEvent, RunEvent

from .test_sf14 import approval_world as approval_world


@pytest.mark.ac("SF-24-AC06")
def test_retention_keeps_pending_approval_and_its_persisted_plan(approval_world, clock, evidence):
    world = approval_world
    run_path = f"/v1/runs/{world.run_id}"
    paths = ("", "/approvals", "/plan", "/event-history")
    before = {path: world.get(run_path + path).raise_for_status().json() for path in paths}
    assert before[""]["phase"] == "WAIT_PLAN_APPROVAL"
    assert before["/approvals"] and before["/plan"]["plans"]
    assert before["/event-history"]["events"]
    root = world.client.app.state.service.artifacts.storage.root
    root.mkdir(parents=True, exist_ok=True)
    with world.database.transaction() as session:
        events = list(session.scalars(select(RunEvent).where(RunEvent.run_id == world.run_id)))
        assert len(events) == len(before["/event-history"]["events"])

    clock.advance(91 * DAY)
    first = collect(world.database, root)
    assert first["runs_purged"] == 1
    second = collect(world.database, root)
    assert second == {
        "marked_deleted": 0,
        "events_removed": 0,
        "runs_purged": 0,
        "tasks_purged": 0,
    }
    assert {path: world.get(run_path + path).raise_for_status().json() for path in paths} == before
    with world.database.transaction() as session:
        audit = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.operation.in_(["run.retention.purge", "run.evidence.collect"])
                )
            )
        )
        assert any(row.operation == "run.retention.purge" for row in audit)
        assert not any(row.body.get("run_id") == world.run_id for row in audit)
    evidence(
        "Repeated collection retains the pending approval, Plan and all history",
        {"clock_advanced_days": 91, "first": first, "second": second, "unchanged": True},
        run_id=world.run_id,
    )
