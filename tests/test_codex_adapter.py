import base64
import json
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from runner import entry
from software_factory.adapters import AgentExecutionError
from software_factory.codex_adapter import (
    ChangeSet,
    CodexAdapter,
    CodexReviewer,
    archive_files,
    make_patch,
)
from software_factory.codex_engine import CodexError, CodexResponse
from software_factory.contracts import ModelCallUsage, digest

ROOT = Path(__file__).resolve().parents[1]
PLAN = {
    "units": [
        {
            "id": "one",
            "objective": "Return 2",
            "acceptance_ids": ["AC-1"],
            "depends_on": [],
            "proposed_write_paths": ["src/example.py"],
        }
    ],
    "assumptions": [],
    "open_questions": [],
}
CHANGES = {
    "summary": "Return 2",
    "acceptance_ids": ["AC-1"],
    "changes": [{"path": "src/example.py", "content": "value = 2\n"}],
}


class Engine:
    manifest = SimpleNamespace(model="gpt-6.1-sol", version="test")
    manifest_hash = "1" * 64

    def __init__(self, result=None, failure=None):
        self.output, self.failure, self.calls = result, failure, []

    def run(self, context, schema, cancelled, **kwargs):
        self.calls.append(context)
        if self.failure:
            raise self.failure
        value = (
            self.output
            if self.output is not None
            else PLAN
            if context["role"] == "PLAN"
            else CHANGES
        )
        return CodexResponse(
            value,
            ModelCallUsage(
                call_id="codex-test", input_tokens=10, cached_input_tokens=2, output_tokens=3
            ),
            "thread-1",
        )

    def cancel(self):
        pass


@pytest.fixture
def job():
    source_digest, archive, _ = entry.pack(
        [
            ("src/example.py", "100644", b"value = 1\n"),
            ("checks/golden.py", "100644", b"assert True\n"),
        ]
    )
    pricing = json.loads((ROOT / "config/codex_pricing.json").read_text())
    policy = {
        "allowed_write_paths": ["src"],
        "protected_paths": ["checks"],
        "protected_manifest": {},
        "allow_external_model": True,
        "pricing_manifest": pricing,
    }
    return {
        "kind": "PLAN",
        "attempt_id": "attempt-1",
        "base_sha": "a" * 40,
        "execution": {"timeout_seconds": 10},
        "plan": PLAN,
        "snapshot": {
            "agent_adapter": "codex_cli",
            "agent_configuration": {"engine_manifest_hash": "1" * 64},
            "policy": policy,
            "task_spec": {
                "task": {
                    "objective": "Return 2",
                    "acceptance_criteria": [{"id": "AC-1", "description": "Return 2"}],
                }
            },
        },
        "input": {},
        "source": {
            "sha": "a" * 40,
            "source_digest": source_digest,
            "archive": base64.b64encode(archive).decode(),
        },
    }


def execute(engine, job):
    adapter = CodexAdapter(engine)
    adapter.start(job)
    deadline = time.monotonic() + 3
    while adapter.result() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter.result() is not None
    return adapter.result()


def test_plan_uses_only_scoped_source_and_measured_estimated_usage(job):
    engine = Engine()
    result = execute(engine, job)
    assert result.outcome == "EXECUTED" and result.result == PLAN
    assert engine.calls[0]["source_files"] == {"src/example.py": "value = 1\n"}
    assert {"accepted_plan", "repair_failures"}.isdisjoint(engine.calls[0])
    receipt = result.usage_deliveries[0]
    assert receipt["total_microusd"] == 47 and receipt["final"] is True
    assert receipt["calls"][0]["cached_input_tokens"] == 2
    assert receipt["pricing_manifest_hash"] == digest(job["snapshot"]["policy"]["pricing_manifest"])
    assert result.usage_microusd is None


def test_implementation_produces_real_patch_without_executing_generated_code(job):
    job["kind"] = "IMPLEMENT"
    result = execute(Engine(), job)
    patch = next(a.body for a in result.artifacts if a.kind == "PATCH")
    assert b"-value = 1\n+value = 2\n" in patch and result.result["simulation"] is False


