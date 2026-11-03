import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from software_factory.models import Check, Gate, Principal, Run, Worker
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.worker import Supervisor
from tests.test_assembler import runner as runner

from .helpers import dump, worker_client
from .test_sf10 import pipeline as pipeline
from .test_sf11 import Reviewer, checks
from .test_sf11 import source_bundle as source_bundle


def prepared(pipeline, kind="VERIFY", reviewer=None, adapter=None):
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).capabilities = [kind, "adapter:fake"]
    downstream, _ = worker_client(pipeline.managed)
    captured = {}

    def intercept(request):
        if request.url.path.endswith("/complete"):
            captured.update(
                path=request.url.path,
                body=json.loads(request.content),
                key=request.headers["Idempotency-Key"],
            )
            return httpx.Response(200, json={"prepared_without_delivery": True})
        return downstream.send(request)

    client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(intercept))
    assert Supervisor(
        client,
        adapter or PipelineAdapter(pipeline.runner, reviewer=reviewer),
        heartbeat_interval=0.1,
    ).run_once() == {"prepared_without_delivery": True}
    assert captured
    return captured


def deliver(pipeline, report, *, body=None, key=None):
    return pipeline.client.post(
        report["path"],
        json=body or report["body"],
        headers={"Idempotency-Key": key or report["key"]},
    )


@pytest.mark.ac("SF-11-AC02", "C-03", "C-04")
@pytest.mark.parametrize("trial", range(20))
def test_concurrent_evidence_completion_commits_once(pipeline, trial, evidence):
    pipeline.prepare()
    pipeline.assemble()
    report = prepared(pipeline)
    barrier = Barrier(2)

    def complete():
        barrier.wait(5)
        return deliver(pipeline, report)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: complete(), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert deliver(pipeline, report).json() == responses[0].json()
    assert deliver(pipeline, report, body=report["body"] | {"usage_microusd": 1}).status_code == 409
    with pipeline.world.database.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Check)) == 1
    evidence(
        "20 concurrent exact replays persist each Check/settlement/event once",
        {"trial": trial, "statuses": [r.status_code for r in responses], "rows": 1},
    )


