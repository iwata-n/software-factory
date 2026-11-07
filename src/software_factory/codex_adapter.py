"""Codex-generated plans, text changes and independent structured code reviews."""

import base64
import difflib
import hashlib
import io
import json
import tarfile
from pathlib import PurePosixPath
from threading import Event, Lock, Thread
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .adapters import AgentExecutionError, AgentResult, Capabilities, ProducedArtifact
from .candidate_mount import readonly_tar
from .codex_engine import CodexError, sensitive_input
from .contracts import FindingLocation, Name, Plan, digest, path_within, safe_path
from .metering import price_call
from .project_reader import run_project_model
from .sandbox import STOP_UNCONFIRMED, RunnerError
from .security import introduces_secret, redact, sensitive_patch


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FileChange(ModelOutput):
    path: Name
    content: Annotated[str, Field(max_length=256 * 1024)] | None
    edits: list["TextEdit"] = Field(default_factory=list, max_length=100)


class TextEdit(ModelOutput):
    old_text: Annotated[str, Field(min_length=1, max_length=32768)]
    new_text: Annotated[str, Field(max_length=65536)]


class ChangeSet(ModelOutput):
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    acceptance_ids: list[Name] = Field(min_length=1, max_length=100)
    changes: list[FileChange] = Field(min_length=1, max_length=40)


class RepairSet(ChangeSet):
    addressed_failure_ids: list[Name] = Field(min_length=1, max_length=100)


class ReviewCriterion(ModelOutput):
    criterion_id: Name
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    explanation: Annotated[str, Field(min_length=1, max_length=4000)]
    locations: list[FindingLocation] = Field(min_length=1, max_length=20)


class ReviewFinding(ModelOutput):
    criterion_id: Name
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal["CODE", "SPEC_CHANGE_REQUIRED"]
    stable_code: Name
    location: FindingLocation
    description: Annotated[str, Field(min_length=1, max_length=4000)]


class ReviewSet(ModelOutput):
    criteria: list[ReviewCriterion] = Field(min_length=1, max_length=100)
    findings: list[ReviewFinding] = Field(max_length=100)


class ReviewOutput(dict):
    def __init__(self, report, deliveries, audit):
        super().__init__(report)
        self.usage_deliveries, self.audit = deliveries, audit


def archive_files(archive, expected_digest):
    try:
        packed = readonly_tar(archive)
        files, fingerprint = {}, hashlib.sha256()
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:") as bundle:
            for info in bundle:
                mode = "120000" if info.issym() else "100755" if info.mode & 0o111 else "100644"
                body = info.linkname.encode() if info.issym() else bundle.extractfile(info).read()
                files[info.name] = (mode, body)
        for path, (mode, body) in sorted(files.items()):
            fingerprint.update(
                path.encode() + b"\0" + mode.encode() + b"\0" + hashlib.sha256(body).digest()
            )
        if fingerprint.hexdigest() != expected_digest:
            raise CodexError("MODEL_SOURCE_DIGEST_MISMATCH", "POLICY")
        return files
    except (RunnerError, tarfile.TarError, ValueError, OSError, UnicodeError):
        raise CodexError("MODEL_SOURCE_INVALID", "POLICY") from None


def context_files(files, policy):
    selected = {}
    roots = policy.get("allowed_read_paths", policy["allowed_write_paths"]) + list(
        policy.get("protected_manifest", {})
    )
    for path, (mode, body) in files.items():
        if mode != "120000" and any(path_within(path, root) for root in roots):
            try:
                selected[path] = body.decode("utf-8")
            except UnicodeError:
                raise CodexError("MODEL_BINARY_INPUT_UNSUPPORTED", "POLICY") from None
    if not selected:
        raise CodexError("MODEL_SOURCE_CONTEXT_MISSING", "POLICY")
    return selected