@pytest.mark.parametrize("attack", ["policy", "manifest", "pricing", "source_digest", "sensitive"])
def test_denied_or_invalid_context_never_calls_codex(job, attack):
    if attack == "policy":
        job["snapshot"]["policy"]["allow_external_model"] = False
    elif attack == "manifest":
        job["snapshot"]["agent_configuration"]["engine_manifest_hash"] = "2" * 64
    elif attack == "pricing":
        job["snapshot"]["policy"]["pricing_manifest"]["basis"] = "provider_invoice"
    elif attack == "source_digest":
        job["source"]["source_digest"] = "0" * 64
    else:
        job["snapshot"]["task_spec"]["task"]["objective"] = "Bearer secret-canary"
    engine = Engine()
    result = execute(engine, job)
    assert not engine.calls and result.category == "POLICY"
    assert result.usage_deliveries[0]["total_microusd"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        ".git/config",
        "checks/golden.py",
        "src/other.py",
        "src/conftest.py",
        "src/auth/key.py",
    ],
)
def test_model_cannot_expand_policy_or_plan_or_modify_protected_files(job, path):
    job["kind"] = "IMPLEMENT"
    changes = dict(CHANGES, changes=[{"path": path, "content": "bad\n"}])
    result = execute(Engine(changes), job)
    assert result.category == "POLICY" and not result.artifacts
    assert result.usage_deliveries[0]["total_microusd"] == 47


def test_no_code_change_and_incomplete_acceptance_coverage_are_not_success(job):
    job["kind"] = "IMPLEMENT"
    same = dict(CHANGES, changes=[{"path": "src/example.py", "content": "value = 1\n"}])
    result = execute(Engine(same), job)
    assert result.reason == "NO_CODE_CHANGE"
    result = execute(Engine(dict(CHANGES, acceptance_ids=["other"])), job)
    assert result.reason == "MODEL_ACCEPTANCE_COVERAGE"


def test_missing_usage_is_unknown_and_schema_failure_preserves_measured_usage(job):
    missing = execute(Engine(failure=CodexError("CODEX_EVENT_SCHEMA_INVALID")), job)
    assert missing.usage_microusd is None and missing.usage_deliveries == ()
    counts = ModelCallUsage(call_id="one", input_tokens=10, output_tokens=3)
    measured = execute(Engine(failure=CodexError("CODEX_RESULT_SCHEMA_INVALID", usage=counts)), job)
    assert measured.usage_deliveries[0]["total_microusd"] == 50


def test_repair_reads_granted_parent_candidate_instead_of_root_source(job):
    code, archive, _ = entry.pack([("src/example.py", "100644", b"value = 0\n")])
    job["kind"] = "REPAIR"
    job["input"] = {
        "candidate": {"source_digest": code},
        "source_artifact_id": "source-2",
        "failure_set": [["check", "unit", "FAIL_ONE", "high", "src/example.py", "value"]],
    }
    requests = []

    def receive(request):
        requests.append(request.url.path)
        return httpx.Response(200, content=archive)

    failure = job["input"]["failure_set"][0]
    engine = Engine(CHANGES | {"addressed_failure_ids": [digest(failure)]})
    adapter = CodexAdapter(engine)
    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(receive)
    ) as client:
        adapter.bind_transport(client, "/v1/worker/leases/test", 2)
        adapter.start(job)
        deadline = time.monotonic() + 3
        while adapter.result() is None and time.monotonic() < deadline:
            time.sleep(0.01)
    assert adapter.result().outcome == "EXECUTED"
    assert engine.calls[0]["source_files"]["src/example.py"] == "value = 0\n"
    assert engine.calls[0]["repair_failures"] == [{"id": digest(failure), "failure": failure}]
    assert adapter.result().result["addressed_failure_ids"] == [digest(failure)]
    assert requests == ["/v1/worker/leases/test/inputs/source-2/content"]


def test_patch_handles_creation_deletion_and_missing_final_newline(job):
    files = {"src/old.py": ("100644", b"old")}
    policy = job["snapshot"]["policy"]
    plan = {"units": [{"proposed_write_paths": ["src"]}]}
    patch = make_patch(
        ChangeSet.model_validate(
            {
                "summary": "replace",
                "acceptance_ids": ["AC-1"],
                "changes": [
                    {"path": "src/old.py", "content": None},
                    {"path": "src/new.py", "content": "new"},
                ],
            }
        ),
        files,
        policy,
        plan,
        ["AC-1"],
    )
    assert b"/dev/null" in patch and patch.count(b"No newline at end of file") == 2


