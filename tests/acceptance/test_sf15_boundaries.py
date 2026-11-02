from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from software_factory.models import ApprovalDecision, BudgetVersion, Run
from software_factory.service import event
from tests.test_control_plane import completion, submit

from .helpers import dump
from .test_sf15 import approve_budget, budget_request, costs, report


def charge_fixture(world, amount):
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        money = world.client.app.state.service.costs
        money.append(
            s, run, None, "fixture-opening-spend", "SPENT", "ADJUSTMENT", amount, "simulation"
        )
        money.refresh(s, run)


@pytest.mark.ac("SF-15-AC02")
@pytest.mark.parametrize("available,status", [(99999, 204), (100000, 200)])
def test_budget_admission_strict_boundary_uses_ledger(world, available, status, evidence):
    charge_fixture(world, 1000000 - available)
    reply = world.claim()
    assert reply.status_code == status
    money = costs(world)
    assert money["available_microusd"] == (available if status == 204 else 0)
    assert money["reserved_microusd"] == (0 if status == 204 else 100000)
    if status == 204:
        assert world.run()["stop_reason"] == "BUDGET_EXHAUSTED"
    evidence(
        "99,999 cannot reserve 100,000; exactly 100,000 can reserve once",
        {"available_before": available, "status": status, "money": money},
    )


@pytest.mark.ac("SF-15-AC02", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_two_workers_contending_for_last_reservation_never_overbook(world, trial, evidence):
    charge_fixture(world, 900000)
    second = world.make_worker()["worker_token"]
    barrier = Barrier(2)

    def claim(token):
        barrier.wait(5)
        return world.claim(token=token)

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(claim, [world.worker_token, second]))
    assert sorted(r.status_code for r in replies) == [200, 204]
    money = costs(world)
    assert money["reserved_microusd"] == 100000 and money["available_microusd"] == 0
    assert len([e for e in money["entries"] if e["kind"] == "RESERVATION"]) == 1
    evidence(
        "20 concurrent last-budget claims reserve once without negative available balance",
        {"trial": trial, "statuses": [r.status_code for r in replies], "money": money},
    )


