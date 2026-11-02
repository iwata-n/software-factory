import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.adapters import AgentResult, Capabilities, FakeAgent, ProducedArtifact
from software_factory.models import Attempt, Candidate, Job, Worker
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.repair import compare_failures, constraint_digest
from software_factory.worker import Supervisor
from tests.test_assembler import PATCH
from tests.test_assembler import runner as runner

from .helpers import worker_client
from .test_sf10 import pipeline as pipeline
from .test_sf11 import Reviewer, stage
from .test_sf11 import source_bundle as source_bundle
from .test_sf12_integration import rows


def value_patch(old, new):
    return (
        f"--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-value = {old}\n+value = {new}\n"
    ).encode()


class Defects(Reviewer):
    def __init__(self, codes=("A", "B"), line=1, category="CODE"):
        super().__init__("FAIL")
        self.codes, self.line, self.category = codes, line, category

    def review(self, context, cancelled):
        result = super().review(context, cancelled)
        result["findings"] = [
            {
                "id": f"{code}-at-{self.line}",
                "candidate_id": context["target"]["candidate_id"],
                "criterion_id": "AC-1",
                "severity": "high",
                "stable_code": code,
                "category": self.category,
                "location": {
                    "path": "src/example.py",
                    "start_line": self.line,
                    "end_line": self.line,
                    "symbol": "value",
                },
                "description": f"defect {code} at line {self.line}; text can change",
                "evidence_artifact_ids": [context["source_artifact_id"]],
            }
            for code in self.codes
        ]
        return result


def review(pipeline, backend):
    return stage(pipeline, "REVIEW", PipelineAdapter(pipeline.runner, reviewer=backend))


def initial_failure(pipeline, reviewer=None):
    pipeline.prepare(PATCH.replace(b"+value = 2", b"+value = 3"))
    pipeline.assemble()
    verifier, trace = stage(pipeline, "VERIFY")
    assert verifier["accepted"]
    assert review(pipeline, reviewer or Defects())[0]["accepted"]
    assert pipeline.world.run()["phase"] == "REPAIR"
    return pipeline.world.run()["current_candidate_id"], trace


def repair_adapter(patch):
    class RepairAgent(FakeAgent):
        capabilities = Capabilities(kinds=("REPAIR",))

        def start(self, job):
            assert job["input"]["constraints_digest"] == constraint_digest(job["snapshot"])
            assert job["input"]["repair_round"] in (1, 2)
            assert job["input"]["failure_set"]
            self._result = AgentResult(
                "EXECUTED",
                {
                    "progress": "claimed fully repaired; server must decide",
                    "required_checks": [],
                    "allowed_write_paths": ["/"],
                    "constraints_digest": "0" * 64,
                },
                artifacts=(ProducedArtifact("patch", "PATCH", patch),),
            )

    return RepairAgent()


def repair(pipeline, patch):
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).capabilities = ["REPAIR", "adapter:fake"]

    client, trace = worker_client(pipeline.managed)
    result = Supervisor(client, repair_adapter(patch), heartbeat_interval=0.1).run_once()
    assert result["accepted"]
    return trace


def assemble_repair(pipeline):
    result, trace = stage(pipeline, "ASSEMBLE_CANDIDATE")
    assert result["accepted"]
    assert not any(path.endswith("/source") for path in trace["paths"])
    return pipeline.world.run()


@pytest.mark.ac("SF-13-AC01", "SF-13-AC04")
def test_two_real_progressing_rounds_then_no_third_repair(pipeline, evidence):
    first, _ = initial_failure(pipeline)
    before = pipeline.world.get(f"/v1/candidates/{first}").json()
    assert len([j for j in rows(pipeline, Job) if j.kind == "REPAIR"]) == 1
    parents = [first]
    for old, new, codes in ((3, 4, ("A",)), (4, 5, ())):
        repair(pipeline, value_patch(old, new))
        assembled = assemble_repair(pipeline)
        cid = assembled["current_candidate_id"]
        item = pipeline.world.get(f"/v1/candidates/{cid}").json()
        assert item["metadata_body"]["parent_candidate_id"] == parents[-1]
        previous = pipeline.world.get(f"/v1/candidates/{parents[-1]}").json()
        assert item["metadata_body"]["parent_sha"] == previous["head_sha"]
        assert item["base_sha"] == before["base_sha"]
        parents.append(cid)
        assert stage(pipeline, "VERIFY")[0]["accepted"]
        assert review(pipeline, Defects(codes))[0]["accepted"]
    run = pipeline.world.run()
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "REPAIR_LIMIT_REACHED")
    repairs = [j for j in rows(pipeline, Job) if j.kind == "REPAIR"]
    assert len(repairs) == 2 and sorted(j.payload["repair_round"] for j in repairs) == [1, 2]
    assert len(rows(pipeline, Candidate)) == 3
    assert pipeline.world.get(f"/v1/candidates/{first}").json() == before
    assert not [j for j in rows(pipeline, Job) if j.kind == "PUBLISH_DRAFT"]
    evidence(
        "only two Repairs; strict failure reduction does not bypass the round limit",
        {"candidates": parents, "rounds": [j.payload["repair_round"] for j in repairs], "run": run},
    )


