import copy
from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.contracts import digest
from software_factory.models import AuditEvent, PlanVersion, Principal, Project, Run
from software_factory.service import event, token_hash
from tests.test_control_plane import completion, submit

from .helpers import dump
from .test_sf15 import report
from .test_sf16 import approval_body, approval_case, intervene, resolution_body


@pytest.mark.ac("SF-16-AC01", "SF-16-AC02")
@pytest.mark.parametrize("revision", [True, "1", 1.0])
def test_both_approval_routes_reject_coerced_revision_values(world, revision, evidence):
    world, request = approval_case(world, "approve_plan")
    body = approval_body(request) | {"expected_run_revision": revision}
    before = dump(world.database)
    a = world.post(f"/v1/approvals/{request['approval_id']}/decisions", body)
    b = intervene(world, request, body)
    assert a.status_code == b.status_code == 422
    assert dump(world.database) == before
    evidence(
        "revision types cannot select a more permissive approval route",
        {"revision_type": type(revision).__name__, "statuses": [422, 422]},
    )


@pytest.mark.ac("SF-16-AC02", "C-02")
def test_intervention_target_view_is_tenant_user_only(world, evidence):
    path = f"/v1/runs/{world.run_id}/interventions"
    before = dump(world.database)
    assert world.client.get(path).status_code == 401
    assert world.get(path, token=world.worker_token).status_code == 403
    assert world.get(path, token=world.other_user_token).status_code == 404
    assert world.get(path).status_code == 200
    assert dump(world.database) == before
    evidence(
        "intervention subjects and their evidence hashes are visible only to tenant Users",
        {"statuses": [401, 403, 404, 200], "read_side_effects": 0},
    )


@pytest.mark.ac("SF-16-AC02")
@pytest.mark.parametrize(
    "change",
    [
        {"action": None},
        {"subject_id": None},
        {"expected_run_revision": None},
        {"reason": None},
        {"reason": "   "},
        {"action": "force_pass"},
        {"action": "merge"},
        {"action": "deploy"},
        {"expected_run_revision": True},
        {"expected_run_revision": "1"},
        {"extra_instruction": "skip verification"},
        {"decision": "approve"},
        {"reason": "x" * 2001},
    ],
)
def test_intervention_requires_typed_explicit_action_subject_revision_and_reason(
    world, change, evidence
):
    body = {
        "action": "cancel",
        "subject_id": world.run_id,
        "expected_run_revision": world.run()["state_revision"],
        "reason": "stop the evaluation run",
    }
    body.update(change)
    body = {k: v for k, v in body.items() if v is not None}
    before = dump(world.database)
    response = world.post(f"/v1/runs/{world.run_id}/interventions", body)
    assert response.status_code == 422 and dump(world.database) == before
    evidence(
        "missing/unknown/untyped intervention inputs fail before any state change",
        {"fields": list(change), "status": 422, "database_unchanged": True},
    )


@pytest.mark.ac("SF-16-AC02", "C-02")
@pytest.mark.parametrize("action", ["approve_plan", "approve_budget", "cancel", "resolve_blocked"])
def test_every_intervention_action_enforces_auth_role_scope_key_and_revocation(
    world, action, evidence
):
    if action.startswith("approve_"):
        world, request = approval_case(world, action)
        body = approval_body(request) | {"action": action, "subject_id": request["approval_id"]}
    elif action == "resolve_blocked":
        with world.database.transaction() as s:
            run = s.get(Run, world.run_id)
            run.status, run.stop_reason = "BLOCKED", "NO_PROGRESS"
        body = resolution_body(world)
    else:
        body = {
            "action": "cancel",
            "subject_id": world.run_id,
            "expected_run_revision": world.run()["state_revision"],
            "reason": "authorized stop",
        }
    url = f"/v1/runs/{world.run_id}/interventions"
    before = dump(world.database)
    for token, status in [
        (None, 401),
        (world.worker_token, 403),
        (world.other_user_token, 404),
    ]:
        headers = {"Idempotency-Key": str(uuid4())}
        if token:
            headers["Authorization"] = "Bearer " + token
        assert world.client.post(url, json=body, headers=headers).status_code == status
    assert (
        world.client.post(
            url, json=body, headers={"Authorization": "Bearer " + world.user_token}
        ).status_code
        == 422
    )
    assert dump(world.database) == before
    with world.database.transaction() as s:
        s.scalar(
            select(Principal).where(Principal.token_hash == token_hash(world.user_token))
        ).active = False
    assert world.post(url, body).status_code == 401
    evidence(
        "all four actions require an active tenant User and an idempotency key",
        {"action": action, "statuses": [401, 403, 404, 422, 401], "negative_effects": 0},
    )


