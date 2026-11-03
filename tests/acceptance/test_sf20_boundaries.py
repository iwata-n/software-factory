"""SF-20 local evidence. Live Codex and runner evidence is collected separately."""

import pytest

from software_factory.contracts import ModelCallUsage, digest
from software_factory.metering import price_call
from software_factory.models import Project
from tests.test_control_plane import completion, submit

from .test_sf15 import MANIFEST, costs, report


@pytest.mark.ac("SF-20-AC04")
def test_fixture_usage_replays_once_after_execution_right_ends(world, evidence):
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "price fixture"},
    )
    manifest = MANIFEST | {"input_usd_per_million": "100", "output_usd_per_million": "200"}
    with world.database.transaction() as session:
        project = session.get(Project, world.project_id)
        project.policy = project.policy | {"pricing_manifest": manifest}
    world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()[
        "run_id"
    ]
    assignment = world.claim().json()
    assert submit(
        world, assignment, body=completion(assignment) | {"usage_microusd": None}
    ).is_success

    call = ModelCallUsage(call_id="fixture-call", input_tokens=2, output_tokens=3)
    assert price_call(manifest, call) == 800
    extra = {"pricing_manifest_hash": digest(manifest), "calls": [call.model_dump()]}
    receipts = [
        report(world, assignment, 800, delivery="fixed-delivery", extra=extra) for _ in range(2)
    ]
    assert all(reply.is_success for reply in receipts)
    assert receipts[0].json()["receipt"] == receipts[1].json()["receipt"]
    ledger = costs(world)
    assert ledger["spent_microusd"] == 800
    assert ledger["reserved_microusd"] == 0
    assert len(ledger["receipts"]) == 1
    evidence("2×100 + 3×200 = 800 micro USD, late delivery applied once", ledger)