def validate_policy(engine, snapshot):
    policy = snapshot["policy"]
    if snapshot["agent_adapter"] != "codex_cli" or not policy.get("allow_external_model"):
        raise CodexError("POLICY_DENIED", "POLICY")
    if snapshot["agent_configuration"].get("engine_manifest_hash") != engine.manifest_hash:
        raise CodexError("CODEX_MANIFEST_MISMATCH", "POLICY")
    pricing = policy.get("pricing_manifest")
    if (
        not pricing
        or pricing["provider"] != "codex_cli"
        or pricing["model"] != engine.manifest.model
        or pricing["basis"] != "budget_estimate"
    ):
        raise CodexError("CODEX_PRICING_MANIFEST_REQUIRED", "POLICY")
    return policy


def delivery(snapshot, response, attempt_id):
    if response.usage is None:
        return ()
    rates = snapshot["policy"]["pricing_manifest"]
    calls = getattr(response, "calls", ()) or (response.usage,)
    return (
        {
            "provider": "codex_cli",
            "delivery_id": "codex-" + attempt_id,
            "sequence": 1,
            "total_microusd": sum(price_call(rates, call) for call in calls),
            "currency": "USD",
            "final": True,
            "pricing_manifest_hash": digest(rates),
            "calls": [call.model_dump() for call in calls],
        },
    )


def no_call_delivery(snapshot, attempt_id):
    rates = snapshot.get("policy", {}).get("pricing_manifest")
    if not rates or rates.get("provider") != "codex_cli":
        return ()
    return (
        {
            "provider": "codex_cli",
            "delivery_id": "codex-no-call-" + attempt_id,
            "sequence": 1,
            "total_microusd": 0,
            "currency": "USD",
            "final": True,
            "pricing_manifest_hash": digest(rates),
            "calls": [],
        },
    )


def model_context(job, files, policy):
    role = job["kind"]
    context = {
        "role": role,
        "task": job["snapshot"]["task_spec"]["task"],
        "allowed_write_paths": policy["allowed_write_paths"],
        "protected_paths": policy["protected_paths"] + list(policy.get("protected_manifest", {})),
        "source_files": {} if "allowed_read_paths" in policy else context_files(files, policy),
        "instructions": {
            "PLAN": "One unit covering every acceptance ID. Propose exact permitted file paths.",
            "IMPLEMENT": (
                "For existing files, prefer content=null and edits of exact old_text/new_text. "
                "Each old_text must occur exactly once in the current source. Read the actual "
                "lines first; do not guess anchors or include [REDACTED]. Preserve unrelated code. "
                "New files use full UTF-8 content and edits=[]. content=null with edits=[] deletes."
                " Return one entry per path. Do not embed real or sample credentials, even in "
                "tests/docs. Construct authentication from existing runtime fixtures and values."
            ),
            "REPAIR": (
                "Repair the supplied failures and list the corresponding IDs in "
                "addressed_failure_ids. Preserve protected tests and scope."
                " Prefer exact old_text/new_text edits with content=null for existing files; "
                "read the actual source first. New files use content and edits=[]."
            ),
            "REVIEW": "Inspect every AC. Cite existing source locations. Do not claim execution.",
        }[role],
    }
    if role in {"IMPLEMENT", "REPAIR"}:
        context["accepted_plan"] = job["plan"]
    if role == "REPAIR":
        failures = job["input"]["failure_set"]
        if not failures:
            raise CodexError("MODEL_REPAIR_FAILURES_REQUIRED", "POLICY")
        context["repair_failures"] = [
            {"id": digest(failure), "failure": failure} for failure in failures
        ]
    return context


