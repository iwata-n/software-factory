import copy
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from software_factory.contracts import digest
from software_factory.models import ApprovalDecision, Attempt, Job, Project
from software_factory.service import Service
from tests.test_control_plane import completion, submit

from .helpers import jobs


def make_approval_world(world):
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "approval fixture"},
    )
    with world.database.transaction() as s:
        project = s.get(Project, world.project_id)
        project.policy = project.policy | {"require_plan_approval": True}
    world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()[
        "run_id"
    ]
    assigned = world.claim().json()
    assert submit(world, assigned).status_code == 200
    assert world.run()["phase"] == "WAIT_PLAN_APPROVAL"
    return world


@pytest.fixture
def approval_world(world):
    return make_approval_world(world)


def pending(world):
    response = world.get(f"/v1/runs/{world.run_id}/approvals")
    assert response.status_code == 200, response.text
    return response.json()[-1]


def decision_body(world, decision="approve"):
    request = pending(world)
    return {
        "expected_run_revision": request["target"]["run_revision"],
        "target_hash": request["target_hash"],
        "decision": decision,
        "reason": "reviewed the fixed plan and constraints",
    }


def decide(world, decision="approve", key=None, body=None, token=None):
    request = pending(world)
    return world.post(
        f"/v1/approvals/{request['approval_id']}/decisions",
        body or decision_body(world, decision),
        key=key,
        token=token,
    )


@pytest.mark.ac("SF-14-AC01")
@pytest.mark.parametrize("invalid", ["structure", "coverage"])
def test_one_replan_preserves_old_invalid_plan_and_spec(world, invalid, evidence):
    snapshot = world.run()["snapshot"]
    first = world.claim().json()
    payload = completion(first)
    if invalid == "structure":
        payload["result"]["units"] = []
    else:
        payload["result"]["units"][0]["acceptance_ids"] = ["UNKNOWN"]
    assert submit(world, first, body=payload).status_code == 200
    assert (world.run()["status"], world.run()["phase"]) == ("QUEUED", "PLAN")
    plans = world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    assert len(plans) == 1 and plans[0]["validation_error"]
    second = world.claim().json()
    assert second["job"]["input"]["replan_count"] == 1
    assert second["job"]["input"]["spec_hash"] == snapshot["task_spec_hash"]
    assert second["job"]["input"]["base_sha"] == world.run()["base_sha"]
    again = completion(second)
    again["result"]["units"] = []
    assert submit(world, second, body=again).status_code == 200
    after = world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    assert [p["version"] for p in after] == [1, 2] and after[0] == plans[0]
    assert world.run()["status"] == "BLOCKED" and world.run()["snapshot"] == snapshot
    assert len([j for j in jobs(world) if j.kind == "PLAN"]) == 2
    assert not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    evidence(
        "structural/coverage regeneration creates one new immutable Plan version",
        {"invalid": invalid, "plans": after, "run": world.run()},
    )


@pytest.mark.ac("SF-14-AC01")
@pytest.mark.parametrize("issue", ["question", "permissions"])
def test_question_or_permission_problem_does_not_auto_regenerate(world, issue, evidence):
    assigned = world.claim().json()
    payload = completion(assigned)
    if issue == "question":
        payload["result"]["open_questions"] = ["Which acceptance behavior is intended?"]
    else:
        payload["result"]["units"][0]["proposed_write_paths"] = ["src/auth/access.py"]
    assert submit(world, assigned, body=payload).status_code == 200
    assert world.run()["status"] == "BLOCKED" and len(jobs(world)) == 1
    evidence(
        "unresolved questions and protected permissions stop instead of regenerating", world.run()
    )


