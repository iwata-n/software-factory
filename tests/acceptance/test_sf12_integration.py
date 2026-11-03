from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from software_factory.models import Attempt, AuditEvent, Gate, Job, OutboxEvent, Principal, Run
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.service import DomainError, Service, token_hash
from tests.test_assembler import PATCH
from tests.test_assembler import runner as runner

from .test_sf10 import pipeline as pipeline
from .test_sf11 import Reviewer, stage
from .test_sf11 import source_bundle as source_bundle
from .test_sf11_boundaries import deliver, prepared


def rows(pipeline, model):
    with pipeline.world.database.transaction() as s:
        return list(s.scalars(select(model).where(model.run_id == pipeline.world.run_id)))


def pass_local(pipeline):
    pipeline.prepare()
    pipeline.assemble()
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert stage(
        pipeline,
        "REVIEW",
        PipelineAdapter(pipeline.runner, reviewer=Reviewer()),
    )[0]["accepted"]
    (gate,) = rows(pipeline, Gate)
    assert gate.status == "PASS"
    return gate


@pytest.mark.ac("SF-12-AC01", "SF-12-AC03", "SF-12-AC04", "C-03", "C-04")
@pytest.mark.parametrize("trial", range(20))
def test_arrival_orders_completion_and_reconcile_create_one_gate_and_next_job(
    pipeline, trial, evidence
):
    pipeline.prepare()
    pipeline.assemble()
    order = ["VERIFY", "REVIEW"] if trial % 2 == 0 else ["REVIEW", "VERIFY"]
    first = prepared(pipeline, order[0], Reviewer())
    assert deliver(pipeline, first).status_code == 200
    assert rows(pipeline, Gate) == [] and pipeline.world.run()["reserved_microusd"] == 0
    last = prepared(pipeline, order[1], Reviewer())
    barrier = Barrier(3)

    def complete():
        barrier.wait(5)
        return deliver(pipeline, last)

    def reconcile():
        barrier.wait(5)
        return pipeline.world.client.app.state.service.reconcile()

    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(complete), pool.submit(complete), pool.submit(reconcile)]
        responses = [future.result(timeout=15) for future in futures]
    assert responses[0].status_code == responses[1].status_code == 200
    assert responses[0].json() == responses[1].json()
    (gate,) = rows(pipeline, Gate)
    assert gate.status == "PASS" and len(gate.evidence_ids) == 2
    assert len(gate.evidence_digest) == 64 and gate.policy_version == 1
    publications = [j for j in rows(pipeline, Job) if j.kind == "PUBLISH_DRAFT"]
    assert len(publications) == 1 and publications[0].payload["gate_id"] == gate.id
    assert pipeline.world.run()["phase"] == "PUBLISH_DRAFT"
    assert pipeline.world.run()["status"] == "QUEUED"
    assert pipeline.world.run()["reserved_microusd"] == 0
    with pipeline.world.database.transaction() as s:
        assert (
            s.scalar(select(func.count()).select_from(Attempt).where(Attempt.state == "LEASED"))
            == 0
        )
        assert (
            s.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.run_id == pipeline.world.run_id,
                    OutboxEvent.kind == "LOCAL_GATE_DECIDED",
                )
            )
            == 1
        )
    evidence(
        "both orders and 20 completion/reconcile races persist one Gate and publication intent",
        {
            "trial": trial,
            "order": order,
            "gate_id": gate.id,
            "evidence_ids": gate.evidence_ids,
            "digest": gate.evidence_digest,
            "publisher_jobs": 1,
        },
        candidate_id=gate.candidate_id,
    )