def make_patch(changes, files, policy, plan, acceptance_ids):
    if set(changes.acceptance_ids) != set(acceptance_ids) or len(
        set(changes.acceptance_ids)
    ) != len(changes.acceptance_ids):
        raise CodexError("MODEL_ACCEPTANCE_COVERAGE", "INCONCLUSIVE")
    paths, output = set(), []
    protected = policy["protected_paths"] + list(policy.get("protected_manifest", {}))
    if "." not in policy["allowed_write_paths"]:
        protected += [".github", "migrations", "auth", "billing", "src/auth", "src/billing"]
    planned = [path for unit in plan["units"] for path in unit["proposed_write_paths"]]
    project_scope = "." in policy["allowed_write_paths"] and not policy.get("require_plan_approval")
    links = [path for path, (mode, _) in files.items() if mode == "120000"]
    for change in changes.changes:
        path = change.path
        if (
            not safe_path(path)
            or any(ord(c) < 32 for c in path)
            or path in paths
            or any(part.lower() == ".git" for part in path.split("/"))
            or any(path_within(path, link) for link in links)
            or not any(path_within(path, root) for root in policy["allowed_write_paths"])
            or (not project_scope and not any(path_within(path, root) for root in planned))
            or any(path_within(path, root) for root in protected)
            or (
                "." not in policy["allowed_write_paths"]
                and PurePosixPath(path).name
                in {
                    "conftest.py",
                    "pytest.ini",
                    "pyproject.toml",
                    "tox.ini",
                    "setup.cfg",
                    ".coveragerc",
                }
            )
        ):
            raise CodexError(
                "MODEL_CHANGE_PATH_DENIED",
                "POLICY",
                details={
                    "path": path,
                    "duplicate_path": path in paths,
                    "outside_project": not any(
                        path_within(path, root) for root in policy["allowed_write_paths"]
                    ),
                    "outside_approved_plan": not project_scope
                    and not any(path_within(path, root) for root in planned),
                    "protected_path": any(path_within(path, root) for root in protected),
                    "symlink_path": any(path_within(path, link) for link in links),
                },
            )
        paths.add(path)
        old = files[path][1].decode("utf-8") if path in files else ""
        new = change.content
        if change.edits:
            if path not in files or new is not None:
                raise CodexError("MODEL_EDIT_MODE_INVALID", "POLICY")
            new = old
            for edit in change.edits:
                if new.count(edit.old_text) != 1:
                    raise CodexError(
                        "MODEL_EDIT_ANCHOR_MISMATCH",
                        "INCONCLUSIVE",
                        details={
                            "path": path,
                            "anchor_matches": new.count(edit.old_text),
                        },
                    )
                new = new.replace(edit.old_text, edit.new_text, 1)
        if new is None and path not in files:
            raise CodexError("MODEL_DELETE_MISSING_PATH", "POLICY")
        if new is not None and ("\x00" in new or introduces_secret(path, old, new)):
            raise CodexError(
                "MODEL_CHANGE_CONTENT_DENIED",
                "POLICY",
                details={
                    "path": path,
                    "hint": (
                        "Use runtime authentication fixtures; do not embed credentials "
                        "in code, tests or documentation."
                    ),
                },
            )
        for line in difflib.unified_diff(
            old.splitlines(True),
            (new or "").splitlines(True),
            fromfile="a/" + path if path in files else "/dev/null",
            tofile="b/" + path if new is not None else "/dev/null",
        ):
            output.append(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            )
    patch = "".join(output).encode()
    if not patch:
        raise CodexError("NO_CODE_CHANGE", "POLICY")
    if len(patch) > 16 * 1024 * 1024:
        raise CodexError("MODEL_PATCH_TOO_LARGE", "POLICY")
    return patch


def audit(engine, response, role):
    return {
        "engine": "codex-cli",
        "engine_version": engine.manifest.version,
        "model": engine.manifest.model,
        "engine_manifest_hash": engine.manifest_hash,
        "execution_id": response.thread_id,
        "role": role,
        "input_count": response.usage.input_tokens,
        "cached_input_count": response.usage.cached_input_tokens,
        "output_count": response.usage.output_tokens,
        "currency_basis": "budget_estimate",
        "model_call_count": len(getattr(response, "calls", ()) or (response.usage,)),
        "token_basis": "cli_turn_total",
        "supports_resume": False,
        "startup_diagnostics": response.startup_diagnostics,
    }


