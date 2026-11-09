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
    review_comparison,
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


@pytest.mark.ac("SF-20-AC02")
def test_plan_uses_only_scoped_source_and_measured_estimated_usage(job, evidence):
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
    evidence(
        "PLAN uses only scoped source and produces versioned usage",
        {
            "prompt_keys": sorted(engine.calls[0]),
            "result": result.outcome,
            "amount": receipt["total_microusd"],
        },
    )


@pytest.mark.ac("SF-20-AC02")
def test_implementation_produces_real_patch_without_executing_generated_code(job, evidence):
    job["kind"] = "IMPLEMENT"
    engine = Engine()
    result = execute(engine, job)
    assert engine.calls[0]["accepted_plan"] == PLAN
    assert "repair_failures" not in engine.calls[0]
    assert engine.calls[0]["source_files"] == {"src/example.py": "value = 1\n"}
    patch = next(a.body for a in result.artifacts if a.kind == "PATCH")
    assert b"-value = 1\n+value = 2\n" in patch and result.result["simulation"] is False
    evidence(
        "IMPLEMENT produces a patch from the accepted plan",
        {
            "bytes": len(patch),
            "outcome": result.outcome,
            "files": sorted(engine.calls[0]["source_files"]),
        },
    )


@pytest.mark.parametrize("role", ["PLAN", "IMPLEMENT"])
def test_adapter_reads_project_source_then_returns_plan_or_patch(job, role):
    job["kind"] = role
    job["snapshot"]["policy"].update(allowed_read_paths=["."], allowed_write_paths=["."])

    class ProjectEngine(Engine):
        def run(self, context, schema, cancelled, **kwargs):
            self.calls.append(context)
            if len(self.calls) == 1:
                assert {item["path"] for item in context["project_file_index"]} == {
                    "src/example.py",
                    "checks/golden.py",
                }
                output = {
                    "read_requests": [
                        {"path": "src/example.py", "start_line": 1, "line_count": 10}
                    ],
                    "result": None,
                }
            else:
                assert context["project_file_excerpts"][0]["content"] == "value = 1\n"
                output = {"read_requests": [], "result": PLAN if role == "PLAN" else CHANGES}
            schema.model_validate(output)
            return CodexResponse(
                output,
                ModelCallUsage(
                    call_id=f"call-{len(self.calls)}",
                    input_tokens=10,
                    cached_input_tokens=2,
                    output_tokens=3,
                ),
                "thread",
            )

    result = execute(ProjectEngine(), job)
    assert result.outcome == "EXECUTED"
    receipt = result.usage_deliveries[0]
    assert receipt["total_microusd"] == 94 and len(receipt["calls"]) == 2
    if role == "PLAN":
        assert result.result == PLAN
        assert all(artifact.kind != "PATCH" for artifact in result.artifacts)
    else:
        assert any(artifact.kind == "PATCH" for artifact in result.artifacts)


@pytest.mark.parametrize("attack", ["policy", "manifest", "pricing", "source_digest", "sensitive"])
@pytest.mark.ac("SF-20-AC05")
def test_denied_or_invalid_context_never_calls_codex(job, attack, evidence):
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
    evidence(
        "Denied model context has zero calls and zero estimated spend",
        {"case": attack, "engine_calls": len(engine.calls), "reason": result.reason},
    )


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
@pytest.mark.ac("SF-20-AC02")
def test_model_cannot_expand_policy_or_plan_or_modify_protected_files(job, path, evidence):
    job["kind"] = "IMPLEMENT"
    changes = dict(CHANGES, changes=[{"path": path, "content": "bad\n"}])
    result = execute(Engine(changes), job)
    assert result.category == "POLICY" and not result.artifacts
    assert result.usage_deliveries[0]["total_microusd"] == 47
    evidence(
        "Denied change produces no artifact",
        {"path": path, "category": result.category, "reason": result.reason},
    )


def test_no_code_change_and_incomplete_acceptance_coverage_are_not_success(job):
    job["kind"] = "IMPLEMENT"
    same = dict(CHANGES, changes=[{"path": "src/example.py", "content": "value = 1\n"}])
    result = execute(Engine(same), job)
    assert result.reason == "NO_CODE_CHANGE"
    result = execute(Engine(dict(CHANGES, acceptance_ids=["other"])), job)
    assert result.reason == "MODEL_ACCEPTANCE_COVERAGE"


def test_exact_edits_preserve_large_files_and_existing_code_without_full_regeneration(job):
    original = "token = issue_token()\n" + "# unchanged\n" * 20000 + "value = 1\n"
    changes = ChangeSet(
        summary="Return 2",
        acceptance_ids=["AC-1"],
        changes=[
            {
                "path": "src/example.py",
                "content": None,
                "edits": [{"old_text": "value = 1\n", "new_text": "value = 2\n"}],
            }
        ],
    )
    patch = make_patch(
        changes,
        {"src/example.py": ("100644", original.encode())},
        job["snapshot"]["policy"],
        PLAN,
        ["AC-1"],
    )
    assert b"-value = 1\n+value = 2\n" in patch and len(patch) < 500
    assert original.endswith("value = 1\n")