@pytest.mark.ac("SF-14-AC02")
def test_pending_plan_approval_has_fixed_target_deadline_and_no_execution_slot(
    approval_world, clock, evidence
):
    world = approval_world
    run = world.run()
    request = pending(world)
    plan = world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"][0]
    assert request["plan_id"] == plan["plan_id"] and request["target"]["plan_hash"] == plan[
        "body_hash"
    ] == digest(plan["body"])
    assert request["target_hash"] == digest(request["target"])
    assert request["target"]["run_revision"] == run["state_revision"]
    assert request["target"]["spec_hash"] == run["snapshot"]["task_spec_hash"]
    assert (
        request["target"]["base_sha"] == run["base_sha"]
        and request["target"]["candidate_id"] is None
    )
    assert request["target"]["policy_hash"] == run["snapshot"]["policy_hash"]
    assert request["expires_at"] - request["created_at"] == 86400
    assert run["reserved_microusd"] == 0 and not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    assert world.claim().status_code == 204
    with world.database.transaction() as s:
        assert not list(
            s.scalars(
                select(Attempt)
                .join(Job, Attempt.job_id == Job.id)
                .where(Job.run_id == world.run_id, Attempt.state == "LEASED")
            )
        )
    clock.advance(100)
    Service(world.database).reconcile()
    assert pending(world) == request and world.run()["wait_context"] == run["wait_context"]
    evidence(
        "fixed Plan/Spec/base/Profile/Policy/revision and deadline survive restart",
        {"request": request, "plan": plan, "reserved": run["reserved_microusd"]},
    )


@pytest.mark.ac("SF-14-AC03", "SF-14-AC05", "C-03")
@pytest.mark.parametrize("choice", ["approve", "deny"])
def test_current_approval_or_denial_is_recorded_and_replayed_once(approval_world, choice, evidence):
    world = approval_world
    snapshot = world.run()["snapshot"]
    budget = world.run()["budget_microusd"]
    body = decision_body(world, choice)
    response = decide(world, key="reviewed-plan", body=body)
    assert response.status_code == 200, response.text
    assert decide(world, key="reviewed-plan", body=body).json() == response.json()
    assert (
        decide(
            world,
            key="reviewed-plan",
            body=body | {"decision": "deny" if choice == "approve" else "approve"},
        ).status_code
        == 409
    )
    run = world.run()
    if choice == "approve":
        assert (run["status"], run["phase"]) == ("QUEUED", "IMPLEMENT")
        (implementation,) = [j for j in jobs(world) if j.kind == "IMPLEMENT"]
        assert implementation.payload["plan_id"] == response.json()["approval"]["plan_id"]
        assigned = world.claim().json()
        assert (
            assigned["job"]["plan"]
            == world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"][0]["body"]
        )
    else:
        assert (run["status"], run["stop_reason"]) == ("BLOCKED", "PLAN_REJECTED")
        assert not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    assert run["snapshot"] == snapshot and run["budget_microusd"] == budget
    with world.database.transaction() as s:
        decisions = list(
            s.scalars(select(ApprovalDecision).where(ApprovalDecision.run_id == world.run_id))
        )
    assert len(decisions) == 1 and decisions[0].reason == body["reason"]
    assert decisions[0].before_revision == body["expected_run_revision"]
    evidence(
        "reasoned decision creates one next action with unchanged constraints",
        {"choice": choice, "response": response.json(), "decisions": len(decisions)},
    )


@pytest.mark.ac("SF-14-AC04")
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
def test_new_plan_approval_expires_at_equality(approval_world, clock, offset, evidence):
    world = approval_world
    request = pending(world)
    clock.value = request["expires_at"] + offset
    result = decide(world)
    assert result.status_code == (200 if offset < 0 else 409), result.text
    if offset >= 0:
        assert result.json()["code"] == "APPROVAL_EXPIRED"
        assert not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    evidence(
        "strict DB deadline boundary applies to new approval decisions",
        {"offset": offset, "status": result.status_code, "body": result.json()},
    )


@pytest.mark.ac("SF-14-AC05")
@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_microusd", 999999999),
        ("required_checks", []),
        ("permissions", ["admin"]),
        ("merge", True),
        ("deploy", True),
    ],
)
def test_general_plan_approval_cannot_grant_other_authority(approval_world, field, value, evidence):
    world = approval_world
    before = world.run()
    reply = decide(world, body=decision_body(world) | {field: value})
    assert reply.status_code == 422
    assert world.run() == before and not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    evidence(
        "unrelated budget/verification/permission/merge/deploy grants are rejected",
        {"field": field, "status": reply.status_code},
    )


