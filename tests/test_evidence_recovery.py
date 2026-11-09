from dataclasses import replace

import pytest
from sqlalchemy import select

from software_factory.contracts import digest
from software_factory.models import Attempt, Gate, Job, Project, Run
from software_factory.pipeline_worker import PipelineAdapter
from tests.acceptance.helpers import dump
from tests.acceptance.test_sf10 import pipeline as pipeline
from tests.acceptance.test_sf11 import Reviewer, stage
from tests.acceptance.test_sf11 import runner as runner
from tests.acceptance.test_sf11 import source_bundle as source_bundle
from tests.acceptance.test_sf16 import resolution_body


def block_review(pipeline, *, local=False, mismatch=False):
    world = pipeline.world
    if local:
        with world.database.transaction() as session:
            run = session.get(Run, world.run_id)
            project = session.get(Project, world.project_id)
            policy = run.snapshot["policy"] | {"delivery_mode": "local_candidate"}
            project.policy = policy
            run.snapshot = run.snapshot | {"policy": policy, "policy_hash": digest(policy)}
    pipeline.prepare()
    pipeline.assemble()
    assert stage(pipeline, "VERIFY")[0]["accepted"]

    class ReviewAdapter(PipelineAdapter):
        def execute_evidence(self, job, cancelled, transport):
            result, events = super().execute_evidence(job, cancelled, transport)
            if mismatch:
                result = replace(result, result=result.result | {"isolation": {"network": "host"}})
            return result, events

    adapter = ReviewAdapter(pipeline.runner, reviewer=Reviewer("INCONCLUSIVE"))
    assert stage(pipeline, "REVIEW", adapter=adapter)[0]["accepted"]
    assert world.run()["stop_reason"] == (
        "EVIDENCE_TARGET_MISMATCH" if mismatch else "MANDATORY_EVIDENCE_MISSING"
    )
    return world


@pytest.mark.parametrize("local", [False, True])
def test_retry_only_inconclusive_review_then_finish_with_real_evidence(pipeline, local):
    world = block_review(pipeline, local=local)
    candidate = world.run()["current_candidate_id"]
    before = dump(world.database)
    path = f"/v1/runs/{world.run_id}/interventions"
    body = resolution_body(world)
    result = world.post(path, body, key="retry-review")
    assert result.status_code == 200, result.text
    after = dump(world.database)
    review = next(job for job in after["jobs"] if job["kind"] == "REVIEW")
    verify = next(job for job in after["jobs"] if job["kind"] == "VERIFY")
    assert result.json()["reevaluation"] == {"status": "RETRY_SCHEDULED", "job_ids": [review["id"]]}
    assert review["state"] == "RETRY_WAIT" and review["attempt_no"] == 1
    assert verify["state"] == "SUCCEEDED" and verify["attempt_no"] == 1
    assert before["checks"] == after["checks"] and before["gates"] == after["gates"]
    assert len(before["jobs"]) == len(after["jobs"])
    assert world.post(path, body, key="retry-review").json() == result.json()
    assert dump(world.database) == after

    adapter = PipelineAdapter(pipeline.runner, reviewer=Reviewer("PASS"))
    assert stage(pipeline, "REVIEW", adapter=adapter)[0]["accepted"]
    run = world.run()
    assert run["current_candidate_id"] == candidate
    assert run["phase"] == ("LOCAL_COMPLETE" if local else "PUBLISH_DRAFT")
    assert run["status"] == ("SUCCEEDED" if local else "QUEUED")
    with world.database.transaction() as session:
        jobs = session.scalars(select(Job).where(Job.run_id == world.run_id)).all()
        assert next(job for job in jobs if job.kind == "REVIEW").attempt_no == 2
        assert next(job for job in jobs if job.kind == "VERIFY").attempt_no == 1
        gates = session.scalars(select(Gate).where(Gate.run_id == world.run_id)).all()
        assert {gate.status for gate in gates} == {"PASS", "INCONCLUSIVE"}
        assert len([job for job in jobs if job.kind == "PUBLISH_DRAFT"]) == (0 if local else 1)


@pytest.mark.parametrize("cause", ["mismatch", "changed_context", "limit", "budget"])
def test_evidence_recovery_rechecks_real_target_attempt_limit_and_budget(pipeline, cause):
    world = block_review(pipeline, mismatch=cause == "mismatch")
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        review = session.scalar(select(Job).where(Job.run_id == run.id, Job.kind == "REVIEW"))
        if cause == "changed_context":
            project = session.get(Project, world.project_id)
            project.policy = project.policy | {"version": project.policy["version"] + 1}
        elif cause == "limit":
            review.attempt_no = run.snapshot["execution"]["max_infra_attempts_per_job"]
        elif cause == "budget":
            attempt = session.scalar(select(Attempt).where(Attempt.job_id == review.id))
            costs = world.client.app.state.service.costs
            costs.append(
                session,
                run,
                attempt,
                "fixture-spent-budget",
                "SPENT",
                "ACTUAL",
                run.budget_microusd - run.snapshot["policy"]["job_reservation_microusd"] + 1,
                "control_plane",
            )
            costs.refresh(session, run)
    before = dump(world.database)
    response = world.post(f"/v1/runs/{world.run_id}/interventions", resolution_body(world))
    if cause == "mismatch":
        assert response.status_code == 200, response.text
        assert response.json()["reevaluation"]["status"] == "RETRY_SCHEDULED"
        assert world.run()["status"] == "QUEUED"
    else:
        assert response.status_code == 409, response.text
        assert (
            response.json()["code"]
            == {
                "changed_context": "INTERVENTION_CONTEXT_STALE",
                "limit": "INFRA_ATTEMPTS_EXHAUSTED",
                "budget": "BUDGET_NOT_RESOLVED",
            }[cause]
        )
        assert dump(world.database) == before


def test_recovery_rejects_stale_evidence_subject_and_another_workspace(pipeline):
    world = block_review(pipeline)
    body = resolution_body(world)
    path = f"/v1/runs/{world.run_id}/interventions"
    assert world.post(path, body, token=world.other_user_token).status_code == 404
    with world.database.transaction() as session:
        gate = session.scalar(select(Gate).where(Gate.run_id == world.run_id))
        session.add(
            Gate(
                **{
                    column.name: getattr(gate, column.name)
                    for column in Gate.__table__.columns
                    if column.name not in {"id", "evidence_digest", "created_at"}
                },
                evidence_digest="f" * 64,
                created_at=gate.created_at + 1,
            )
        )
    before = dump(world.database)
    response = world.post(path, body)
    assert response.status_code == 409 and response.json()["code"] == "INTERVENTION_TARGET_STALE"
    assert dump(world.database) == before