@pytest.mark.ac("SF-13-AC02")
def test_repair_infrastructure_retry_preserves_round_candidate_and_constraints(
    pipeline, clock, evidence
):
    cid, _ = initial_failure(pipeline)
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).capabilities = ["REPAIR", "adapter:fake"]
    first = pipeline.managed.claim(token=pipeline.managed.worker_token).json()
    response = pipeline.client.post(
        f"/v1/worker/leases/{first['lease']['id']}/fail",
        json={
            "fence": first["lease"]["fence"],
            "category": "INFRASTRUCTURE",
            "reason": "worker process stopped",
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200
    clock.advance(2)
    second = pipeline.managed.claim(token=pipeline.managed.worker_token).json()
    assert first["job"]["id"] == second["job"]["id"]
    assert first["job"]["input"] == second["job"]["input"]
    assert (
        first["job"]["input"]["candidate_id"] == cid and first["job"]["input"]["repair_round"] == 1
    )
    with pipeline.world.database.transaction() as s:
        attempts = list(s.scalars(select(Attempt).where(Attempt.job_id == first["job"]["id"])))
    assert len(attempts) == 2 and len({a.input_digest for a in attempts}) == 1
    assert len([j for j in rows(pipeline, Job) if j.kind == "REPAIR"]) == 1
    evidence(
        "worker failure creates another Attempt of the identical Repair Job/round/input",
        {
            "job_id": first["job"]["id"],
            "candidate_id": cid,
            "round": 1,
            "constraints_digest": first["job"]["input"]["constraints_digest"],
            "input_digest": attempts[0].input_digest,
        },
    )


@pytest.mark.ac("SF-13-AC03")
@pytest.mark.parametrize("form", ["unified", "git"])
def test_same_normalized_code_never_promotes_candidate(pipeline, form, evidence):
    cid, _ = initial_failure(pipeline)
    before = pipeline.world.get(f"/v1/candidates/{cid}").json()
    patch = value_patch(3, 3)
    if form == "git":
        patch = b"diff --git a/src/example.py b/src/example.py\n" + patch
    repair(pipeline, patch)
    run = assemble_repair(pipeline)
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "NO_CODE_CHANGE")
    assert run["current_candidate_id"] == cid and len(rows(pipeline, Candidate)) == 1
    assert pipeline.world.get(f"/v1/candidates/{cid}").json() == before
    assert not [j for j in rows(pipeline, Job) if j.kind == "PUBLISH_DRAFT"]
    evidence(
        "different patches with identical path/mode/bytes cannot create a new Candidate",
        {
            "form": form,
            "candidate_id": cid,
            "code_digest": before["code_digest"],
            "reason": run["stop_reason"],
        },
    )


@pytest.mark.ac("SF-13-AC04")
@pytest.mark.parametrize(
    "after,expected",
    [
        ([("a",)], "PROGRESS"),
        ([("a",), ("b",)], "NO_PROGRESS"),
        ([("a",), ("c",)], "NO_PROGRESS"),
        (None, "INCONCLUSIVE"),
    ],
)
def test_failure_set_comparison_table(after, expected, evidence):
    before = [("a",), ("b",)]
    result = compare_failures(before, after)
    assert result == expected
    evidence(
        "only strict set reduction is progress; replacement/addition/unknown stops automation",
        {"before": before, "after": after, "expected": expected, "observed": result},
    )


@pytest.mark.ac("SF-13-AC04")
def test_line_and_message_changes_do_not_claim_progress(pipeline, evidence):
    cid, _ = initial_failure(pipeline)
    old = pipeline.world.get(f"/v1/candidates/{cid}").json()
    repair(pipeline, b"--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1,2 @@\n+\n value = 3\n")
    new = assemble_repair(pipeline)["current_candidate_id"]
    assert new != cid
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert review(pipeline, Defects(line=2))[0]["accepted"]
    run = pipeline.world.run()
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "NO_PROGRESS")
    assert len([j for j in rows(pipeline, Job) if j.kind == "REPAIR"]) == 1
    assert pipeline.world.get(f"/v1/candidates/{cid}").json() == old
    evidence(
        "new code digest with only shifted locations/prose has the same normalized failure set",
        {"old_candidate": cid, "new_candidate": new, "reason": run["stop_reason"]},
    )


