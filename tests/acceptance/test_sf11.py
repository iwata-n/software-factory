import base64
import hashlib

import pytest

from runner import entry
from software_factory.models import Worker
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.worker import Supervisor
from tests.test_assembler import PATCH
from tests.test_assembler import runner as runner

from .helpers import worker_client
from .test_sf10 import pipeline as pipeline

GOLDEN = b"""import os
from pathlib import Path
import pytest
from example import value

def test_expected_value():
    assert value == 2

def test_readonly_candidate():
    with pytest.raises(OSError):
        Path('/candidate/src/example.py').write_text('value = 999')
    with pytest.raises(OSError):
        Path('/candidate/src/example.py').chmod(0o777)

def test_no_credentials():
    assert not {'FACTORY_DATABASE_URL', 'FACTORY_WORKER_TOKEN', 'GITHUB_TOKEN'} & os.environ.keys()
    assert not Path('/var/run/docker.sock').exists()
"""
IDS = [
    "checks.test_golden::" + name
    for name in ("test_expected_value", "test_readonly_candidate", "test_no_credentials")
]


@pytest.fixture
def source_bundle(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "checks").mkdir()
    (tmp_path / "src/example.py").write_bytes(b"value = 1\n")
    (tmp_path / "checks/golden.py").write_bytes(b"# immutable required check\n")
    scenario = getattr(request, "param", "normal")
    golden = GOLDEN
    if scenario == "all_skipped":
        golden += b'\npytestmark = pytest.mark.skip(reason="fixture all skipped")\n'
    (tmp_path / "checks/test_golden.py").write_bytes(golden)
    command = {
        "check_id": "unit_tests",
        "argv": [
            "python",
            "-m",
            "pytest",
            "/candidate/checks/test_golden.py",
            "--rootdir=/candidate",
            "-q",
            "-p",
            "no:cacheprovider",
            "--junitxml=.factory/unit.xml",
        ],
        "expected_test_ids": IDS,
        "timeout_seconds": 30,
        "junit_path": ".factory/unit.xml",
    }
    if scenario in {"missing", "invalid", "zero"}:
        statement = {
            "missing": 'print("no report")',
            "invalid": "from pathlib import Path;Path('.factory/unit.xml').write_text('<broken')",
            "zero": "from pathlib import Path;Path('.factory/unit.xml').write_text('<testsuite/>')",
        }[scenario]
        command["argv"] = ["python", "-c", statement]
    elif scenario == "timeout":
        command["argv"] = ["python", "-c", "import time;time.sleep(5)"]
        command["timeout_seconds"] = 1
    elif scenario == "start_failed":
        command["argv"] = ["/factory-no-such-command"]
    elif scenario == "missing_test":
        command["expected_test_ids"] = IDS + ["checks.test_golden::test_missing"]
    entry.git("init", "-q", ".")
    entry.git("add", ".")
    entry.git("commit", "-qm", "trusted golden fixture")
    sha = entry.git("rev-parse", "HEAD").decode().strip()
    digest, archive, _ = entry.pack(entry.records(sha))
    return {
        "sha": sha,
        "tree": entry.git("rev-parse", sha + "^{tree}").decode().strip(),
        "source_digest": digest,
        "archive": base64.b64encode(archive).decode(),
        "commit_object": base64.b64encode(entry.git("cat-file", "commit", sha)).decode(),
        "protected_manifest": {"checks/test_golden.py": hashlib.sha256(golden).hexdigest()},
        "verification_commands": [command],
        "required_ci_checks": (
            [
                {"provider": "github", "app_id": 123, "check_name": name}
                for name in (
                    ["factory-tests", "factory-lint", "factory-format"]
                    if scenario == "ci_three"
                    else ["factory-tests"]
                )
            ]
            if scenario in {"ci", "ci_three"}
            else []
        ),
    }


def stage(pipeline, kind, adapter=None, lose_response=False):
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).capabilities = [kind, "adapter:fake"]
    client, trace = worker_client(pipeline.managed, lose_completion=lose_response)
    result = Supervisor(
        client, adapter or PipelineAdapter(pipeline.runner), heartbeat_interval=0.1
    ).run_once()
    return result, trace


def checks(pipeline):
    candidate = pipeline.world.run()["current_candidate_id"]
    response = pipeline.world.get(f"/v1/candidates/{candidate}/checks")
    assert response.status_code == 200, response.text
    return response.json()


class Reviewer:
    def __init__(self, status="PASS", finding=False):
        self.status, self.finding = status, finding

    def review(self, context, cancelled):
        assert not cancelled.is_set()
        assert set(context) == {
            "target",
            "task_spec",
            "profile",
            "source_archive",
            "source_artifact_id",
        }
        cid = context["target"]["candidate_id"]
        result = {
            "criteria": [
                {
                    "criterion_id": "AC-1",
                    "status": self.status,
                    "explanation": "Compared src/example.py with the fixed acceptance criterion",
                    "evidence_artifact_ids": [
                        context["source_artifact_id"],
                        "$artifact/review-log",
                    ],
                }
            ]
        }
        if self.finding:
            result["findings"] = [
                {
                    "id": "F-1",
                    "candidate_id": cid,
                    "criterion_id": "AC-1",
                    "severity": "high",
                    "location": {"path": "src/example.py", "start_line": 1, "end_line": 1},
                    "description": "Value does not satisfy the fixed acceptance criterion",
                    "evidence_artifact_ids": [context["source_artifact_id"]],
                }
            ]
        return result


