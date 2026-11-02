from dataclasses import replace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, select

from software_factory.api import create_app
from software_factory.db import db_now
from software_factory.models import Attempt, Principal, Run
from software_factory.service import Service, token_hash
from tests.test_control_plane import completion, submit

from .helpers import dump, jobs, schema_database
from .test_sf14 import make_approval_world
from .test_sf15 import approve_budget, budget_request, costs, report
from .test_sf15_boundaries import charge_fixture


@pytest.mark.ac("SF-15-AC06")
@pytest.mark.parametrize("cause", ["exhausted", "exceeded", "unknown_resolved"])
def test_funding_resume_preserves_job_input_and_never_adopts_old_output(world, cause, evidence):
    first = None
    if cause == "exhausted":
        charge_fixture(world, 950000)
        assert world.claim().status_code == 204
    else:
        first = world.claim().json()
        usage = 1000001 if cause == "exceeded" else None
        assert (
            submit(world, first, body=completion(first) | {"usage_microusd": usage}).status_code
            == 200
        )
        if cause == "unknown_resolved":
            assert report(world, first, 20000).status_code == 200
            assert costs(world)["cost_state"] == "KNOWN"
    before = jobs(world)
    request = budget_request(world).json()["approval"]
    assert approve_budget(world, request).status_code == 200
    resumed = world.claim().json()
    assert resumed["job"]["id"] == before[0].id
    assert resumed["job"]["kind"] == "PLAN"
    attempt_no = jobs(world)[0].attempt_no
    assert attempt_no == (2 if first else 1)
    assert len(jobs(world)) == 1
    assert not world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    evidence(
        "funding retries the same bounded Job/input; rejected output never becomes an adopted Plan",
        {"cause": cause, "attempt_no": attempt_no, "jobs": 1},
    )


@pytest.mark.ac("SF-15-AC06")
def test_budget_change_cannot_override_quality_block(world, evidence):
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.status, run.stop_reason = "BLOCKED", "NO_PROGRESS"
    before = dump(world.database)
    response = budget_request(world)
    assert response.status_code == 409
    assert response.json()["code"] == "BUDGET_CANNOT_OVERRIDE_BLOCK"
    assert dump(world.database) == before
    evidence(
        "budget approval cannot override unchanged code, failed quality or repair ceilings",
        {"reason": "NO_PROGRESS", "status": 409, "state_unchanged": True},
    )


@pytest.mark.ac("SF-15-AC04")
def test_partial_late_usage_cannot_refund_a_provisional_charge(world, clock, evidence):
    assigned = world.claim().json()
    clock.advance(90)
    Service(world.database).reconcile()
    partial = report(world, assigned, 20000, sequence=1, final=False)
    assert partial.status_code == 200
    assert costs(world)["spent_microusd"] == 100000 and costs(world)["cost_state"] == "PROVISIONAL"
    final = report(world, assigned, 20000, sequence=2, final=True)
    assert final.status_code == 200 and costs(world)["spent_microusd"] == 20000
    evidence(
        "only confirmed final usage can refund conservative expired reservation accounting",
        {"partial_spent": partial.json()["run"]["spent_microusd"], "final_spent": 20000},
    )


@pytest.mark.ac("SF-15-AC05")
def test_live_cost_overrun_requests_stop_and_preserves_actual_charge(world, evidence):
    assigned = world.claim().json()
    receipt = report(world, assigned, 1000001, final=True)
    assert receipt.status_code == 200 and world.run()["status"] == "CANCELLING"
    heartbeat = world.post(
        f"/v1/worker/leases/{assigned['lease']['id']}/heartbeat",
        {"fence": assigned["lease"]["fence"]},
        token=world.worker_token,
    )
    assert heartbeat.json()["cancel_requested"] is True
    assert submit(world, assigned).status_code == 200
    assert (world.run()["status"], world.run()["stop_reason"]) == ("BLOCKED", "BUDGET_EXCEEDED")
    assert costs(world)["spent_microusd"] == 1000001 and costs(world)["reserved_microusd"] == 0
    assert not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    evidence(
        "live overrun stops execution and retains the provider-confirmed amount beyond the cap",
        costs(world),
    )