@pytest.mark.ac("SF-15-AC03")
@pytest.mark.parametrize(
    "change",
    [
        {"total_microusd": -1},
        {"total_microusd": 1.5},
        {"total_microusd": "25000"},
        {"total_microusd": True},
        {"total_microusd": 10**12 + 1},
        {"currency": "JPY"},
        {"sequence": 0},
        {"sequence": 1.0},
        {"final": "true"},
        {"calls": [{"call_id": "x", "input_tokens": 1.2, "output_tokens": 0}]},
        {
            "calls": [
                {"call_id": "x", "input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0}
            ]
        },
    ],
)
def test_invalid_usage_never_posts_financial_entries(world, change, evidence):
    assigned = world.claim().json()
    before = dump(world.database)
    reply = report(world, assigned, 25000, extra=change)
    assert reply.status_code == 422, reply.text
    assert dump(world.database) == before
    evidence(
        "usage requires integer USD and typed counts/sequence/final; invalid input posts nothing",
        {"invalid_fields": list(change), "status": reply.status_code},
    )


@pytest.mark.ac("SF-15-AC03", "C-02", "C-06")
def test_usage_authority_is_own_attempt_and_secret_ids_are_rejected(world, evidence):
    assigned = world.claim().json()
    before = dump(world.database)
    other = world.make_worker()["worker_token"]
    # Enrollment itself is outside the negative-operation comparison.
    before = dump(world.database)
    path = f"/v1/worker/attempts/{assigned['job']['attempt_id']}/usage"
    payload = {
        "provider": "fixture",
        "delivery_id": "delivery-test",
        "sequence": 1,
        "total_microusd": 20000,
        "currency": "USD",
        "final": True,
    }
    for token, status in ((None, 401), (world.user_token, 403), (other, 404)):
        headers = {"Idempotency-Key": str(uuid4())}
        if token:
            headers["Authorization"] = "Bearer " + token
        assert world.client.post(path, json=payload, headers=headers).status_code == status
    assert (
        report(world, assigned, 20000, delivery="ghp_PRIVATE_ACCOUNTING_CANARY").status_code == 422
    )
    assert report(world, assigned, 20000, delivery=world.worker_token).status_code == 422
    assert dump(world.database) == before
    assert (
        world.get(f"/v1/runs/{world.run_id}/costs", token=world.other_user_token).status_code == 404
    )
    assert world.get(f"/v1/runs/{world.run_id}/costs", token=world.worker_token).status_code == 403
    evidence(
        "accounting cannot access another attempt; credential-like IDs are rejected",
        {"own_attempt_only": True, "secret_ids_rejected": 2, "negative_effects": 0},
    )


@pytest.mark.ac("SF-15-AC01", "C-04")
@pytest.mark.parametrize("point", ["cost.entry", "write.before_commit"])
def test_reservation_and_usage_postings_roll_back_with_execution(world, point, evidence):
    assigned = world.claim().json()
    before = dump(world.database)
    service = world.client.app.state.service

    def fail(name):
        if name == point:
            raise RuntimeError("injected monetary transaction failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            submit(
                world,
                assigned,
                key="cost-completion",
                body=completion(assigned) | {"usage_microusd": 25000},
            )
    finally:
        service.checkpoint = lambda name: None
    assert dump(world.database) == before
    assert (
        submit(
            world,
            assigned,
            key="cost-completion",
            body=completion(assigned) | {"usage_microusd": 25000},
        ).status_code
        == 200
    )
    assert costs(world)["spent_microusd"] == 25000 and costs(world)["reserved_microusd"] == 0
    evidence(
        "ledger, fee cache, next Job, result and replay row share one commit",
        {"checkpoint": point, "rollback_equal": True, "spent": 25000},
    )


@pytest.mark.ac("SF-15-AC06")
def test_stale_budget_target_and_below_obligation_requests_do_not_change_limits(world, evidence):
    original = world.run()["budget_microusd"]
    requested = budget_request(world).json()["approval"]
    with world.database.transaction() as s:
        event(s, s.get(Run, world.run_id), "OTHER_APPROVAL_CONTEXT", {"fixture": True})
    before = dump(world.database)
    assert approve_budget(world, requested).status_code == 409
    assert dump(world.database) == before and world.run()["budget_microusd"] == original
    evidence(
        "old revision cannot approve a budget after any intervening context change",
        {"status": 409, "budget_microusd": original},
    )


@pytest.mark.ac("SF-15-AC06")
def test_new_budget_must_cover_actual_posted_obligations(world, evidence):
    assigned = world.claim().json()
    assert (
        submit(world, assigned, body=completion(assigned) | {"usage_microusd": 2500000}).status_code
        == 200
    )
    before = dump(world.database)
    reply = budget_request(world, 2000000)
    assert reply.status_code == 422 and reply.json()["code"] == "BUDGET_BELOW_OBLIGATIONS"
    assert dump(world.database) == before and world.run()["budget_microusd"] == 1000000
    evidence(
        "proposed limit cannot be below posted spend plus reservations",
        {"spent": 2500000, "proposed": 2000000, "status": 422},
    )


@pytest.mark.ac("SF-15-AC06", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_budget_approve_deny_race_creates_at_most_one_new_version(world, trial, evidence):
    request = budget_request(world).json()["approval"]
    barrier = Barrier(2)

    def decide(choice):
        barrier.wait(5)
        return approve_budget(world, request, choice=choice, key="race-" + choice)

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(decide, ["approve", "deny"]))
    assert sorted(r.status_code for r in replies) == [200, 409]
    with world.database.transaction() as s:
        versions = list(
            s.scalars(select(BudgetVersion).where(BudgetVersion.run_id == world.run_id))
        )
        assert s.scalar(select(func.count()).select_from(ApprovalDecision)) == 1
    assert len(versions) == (2 if replies[0].status_code == 200 else 1)
    assert world.run()["reserved_microusd"] == 0
    evidence(
        "20 budget approve/deny races have one decision and at most one new budget version",
        {"trial": trial, "statuses": [r.status_code for r in replies], "versions": len(versions)},
    )


@pytest.mark.ac("SF-15-AC01", "SF-15-AC04")
def test_cost_receipt_and_budget_history_are_append_only(world, evidence):
    assigned = world.claim().json()
    assert report(world, assigned, 20000).status_code == 200
    for table in ("cost_entries", "usage_receipts", "budget_versions"):
        with (
            pytest.raises(IntegrityError, match="EVIDENCE_IMMUTABLE"),
            world.database.transaction() as s,
        ):
            s.execute(text(f"DELETE FROM {table}"))
    evidence(
        "reservation/usage/adjustment receipts and budget history cannot be overwritten or deleted",
        {"immutable_tables": ["cost_entries", "usage_receipts", "budget_versions"]},
    )
