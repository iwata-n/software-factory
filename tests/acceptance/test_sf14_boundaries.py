import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from software_factory.contracts import digest
from software_factory.db import db_now
from software_factory.models import ApprovalDecision, PlanVersion, Principal, Project, Run
from software_factory.service import Service, event, token_hash
from tests.test_control_plane import submit

from .helpers import dump, jobs
from .test_sf14 import approval_world as approval_world
from .test_sf14 import decide, decision_body, make_approval_world, pending


@pytest.mark.ac("SF-14-AC03", "SF-14-AC04", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_approve_deny_race_commits_one_decision(approval_world, trial, evidence):
    world = approval_world
    request = pending(world)
    path = f"/v1/approvals/{request['approval_id']}/decisions"
    barrier = Barrier(2)
    payloads = [decision_body(world, "approve"), decision_body(world, "deny")]

    def send(index):
        barrier.wait(5)
        return world.post(path, payloads[index], key=f"race-{index}")

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(send, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = 0 if responses[0].status_code == 200 else 1
    assert (
        world.post(path, payloads[winner], key=f"race-{winner}").json() == responses[winner].json()
    )
    with world.database.transaction() as s:
        votes = list(
            s.scalars(select(ApprovalDecision).where(ApprovalDecision.run_id == world.run_id))
        )
    assert len(votes) == 1 and votes[0].decision == payloads[winner]["decision"]
    assert len([j for j in jobs(world) if j.kind == "IMPLEMENT"]) == (1 if winner == 0 else 0)
    evidence(
        "20 concurrent approve/deny trials have one decision, one or zero implementation intents",
        {
            "trial": trial,
            "statuses": [r.status_code for r in responses],
            "winner": votes[0].decision,
        },
    )


@pytest.mark.ac("SF-14-AC04")
@pytest.mark.parametrize(
    "change,code",
    [
        ("plan", "APPROVAL_PLAN_STALE"),
        ("spec", "APPROVAL_SPEC_STALE"),
        ("policy", "APPROVAL_POLICY_STALE"),
        ("profile", "APPROVAL_PROFILE_STALE"),
        ("base", "APPROVAL_BASE_STALE"),
        ("revision", "APPROVAL_RUN_REVISION_STALE"),
        ("hash", "APPROVAL_TARGET_MISMATCH"),
        ("repository", "APPROVAL_REPOSITORY_REVOKED"),
    ],
)
def test_each_stale_target_is_explained_and_cannot_resume(approval_world, change, code, evidence):
    world = approval_world
    body = decision_body(world)
    if change == "spec":
        spec = copy.deepcopy(world.run()["snapshot"]["task_spec"])
        spec["task"]["objective"] = "Newly revised task objective"
        assert world.post(f"/v1/tasks/{world.task_id}/spec-versions", spec).status_code == 201
    else:
        with world.database.transaction() as s:
            run = s.get(Run, world.run_id)
            if change == "plan":
                old = s.scalar(select(PlanVersion).where(PlanVersion.run_id == run.id))
                revised = copy.deepcopy(old.body)
                revised["assumptions"] = ["new plan revision"]
                s.add(
                    PlanVersion(
                        workspace_id=world.workspace_id,
                        run_id=run.id,
                        version=2,
                        body=revised,
                        body_hash=digest(revised),
                        validation_error=None,
                    )
                )
            elif change == "policy":
                project = s.get(Project, world.project_id)
                project.policy = project.policy | {"job_reservation_microusd": 200000}
            elif change == "profile":
                run.snapshot = run.snapshot | {"verification_profile_hash": "0" * 64}
            elif change == "base":
                run.base_sha = "b" * 40
            elif change == "revision":
                event(s, run, "OTHER_INTERVENTION", {"fixture": True})
            elif change == "repository":
                from software_factory.models import Repository

                s.get(Repository, world.repository_id).active = False
    if change == "hash":
        body["target_hash"] = "0" * 64
    before = dump(world.database)
    reply = decide(world, body=body)
    assert reply.status_code == 409 and reply.json()["code"] == code, reply.text
    assert dump(world.database) == before
    assert not [j for j in jobs(world) if j.kind == "IMPLEMENT"]
    evidence(
        "stale target element produces a specific authorized error and no resumption",
        {"change": change, "status": reply.status_code, "code": reply.json()["code"]},
    )


@pytest.mark.ac("SF-14-AC04")
def test_replanning_invalidates_old_approval_and_only_new_target_can_be_adopted(
    approval_world, evidence
):
    world = approval_world
    old = pending(world)
    oldbody = decision_body(world)
    snapshot = world.run()["snapshot"]
    result = world.post(
        f"/v1/runs/{world.run_id}/replan",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "clarify implementation order",
        },
    )
    assert result.status_code == 202 and world.run()["phase"] == "PLAN"
    assert world.post(f"/v1/approvals/{old['approval_id']}/decisions", oldbody).status_code == 409
    assigned = world.claim().json()
    assert submit(world, assigned).status_code == 200
    current = pending(world)
    assert current["approval_id"] != old["approval_id"] and current["plan_id"] != old["plan_id"]
    assert decide(world).status_code == 200
    assert world.run()["snapshot"] == snapshot
    assert len(world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]) == 2
    evidence(
        "Plan version 2 gets a fresh target; stale approval cannot authorize it",
        {"old_approval": old["approval_id"], "new_approval": current["approval_id"]},
    )


