import copy
from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.contracts import ModelCallUsage, digest
from software_factory.costs import price_call
from software_factory.models import Attempt, Project
from software_factory.service import Service
from tests.test_control_plane import completion, submit

from .helpers import jobs


def costs(world):
    reply = world.get(f"/v1/runs/{world.run_id}/costs")
    assert reply.status_code == 200, reply.text
    return reply.json()


def report(
    world, assignment, total, *, sequence=1, final=True, delivery=None, key=None, extra=None
):
    data = {
        "provider": "fixture",
        "delivery_id": delivery or str(uuid4()),
        "sequence": sequence,
        "total_microusd": total,
        "currency": "USD",
        "final": final,
    }
    data.update(extra or {})
    return world.post(
        f"/v1/worker/attempts/{assignment['job']['attempt_id']}/usage",
        data,
        token=world.worker_token,
        key=key,
    )


def budget_request(world, limit=2000000, *, key=None):
    run = world.run()
    return world.post(
        f"/v1/runs/{world.run_id}/budget-requests",
        {
            "expected_run_revision": run["state_revision"],
            "current_budget_microusd": run["budget_microusd"],
            "new_budget_microusd": limit,
            "reason": "bounded additional test budget",
        },
        key=key,
    )


def approve_budget(world, request, *, choice="approve", key=None, body=None):
    data = {
        "expected_run_revision": request["target"]["run_revision"],
        "target_hash": request["target_hash"],
        "decision": choice,
        "reason": "reviewed current budget, cost and target",
    }
    return world.post(f"/v1/approvals/{request['approval_id']}/decisions", body or data, key=key)


@pytest.mark.ac("SF-15-AC01")
def test_reservation_and_final_usage_recompute_exactly_from_immutable_entries(world, evidence):
    assigned = world.claim().json()
    active = costs(world)
    assert (
        active["budget_microusd"],
        active["reserved_microusd"],
        active["spent_microusd"],
        active["available_microusd"],
    ) == (1000000, 100000, 0, 900000)
    assert (
        submit(world, assigned, body=completion(assigned) | {"usage_microusd": 25000}).status_code
        == 200
    )
    after = costs(world)
    assert (after["reserved_microusd"], after["spent_microusd"], after["available_microusd"]) == (
        0,
        25000,
        975000,
    )
    for account, expected in [("RESERVED", 0), ("SPENT", 25000)]:
        assert (
            sum(e["amount_microusd"] for e in after["entries"] if e["account"] == account)
            == expected
        )
    assert {e["kind"] for e in after["entries"]} == {"RESERVATION", "RELEASE", "ACTUAL"}
    assert all(
        e["currency"] == "USD" and e["attempt_id"] == assigned["job"]["attempt_id"]
        for e in after["entries"]
    )
    assert world.run()["spent_microusd"] == after["spent_microusd"]
    evidence(
        "1,000,000 budget reserves 100,000 and settles 25,000; every balance is ledger-derived",
        {"active": active, "after": after},
        attempt_id=assigned["job"]["attempt_id"],
    )


@pytest.mark.ac("SF-15-AC03")
def test_duplicate_and_reverse_sequence_usage_never_double_charge(world, evidence):
    assigned = world.claim().json()
    delivered = report(world, assigned, 30000, sequence=2, delivery="delivery-2", key="receipt-2")
    assert delivered.status_code == 200, delivered.text
    assert (costs(world)["spent_microusd"], costs(world)["reserved_microusd"]) == (30000, 70000)
    older = report(world, assigned, 20000, sequence=1, delivery="delivery-1")
    assert older.status_code == 200 and older.json()["receipt"]["applied"] is False
    assert (
        report(world, assigned, 30000, sequence=2, delivery="delivery-2", key="receipt-2").json()
        == delivered.json()
    )
    assert report(world, assigned, 30000, sequence=2, delivery="delivery-2").status_code == 200
    assert report(world, assigned, 40000, sequence=2, delivery="delivery-2").status_code == 409
    assert costs(world)["spent_microusd"] == 30000 and len(costs(world)["receipts"]) == 2
    assert submit(world, assigned).status_code == 200
    assert (costs(world)["spent_microusd"], costs(world)["reserved_microusd"]) == (30000, 0)
    evidence(
        "provider delivery ID dedup, sequence ordering and final settlement each apply once",
        costs(world),
    )


@pytest.mark.ac("SF-15-AC04")
def test_expired_reservation_is_adjusted_once_by_late_confirmed_actual_usage(
    world, clock, evidence
):
    assigned = world.claim().json()
    clock.advance(90)
    Service(world.database).reconcile()
    provisional = costs(world)
    assert provisional["spent_microusd"] == 100000 and provisional["cost_state"] == "PROVISIONAL"
    result = report(world, assigned, 20000, delivery="confirmed-after-expiry", key="late-cost")
    assert result.status_code == 200, result.text
    assert (
        report(world, assigned, 20000, delivery="confirmed-after-expiry", key="late-cost").json()
        == result.json()
    )
    assert report(world, assigned, 20000, delivery="confirmed-after-expiry").status_code == 200
    final = costs(world)
    assert (final["spent_microusd"], final["reserved_microusd"], final["cost_state"]) == (
        20000,
        0,
        "KNOWN",
    )
    (adjustment,) = [e for e in final["entries"] if e["kind"] == "ADJUSTMENT"]
    assert adjustment["amount_microusd"] == -80000
    assert [e for e in final["entries"] if e["kind"] == "PROVISIONAL"][0][
        "amount_microusd"
    ] == 100000
    assert submit(world, assigned).status_code == 409
    assert not world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    evidence(
        "late verified accounting refunds 80,000 once without restoring expired execution rights",
        {"provisional": provisional, "final": final, "stale_result": 409},
    )