class CodexAdapter:
    stop_margin_seconds = 10
    capabilities = Capabilities(kinds=("PLAN", "IMPLEMENT", "REPAIR"), adapters=("codex_cli",))

    def __init__(self, engine):
        self.engine = engine
        self._result, self._events = None, []
        self._cancel, self._lock, self._thread = Event(), Lock(), None

    def bind_transport(self, client, lease_path, fence):
        self._transport = (client, lease_path, fence)

    def start(self, job):
        if job.get("resume"):
            raise ValueError("Codex resume is unsupported")
        if job["kind"] not in self.capabilities.kinds:
            raise ValueError("unsupported Codex role")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Codex stage is already running")
            self._result, self._cancel = None, Event()
            self._events = [
                {
                    "kind": "AGENT_STARTED",
                    "body": {
                        "engine": "codex-cli",
                        "role": job["kind"],
                        "execution_id": job["attempt_id"],
                    },
                }
            ]
            self._thread = Thread(target=self._execute, args=(job, self._cancel), daemon=True)
            self._thread.start()

    def _execute(self, job, cancelled):
        receipts, called = (), False
        try:
            snapshot = job["snapshot"]
            policy = validate_policy(self.engine, snapshot)
            if job["kind"] == "REPAIR":
                candidate = job["input"]["candidate"]
                client, path, fence = self._transport
                fetched = client.get(
                    path + "/inputs/" + job["input"]["source_artifact_id"] + "/content",
                    params={"fence": fence},
                    timeout=10,
                )
                fetched.raise_for_status()
                files = archive_files(fetched.content, candidate["source_digest"])
            else:
                source = job.get("source")
                if not source or source["sha"] != job["base_sha"]:
                    raise CodexError("MODEL_FIXED_SOURCE_REQUIRED", "POLICY")
                files = archive_files(
                    base64.b64decode(source["archive"], validate=True), source["source_digest"]
                )
            context = model_context(job, files, policy)
            # Validate sensitivity/size before marking a provider invocation as possible.
            if sensitive_input(context):
                raise CodexError("SENSITIVE_MODEL_INPUT", "POLICY")
            schema = {"PLAN": Plan, "IMPLEMENT": ChangeSet, "REPAIR": RepairSet}[job["kind"]]
            called = True
            if "allowed_read_paths" in policy:

                def progress(round_number, paths):
                    with self._lock:
                        self._events.append(
                            {
                                "kind": "PROGRESS",
                                "body": {
                                    "message": "Codex がプロジェクトの参照・検索を進めています。",
                                    "round": round_number,
                                    "paths": paths,
                                },
                            }
                        )

                def validate(value):
                    try:
                        changed = schema.model_validate(value)
                        patch = make_patch(
                            changed,
                            files,
                            policy,
                            job["plan"],
                            [
                                criterion["id"]
                                for criterion in snapshot["task_spec"]["task"][
                                    "acceptance_criteria"
                                ]
                            ],
                        )
                        if job["kind"] == "REPAIR":
                            known = {item["id"] for item in context["repair_failures"]}
                            claimed = changed.addressed_failure_ids
                            if len(set(claimed)) != len(claimed) or not set(claimed) <= known:
                                raise CodexError(
                                    "MODEL_REPAIR_FAILURE_IDS_INVALID",
                                    details={
                                        "hint": "Use only identifiers from repair_failures.",
                                    },
                                )
                        if sensitive_patch(patch.decode()):
                            raise CodexError(
                                "MODEL_PATCH_CONTENT_DENIED",
                                "POLICY",
                                details={
                                    "hint": (
                                        "Use runtime authentication fixtures; do not embed "
                                        "credentials in new code or documentation."
                                    ),
                                },
                            )
                    except CodexError as error:
                        with self._lock:
                            self._events.append(
                                {
                                    "kind": "PROGRESS",
                                    "body": {
                                        "message": "変更の検査結果を返し、自己修正中です。",
                                        "code": error.code,
                                        "details": redact(error.details),
                                    },
                                }
                            )
                        raise

                response = run_project_model(
                    self.engine,
                    context,
                    schema,
                    cancelled,
                    files=files,
                    read_paths=policy["allowed_read_paths"],
                    write_paths=policy["allowed_write_paths"],
                    timeout=job["execution"]["timeout_seconds"],
                    progress=progress,
                    validate_result=validate if job["kind"] in {"IMPLEMENT", "REPAIR"} else None,
                )
            else:
                response = self.engine.run(
                    context, schema, cancelled, timeout=job["execution"]["timeout_seconds"]
                )
            receipts = delivery(snapshot, response, job["attempt_id"])
            record = audit(self.engine, response, job["kind"])
            log = ProducedArtifact("codex-log", "LOG", json.dumps(record).encode())
            if job["kind"] == "PLAN":
                value, artifacts = response.value, (log,)
            else:
                changed = schema.model_validate(response.value)
                patch = make_patch(
                    changed,
                    files,
                    policy,
                    job["plan"],
                    [c["id"] for c in snapshot["task_spec"]["task"]["acceptance_criteria"]],
                )
                value = {"summary": changed.summary, "simulation": False}
                if job["kind"] == "REPAIR":
                    claimed = changed.addressed_failure_ids
                    known = {item["id"] for item in context["repair_failures"]}
                    if len(set(claimed)) != len(claimed) or not set(claimed) <= known:
                        raise CodexError("MODEL_REPAIR_FAILURE_IDS_INVALID")
                    value["addressed_failure_ids"] = claimed
                artifacts = (ProducedArtifact("patch", "PATCH", patch), log)
            result = AgentResult(
                "EXECUTED",
                value,
                usage_microusd=None,
                artifacts=artifacts,
                usage_deliveries=receipts,
            )
        except CodexError as error:
            if error.details:
                with self._lock:
                    self._events.append(
                        {
                            "kind": "LOG",
                            "body": {
                                "message": error.code,
                                "details": redact(error.details),
                            },
                        }
                    )
            if error.usage is not None:
                receipts = delivery(job["snapshot"], error, job["attempt_id"])
            elif not called or error.before_launch:
                receipts = no_call_delivery(job["snapshot"], job["attempt_id"])
            result = AgentResult(
                "FAILED",
                {},
                category=error.category,
                reason=error.code,
                usage_microusd=None,
                usage_deliveries=receipts,
            )
        except (ValueError, KeyError, UnicodeError):
            result = AgentResult(
                "FAILED",
                {},
                category="INCONCLUSIVE",
                reason="CODEX_INPUT_OR_RESULT_INVALID",
                usage_microusd=None,
                usage_deliveries=receipts,
            )
        except httpx.HTTPError:
            result = AgentResult(
                "FAILED",
                {},
                category="INFRASTRUCTURE",
                reason="CODEX_INPUT_UNAVAILABLE",
                usage_microusd=None,
                usage_deliveries=receipts,
            )
        except Exception:
            result = AgentResult(
                "FAILED",
                {},
                category="INFRASTRUCTURE",
                reason="CODEX_STAGE_CRASHED",
                usage_microusd=None,
                usage_deliveries=receipts,
            )
        with self._lock:
            if self._result and self._result.reason == STOP_UNCONFIRMED:
                return
            if cancelled.is_set() and result.reason != STOP_UNCONFIRMED:
                result = AgentResult(
                    "CANCELLED", {}, usage_microusd=None, usage_deliveries=receipts
                )
            self._result = result
            self._events.append(
                {
                    "kind": "AGENT_FINISHED",
                    "body": {
                        "engine": "codex-cli",
                        "role": job["kind"],
                        "execution_id": job["attempt_id"],
                        "outcome": result.outcome,
                    },
                }
            )

    def events(self):
        with self._lock:
            events, self._events = self._events, []
            return events

    def result(self):
        with self._lock:
            return self._result

    def cancel(self):
        self._cancel.set()
        stop_unconfirmed = False
        try:
            self.engine.cancel()
        except CodexError as error:
            if error.code != STOP_UNCONFIRMED:
                raise
            stop_unconfirmed = True
        if self._thread:
            self._thread.join(timeout=2)
            stop_unconfirmed |= self._thread.is_alive()
        with self._lock:
            if self._result and self._result.reason == STOP_UNCONFIRMED:
                return
            if stop_unconfirmed or self._result is None:
                self._result = AgentResult(
                    "FAILED" if stop_unconfirmed else "CANCELLED",
                    {},
                    category="INFRASTRUCTURE" if stop_unconfirmed else None,
                    reason=STOP_UNCONFIRMED if stop_unconfirmed else None,
                    usage_microusd=None,
                    usage_deliveries=self._result.usage_deliveries if self._result else (),
                )