@pytest.mark.ac("SF-14-AC06")
@pytest.mark.parametrize("field", ["objective", "acceptance_criteria", "repository_id"])
def test_fundamental_replan_change_requires_new_spec_and_run(approval_world, field, evidence):
    world = approval_world
    before = world.run()["snapshot"]
    task = copy.deepcopy(before["task_spec"]["task"])
    if field == "objective":
        task[field] = "A fundamentally different requested goal"
    elif field == "acceptance_criteria":
        task[field] = [{"id": "NEW-AC", "description": "Different required behavior"}]
    else:
        task[field] = str(uuid4())
    result = world.post(
        f"/v1/runs/{world.run_id}/replan",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "scope changed",
            "requested_task": task,
        },
    )
    assert result.status_code == 202, result.text
    assert (world.run()["status"], world.run()["stop_reason"]) == (
        "BLOCKED",
        "SPEC_CHANGE_REQUIRED",
    )
    assert (
        world.run()["snapshot"] == before and len([j for j in jobs(world) if j.kind == "PLAN"]) == 1
    )
    assert pending(world)["status"] == "INVALIDATED"
    evidence(
        "changed goal/required AC/repository does not overwrite the running Spec",
        {"field": field, "run": world.run()},
    )


@pytest.mark.ac("SF-14-AC02", "SF-14-AC04")
def test_plan_and_approval_target_are_immutable_in_database(approval_world, evidence):
    world = approval_world
    plan = world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"][0]
    request = pending(world)
    for sql, reason in (
        ("UPDATE plan_versions SET body='{}'::json WHERE id=:id", "EVIDENCE_IMMUTABLE"),
        (
            "UPDATE approval_requests SET target='{}'::json WHERE id=:id",
            "APPROVAL_TARGET_IMMUTABLE",
        ),
    ):
        identity = plan["plan_id"] if "plan_versions" in sql else request["approval_id"]
        with pytest.raises(IntegrityError, match=reason), world.database.transaction() as s:
            s.execute(text(sql), {"id": identity})
    assert world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"][0] == plan
    assert pending(world) == request
    evidence(
        "Plan body and approval target cannot be mutated through SQL",
        {"plan_hash": plan["body_hash"], "target_hash": request["target_hash"]},
    )


@pytest.mark.ac("SF-14-AC01")
def test_valid_regenerated_plan_is_adopted_as_version_two(world, evidence):
    first = world.claim().json()
    invalid = completion(first)
    invalid["result"]["units"] = []
    assert submit(world, first, body=invalid).status_code == 200
    second = world.claim().json()
    assert submit(world, second).status_code == 200
    plans = world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    (implementation,) = [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    assert [p["version"] for p in plans] == [1, 2]
    assert plans[0]["validation_error"] and plans[1]["validation_error"] is None
    assert implementation.payload["plan_id"] == plans[1]["plan_id"]
    assigned = world.claim().json()
    assert assigned["job"]["plan"] == plans[1]["body"]
    evidence(
        "only regenerated valid Plan version 2 is used by the implementation Job",
        {"plan_id": plans[1]["plan_id"], "plan_hash": plans[1]["body_hash"]},
    )


@pytest.mark.ac("SF-14-AC01")
def test_task_can_reduce_automatic_replanning_limit_to_zero(world, evidence):
    spec = copy.deepcopy(world.run()["snapshot"]["task_spec"])
    spec["execution"]["max_replans"] = 0
    assert world.post(f"/v1/tasks/{world.task_id}/spec-versions", spec).status_code == 201
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "test lower replan limit",
        },
    )
    world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 2}).json()[
        "run_id"
    ]
    assigned = world.claim().json()
    body = completion(assigned)
    body["result"]["units"] = []
    assert submit(world, assigned, body=body).status_code == 200
    assert (
        world.run()["status"] == "BLOCKED"
        and len([j for j in jobs(world) if j.kind == "PLAN"]) == 1
    )
    evidence("a smaller explicit max_replans=0 is respected before any regeneration", world.run())


@pytest.mark.ac("SF-14-AC03", "C-03")
def test_accepted_approval_replay_after_deadline_is_exact_without_another_effect(
    approval_world, clock, evidence
):
    world = approval_world
    body = decision_body(world)
    first = decide(world, key="accepted-before-expiry", body=body)
    assert first.status_code == 200
    clock.value = pending(world)["expires_at"] + 1
    before = world.run()
    replay = decide(world, key="accepted-before-expiry", body=body)
    assert replay.status_code == first.status_code and replay.json() == first.json()
    assert world.run() == before and len([j for j in jobs(world) if j.kind == "IMPLEMENT"]) == 1
    evidence(
        "accepted keyed response survives expiry without issuing a new approval or Job",
        {"response_equal": True, "implementations": 1},
    )
