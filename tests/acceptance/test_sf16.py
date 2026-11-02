from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from software_factory.models import ApprovalDecision, AuditEvent, Run
from tests.test_control_plane import completion, submit

from .helpers import dump, jobs
from .test_sf14 import make_approval_world, pending
from .test_sf15 import budget_request, report


def approval_case(world, action):
    if action == "approve_plan":
        world = make_approval_world(world)
        request = pending(world)
    else:
        request = budget_request(world).json()["approval"]
    return world, request


def approval_body(request, decision="approve"):
    return {
        "target_hash": request["target_hash"],
        "expected_run_revision": request["target"]["run_revision"],
        "reason": "reviewed the fixed subject and constraints",
        "decision": decision,
    }


def intervene(world, request, body, key=None):
    return world.post(
        f"/v1/runs/{world.run_id}/interventions",
        body | {"action": request["action"], "subject_id": request["approval_id"]},
        key=key,
    )


def resolution_body(world):
    target = world.get(f"/v1/runs/{world.run_id}/interventions").json()
    return {
        "action": "resolve_blocked",
        "subject_id": world.run_id,
        "expected_run_revision": target["target"]["run_revision"],
        "target_hash": target["target_hash"],
        "reason": "confirmed the recorded cause has been resolved",
    }


@pytest.mark.ac("SF-16-AC01", "SF-16-AC06", "C-03")
@pytest.mark.parametrize("action", ["approve_plan", "approve_budget"])
@pytest.mark.parametrize("choice", ["approve", "deny"])
@pytest.mark.parametrize("first", ["individual", "intervention"])
def test_both_approval_routes_share_decision_and_cross_route_replay(
    world, action, choice, first, evidence
):
    world, request = approval_case(world, action)
    body = approval_body(request, choice)

    def individual(key):
        return world.post(f"/v1/approvals/{request['approval_id']}/decisions", body, key=key)

    def intervention(key):
        return intervene(world, request, body, key)

    a, b = (individual, intervention) if first == "individual" else (intervention, individual)
    result = a("shared-decision")
    assert result.status_code == 200, result.text
    after = dump(world.database)
    assert b("shared-decision").json() == result.json()
    assert dump(world.database) == after
    assert b("another-decision").status_code == 409
    with world.database.transaction() as s:
        decisions = list(
            s.scalars(select(ApprovalDecision).where(ApprovalDecision.run_id == world.run_id))
        )
        audits = list(
            s.scalars(
                select(AuditEvent).where(
                    AuditEvent.operation.in_(["approval.decide", "budget.decide"])
                )
            )
        )
    assert len(decisions) == len(audits) == 1
    assert audits[0].body["action"] == action and audits[0].body["reason"] == body["reason"]
    assert audits[0].body["before_revision"] < audits[0].body["after_revision"]
    assert world.run()["reserved_microusd"] == 0
    evidence(
        "both routes share authorization, state command, audit and the exact same replay result",
        {"action": action, "decision": choice, "first": first, "decisions": 1, "audits": 1},
    )


@pytest.mark.ac("SF-16-AC04", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_cross_route_approve_deny_race_commits_one_decision(world, trial, evidence):
    action = "approve_plan" if trial % 2 == 0 else "approve_budget"
    world, request = approval_case(world, action)
    barrier = Barrier(2)
    bodies = [approval_body(request, "approve"), approval_body(request, "deny")]

    def send(index):
        barrier.wait(5)
        if index == 0:
            return world.post(
                f"/v1/approvals/{request['approval_id']}/decisions", bodies[index], key="race-0"
            )
        return intervene(world, request, bodies[index], "race-1")

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(send, [0, 1]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    with world.database.transaction() as s:
        votes = list(
            s.scalars(
                select(ApprovalDecision).where(
                    ApprovalDecision.approval_id == request["approval_id"]
                )
            )
        )
    assert len(votes) == 1
    assert len([j for j in jobs(world) if j.kind == "IMPLEMENT"]) <= 1
    evidence(
        "20 cross-route approve/deny races commit one decision and at most one next Job",
        {"trial": trial, "action": action, "statuses": [r.status_code for r in responses]},
    )


@pytest.mark.ac("SF-16-AC05")
def test_resolve_usage_unknown_requires_final_receipt_then_retries_same_job(world, evidence):
    assigned = world.claim().json()
    assert (
        submit(world, assigned, body=completion(assigned) | {"usage_microusd": None}).status_code
        == 200
    )
    before = dump(world.database)
    response = world.post(f"/v1/runs/{world.run_id}/interventions", resolution_body(world))
    assert response.status_code == 409 and response.json()["code"] == "USAGE_NOT_RECONCILED"
    assert dump(world.database) == before
    old = resolution_body(world)
    assert report(world, assigned, 20000).status_code == 200
    assert world.post(f"/v1/runs/{world.run_id}/interventions", old).status_code == 409
    response = world.post(
        f"/v1/runs/{world.run_id}/interventions", resolution_body(world), key="resolved"
    )
    assert response.status_code == 200, response.text
    assert response.json()["reevaluation"]["cost_state"] == "KNOWN"
    resumed = world.claim().json()
    assert resumed["job"]["id"] == assigned["job"]["id"]
    assert resumed["job"]["input"] == assigned["job"]["input"]
    assert len(jobs(world)) == 1 and jobs(world)[0].attempt_no == 2
    assert not world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    evidence(
        "a trusted final receipt resolves unknown usage; the original Job is retried boundedly",
        {"rejected_unknown": 409, "stale_target": 409, "retries": 1, "adopted_old_outputs": 0},
    )


@pytest.mark.ac("SF-16-AC05")
@pytest.mark.parametrize(
    "cause",
    [
        "NO_CODE_CHANGE",
        "NO_PROGRESS",
        "REPAIR_LIMIT_REACHED",
        "REPAIR_COMPARISON_INCONCLUSIVE",
        "SPEC_CHANGE_REQUIRED",
        "PROTECTED_VERIFICATION_CHANGE",
        "CI_TIMEOUT",
        "PLAN_REJECTED",
        "INFRA_ATTEMPTS_EXHAUSTED",
    ],
)
def test_general_resolution_cannot_override_quality_spec_or_attempt_limits(world, cause, evidence):
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.status, run.stop_reason = "BLOCKED", cause
    before = dump(world.database)
    response = world.post(f"/v1/runs/{world.run_id}/interventions", resolution_body(world))
    assert response.status_code == 409
    assert response.json()["code"] == "BLOCK_REQUIRES_SPECIFIC_ACTION"
    assert dump(world.database) == before
    evidence(
        "general human approval cannot change quality, constraints, Spec or retry/Repair ceilings",
        {"cause": cause, "status": 409, "database_unchanged": True},
    )