class CodexReviewer:
    def __init__(self, engine, job=None):
        self.engine, self.job = engine, job

    def for_job(self, job):
        return CodexReviewer(self.engine, job)

    def review(self, context, cancelled):
        job, receipts, called = self.job, (), False
        if job is None or job["kind"] != "REVIEW":
            raise AgentExecutionError("CODEX_REVIEW_JOB_REQUIRED", "POLICY")
        try:
            snapshot = job["snapshot"]
            policy = validate_policy(self.engine, snapshot)
            files = archive_files(context["source_archive"], context["target"]["source_digest"])
            prompt = model_context(job, files, policy)
            if sensitive_input(prompt):
                raise CodexError("SENSITIVE_MODEL_INPUT", "POLICY")
            called = True
            if "allowed_read_paths" in policy:
                response = run_project_model(
                    self.engine,
                    prompt,
                    ReviewSet,
                    cancelled,
                    files=files,
                    read_paths=policy["allowed_read_paths"],
                    write_paths=policy["allowed_write_paths"],
                    timeout=job["execution"]["timeout_seconds"],
                )
            else:
                response = self.engine.run(
                    prompt, ReviewSet, cancelled, timeout=job["execution"]["timeout_seconds"]
                )
            receipts = delivery(snapshot, response, job["attempt_id"])
            reviewed = ReviewSet.model_validate(response.value)
            required = {c["id"] for c in snapshot["task_spec"]["task"]["acceptance_criteria"]}
            actual = [c.criterion_id for c in reviewed.criteria]
            if set(actual) != required or len(set(actual)) != len(actual):
                raise CodexError("CODEX_REVIEW_COVERAGE")

            def location(item):
                if "allowed_read_paths" in policy:
                    source = files.get(item.path)
                    if (
                        not source
                        or source[0] == "120000"
                        or not any(
                            path_within(item.path, root) for root in policy["allowed_read_paths"]
                        )
                    ):
                        raise CodexError("CODEX_REVIEW_LOCATION_INVALID")
                    try:
                        lines = len(source[1].decode("utf-8").splitlines())
                    except UnicodeError:
                        raise CodexError("CODEX_REVIEW_LOCATION_INVALID") from None
                elif item.path in prompt["source_files"]:
                    lines = len(prompt["source_files"][item.path].splitlines())
                else:
                    raise CodexError("CODEX_REVIEW_LOCATION_INVALID")
                if item.end_line > max(1, lines):
                    raise CodexError("CODEX_REVIEW_LOCATION_INVALID")
                return item.model_dump()

            references = [context["source_artifact_id"], "$artifact/review-log"]
            criteria = []
            for criterion in reviewed.criteria:
                locations = [location(item) for item in criterion.locations]
                explanation = (
                    criterion.explanation + " " + json.dumps(locations, ensure_ascii=False)
                )
                criteria.append(
                    {
                        "criterion_id": criterion.criterion_id,
                        "status": criterion.status,
                        "explanation": explanation[:4000],
                        "evidence_artifact_ids": references,
                    }
                )
            findings = []
            for index, finding in enumerate(reviewed.findings):
                if finding.criterion_id not in required:
                    raise CodexError("CODEX_REVIEW_COVERAGE")
                findings.append(
                    {
                        **finding.model_dump(),
                        "id": "codex-finding-" + str(index),
                        "candidate_id": context["target"]["candidate_id"],
                        "location": location(finding.location),
                        "evidence_artifact_ids": references,
                    }
                )
            return ReviewOutput(
                {"criteria": criteria, "findings": findings},
                receipts,
                audit(self.engine, response, "REVIEW"),
            )
        except CodexError as error:
            if error.usage is not None:
                receipts = delivery(job["snapshot"], error, job["attempt_id"])
            elif not called or error.before_launch:
                receipts = no_call_delivery(job["snapshot"], job["attempt_id"])
            raise AgentExecutionError(
                error.code, error.category, usage_deliveries=receipts
            ) from None
        except (ValueError, KeyError, UnicodeError):
            raise AgentExecutionError(
                "CODEX_REVIEW_SCHEMA_INVALID", "INCONCLUSIVE", usage_deliveries=receipts
            ) from None

    def cancel(self):
        try:
            self.engine.cancel()
        except CodexError as error:
            raise AgentExecutionError(error.code, error.category) from None