@pytest.mark.ac("SF-11-AC01", "SF-11-AC02")
def test_fresh_verifier_uses_immutable_candidate_and_fixed_command(pipeline, monkeypatch, evidence):
    monkeypatch.setenv("FACTORY_WORKER_TOKEN", "fixture-inherited-secret-canary")
    pipeline.prepare()
    pipeline.assemble()
    candidate = pipeline.world.run()["current_candidate_id"]
    before = pipeline.world.get(f"/v1/candidates/{candidate}").json()
    result, _ = stage(pipeline, "VERIFY")
    assert result["accepted"]
    (item,) = checks(pipeline)
    assert item["status"] == "PASS" and item["eligible"] is True
    assert item["origin"] == "managed_verifier"
    assert item["test_ids"] == IDS and item["counts"]["tests"] == 3
    assert item["exit_code"] == 0 and item["finished_at"] >= item["started_at"]
    assert item["target"]["head_sha"] == before["head_sha"]
    assert item["metadata_body"]["command"] == pipeline.source["verification_commands"][0]
    (mount,) = item["metadata_body"]["isolation"]["mounts"]
    assert mount["Type"] == "volume" and mount["RW"] is False
    assert item["metadata_body"]["isolation"]["network"] == "none"
    assert pipeline.world.get(f"/v1/candidates/{candidate}").json() == before
    assert len(item["artifact_ids"]) == 2
    assert pipeline.world.run()["status"] == "QUEUED"
    evidence(
        "fresh isolated runner uses fixed tests and read-only Candidate without credentials",
        item,
        candidate_id=candidate,
        attempt_id=item["attempt_id"],
    )


@pytest.mark.ac("SF-11-AC02")
def test_exit_one_is_failed_evidence_not_an_infrastructure_completion(pipeline, evidence):
    pipeline.prepare(PATCH.replace(b"+value = 2", b"+value = 3"))
    pipeline.assemble()
    result, _ = stage(pipeline, "VERIFY")
    assert result["accepted"]
    (item,) = checks(pipeline)
    assert item["status"] == "FAIL" and item["exit_code"] == 1
    assert item["counts"]["failed"] == 1 and item["eligible"]
    evidence("EXECUTED/accepted does not mean PASS; real failing test is persisted", item)


@pytest.mark.ac("SF-11-AC03")
@pytest.mark.parametrize(
    "source_bundle,expected,reason",
    [
        ("timeout", "ERROR", "TIMEOUT"),
        ("start_failed", "ERROR", "START_FAILED"),
        ("invalid", "INCONCLUSIVE", "REPORT_INVALID"),
        ("zero", "INCONCLUSIVE", "ZERO_TESTS"),
        ("missing", "INCONCLUSIVE", "REPORT_MISSING"),
        ("all_skipped", "INCONCLUSIVE", "ALL_SKIPPED"),
        ("missing_test", "INCONCLUSIVE", "REQUIRED_TESTS_MISSING"),
    ],
    indirect=["source_bundle"],
)
def test_real_verifier_cannot_false_green(pipeline, expected, reason, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    (item,) = checks(pipeline)
    assert (item["status"], item["metadata_body"]["reason"]) == (expected, reason)
    evidence(
        "real fixed command failures, missing/invalid reports, zero/skip/missing IDs never pass",
        item,
    )


@pytest.mark.ac("SF-11-AC04", "SF-11-AC06")
@pytest.mark.parametrize("status", ["PASS", "FAIL", "INCONCLUSIVE"])
def test_review_criteria_and_concrete_findings(pipeline, status, evidence):
    pipeline.prepare()
    pipeline.assemble()
    adapter = PipelineAdapter(pipeline.runner, reviewer=Reviewer(status, finding=status == "FAIL"))
    assert stage(pipeline, "REVIEW", adapter)[0]["accepted"]
    (item,) = checks(pipeline)
    assert item["status"] == status and item["kind"] == "REVIEW" and item["eligible"]
    assert item["origin"] == "managed_reviewer"
    cid = pipeline.world.run()["current_candidate_id"]
    findings = pipeline.world.get(f"/v1/candidates/{cid}/findings").json()
    assert len(findings) == (1 if status == "FAIL" else 0)
    if findings:
        assert findings[0]["severity"] == "high" and findings[0]["location"]["start_line"] == 1
    evidence(
        "review decisions are per AC; findings have real Candidate/file/line/artifact references",
        {"check": item, "findings": findings},
        candidate_id=cid,
    )


@pytest.mark.ac("SF-11-AC06")
def test_unconfigured_reviewer_produces_inconclusive_criteria(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble()
    assert stage(pipeline, "REVIEW")[0]["accepted"]
    (item,) = checks(pipeline)
    assert item["status"] == "INCONCLUSIVE" and item["artifact_ids"] == []
    evidence("missing review backend and unsupported prose never synthesize a PASS", item)


@pytest.mark.ac("SF-11-AC05")
@pytest.mark.parametrize(
    "field", ["head_sha", "source_digest", "spec_hash", "profile_hash", "image_digest"]
)
def test_mismatched_tuple_is_history_without_eligible_pass(pipeline, field, evidence):
    pipeline.prepare()
    pipeline.assemble()

    class Changed(PipelineAdapter):
        def execute_evidence(self, job, cancelled, transport):
            result, events = super().execute_evidence(job, cancelled, transport)
            target = result.result["target"]
            target[field] = ("sha256:" if field == "image_digest" else "") + "0" * (
                40 if field == "head_sha" else 64
            )
            return result, events

    assert stage(pipeline, "VERIFY", Changed(pipeline.runner))[0]["accepted"]
    (item,) = checks(pipeline)
    assert item["status"] == "PASS" and item["eligible"] is False
    assert item["origin"] == "managed_verifier"
    evidence(
        "mismatched target remains attributed history without eligible mandatory evidence",
        item,
        mismatch_field=field,
    )
