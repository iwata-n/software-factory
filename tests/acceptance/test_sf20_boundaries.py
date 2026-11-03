"""SF-20 local evidence. Live Codex and runner evidence is collected separately."""

import pytest

from software_factory.codex_adapter import delivery
from software_factory.codex_engine import parse_events
from software_factory.contracts import ModelCallUsage, digest
from software_factory.metering import price_call
from software_factory.models import Project, Run, Worker
from tests.test_codex_engine import encoded, events
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


@pytest.mark.ac("SF-20-AC04")
def test_codex_turn_counts_and_budget_estimate_are_recorded_separately(world, evidence):
    assignment = world.claim().json()
    attempt_id = assignment["job"]["attempt_id"]
    rates = MANIFEST | {
        "version": "codex-test-rates",
        "provider": "codex_cli",
        "basis": "budget_estimate",
        "input_usd_per_million": "100",
        "cached_input_usd_per_million": "25",
        "output_usd_per_million": "200",
    }
    # The usage boundary receives an existing attempt and a server-authorized Worker.
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        run.snapshot = snapshot = run.snapshot | {
            "agent_adapter": "codex_cli",
            "simulation": False,
            "policy": run.snapshot["policy"] | {"pricing_manifest": rates},
        }
        worker = session.get(Worker, world.worker_id)
        worker.managed = True
        worker.capabilities = [*worker.capabilities, "usage:trusted"]

    response = parse_events(encoded(events()))
    (body,) = delivery(snapshot, response, attempt_id)
    assert body["total_microusd"] == 1450
    path = f"/v1/worker/attempts/{attempt_id}/usage"
    for change, error in (
        ({"total_microusd": 1451}, "USAGE_AMOUNT_MISMATCH"),
        ({"pricing_manifest_hash": "0" * 64}, "PRICING_MANIFEST_MISMATCH"),
    ):
        rejected = world.post(path, body | change, token=world.worker_token)
        assert rejected.status_code == 409 and rejected.json()["code"] == error
    assert costs(world)["receipts"] == []

    accepted = world.post(path, body, token=world.worker_token, key="usage-first")
    replayed = world.post(path, body, token=world.worker_token, key="usage-replay")
    assert accepted.status_code == replayed.status_code == 200
    assert accepted.json()["receipt"] == replayed.json()["receipt"]
    ledger = costs(world)
    (receipt,) = ledger["receipts"]
    assert receipt["attempt_id"] == attempt_id
    assert receipt["origin"] == "trusted_supervisor"
    assert receipt["metadata_body"] == {
        "calls": [
            {
                "call_id": "codex-test-thread",
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 3,
            }
        ],
        "pricing_manifest_hash": digest(rates),
        "pricing_version": "codex-test-rates",
        "currency_basis": "budget_estimate",
        "token_basis": "cli_turn_total",
    }
    assert ledger["spent_microusd"] == 1450
    evidence(
        "Measured CLI turn totals and a versioned budget estimate are stored once",
        {"receipt": receipt, "spent_microusd": ledger["spent_microusd"]},
        attempt_id=attempt_id,
        input_origin="recorded_cli_event_fixture",
    )