@pytest.mark.ac("SF-15-AC06")
def test_budget_grant_reissues_plan_approval_on_final_revision(world, evidence):
    world = make_approval_world(world)
    old = world.get(f"/v1/runs/{world.run_id}/approvals").json()[0]
    request = budget_request(world).json()["approval"]
    result = approve_budget(world, request)
    assert result.status_code == 200, result.text
    assert (
        world.run()["phase"] == "WAIT_PLAN_APPROVAL" and world.run()["status"] == "WAITING_EXTERNAL"
    )
    active = [
        a
        for a in world.get(f"/v1/runs/{world.run_id}/approvals").json()
        if a["status"] == "PENDING"
    ]
    assert len(active) == 1 and active[0]["action"] == "approve_plan"
    new = active[0]
    assert new["approval_id"] != old["approval_id"]
    assert new["target"]["run_revision"] == world.run()["state_revision"]
    assert new["target"]["budget_microusd"] == 2000000
    reply = world.post(
        f"/v1/approvals/{new['approval_id']}/decisions",
        {
            "expected_run_revision": new["target"]["run_revision"],
            "target_hash": new["target_hash"],
            "decision": "approve",
            "reason": "rechecked the plan with the new budget",
        },
    )
    assert reply.status_code == 200 and world.run()["phase"] == "IMPLEMENT"
    evidence(
        "budget approval never adopts a Plan; a fresh Plan approval binds the final new revision",
        {"old_approval": old["approval_id"], "new_approval": new["approval_id"], "budget": 2000000},
    )


@pytest.mark.ac("SF-15-AC06")
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
def test_budget_approval_deadline_is_strict_and_survives_restart(world, clock, offset, evidence):
    request = budget_request(world).json()["approval"]
    clock.value = request["expires_at"] + offset
    reply = approve_budget(world, request)
    assert reply.status_code == (200 if offset < 0 else 409)
    if offset >= 0:
        Service(world.database).reconcile()
        assert world.run()["stop_reason"] == "BUDGET_APPROVAL_EXPIRED"
        assert world.run()["budget_microusd"] == 1000000
    evidence(
        "budget request expiry uses the same durable 24h boundary as other approvals",
        {"offset": offset, "status": reply.status_code, "budget": world.run()["budget_microusd"]},
    )