@pytest.mark.ac("SF-14-AC02", "SF-14-AC03", "C-04")
@pytest.mark.parametrize("point", ["approval.created", "approval.decided"])
def test_approval_request_and_decision_are_atomic(world, point, evidence):
    if point == "approval.decided":
        world = make_approval_world(world)

        def operation():
            return decide(world, key="atomic-decision")
    else:
        world.post(
            f"/v1/runs/{world.run_id}/cancel",
            {"expected_run_revision": world.run()["state_revision"], "reason": "fixture"},
        )
        with world.database.transaction() as s:
            project = s.get(Project, world.project_id)
            project.policy = project.policy | {"require_plan_approval": True}
        world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()[
            "run_id"
        ]
        assigned = world.claim().json()

        def operation():
            return submit(world, assigned, key="atomic-plan")

    before = dump(world.database)
    service = world.client.app.state.service

    def fail(name):
        if name == point:
            raise RuntimeError("injected approval failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            operation()
    finally:
        service.checkpoint = lambda name: None
    assert dump(world.database) == before
    assert operation().status_code == 200
    evidence(
        "approval/decision, state, Job, fees, audit, Outbox and replay row roll back together",
        {"checkpoint": point, "rollback_equal": True},
    )


@pytest.mark.ac("SF-14-AC02", "SF-14-AC04")
def test_reconcile_expires_pending_approval_without_resetting_deadline(
    approval_world, clock, evidence
):
    world = approval_world
    request = pending(world)
    clock.value = request["expires_at"]
    Service(world.database).reconcile()
    assert pending(world)["status"] == "EXPIRED"
    assert pending(world)["expires_at"] == request["expires_at"]
    assert world.run()["stop_reason"] == "PLAN_APPROVAL_EXPIRED"
    assert decide(world).status_code == 409 and world.run()["reserved_microusd"] == 0
    evidence(
        "durable request expires at equality and does not obtain a lease/reservation",
        pending(world),
    )


@pytest.mark.ac("SF-14-AC06")
@pytest.mark.parametrize("finish", ["complete", "expire"])
def test_scope_change_stops_active_execution_before_blocking_for_new_spec(world, finish, evidence):
    assigned = world.claim().json()
    snapshot = world.run()["snapshot"]
    task = copy.deepcopy(snapshot["task_spec"]["task"])
    task["objective"] = "A different requested objective"
    result = world.post(
        f"/v1/runs/{world.run_id}/replan",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "the required goal changed",
            "requested_task": task,
        },
    )
    assert result.status_code == 202 and world.run()["status"] == "CANCELLING"
    heartbeat = world.post(
        f"/v1/worker/leases/{assigned['lease']['id']}/heartbeat",
        {"fence": assigned["lease"]["fence"]},
        token=world.worker_token,
    )
    assert heartbeat.json()["cancel_requested"] is True
    if finish == "complete":
        assert submit(world, assigned).status_code == 200
    else:
        from software_factory.models import Attempt

        with world.database.transaction() as s:
            attempt = s.scalar(select(Attempt).where(Attempt.lease_id == assigned["lease"]["id"]))
            attempt.expires_at = db_now(s) - 1
        Service(world.database).reconcile()
    assert (world.run()["status"], world.run()["stop_reason"]) == (
        "BLOCKED",
        "SPEC_CHANGE_REQUIRED",
    )
    assert world.run()["snapshot"] == snapshot and not [
        j for j in jobs(world) if j.kind == "IMPLEMENT"
    ]
    assert (
        world.post(
            f"/v1/runs/{world.run_id}/replan",
            {"expected_run_revision": world.run()["state_revision"], "reason": "retry old run"},
        ).status_code
        == 409
    )
    evidence(
        "active old-scope work is cancelled before durable SPEC_CHANGE_REQUIRED; snapshot is fixed",
        {"finish": finish, "run": world.run()},
    )


@pytest.mark.ac("SF-14-AC03", "C-02")
def test_approval_and_replan_apis_enforce_role_workspace_revocation_and_key(
    approval_world, evidence
):
    world = approval_world
    request = pending(world)
    body = decision_body(world)
    replan = {"expected_run_revision": world.run()["state_revision"], "reason": "clarify same goal"}
    routes = [
        ("GET", f"/v1/runs/{world.run_id}/approvals", None),
        ("GET", f"/v1/approvals/{request['approval_id']}", None),
        ("POST", f"/v1/approvals/{request['approval_id']}/decisions", body),
        ("POST", f"/v1/runs/{world.run_id}/replan", replan),
    ]
    before = dump(world.database)
    for method, path, payload in routes:
        for token, status in (
            (None, 401),
            (world.worker_token, 403),
            (world.other_user_token, 404),
        ):
            headers = {"Idempotency-Key": str(uuid4())}
            if token:
                headers["Authorization"] = "Bearer " + token
            reply = world.client.request(method, path, json=payload, headers=headers)
            assert reply.status_code == status, (path, reply.text)
        if method == "POST":
            assert (
                world.client.post(
                    path, json=payload, headers={"Authorization": "Bearer " + world.user_token}
                ).status_code
                == 422
            )
    assert dump(world.database) == before
    for _method, path, _payload in routes[:2]:
        assert world.get(path).status_code == 200
    with world.database.transaction() as s:
        s.scalar(
            select(Principal).where(Principal.token_hash == token_hash(world.user_token))
        ).active = False
    for method, path, payload in routes:
        assert (
            world.client.request(
                method,
                path,
                json=payload,
                headers={
                    "Authorization": "Bearer " + world.user_token,
                    "Idempotency-Key": str(uuid4()),
                },
            ).status_code
            == 401
        )
    with world.database.transaction() as s:
        assert s.scalar(select(func.count()).select_from(ApprovalDecision)) == 0
    evidence(
        "four routes enforce auth/role/workspace/revocation and mandatory write keys",
        {"routes": 4, "negative_database_unchanged": True, "decisions": 0},
    )