@pytest.mark.ac("SF-13-AC05")
@pytest.mark.parametrize("attack", ["protected", "skip"])
def test_repair_cannot_shrink_failures_by_weakening_required_checks(pipeline, attack, evidence):
    cid, _ = initial_failure(pipeline)
    before = pipeline.world.run()["snapshot"]["verification_profile_hash"]
    if attack == "protected":
        patch = (
            b"--- a/checks/golden.py\n+++ b/checks/golden.py\n@@ -1 +1 @@\n"
            b"-# immutable required check\n+# removed\n"
        )
    else:
        patch = (
            b"--- /dev/null\n+++ b/tests/conftest.py\n@@ -0,0 +1 @@\n"
            b"+def pytest_collection_modifyitems(items): items.clear()\n"
        )
    repair(pipeline, patch)
    run = assemble_repair(pipeline)
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "PROTECTED_VERIFICATION_CHANGE")
    assert run["current_candidate_id"] == cid and len(rows(pipeline, Candidate)) == 1
    assert run["snapshot"]["verification_profile_hash"] == before
    evidence(
        "repair protection runs against the actual diff; required evidence cannot be reduced",
        {"attack": attack, "reason": run["stop_reason"], "profile_hash": before},
    )


@pytest.mark.ac("SF-13-AC05")
def test_explicit_new_spec_requirement_stops_normal_repair(pipeline, evidence):
    pipeline.prepare(PATCH.replace(b"+value = 2", b"+value = 3"))
    pipeline.assemble()
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert review(pipeline, Defects(("SPEC",), category="SPEC_CHANGE_REQUIRED"))[0]["accepted"]
    run = pipeline.world.run()
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "SPEC_CHANGE_REQUIRED")
    assert not [j for j in rows(pipeline, Job) if j.kind == "REPAIR"]
    evidence(
        "a structured need for a new Spec stops the ordinary Repair path for human judgment", run
    )


@pytest.mark.ac("SF-13-AC06")
def test_repair_empty_failure_set_reverifies_new_candidate_and_rejects_old_results(
    pipeline, evidence
):
    first, trace = initial_failure(pipeline)
    before = pipeline.world.get(f"/v1/candidates/{first}").json()
    before_checks = pipeline.world.get(f"/v1/candidates/{first}/checks").json()
    repair(pipeline, value_patch(3, 2))
    second = assemble_repair(pipeline)["current_candidate_id"]
    assert second != first
    assert pipeline.world.get(f"/v1/candidates/{second}/checks").json() == []
    old = trace["completion_requests"][0]
    old_body = json.loads(old["body"])
    old_attempt = old["key"].split(":", 1)[1]
    with pipeline.world.database.transaction() as s:
        attempt = s.get(Attempt, old_attempt)
        path = f"/v1/worker/leases/{attempt.lease_id}/complete"
    assert (
        pipeline.client.post(
            path, json=old_body, headers={"Idempotency-Key": old["key"]}
        ).status_code
        == 200
    )
    assert (
        pipeline.client.post(
            path, json=old_body, headers={"Idempotency-Key": str(uuid4())}
        ).status_code
        == 409
    )
    assert pipeline.world.get(f"/v1/candidates/{second}/checks").json() == []
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert review(pipeline, Reviewer())[0]["accepted"]
    assert pipeline.world.run()["phase"] == "PUBLISH_DRAFT"
    assert pipeline.world.get(f"/v1/candidates/{first}").json() == before
    assert pipeline.world.get(f"/v1/candidates/{first}/checks").json() == before_checks
    current = pipeline.world.get(f"/v1/candidates/{second}/verification").json()
    (gate,) = current["gates"]
    assert (
        gate["status"] == "PASS"
        and gate["metadata_body"]["repair_progress"]["status"] == "PROGRESS"
    )
    assert gate["metadata_body"]["failure_set"] == []
    evidence(
        "new Candidate gets independent evidence; old late/replayed results never satisfy it",
        {"old_candidate": first, "new_candidate": second, "gate": gate},
        candidate_id=second,
    )
