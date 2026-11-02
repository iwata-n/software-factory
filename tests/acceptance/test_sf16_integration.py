import pytest
from sqlalchemy import select

from software_factory.db import db_now
from software_factory.models import Gate, PlanVersion, Run
from tests.test_assembler import runner as runner

from .helpers import dump
from .test_sf10 import pipeline as pipeline
from .test_sf11 import source_bundle as source_bundle
from .test_sf11 import stage
from .test_sf12_integration import begin_ci, pass_local
from .test_sf15 import budget_request
from .test_sf16 import approval_body, intervene, resolution_body


@pytest.mark.ac("SF-16-AC03")
def test_null_plan_candidate_cannot_authorize_a_later_current_candidate(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    world = pipeline.world
    service = world.client.app.state.service
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        candidate = run.current_candidate_id
        run.current_candidate_id = None
        run.status, run.phase = "WAITING_EXTERNAL", "WAIT_PLAN_APPROVAL"
        plan = s.scalar(select(PlanVersion).where(PlanVersion.run_id == run.id))
        request = service.plans.request_approval(s, run, plan)
        request_id = request.id
    request = world.get(f"/v1/approvals/{request_id}").json()
    assert request["target"]["candidate_id"] is None
    with world.database.transaction() as s:
        s.get(Run, world.run_id).current_candidate_id = candidate
    before = dump(world.database)
    body = approval_body(request)
    a = world.post(f"/v1/approvals/{request_id}/decisions", body)
    b = intervene(world, request, body)
    assert a.status_code == b.status_code == 409
    assert a.json() == b.json() == {"code": "APPROVAL_CANDIDATE_STALE"}
    assert dump(world.database) == before
    evidence(
        "neither route can rebind a null Candidate to an actual managed Candidate",
        {"candidate_id": candidate, "bound_candidate": None, "status": 409},
        candidate_id=candidate,
    )


@pytest.mark.ac("SF-16-AC03")
def test_new_gate_evidence_invalidates_budget_approval_in_both_routes(pipeline, evidence):
    gate = pass_local(pipeline)
    world = pipeline.world
    request = budget_request(world).json()["approval"]
    assert request["target"]["evidence_digest"] == gate.evidence_digest
    with world.database.transaction() as s:
        s.add(
            Gate(
                workspace_id=world.workspace_id,
                run_id=world.run_id,
                candidate_id=gate.candidate_id,
                stage="LOCAL",
                status="INCONCLUSIVE",
                reason="MANDATORY_EVIDENCE_MISSING",
                target=gate.target,
                evidence_ids=[],
                evidence_digest="f" * 64,
                policy_version=gate.policy_version,
                metadata_body={"fixture": "later trusted reevaluation"},
                created_at=db_now(s) + 1,
            )
        )
    before = dump(world.database)
    body = approval_body(request)
    a = world.post(f"/v1/approvals/{request['approval_id']}/decisions", body)
    b = intervene(world, request, body)
    assert a.status_code == b.status_code == 409
    assert a.json() == b.json() and dump(world.database) == before
    evidence(
        "a later evidence-set hash invalidates a fixed budget subject through either route",
        {"old_evidence": gate.evidence_digest, "new_evidence": "f" * 64, "status": 409},
        candidate_id=gate.candidate_id,
    )


@pytest.mark.ac("SF-16-AC05")
def test_resolution_recalculates_actual_evidence_and_reuses_publication_intent_once(
    pipeline, evidence
):
    passed = pass_local(pipeline)
    world = pipeline.world
    with world.database.transaction() as s:
        # Contract fixture: a persisted missing-evidence view precedes a resolution
        # against the actual managed verifier/reviewer records still in the DB.
        s.add(
            Gate(
                workspace_id=world.workspace_id,
                run_id=world.run_id,
                candidate_id=passed.candidate_id,
                stage="LOCAL",
                status="INCONCLUSIVE",
                reason="MANDATORY_EVIDENCE_MISSING",
                target=passed.target,
                evidence_ids=[],
                evidence_digest="e" * 64,
                policy_version=passed.policy_version,
                metadata_body={"fixture": "persisted missing-evidence view"},
                created_at=db_now(s) + 1,
            )
        )
        run = s.get(Run, world.run_id)
        run.status, run.phase, run.stop_reason = (
            "BLOCKED",
            "VERIFY_REVIEW",
            "MANDATORY_EVIDENCE_MISSING",
        )
    body = resolution_body(world)
    result = world.post(f"/v1/runs/{world.run_id}/interventions", body, key="recheck-evidence")
    assert result.status_code == 200, result.text
    assert result.json()["reevaluation"] == {"gate_id": passed.id, "status": "PASS"}
    assert world.run()["phase"] == "PUBLISH_DRAFT" and world.run()["status"] == "QUEUED"
    after = dump(world.database)
    assert (
        world.post(f"/v1/runs/{world.run_id}/interventions", body, key="recheck-evidence").json()
        == result.json()
    )
    assert dump(world.database) == after
    assert len([j for j in after["jobs"] if j["kind"] == "PUBLISH_DRAFT"]) == 1
    evidence(
        "resolution recalculates real managed evidence and reuses one matching publication Job",
        {"gate_id": passed.id, "publication_jobs": 1, "fixture": "stale missing-evidence view"},
        candidate_id=passed.candidate_id,
    )


@pytest.mark.ac("SF-16-AC05")
@pytest.mark.parametrize("source_bundle", ["missing"], indirect=True)
def test_resolution_cannot_turn_missing_real_verifier_evidence_into_pass(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert stage(pipeline, "REVIEW", adapter=None)[0]["accepted"]
    world = pipeline.world
    assert world.run()["status"] == "BLOCKED"
    before = dump(world.database)
    response = world.post(f"/v1/runs/{world.run_id}/interventions", resolution_body(world))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "EVIDENCE_NOT_RESOLVED"
    assert dump(world.database) == before
    assert not [j for j in before["jobs"] if j["kind"] == "PUBLISH_DRAFT"]
    evidence(
        "a missing actual JUnit and Reviewer evidence remain inconclusive after human resolution",
        {"status": 409, "reason": world.run()["stop_reason"], "publication_jobs": 0},
    )


@pytest.mark.ac("SF-16-AC05", "SF-16-AC06")
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("source_bundle", ["ci"], indirect=True)
def test_resolving_financial_pause_restores_ci_wait_without_resetting_deadline(
    pipeline, clock, expired, evidence
):
    gate = pass_local(pipeline)
    context = begin_ci(pipeline, gate)
    world = pipeline.world
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.status, run.stop_reason = "BLOCKED", "USAGE_UNKNOWN"
        run.wait_context = {"kind": "COST_BLOCKED", "previous_wait": context}
    clock.value = context["deadline_at"] if expired else context["deadline_at"] - 1
    before = dump(world.database)
    response = world.post(f"/v1/runs/{world.run_id}/interventions", resolution_body(world))
    if expired:
        assert response.status_code == 409 and response.json()["code"] == "CI_WAIT_NOT_RESUMABLE"
        assert dump(world.database) == before
    else:
        assert response.status_code == 200, response.text
        run = world.run()
        assert (run["status"], run["phase"]) == ("WAITING_EXTERNAL", "WAIT_CI")
        assert run["wait_context"]["deadline_at"] == context["deadline_at"]
        assert run["reserved_microusd"] == 0
    evidence(
        "financial resolution restores the original CI wait without extending its deadline",
        {"expired": expired, "status": response.status_code, "deadline": context["deadline_at"]},
    )