@pytest.mark.ac("SF-15-AC05")
@pytest.mark.parametrize("usage,reason", [(1000001, "BUDGET_EXCEEDED"), (None, "USAGE_UNKNOWN")])
def test_overrun_or_unmetered_execution_stops_without_fake_zero(world, usage, reason, evidence):
    assigned = world.claim().json()
    result = submit(world, assigned, body=completion(assigned) | {"usage_microusd": usage})
    assert result.status_code == 200
    run = world.run()
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", reason)
    assert run["reserved_microusd"] == 0 and run["spent_microusd"] == (usage or 100000)
    assert not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    if usage is None:
        assert run["cost_state"] == "UNKNOWN"
        with world.database.transaction() as s:
            attempt = s.get(Attempt, assigned["job"]["attempt_id"])
        assert attempt.result["usage_microusd"] is None
    evidence(
        "actual overrun is retained; missing metering is provisionally charged and blocked",
        {"reported": usage, "run": run, "ledger": costs(world)},
    )


@pytest.mark.ac("SF-15-AC06")
def test_only_current_budget_approval_adds_a_version_and_resumes_reservation(world, evidence):
    snapshot = copy.deepcopy(world.run()["snapshot"])
    request_reply = budget_request(world, key="request-budget")
    assert request_reply.status_code == 200, request_reply.text
    request = request_reply.json()["approval"]
    assert world.run()["phase"] == "WAIT_BUDGET_APPROVAL" and world.claim().status_code == 204
    assert request["target"]["current_budget_microusd"] == 1000000
    assert request["target"]["new_budget_microusd"] == 2000000
    approved = approve_budget(world, request, key="approve-budget")
    assert approved.status_code == 200, approved.text
    assert approve_budget(world, request, key="approve-budget").json() == approved.json()
    assert world.run()["snapshot"] == snapshot and world.run()["budget_version"] == 2
    assert world.run()["budget_microusd"] == 2000000
    assigned = world.claim().json()
    assert assigned["job"]["kind"] == "PLAN"
    assert (costs(world)["reserved_microusd"], costs(world)["available_microusd"]) == (
        100000,
        1900000,
    )
    evidence(
        "a reasoned approval adds budget version 2 while retaining the original snapshot",
        {"request": request, "approved": approved.json(), "costs": costs(world)},
    )


@pytest.mark.ac("SF-15-AC05", "SF-15-AC06")
def test_budget_increase_cannot_clear_unknown_usage(world, evidence):
    assigned = world.claim().json()
    assert (
        submit(world, assigned, body=completion(assigned) | {"usage_microusd": None}).status_code
        == 200
    )
    request = budget_request(world).json()["approval"]
    approved = approve_budget(world, request)
    assert approved.status_code == 200, approved.text
    assert world.run()["budget_microusd"] == 2000000
    assert world.run()["stop_reason"] == "USAGE_UNKNOWN" and world.claim().status_code == 204
    evidence("more budget does not fabricate usage or authorize an unmetered result", world.run())


MANIFEST = {
    "version": "fixture-1",
    "provider": "fixture",
    "model": "fixture-model",
    "currency": "USD",
    "basis": "provider_invoice",
    "source_url": "https://fixture.example/pricing",
    "input_usd_per_million": "0.2",
    "cached_input_usd_per_million": "0.1",
    "output_usd_per_million": "0.3",
}


@pytest.mark.ac("SF-15-AC01", "SF-15-AC03")
def test_decimal_charge_rounds_up_per_call_and_validates_frozen_manifest(world, evidence):
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "metered fixture"},
    )
    with world.database.transaction() as s:
        project = s.get(Project, world.project_id)
        project.policy = project.policy | {"pricing_manifest": MANIFEST}
    world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()[
        "run_id"
    ]
    assigned = world.claim().json()
    calls = [ModelCallUsage(call_id=f"call-{i}", input_tokens=1, output_tokens=0) for i in range(2)]
    charges = [price_call(MANIFEST, c) for c in calls]
    assert charges == [1, 1]
    extra = {"pricing_manifest_hash": digest(MANIFEST), "calls": [c.model_dump() for c in calls]}
    assert report(world, assigned, 1, extra=extra).status_code == 409
    accepted = report(world, assigned, 2, extra=extra)
    assert accepted.status_code == 200, accepted.text
    assert costs(world)["spent_microusd"] == 2
    with world.database.transaction() as s:
        from software_factory.models import UsageReceipt

        saved = s.scalar(select(UsageReceipt).where(UsageReceipt.run_id == world.run_id))
    assert saved.metadata_body["calls"][0]["input_tokens"] == 1
    evidence(
        "frozen Decimal rates charge two sub-micro calls as 1+1, not aggregate rounding",
        {
            "per_call_microusd": charges,
            "charged_microusd": costs(world)["spent_microusd"],
            "manifest_hash": digest(MANIFEST),
            "metering_counter_preserved": True,
        },
    )