@pytest.mark.ac("SF-12-AC02")
def test_real_fail_creates_repair_intent_without_publication(pipeline, evidence):
    pipeline.prepare(PATCH.replace(b"+value = 2", b"+value = 3"))
    pipeline.assemble()
    assert deliver(pipeline, prepared(pipeline, "VERIFY")).status_code == 200
    assert (
        deliver(pipeline, prepared(pipeline, "REVIEW", Reviewer("FAIL", finding=True))).status_code
        == 200
    )
    (gate,) = rows(pipeline, Gate)
    assert gate.status == "FAIL" and pipeline.world.run()["phase"] == "REPAIR"
    (repair,) = [j for j in rows(pipeline, Job) if j.kind == "REPAIR"]
    assert repair.payload["candidate_id"] == gate.candidate_id
    assert repair.payload["repair_round"] == 1 and repair.payload["finding_ids"]
    assert not [j for j in rows(pipeline, Job) if j.kind == "PUBLISH_DRAFT"]
    evidence(
        "confirmed failure creates one same-Candidate Repair intent and no publication",
        {"gate_status": gate.status, "repair_input": repair.payload},
    )


@pytest.mark.ac("SF-20-AC02")
def test_model_review_pass_cannot_override_required_verifier_failure(pipeline, evidence):
    pipeline.prepare(PATCH.replace(b"+value = 2", b"+value = 3"))
    assert pipeline.assemble()["accepted"]
    review = prepared(pipeline, "REVIEW", Reviewer("PASS"))
    assert deliver(pipeline, review).status_code == 200
    assert rows(pipeline, Gate) == []
    assert not [job for job in rows(pipeline, Job) if job.kind == "PUBLISH_DRAFT"]

    assert deliver(pipeline, prepared(pipeline, "VERIFY")).status_code == 200
    (gate,) = rows(pipeline, Gate)
    assert gate.status == "FAIL" and pipeline.world.run()["phase"] == "REPAIR"
    assert not [job for job in rows(pipeline, Job) if job.kind == "PUBLISH_DRAFT"]
    evidence(
        "Review PASS alone cannot produce a Gate; the required verifier failure requests repair",
        {"review": "PASS", "gate": gate.status, "publication_jobs": 0},
        candidate_id=gate.candidate_id,
    )