@pytest.mark.ac("SF-11-AC02", "C-04")
def test_evidence_flush_rollback_and_immutable_history(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    report = prepared(pipeline, "REVIEW", Reviewer("FAIL", finding=True))
    before = dump(pipeline.world.database)
    service = pipeline.world.client.app.state.service

    def fail(point):
        if point == "evidence.created":
            raise RuntimeError("injected evidence transaction failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            deliver(pipeline, report)
    finally:
        service.checkpoint = lambda name: None
    assert dump(pipeline.world.database) == before
    assert deliver(pipeline, report).status_code == 200
    for table in ("checks", "findings"):
        with (
            pytest.raises(IntegrityError, match="EVIDENCE_IMMUTABLE"),
            pipeline.world.database.transaction() as s,
        ):
            s.execute(text(f"DELETE FROM {table}"))
    evidence(
        "Check/Finding, settlement and event roll back together; history is immutable",
        {"rollback_equal": True, "tables_immutable": ["checks", "findings"]},
    )


@pytest.mark.ac("SF-11-AC04")
@pytest.mark.parametrize(
    "fault,expected",
    [
        ("candidate", 409),
        ("criterion", 409),
        ("path", 409),
        ("line", 409),
        ("traversal", 422),
        ("severity", 422),
        ("missing_evidence", 422),
        ("artifact", 404),
    ],
)
def test_invalid_finding_references_reject_entire_completion(pipeline, fault, expected, evidence):
    pipeline.prepare()
    pipeline.assemble()
    report = prepared(pipeline, "REVIEW", Reviewer("FAIL", finding=True))
    body = copy.deepcopy(report["body"])
    finding = body["result"]["findings"][0]
    if fault == "candidate":
        finding["candidate_id"] = str(uuid4())
    elif fault == "criterion":
        finding["criterion_id"] = "UNKNOWN"
    elif fault == "path":
        finding["location"]["path"] = "src/missing.py"
    elif fault == "line":
        finding["location"]["end_line"] = 999
    elif fault == "traversal":
        finding["location"]["path"] = "../escape"
    elif fault == "severity":
        finding["severity"] = "blocking"
    elif fault == "missing_evidence":
        finding["evidence_artifact_ids"] = []
    else:
        finding["evidence_artifact_ids"] = [str(uuid4())]
    before = dump(pipeline.world.database)
    response = deliver(pipeline, report, body=body)
    assert response.status_code == expected, response.text
    assert dump(pipeline.world.database) == before
    evidence(
        "invalid Candidate/criterion/location/severity/evidence cannot create any Check or Finding",
        {"fault": fault, "status": response.status_code, "unchanged": True},
    )


@pytest.mark.ac("SF-11-AC05", "SF-11-AC06", "SF-21-AC07", "C-02")
def test_unmanaged_or_claimed_origin_cannot_promote_results(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert pipeline.world.claim().status_code == 204
    report = prepared(pipeline)
    before = dump(pipeline.world.database)
    body = copy.deepcopy(report["body"])
    body["result"]["origin"] = "managed_verifier"
    assert deliver(pipeline, report, body=body).status_code == 422
    body = copy.deepcopy(report["body"])
    body["result"]["checks"][0]["status"] = "PASS"
    assert deliver(pipeline, report, body=body, key=str(uuid4())).status_code == 422
    body = copy.deepcopy(report["body"])
    body["result"]["runner_options"] = {"privileged": True, "network": "host"}
    assert deliver(pipeline, report, body=body, key=str(uuid4())).status_code == 422
    assert dump(pipeline.world.database) == before
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).managed = False
    assert deliver(pipeline, report, key=str(uuid4())).status_code == 403
    assert checks(pipeline) == []
    with pipeline.world.database.transaction() as session:
        assert session.scalar(select(Gate).where(Gate.run_id == pipeline.world.run_id)) is None
    evidence(
        "Unmanaged and forged verifier reports cannot create Check or Gate evidence",
        {
            "unmanaged_claim": 204,
            "origin_spoof": 422,
            "verdict_spoof": 422,
            "runner_option_spoof": 422,
            "revoked_managed": 403,
            "gate_created": False,
        },
    )


@pytest.mark.ac("SF-11-AC05")
def test_old_candidate_results_remain_ineligible_history(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    report = prepared(pipeline)
    with pipeline.world.database.transaction() as s:
        s.get(Run, pipeline.world.run_id).current_candidate_id = None
    assert deliver(pipeline, report).status_code == 200
    with pipeline.world.database.transaction() as s:
        (item,) = s.scalars(select(Check))
        assert item.status == "PASS" and item.eligible is False
    evidence(
        "retired Candidate evidence stays attributed and cannot meet current mandatory evidence",
        {
            "history_status": item.status,
            "eligible": item.eligible,
            "candidate_id": item.candidate_id,
        },
    )


@pytest.mark.ac("SF-11-AC06")
@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_vague_unreferenced_review_is_inconclusive(pipeline, status, evidence):
    class Vague:
        def review(self, context, cancelled):
            return {
                "criteria": [
                    {
                        "criterion_id": "AC-1",
                        "status": status,
                        "explanation": "looks okay or maybe wrong",
                        "evidence_artifact_ids": [],
                    }
                ]
            }

    pipeline.prepare()
    pipeline.assemble()
    report = prepared(pipeline, "REVIEW", Vague())
    assert deliver(pipeline, report).status_code == 200
    (item,) = checks(pipeline)
    assert item["status"] == "INCONCLUSIVE"
    evidence("unreferenced review prose is not a concrete acceptance verdict", item)


@pytest.mark.ac("SF-11-AC04", "C-02")
def test_evidence_api_scope_role_and_revocation(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    report = prepared(pipeline, "REVIEW", Reviewer("FAIL", finding=True))
    assert deliver(pipeline, report).status_code == 200
    cid = pipeline.world.run()["current_candidate_id"]
    before = dump(pipeline.world.database)
    for suffix in ("checks", "findings"):
        route = f"/v1/candidates/{cid}/{suffix}"
        assert pipeline.world.client.get(route).status_code == 401
        assert pipeline.world.get(route, token=pipeline.managed.worker_token).status_code == 403
        assert pipeline.world.get(route, token=pipeline.world.other_user_token).status_code == 404
        assert pipeline.world.get(route).status_code == 200
    assert dump(pipeline.world.database) == before
    with pipeline.world.database.transaction() as s:
        principal = s.scalar(
            select(Principal).where(
                Principal.role == "USER", Principal.workspace_id == pipeline.world.workspace_id
            )
        )
        principal.active = False
    assert pipeline.world.get(f"/v1/candidates/{cid}/checks").status_code == 401
    evidence(
        "new evidence endpoints are User/workspace scoped and reject revoked tokens",
        {"routes": 2, "unauthorized": 401, "role": 403, "tenant": 404, "revoked": 401},
    )