@pytest.mark.parametrize("source,old", [("same\nsame\n", "same\n"), ("original\n", "missing\n")])
def test_edits_with_missing_or_ambiguous_anchors_never_produce_a_patch(job, source, old):
    changes = ChangeSet(
        summary="Denied",
        acceptance_ids=["AC-1"],
        changes=[
            {
                "path": "src/example.py",
                "content": None,
                "edits": [{"old_text": old, "new_text": "replacement\n"}],
            }
        ],
    )
    with pytest.raises(CodexError, match="MODEL_EDIT_ANCHOR_MISMATCH"):
        make_patch(
            changes,
            {"src/example.py": ("100644", source.encode())},
            job["snapshot"]["policy"],
            PLAN,
            ["AC-1"],
        )


def test_code_parameters_and_interpolation_are_not_mistaken_for_credentials(job):
    code = 'def headers(token: Token):\n    return {"Authorization": f"Bearer {token}"}\n'
    changes = ChangeSet(
        summary="Add helper",
        acceptance_ids=["AC-1"],
        changes=[{"path": "src/example.py", "content": code}],
    )
    assert b"def headers" in make_patch(changes, {}, job["snapshot"]["policy"], PLAN, ["AC-1"])
    denied = ChangeSet(
        summary="Denied",
        acceptance_ids=["AC-1"],
        changes=[{"path": "src/example.py", "content": 'value = "ghp_REAL_CREDENTIAL_CANARY"\n'}],
    )
    with pytest.raises(CodexError, match="MODEL_CHANGE_CONTENT_DENIED"):
        make_patch(denied, {}, job["snapshot"]["policy"], PLAN, ["AC-1"])


def test_project_scope_allows_helpers_for_auto_plans_but_preserves_human_plan_scope(job):
    policy = job["snapshot"]["policy"] | {"allowed_write_paths": ["."]}
    changes = ChangeSet(
        summary="Add helper",
        acceptance_ids=["AC-1"],
        changes=[{"path": "src/helper.py", "content": "value = 1\n"}],
    )
    assert b"+++ b/src/helper.py" in make_patch(changes, {}, policy, PLAN, ["AC-1"])
    with pytest.raises(CodexError, match="MODEL_CHANGE_PATH_DENIED") as failure:
        make_patch(changes, {}, policy | {"require_plan_approval": True}, PLAN, ["AC-1"])
    assert failure.value.details["path"] == "src/helper.py"


def test_missing_usage_is_unknown_and_schema_failure_preserves_measured_usage(job):
    missing = execute(Engine(failure=CodexError("CODEX_EVENT_SCHEMA_INVALID")), job)
    assert missing.usage_microusd is None and missing.usage_deliveries == ()
    counts = ModelCallUsage(call_id="one", input_tokens=10, output_tokens=3)
    measured = execute(Engine(failure=CodexError("CODEX_RESULT_SCHEMA_INVALID", usage=counts)), job)
    assert measured.usage_deliveries[0]["total_microusd"] == 50


@pytest.mark.ac("SF-20-AC02")
def test_repair_reads_granted_parent_candidate_instead_of_root_source(job, evidence):
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
    evidence(
        "REPAIR reads the parent Candidate and returns only known failure IDs",
        {
            "requests": requests,
            "addressed_failure_ids": adapter.result().result["addressed_failure_ids"],
        },
    )


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
@pytest.mark.ac("SF-20-AC02")
def test_codex_review_binds_evidence_to_current_candidate_and_real_locations(
    job, invalid, evidence
):
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
        engine = Engine(reviewed)
        result = CodexReviewer(engine).for_job(job).review(context, Event())
        (prompt,) = engine.calls
        assert prompt["role"] == "REVIEW"
        assert prompt["source_files"] == {"src/example.py": "value = 1\n"}
        assert {"accepted_plan", "repair_failures", "target", "source_artifact_id"}.isdisjoint(
            prompt
        )
        assert result["criteria"][0]["evidence_artifact_ids"] == [
            "current-source",
            "$artifact/review-log",
        ]
        assert result.usage_deliveries[0]["total_microusd"] == 47
    evidence(
        "REVIEW accepts the current source line and rejects an out-of-range location",
        {"line": location["end_line"], "accepted": not invalid},
    )