@pytest.mark.ac("SF-12-AC02")
@pytest.mark.parametrize("source_bundle", ["timeout"], indirect=True)
def test_infrastructure_error_retries_same_job_and_input_until_limit(pipeline, clock, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert deliver(pipeline, prepared(pipeline, "VERIFY")).status_code == 200
    assert deliver(pipeline, prepared(pipeline, "REVIEW", Reviewer())).status_code == 200
    assert rows(pipeline, Gate)[0].status == "ERROR"
    (verifier,) = [j for j in rows(pipeline, Job) if j.kind == "VERIFY"]
    original = verifier.payload
    for delay in (2, 4):
        clock.advance(delay)
        assert stage(pipeline, "VERIFY")[0]["accepted"]
    (verifier,) = [j for j in rows(pipeline, Job) if j.kind == "VERIFY"]
    assert verifier.payload == original and verifier.attempt_no == 3 and verifier.state == "FAILED"
    assert pipeline.world.run()["stop_reason"] == "INFRA_ATTEMPTS_EXHAUSTED"
    with pipeline.world.database.transaction() as s:
        attempts = list(s.scalars(select(Attempt).where(Attempt.job_id == verifier.id)))
    assert len(attempts) == 3 and len({a.input_digest for a in attempts}) == 1
    assert not [j for j in rows(pipeline, Job) if j.kind in {"REPAIR", "PUBLISH_DRAFT"}]
    evidence(
        "ERROR retries the same Job/input, never repairs code, and stops after three attempts",
        {
            "attempts": len(attempts),
            "input_digest": attempts[0].input_digest,
            "run": pipeline.world.run(),
        },
    )


@pytest.mark.ac("SF-12-AC02")
@pytest.mark.parametrize("source_bundle", ["missing"], indirect=True)
def test_inconclusive_blocks_without_repair(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert deliver(pipeline, prepared(pipeline, "VERIFY")).status_code == 200
    assert deliver(pipeline, prepared(pipeline, "REVIEW", Reviewer())).status_code == 200
    (gate,) = rows(pipeline, Gate)
    assert gate.status == "INCONCLUSIVE" and pipeline.world.run()["status"] == "BLOCKED"
    assert not [j for j in rows(pipeline, Job) if j.kind in {"REPAIR", "PUBLISH_DRAFT"}]
    evidence(
        "missing mandatory evidence blocks instead of publishing or spending Repair rounds",
        {"status": gate.status, "reason": pipeline.world.run()["stop_reason"]},
    )


@pytest.mark.ac("SF-12-AC05")
@pytest.mark.parametrize("change", ["candidate", "profile", "policy", "evidence"])
def test_old_gate_never_authorizes_changed_publication_target(pipeline, change, evidence):
    gate = pass_local(pipeline)
    service = pipeline.world.client.app.state.service
    with pipeline.world.database.transaction() as s:
        run = s.get(Run, pipeline.world.run_id)
        service.gates.require_publication(s, run, gate.candidate_id, gate.id, gate.evidence_digest)
        if change == "candidate":
            run.current_candidate_id = None
        elif change == "profile":
            run.snapshot = run.snapshot | {"verification_profile_hash": "0" * 64}
        elif change == "policy":
            run.snapshot = run.snapshot | {"policy_hash": "0" * 64}
    with pytest.raises(DomainError) as denied, pipeline.world.database.transaction() as s:
        service.gates.require_publication(
            s,
            s.get(Run, pipeline.world.run_id),
            gate.candidate_id,
            gate.id,
            "0" * 64 if change == "evidence" else gate.evidence_digest,
        )
    assert denied.value.status == 409
    assert rows(pipeline, Gate)[0].status == "PASS"
    evidence(
        "old Gate remains immutable but cannot authorize changed Candidate/Profile/Policy/evidence",
        {"change": change, "status": denied.value.status, "gate_id": gate.id},
    )


def begin_ci(pipeline, gate):
    service = pipeline.world.client.app.state.service
    draft = {
        "id": "fixture-draft-1",
        "origin_verified": True,
        "draft": True,
        "state": "OPEN",
        **{k: gate.target[k] for k in ("candidate_id", "repository_id", "base_sha", "head_sha")},
        "merge_sha": "3" * 40,
    }
    with pipeline.world.database.transaction() as s:
        run = s.get(Run, pipeline.world.run_id)
        context = service.gates.begin_ci_wait(s, run, gate.id, draft)
        again = service.gates.begin_ci_wait(s, run, gate.id, draft)
        assert context == again
        return context


@pytest.mark.ac("SF-12-AC03")
@pytest.mark.parametrize("source_bundle", ["ci"], indirect=True)
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
def test_ci_wait_deadline_survives_restart_and_holds_no_lease(pipeline, clock, offset, evidence):
    gate = pass_local(pipeline)
    context = begin_ci(pipeline, gate)
    deadline = context["deadline_at"]
    clock.value = deadline + offset
    Service(pipeline.world.database).reconcile()
    run = pipeline.world.run()
    assert run["wait_context"]["deadline_at"] == deadline and run["reserved_microusd"] == 0
    assert pipeline.managed.claim(token=pipeline.managed.worker_token).status_code == 204
    if offset < 0:
        assert (run["status"], run["phase"]) == ("WAITING_EXTERNAL", "WAIT_CI")
        assert len(rows(pipeline, Gate)) == 1
    else:
        assert (run["status"], run["stop_reason"]) == ("BLOCKED", "CI_TIMEOUT")
        assert len(rows(pipeline, Gate)) == 2
    evidence(
        "CI wait survives restart, holds no slot/reservation and expires at equality",
        {"offset": offset, "deadline": deadline, "run": run},
    )


@pytest.mark.ac("SF-12-AC06")
@pytest.mark.parametrize("source_bundle", ["ci"], indirect=True)
@pytest.mark.parametrize("mode", ["head", "merge"])
def test_final_gate_persists_ci_relationship_without_premature_run_success(
    pipeline, mode, evidence
):
    local = pass_local(pipeline)
    context = begin_ci(pipeline, local)
    ci = {
        "id": "fixture-ci-1",
        **context["required_ci"][0],
        "origin_verified": True,
        "status": "PASS",
        "head_sha": local.target["head_sha"],
        "base_sha": local.target["base_sha"],
        "tested_sha": local.target["head_sha"] if mode == "head" else context["draft"]["merge_sha"],
        "merge_parents": []
        if mode == "head"
        else [local.target["base_sha"], local.target["head_sha"]],
    }
    service = pipeline.world.client.app.state.service
    with pipeline.world.database.transaction() as s:
        gate = service.gates.final(s, s.get(Run, pipeline.world.run_id), [ci])
    assert gate.status == "PASS" and gate.stage == "FINAL"
    assert gate.metadata_body["checks"][0] == ci
    assert pipeline.world.run()["phase"] == "FINAL_GATE_READY"
    assert pipeline.world.run()["status"] != "SUCCEEDED"
    evidence(
        "synthetic final contract stores tested SHA/base/head/merge; Run success waits for SF-19",
        {"mode": mode, "gate_id": gate.id, "ci": gate.metadata_body["checks"]},
    )


@pytest.mark.ac("SF-12-AC07", "C-02")
def test_public_apis_cannot_override_gate_and_record_denial(pipeline, evidence):
    gate = pass_local(pipeline)
    candidate = pipeline.world.get(f"/v1/candidates/{gate.candidate_id}").json()
    profile = pipeline.world.run()["snapshot"]["verification_profile"]
    for target in (f"/v1/gates/{gate.id}", f"/v1/candidates/{gate.candidate_id}/verification"):
        assert pipeline.world.client.get(target).status_code == 401
        assert pipeline.world.get(target, token=pipeline.managed.worker_token).status_code == 403
        assert pipeline.world.get(target, token=pipeline.world.other_user_token).status_code == 404
        assert pipeline.world.get(target).status_code == 200
    for method, suffix in (("PUT", ""), ("PATCH", ""), ("DELETE", ""), ("POST", "/override")):
        assert (
            pipeline.world.client.request(method, f"/v1/gates/{gate.id}" + suffix).status_code
            == 401
        )
        for token, expected in (
            (pipeline.world.user_token, 409),
            (pipeline.managed.worker_token, 403),
            (pipeline.world.other_user_token, 404),
        ):
            reply = pipeline.world.client.request(
                method,
                f"/v1/gates/{gate.id}" + suffix,
                headers={"Authorization": "Bearer " + token},
                json={"status": "PASS", "required_checks": []},
            )
            assert reply.status_code == expected
    with pipeline.world.database.transaction() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.operation == "gate.change.denied")
            )
            == 8
        )
    with (
        pytest.raises(IntegrityError, match="EVIDENCE_IMMUTABLE"),
        pipeline.world.database.transaction() as s,
    ):
        s.execute(text("UPDATE gates SET status='FAIL' WHERE id=:id"), {"id": gate.id})
    assert pipeline.world.get(f"/v1/candidates/{gate.candidate_id}").json() == candidate
    assert pipeline.world.run()["snapshot"]["verification_profile"] == profile
    with pipeline.world.database.transaction() as s:
        s.scalar(
            select(Principal).where(Principal.token_hash == token_hash(pipeline.world.user_token))
        ).active = False
    headers = {"Authorization": "Bearer " + pipeline.world.user_token}
    for method, suffix in (
        ("GET", ""),
        ("PUT", ""),
        ("PATCH", ""),
        ("DELETE", ""),
        ("POST", "/override"),
    ):
        assert (
            pipeline.world.client.request(
                method, f"/v1/gates/{gate.id}" + suffix, headers=headers
            ).status_code
            == 401
        )
    assert (
        pipeline.world.client.get(
            f"/v1/candidates/{gate.candidate_id}/verification", headers=headers
        ).status_code
        == 401
    )
    evidence(
        "User/Agent override is denied and audited; Candidate/Profile/Gate remain immutable",
        {
            "gate_id": gate.id,
            "denial_audits": 8,
            "candidate_unchanged": True,
            "profile_unchanged": True,
        },
    )