@pytest.mark.parametrize("invalid", [False, True])
def test_codex_review_binds_evidence_to_current_candidate_and_real_locations(job, invalid):
    job["kind"] = "REVIEW"
    location = {
        "path": "src/example.py",
        "start_line": 1,
        "end_line": 999 if invalid else 1,
        "symbol": "value",
    }
    reviewed = {
        "criteria": [
            {
                "criterion_id": "AC-1",
                "status": "PASS",
                "explanation": "Source assigns 1",
                "locations": [location],
            }
        ],
        "findings": [],
    }
    context = {
        "job": job,
        "source_archive": base64.b64decode(job["source"]["archive"]),
        "source_artifact_id": "current-source",
        "target": {"source_digest": job["source"]["source_digest"], "candidate_id": "candidate-2"},
    }
    if invalid:
        with pytest.raises(AgentExecutionError, match="CODEX_REVIEW_LOCATION_INVALID") as caught:
            CodexReviewer(Engine(reviewed)).for_job(job).review(context, Event())
        assert caught.value.usage_deliveries
    else:
        result = CodexReviewer(Engine(reviewed)).for_job(job).review(context, Event())
        assert result["criteria"][0]["evidence_artifact_ids"] == [
            "current-source",
            "$artifact/review-log",
        ]
        assert result.usage_deliveries[0]["total_microusd"] == 47


def test_archive_digest_is_verified_before_source_can_become_model_input(job):
    with pytest.raises(CodexError, match="MODEL_SOURCE_DIGEST_MISMATCH"):
        archive_files(base64.b64decode(job["source"]["archive"]), "0" * 64)


def test_capabilities_and_events_bind_one_execution_id_to_the_attempt(job):
    engine = Engine()
    adapter = CodexAdapter(engine)
    assert adapter.capabilities.kinds == ("PLAN", "IMPLEMENT", "REPAIR")
    assert not adapter.capabilities.supports_resume
    with pytest.raises(ValueError, match="resume"):
        adapter.start(job | {"resume": "old-thread"})
    with pytest.raises(ValueError, match="role"):
        adapter.start(job | {"kind": "REVIEW"})
    assert not engine.calls and not adapter.events()

    adapter.start(job)
    adapter._thread.join(timeout=3)
    events = adapter.events()
    assert [event["kind"] for event in events] == ["AGENT_STARTED", "AGENT_FINISHED"]
    assert {event["body"]["execution_id"] for event in events} == {job["attempt_id"]}
    assert adapter.result().outcome == "EXECUTED"
    assert not adapter.events()


@pytest.mark.parametrize("claimed", [["unknown"], ["same", "same"]])
def test_repair_rejects_ungrounded_or_duplicate_claims(job, claimed):
    job["kind"] = "REPAIR"
    failure = ["check", "unit", "FAIL_ONE", "high", "src/example.py", "value"]
    job["input"] = {
        "candidate": {"source_digest": job["source"]["source_digest"]},
        "source_artifact_id": "source",
        "failure_set": [failure],
    }
    ids = [digest(failure) if value == "same" else value for value in claimed]
    adapter = CodexAdapter(Engine(CHANGES | {"addressed_failure_ids": ids}))
    archive = base64.b64decode(job["source"]["archive"])
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive)),
    ) as client:
        adapter.bind_transport(client, "/v1/worker/leases/test", 2)
        adapter.start(job)
        adapter._thread.join(timeout=3)
    assert adapter.result().reason == "MODEL_REPAIR_FAILURE_IDS_INVALID"
    assert adapter.result().usage_deliveries
    assert not adapter.result().artifacts


def test_cancellation_before_codex_start_yields_result():
    adapter = CodexAdapter(Engine())
    adapter.cancel()
    assert adapter.result().outcome == "CANCELLED"
    assert adapter.engine.calls == []


def test_codex_signal_failure_is_not_reported_as_cancelled():
    class UnstoppableEngine(Engine):
        def cancel(self):
            raise CodexError("STOP_UNCONFIRMED", "INFRASTRUCTURE")

    adapter = CodexAdapter(UnstoppableEngine())
    adapter.cancel()
    assert adapter.result().outcome == "FAILED"
    assert adapter.result().reason == "STOP_UNCONFIRMED"