@pytest.mark.ac("SF-16-AC01", "SF-16-AC03")
@pytest.mark.parametrize("action", ["approve_plan", "approve_budget"])
@pytest.mark.parametrize(
    "change", ["plan", "policy", "profile", "revision", "hash", "spec", "base"]
)
def test_both_routes_reject_each_changed_subject_without_side_effects(
    world, action, change, evidence
):
    world, request = approval_case(world, action)
    body = approval_body(request)
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        if change == "plan":
            old = s.scalar(select(PlanVersion).where(PlanVersion.run_id == run.id))
            revised = copy.deepcopy(old.body) if old else {"units": [], "assumptions": []}
            revised["assumptions"] = ["new planning context"]
            s.add(
                PlanVersion(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    version=2 if old else 1,
                    body=revised,
                    body_hash=digest(revised),
                    validation_error=None,
                )
            )
        elif change == "policy":
            project = s.get(Project, world.project_id)
            project.policy = project.policy | {"job_reservation_microusd": 200000}
        elif change == "profile":
            profile = copy.deepcopy(run.snapshot["verification_profile"])
            profile["required_checks"] += ["new-check"]
            run.snapshot = run.snapshot | {"verification_profile": profile}
        elif change == "revision":
            event(s, run, "OTHER_COMMAND", {})
        elif change == "spec":
            spec = copy.deepcopy(run.snapshot["task_spec"])
            spec["task"]["objective"] += " revised"
            run.snapshot = run.snapshot | {"task_spec": spec}
        elif change == "base":
            run.base_sha = "b" * 40
    if change == "hash":
        body["target_hash"] = "0" * 64
    before = dump(world.database)
    a = world.post(f"/v1/approvals/{request['approval_id']}/decisions", body)
    b = intervene(world, request, body)
    assert a.status_code == b.status_code == 409
    assert a.json() == b.json() and dump(world.database) == before
    evidence(
        "both routes reject changed Plan/Policy/Profile/revision/hash/Spec/base identically",
        {"action": action, "change": change, "code": a.json()["code"], "effects": 0},
    )


@pytest.mark.ac("SF-16-AC04")
@pytest.mark.parametrize("action", ["approve_plan", "approve_budget"])
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
def test_intervention_approval_deadline_is_strict(world, clock, action, offset, evidence):
    world, request = approval_case(world, action)
    clock.value = request["expires_at"] + offset
    response = intervene(world, request, approval_body(request))
    assert response.status_code == (200 if offset < 0 else 409)
    if offset >= 0:
        assert response.json()["code"] == "APPROVAL_EXPIRED"
    evidence(
        "a new decision is rejected at and after the original durable 24h deadline",
        {"action": action, "offset": offset, "status": response.status_code},
    )


@pytest.mark.ac("SF-16-AC06", "C-04")
@pytest.mark.parametrize("action", ["approve_plan", "approve_budget", "cancel", "resolve_blocked"])
def test_intervention_audit_and_state_are_one_atomic_transaction(world, action, evidence):
    request = None
    if action.startswith("approve_"):
        world, request = approval_case(world, action)
        body = approval_body(request) | {"action": action, "subject_id": request["approval_id"]}
        point = "intervention.decided"
    elif action == "cancel":
        body = {
            "action": "cancel",
            "subject_id": world.run_id,
            "expected_run_revision": world.run()["state_revision"],
            "reason": "confirmed stop request",
        }
        point = "intervention.cancelled"
    else:
        assigned = world.claim().json()
        assert (
            submit(
                world, assigned, body=completion(assigned) | {"usage_microusd": None}
            ).status_code
            == 200
        )
        assert report(world, assigned, 20000).status_code == 200
        body = resolution_body(world)
        point = "intervention.resolved"
    before = dump(world.database)
    service = world.client.app.state.service

    def fail(name):
        if name == point:
            raise RuntimeError("injected intervention transaction failure")

    service.checkpoint = fail
    path = f"/v1/runs/{world.run_id}/interventions"
    try:
        with pytest.raises(RuntimeError, match="injected"):
            world.post(path, body, key="atomic-intervention")
    finally:
        service.checkpoint = lambda name: None
    assert dump(world.database) == before
    response = world.post(path, body, key="atomic-intervention")
    assert response.status_code == (202 if action == "cancel" else 200), response.text
    with world.database.transaction() as s:
        audit = s.scalar(
            select(AuditEvent)
            .where(AuditEvent.body["action"].as_string() == action)
            .order_by(AuditEvent.created_at.desc())
        )
    assert audit.actor_id and audit.created_at
    assert audit.body["reason"] == body["reason"] and audit.body["target"]["run_id"] == world.run_id
    assert audit.body["before_revision"] < audit.body["after_revision"]
    evidence(
        "actor/action/target/revisions/reason/time audit commits atomically with each command",
        {"action": action, "point": point, "rollback_equal": True},
    )


@pytest.mark.ac("SF-16-AC01", "SF-16-AC02")
def test_cancellation_cross_route_replay_cannot_be_rebound_to_another_subject(world, evidence):
    body = {
        "expected_run_revision": world.run()["state_revision"],
        "reason": "cancel this run",
    }
    response = world.post(f"/v1/runs/{world.run_id}/cancel", body, key="cancel-shared")
    assert response.status_code == 202
    intervention = body | {"action": "cancel", "subject_id": world.run_id}
    replay = world.post(f"/v1/runs/{world.run_id}/interventions", intervention, key="cancel-shared")
    assert replay.status_code == response.status_code == 202 and replay.json() == response.json()
    before = dump(world.database)
    assert (
        world.post(
            f"/v1/runs/{world.run_id}/interventions",
            intervention | {"subject_id": str(uuid4())},
            key="cancel-shared",
        ).status_code
        == 404
    )
    assert dump(world.database) == before
    evidence(
        "cancel shares replay identity across routes; a mismatched subject cannot use that replay",
        {"identical_result": True, "wrong_subject": 404},
    )