@pytest.mark.ac("SF-12-AC04", "C-04")
def test_gate_checkpoint_failure_rolls_back_evidence_and_publication_intent(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert deliver(pipeline, prepared(pipeline, "VERIFY")).status_code == 200
    last = prepared(pipeline, "REVIEW", Reviewer())
    from .helpers import dump

    before = dump(pipeline.world.database)
    service = pipeline.world.client.app.state.service

    def fail(point):
        if point == "gate.created":
            raise RuntimeError("injected Gate failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            deliver(pipeline, last)
    finally:
        service.checkpoint = lambda name: None
    assert dump(pipeline.world.database) == before
    assert deliver(pipeline, last).status_code == 200
    assert len(rows(pipeline, Gate)) == 1
    evidence(
        "Gate, evidence, settlement, next Job and Outbox are one transaction",
        {"rollback_equal": True, "gates_after_retry": 1},
    )


@pytest.mark.ac("SF-12-AC02", "SF-12-AC03")
@pytest.mark.parametrize("source_bundle", ["ci"], indirect=True)
@pytest.mark.parametrize("finish", ["timeout", "exhaust"])
def test_ci_error_requests_bounded_same_target_retry_without_resetting_deadline(
    pipeline, clock, finish, evidence
):
    local = pass_local(pipeline)
    context = begin_ci(pipeline, local)
    deadline = context["deadline_at"]
    ci = {
        "id": "fixture-ci-error",
        **context["required_ci"][0],
        "origin_verified": True,
        "status": "ERROR",
        "head_sha": local.target["head_sha"],
        "base_sha": local.target["base_sha"],
        "tested_sha": local.target["head_sha"],
        "merge_parents": [],
        "run_sequence": 1,
    }
    service = pipeline.world.client.app.state.service
    with pipeline.world.database.transaction() as s:
        error = service.gates.final(s, s.get(Run, pipeline.world.run_id), [ci])
    assert error.status == "ERROR"
    assert pipeline.world.run()["status"] == "WAITING_EXTERNAL"
    assert pipeline.world.run()["reserved_microusd"] == 0
    assert pipeline.world.run()["wait_context"]["deadline_at"] == deadline
    if finish == "timeout":
        clock.value = deadline
        Service(pipeline.world.database).reconcile()
        assert pipeline.world.run()["stop_reason"] == "CI_TIMEOUT"
    else:
        for sequence, delay in ((2, 2), (3, 4)):
            clock.advance(delay)
            ci = ci | {"id": f"fixture-ci-error-{sequence}", "run_sequence": sequence}
            with pipeline.world.database.transaction() as s:
                service.gates.final(s, s.get(Run, pipeline.world.run_id), [ci])
        assert pipeline.world.run()["stop_reason"] == "CI_INFRA_ATTEMPTS_EXHAUSTED"
    with pipeline.world.database.transaction() as s:
        requests = list(
            s.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.run_id == pipeline.world.run_id,
                    OutboxEvent.kind == "CI_INFRA_RETRY_REQUESTED",
                )
            )
        )
    assert len(requests) == (1 if finish == "timeout" else 2)
    assert all(r.body["target"] == local.target for r in requests)
    assert pipeline.world.run()["wait_context"]["deadline_at"] == deadline
    assert not [j for j in rows(pipeline, Job) if j.kind == "REPAIR"]
    evidence(
        "CI ERROR retries the same target with a fixed limit and durable deadline",
        {
            "finish": finish,
            "retry_requests": len(requests),
            "deadline": deadline,
            "run": pipeline.world.run(),
            "gate_statuses": [g.status for g in rows(pipeline, Gate)],
        },
    )