@pytest.mark.parametrize("tamper", [None, "sha", "digest", "archive", "missing"])
@pytest.mark.parametrize("project_access", [False, True])
def test_review_compares_the_candidate_with_verified_pinned_base(job, tamper, project_access):
    job["kind"] = "REVIEW"
    if project_access:
        job["snapshot"]["policy"]["allowed_read_paths"] = ["src"]
    job["snapshot"]["source"] = {
        "base_sha": job["base_sha"],
        "source_digest": job["source"]["source_digest"],
    }
    files = archive_files(
        base64.b64decode(job["source"]["archive"]), job["source"]["source_digest"]
    )
    records = [
        (path, mode, b"value = 2\n" if path == "src/example.py" else body)
        for path, (mode, body) in files.items()
    ]
    candidate_digest, archive, _ = entry.pack(records)
    base = dict(job["source"])
    if tamper == "sha":
        base["sha"] = "b" * 40
    elif tamper == "digest":
        base["source_digest"] = "0" * 64
    elif tamper == "archive":
        base["archive"] = base64.b64encode(archive).decode()
    reviewed = {
        "criteria": [
            {
                "criterion_id": "AC-1",
                "status": "PASS",
                "explanation": "Compared the change from 1 to 2",
                "locations": [{"path": "src/example.py", "start_line": 1, "end_line": 1}],
            }
        ],
        "findings": [],
    }
    engine = Engine({"read_requests": [], "result": reviewed} if project_access else reviewed)
    context = {
        "source_archive": archive,
        "source_artifact_id": "candidate-source",
        "target": {"source_digest": candidate_digest, "candidate_id": "candidate"},
        "base_source": None if tamper == "missing" else base,
    }
    reviewer = CodexReviewer(engine).for_job(job)
    if tamper:
        with pytest.raises(AgentExecutionError):
            reviewer.review(context, Event())
        assert not engine.calls
    else:
        assert reviewer.review(context, Event())["criteria"][0]["status"] == "PASS"
        comparison = engine.calls[0]["change_comparison"]
        assert comparison["base_sha"] == job["base_sha"]
        assert comparison["changed_paths"] == ["src/example.py"]
        assert "-value = 1\n+value = 2\n" in comparison["unified_diff"]
        assert not comparison["diff_truncated"] and not comparison["omitted_paths"]


def test_review_comparison_respects_read_scope_redacts_and_reports_incomplete_context():
    before = {
        "src/main.py": ("100644", b'api_key = "secret-canary"\nvalue = 1\n'),
        "private/hidden.py": ("100644", b"private_before = True\n"),
        "src/removed.py": ("100644", b"removed = True\n"),
        "src/binary": ("100644", b"\xff"),
    }
    after = before | {
        "src/main.py": ("100644", b'api_key = "another-secret"\nvalue = 2\n'),
        "private/hidden.py": ("100644", b"private_after = True\n"),
        "src/binary": ("100644", b"\xfe"),
        "src/added.py": ("100644", b"added = True\n"),
    }
    del after["src/removed.py"]
    policy = {"allowed_read_paths": ["src"], "allowed_write_paths": ["src"]}
    comparison = review_comparison(before, after, policy, "a" * 40)
    rendered = json.dumps(comparison)
    assert "secret-canary" not in rendered and "another-secret" not in rendered
    assert "[REDACTED]" in rendered and "private" not in rendered
    assert comparison["omitted_paths"] == ["src/binary"]
    assert "+added = True" in comparison["unified_diff"]
    assert "-removed = True" in comparison["unified_diff"]
    limited = review_comparison(before, after, policy, "a" * 40, limit=40)
    assert len(limited["unified_diff"].encode()) <= 40 and limited["diff_truncated"]


@pytest.mark.ac("SF-20-AC05")
def test_archive_digest_is_verified_before_source_can_become_model_input(job, evidence):
    with pytest.raises(CodexError, match="MODEL_SOURCE_DIGEST_MISMATCH"):
        archive_files(base64.b64decode(job["source"]["archive"]), "0" * 64)
    evidence(
        "The archive cannot become model input under a mismatched digest",
        {"mismatched_digest_rejected": True},
    )


@pytest.mark.ac("SF-20-AC01")
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
@pytest.mark.ac("SF-20-AC02")
def test_repair_rejects_ungrounded_or_duplicate_claims(job, claimed, evidence):
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
    evidence(
        "REPAIR cannot invent or duplicate addressed failure IDs",
        {
            "ids": ids,
            "reason": adapter.result().reason,
            "artifacts": len(adapter.result().artifacts),
        },
    )


@pytest.mark.ac("SF-20-AC03")
def test_cancellation_before_codex_start_yields_result(evidence):
    adapter = CodexAdapter(Engine())
    adapter.cancel()
    assert adapter.result().outcome == "CANCELLED"
    assert adapter.engine.calls == []
    evidence(
        "Pre-start cancellation does not invoke the model",
        {"outcome": adapter.result().outcome, "engine_calls": len(adapter.engine.calls)},
    )


@pytest.mark.ac("SF-20-AC03")
def test_codex_signal_failure_is_not_reported_as_cancelled(evidence):
    class UnstoppableEngine(Engine):
        def cancel(self):
            raise CodexError("STOP_UNCONFIRMED", "INFRASTRUCTURE")

    adapter = CodexAdapter(UnstoppableEngine())
    adapter.cancel()
    assert adapter.result().outcome == "FAILED"
    assert adapter.result().reason == "STOP_UNCONFIRMED"
    evidence(
        "Unconfirmed stop remains failed",
        {"outcome": adapter.result().outcome, "reason": adapter.result().reason},
    )
