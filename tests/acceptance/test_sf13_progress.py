import base64
import copy
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.models import Candidate, Gate, Job, Run, Worker
from software_factory.repair import compare_failures, failure_set
from tests.test_assembler import runner as runner

from .test_sf10 import pipeline as pipeline
from .test_sf11 import Reviewer, stage
from .test_sf11 import source_bundle as source_bundle
from .test_sf12_integration import begin_ci, pass_local, rows
from .test_sf13 import Defects, assemble_repair, initial_failure, repair, review, value_patch


@pytest.mark.ac("SF-13-AC04")
def test_fingerprint_excludes_ids_locations_and_prose_but_preserves_failure_identity(evidence):
    check = {
        "id": "check-1",
        "status": "FAIL",
        "eligible": True,
        "kind": "VERIFY",
        "key": "unit_tests",
        "exit_code": 1,
        "metadata_body": {"failed_test_ids": ["checks.test_golden::test_expected_value"]},
    }
    finding = {
        "id": "finding-1",
        "eligible": True,
        "severity": "high",
        "criterion_id": "AC-1",
        "stable_code": "MISSING_EMPTY_STATE",
        "location": {"path": "src/example.py", "symbol": "value", "start_line": 1, "end_line": 1},
        "description": "old prose",
    }
    before = failure_set([check], [finding], ["check-1", "finding-1"])
    changed = copy.deepcopy(finding)
    changed |= {"id": "finding-new", "description": "entirely different prose"}
    changed["location"] |= {"start_line": 400, "end_line": 401}
    after = failure_set([check], [changed], ["check-1", "finding-new"])
    assert before == after and compare_failures(before, after) == "NO_PROGRESS"
    for status in ("ERROR", "INCONCLUSIVE"):
        assert failure_set([check | {"status": status}], [], ["check-1"]) is None
    assert failure_set([check | {"eligible": False}], [], ["check-1"]) is None
    evidence(
        "stable key/test/code/severity/path/symbol; ID/line/prose are excluded",
        {"before": before, "after": after, "comparison": "NO_PROGRESS"},
    )


@pytest.mark.ac("SF-13-AC04")
def test_fewer_failures_with_a_new_failure_still_stops(pipeline, evidence):
    initial_failure(pipeline)
    repair(pipeline, value_patch(3, 2))
    assemble_repair(pipeline)
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert review(pipeline, Defects(("A", "C")))[0]["accepted"]
    run = pipeline.world.run()
    assert run["stop_reason"] == "NO_PROGRESS"
    current = pipeline.world.get(
        f"/v1/candidates/{run['current_candidate_id']}/verification"
    ).json()
    progress = current["gates"][0]["metadata_body"]["repair_progress"]
    assert len(progress["after"]) < len(progress["before"])
    assert progress["status"] == "NO_PROGRESS"
    evidence("a lower count with a new normalized failure is not a strict subset", progress)


@pytest.mark.ac("SF-13-AC04")
def test_incomparable_new_evidence_blocks_even_when_agent_claims_progress(pipeline, evidence):
    initial_failure(pipeline)
    repair(pipeline, value_patch(3, 4))
    assemble_repair(pipeline)
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert stage(pipeline, "REVIEW")[0]["accepted"]
    run = pipeline.world.run()
    assert run["status"] == "BLOCKED" and run["stop_reason"] == "REPAIR_COMPARISON_INCONCLUSIVE"
    assert len([j for j in rows(pipeline, Job) if j.kind == "REPAIR"]) == 1
    evidence(
        "incomplete independent review makes progress incomparable despite an Agent claim", run
    )