@pytest.mark.ac("SF-15-AC06", "C-04")
@pytest.mark.parametrize("point", ["budget.requested", "budget.decided"])
def test_budget_version_and_approval_change_share_atomic_commit(world, point, evidence):
    request = None
    if point == "budget.decided":
        request = budget_request(world).json()["approval"]
    before = dump(world.database)
    service = world.client.app.state.service

    def fail(name):
        if name == point:
            raise RuntimeError("injected budget commit failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            if request:
                approve_budget(world, request, key="budget-atomic")
            else:
                budget_request(world, key="budget-atomic")
    finally:
        service.checkpoint = lambda name: None
    assert dump(world.database) == before
    reply = (
        approve_budget(world, request, key="budget-atomic")
        if request
        else budget_request(world, key="budget-atomic")
    )
    assert reply.status_code == 200
    evidence(
        "budget version, decision, state, audit, Outbox and replay response roll back together",
        {"checkpoint": point, "rollback_equal": True},
    )


@pytest.mark.ac("SF-15-AC03", "C-02")
def test_all_money_routes_enforce_workspace_role_revocation_and_missing_keys(world, evidence):
    assigned = world.claim().json()
    assert submit(world, assigned).status_code == 200
    bworker = replace(world, workspace_id=world.other_workspace_id).make_worker()["worker_token"]
    url = f"/v1/worker/attempts/{assigned['job']['attempt_id']}/usage"
    usage = {
        "provider": "fixture",
        "delivery_id": "auth-case",
        "sequence": 1,
        "total_microusd": 0,
        "final": True,
    }
    run = world.run()
    budget = {
        "expected_run_revision": run["state_revision"],
        "current_budget_microusd": 1000000,
        "new_budget_microusd": 2000000,
        "reason": "authorized budget test",
    }
    routes = [
        (
            "GET",
            f"/v1/runs/{world.run_id}/costs",
            None,
            [(None, 401), (world.worker_token, 403), (world.other_user_token, 404)],
        ),
        ("POST", url, usage, [(None, 401), (world.user_token, 403), (bworker, 404)]),
        (
            "POST",
            f"/v1/runs/{world.run_id}/budget-requests",
            budget,
            [(None, 401), (world.worker_token, 403), (world.other_user_token, 404)],
        ),
    ]
    before = dump(world.database)
    for method, path, body, actors in routes:
        for token, status in actors:
            headers = {"Idempotency-Key": str(uuid4())}
            if token:
                headers["Authorization"] = "Bearer " + token
            assert (
                world.client.request(method, path, json=body, headers=headers).status_code == status
            )
        if method == "POST":
            token = world.worker_token if path == url else world.user_token
            assert (
                world.client.post(
                    path, json=body, headers={"Authorization": "Bearer " + token}
                ).status_code
                == 422
            )
    assert dump(world.database) == before
    with world.database.transaction() as s:
        for secret in (world.user_token, world.worker_token):
            s.scalar(
                select(Principal).where(Principal.token_hash == token_hash(secret))
            ).active = False
    for method, path, body, _ in routes:
        token = world.worker_token if path == url else world.user_token
        assert (
            world.client.request(
                method,
                path,
                json=body,
                headers={"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid4())},
            ).status_code
            == 401
        )
    evidence(
        "three money routes enforce auth/role/workspace/key/revocation with no denied side effects",
        {"routes": 3, "negative_database_unchanged": True},
    )


@pytest.mark.ac("SF-15-AC03")
def test_unmanaged_worker_cannot_attest_real_provider_usage(world, evidence):
    assigned = world.claim().json()
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.snapshot = run.snapshot | {"agent_adapter": "codex_cli", "simulation": False}
    before = dump(world.database)
    reply = report(world, assigned, 20000)
    assert reply.status_code == 403 and reply.json()["code"] == "TRUSTED_USAGE_AUTHORITY_REQUIRED"
    assert dump(world.database) == before
    evidence(
        "a self-reported real-provider fee needs server-issued managed usage authority",
        {"status": reply.status_code, "ledger_unchanged": True},
    )


@pytest.mark.ac("SF-15-AC02")
def test_cache_drift_cannot_create_new_reservation(world, evidence):
    with world.database.transaction() as s:
        s.get(Run, world.run_id).spent_microusd = 1
    reply = world.claim()
    assert reply.status_code == 503 and reply.json()["code"] == "COST_LEDGER_INCONSISTENT"
    with world.database.transaction() as s:
        assert not list(s.scalars(select(Attempt)))
    assert not [e for e in costs(world)["entries"] if e["kind"] == "RESERVATION"]
    evidence(
        "a ledger/cache mismatch fails admission before granting any execution right",
        {"status": reply.status_code, "attempts_created": 0},
    )


@pytest.mark.ac("SF-15-AC01", "SF-15-AC04", "C-01")
@pytest.mark.parametrize("state", ["active", "expired"])
def test_migration_preserves_actual_and_reserved_or_provisional_balances(world, state, evidence):
    first = world.claim().json()
    if state == "active":
        assert (
            submit(world, first, body=completion(first) | {"usage_microusd": 25000}).status_code
            == 200
        )
        world.claim()
    else:
        with world.database.transaction() as s:
            s.get(Attempt, first["job"]["attempt_id"]).expires_at = db_now(s) - 1
        Service(world.database).reconcile()
    original = dump(world.database)
    with schema_database(world.database, "0009") as old:
        with old.engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection)
            for name in (
                "workspaces",
                "principals",
                "worker_enrollments",
                "workers",
                "projects",
                "repositories",
                "tasks",
                "task_spec_versions",
                "runs",
                "jobs",
                "plan_versions",
                "job_attempts",
                "job_dependencies",
            ):
                table = metadata.tables[name]
                for row in original[name]:
                    connection.execute(
                        table.insert().values(**{k: v for k, v in row.items() if k in table.c})
                    )
        before = dump(old)
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
        with old.transaction() as s:
            run = s.get(Run, world.run_id)
            money = Service(old).costs.view(s, run)
            Service(old).costs.require_balanced(s, run)
        assert money["spent_microusd"] == (25000 if state == "active" else 100000)
        assert money["reserved_microusd"] == (100000 if state == "active" else 0)
        after = dump(old)
        for name in ("tasks", "task_spec_versions", "job_attempts"):
            assert after[name] == before[name]
        client = TestClient(create_app(old))
        if state == "expired":
            response = client.post(
                f"/v1/worker/attempts/{first['job']['attempt_id']}/usage",
                json={
                    "provider": "fixture",
                    "delivery_id": "legacy-confirmed",
                    "sequence": 1,
                    "total_microusd": 20000,
                    "currency": "USD",
                    "final": True,
                },
                headers={
                    "Authorization": "Bearer " + world.worker_token,
                    "Idempotency-Key": "legacy-cost",
                },
            )
            assert response.status_code == 200 and response.json()["run"]["spent_microusd"] == 20000
        evidence(
            "0009 balances migrate without fabricating fees; old attempts remain reconcilable",
            {"state": state, "migrated": money, "parent_data_equal": True},
        )