@pytest.mark.ac("SF-13-AC03")
def test_alternate_commit_message_with_identical_actual_source_cannot_promote(pipeline, evidence):
    cid, _ = initial_failure(pipeline)
    previous = pipeline.world.get(f"/v1/candidates/{cid}").json()
    repair(pipeline, value_patch(3, 3))
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).capabilities = [
            "ASSEMBLE_CANDIDATE",
            "adapter:fake",
        ]
    assigned = pipeline.managed.claim(token=pipeline.managed.worker_token).json()
    path = f"/v1/worker/leases/{assigned['lease']['id']}"
    fence = assigned["lease"]["fence"]
    body = pipeline.world.get(f"/v1/artifacts/{previous['source_artifact_id']}/content").content
    artifact = pipeline.client.post(
        path + "/artifacts",
        json={
            "fence": fence,
            "kind": "SOURCE",
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
        headers={"Idempotency-Key": str(uuid4())},
    ).json()["id"]
    assert (
        pipeline.client.put(
            path + f"/artifacts/{artifact}/content",
            params={"fence": fence},
            content=body,
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 200
    )
    assert (
        pipeline.client.post(
            path + f"/artifacts/{artifact}/finalize",
            json={"fence": fence},
            headers={"Idempotency-Key": str(uuid4())},
        ).status_code
        == 200
    )
    commit = (
        f"tree {previous['tree_sha']}\nparent {previous['head_sha']}\n"
        "author Factory <factory@localhost> 946684800 +0000\n"
        "committer Factory <factory@localhost> 946684800 +0000\n\nDifferent message only\n"
    ).encode()
    head = hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
    report = {
        key: previous[key]
        for key in (
            "base_sha",
            "tree_sha",
            "source_digest",
            "spec_hash",
            "profile_hash",
            "image_digest",
        )
    }
    report |= {
        "head_sha": head,
        "parent_sha": previous["head_sha"],
        "source_artifact_id": artifact,
        "patch_artifact_id": assigned["job"]["input"]["patch_artifact_id"],
        "commit_object": base64.b64encode(commit).decode(),
        "file_manifest": previous["metadata_body"]["file_manifest"],
        "changed_paths": ["src/example.py"],
        "isolation": previous["metadata_body"]["isolation"],
    }
    response = pipeline.client.post(
        path + "/complete",
        json={"fence": fence, "outcome": "EXECUTED", "result": report, "artifact_ids": [artifact]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    assert pipeline.world.run()["stop_reason"] == "NO_CODE_CHANGE"
    assert (
        pipeline.world.run()["current_candidate_id"] == cid and len(rows(pipeline, Candidate)) == 1
    )
    assert pipeline.world.get(f"/v1/candidates/{cid}").json() == previous
    evidence(
        "different valid commit object/head with byte-identical source is blocked before promotion",
        {
            "old_head": previous["head_sha"],
            "submitted_head": head,
            "source_digest": previous["source_digest"],
            "reason": "NO_CODE_CHANGE",
        },
    )


def synthetic_ci(context, failed, generation):
    target = context["target"]
    return [
        {
            "id": f"ci-{generation}-{c['check_name']}",
            **c,
            "origin_verified": True,
            "status": "FAIL" if c["check_name"] in failed else "PASS",
            "run_sequence": generation,
            "head_sha": target["head_sha"],
            "base_sha": target["base_sha"],
            "tested_sha": target["head_sha"],
            "merge_parents": [],
        }
        for c in context["required_ci"]
    ]


@pytest.mark.ac("SF-13-AC01", "SF-13-AC04")
@pytest.mark.parametrize("source_bundle", ["ci_three"], indirect=True)
def test_final_failures_obey_the_same_two_round_limit(pipeline, evidence):
    local = pass_local(pipeline)
    service = pipeline.world.client.app.state.service
    failed = ["factory-tests", "factory-lint", "factory-format"]
    for generation in (1, 2, 3):
        context = begin_ci(pipeline, local)
        with pipeline.world.database.transaction() as s:
            final = service.gates.final(
                s, s.get(Run, pipeline.world.run_id), synthetic_ci(context, failed, generation)
            )
        assert final.status == "FAIL"
        if generation == 3:
            break
        assert pipeline.world.run()["phase"] == "REPAIR"
        old = "2" if generation == 1 else "2 # fix-1"
        repair(pipeline, value_patch(old, f"2 # fix-{generation}"))
        assemble_repair(pipeline)
        assert stage(pipeline, "VERIFY")[0]["accepted"]
        assert review(pipeline, Reviewer())[0]["accepted"]
        current = pipeline.world.run()["current_candidate_id"]
        with pipeline.world.database.transaction() as s:
            local = s.scalar(
                select(Gate).where(Gate.candidate_id == current, Gate.stage == "LOCAL")
            )
        assert local.metadata_body["repair_progress"]["status"] == "PENDING_FINAL_VALIDATION"
        failed = failed[1:]
    run = pipeline.world.run()
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "REPAIR_LIMIT_REACHED")
    repairs = [j for j in rows(pipeline, Job) if j.kind == "REPAIR"]
    assert len(repairs) == 2 and all(j.payload["repair_stage"] == "FINAL" for j in repairs)
    evidence(
        "Final FAIL uses the same fixed input/strict progress/two-round cap as Local FAIL",
        {"rounds": [j.payload["repair_round"] for j in repairs], "run": run},
    )


@pytest.mark.ac("SF-13-AC06")
@pytest.mark.parametrize("source_bundle", ["ci"], indirect=True)
def test_final_repair_progress_is_decided_after_new_final_evidence(pipeline, evidence):
    local = pass_local(pipeline)
    service = pipeline.world.client.app.state.service
    context = begin_ci(pipeline, local)
    with pipeline.world.database.transaction() as s:
        service.gates.final(
            s, s.get(Run, pipeline.world.run_id), synthetic_ci(context, ["factory-tests"], 1)
        )
    repair(pipeline, value_patch("2", "2 # corrected"))
    assemble_repair(pipeline)
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert review(pipeline, Reviewer())[0]["accepted"]
    with pipeline.world.database.transaction() as s:
        local = s.scalar(
            select(Gate).where(
                Gate.candidate_id == pipeline.world.run()["current_candidate_id"],
                Gate.stage == "LOCAL",
            )
        )
    assert local.metadata_body["repair_progress"]["status"] == "PENDING_FINAL_VALIDATION"
    context = begin_ci(pipeline, local)
    with pipeline.world.database.transaction() as s:
        final = service.gates.final(
            s, s.get(Run, pipeline.world.run_id), synthetic_ci(context, [], 2)
        )
    assert final.status == "PASS" and final.metadata_body["repair_progress"]["status"] == "PROGRESS"
    assert final.metadata_body["failure_set"] == []
    assert pipeline.world.run()["phase"] == "FINAL_GATE_READY"
    evidence(
        "Local PASS defers Final-origin progress; only new matching Final evidence clears B",
        {
            "local_progress": local.metadata_body["repair_progress"],
            "final_progress": final.metadata_body["repair_progress"],
        },
    )
