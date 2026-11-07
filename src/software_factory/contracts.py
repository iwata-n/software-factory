"""Versioned input contracts. Unrecognized instructions are rejected, never ignored."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=200)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def safe_path(path: str) -> bool:
    return (
        bool(path)
        and not any(character in path for character in "*?[]\x00")
        and "\\" not in path
        and not PurePosixPath(path).is_absolute()
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Criterion(Contract):
    id: Name
    description: Annotated[str, Field(min_length=1, max_length=4000)]


class Source(Contract):
    provider: Literal["manual", "api", "jira"] = "manual"
    external_id: str | None = Field(default=None, max_length=500)


class TaskDefinition(Contract):
    title: Name = Field(default="", validate_default=True)
    repository_id: Name
    source: Source = Field(default_factory=Source)
    objective: Annotated[str, Field(min_length=1, max_length=16000)]
    base_ref: Name = "main"
    acceptance_criteria: list[Criterion] = Field(default_factory=list, max_length=100)
    constraints: list[Name] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def title_from_objective(cls, data):
        if isinstance(data, dict) and isinstance(data.get("objective"), str):
            title = data.get("title", "")
            objective = data["objective"].strip()
            if isinstance(title, str) and not title.strip() and objective:
                return data | {"title": objective.splitlines()[0][:80]}
        return data

    @model_validator(mode="after")
    def unique_criteria(self):
        ids = [item.id for item in self.acceptance_criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance criterion IDs must be unique")
        return self


class Execution(Contract):
    strategy: Literal["implement_and_review"] = "implement_and_review"
    max_implementation_units: Literal[1] = 1
    max_parallel_jobs: Literal[1] = 1
    max_infra_attempts_per_job: int = Field(default=3, ge=1, le=3)
    max_repair_rounds: int = Field(default=2, ge=0, le=2)
    max_replans: int = Field(default=1, ge=0, le=1)
    max_job_execution_seconds: int = Field(default=2700, ge=1, le=2700)


class Verification(Contract):
    profile_id: Name
    required_checks: list[Name] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_checks(self):
        if len(self.required_checks) != len(set(self.required_checks)):
            raise ValueError("required check IDs must be unique")
        return self


class PolicyRequest(Contract):
    merge: Literal["prohibited"] = "prohibited"
    deploy: Literal["prohibited"] = "prohibited"


class TaskSpec(Contract):
    schema_version: Literal[1] = 1
    task: TaskDefinition
    execution: Execution = Field(default_factory=Execution)
    verification: Verification
    policy_request: PolicyRequest = Field(default_factory=PolicyRequest)


class VerificationCommand(Contract):
    check_id: Name
    argv: list[Name] = Field(min_length=1, max_length=50)
    expected_test_ids: list[Name] = Field(min_length=1, max_length=10000)
    timeout_seconds: int = Field(default=120, ge=1, le=2700)
    junit_path: Name = ".factory/junit.xml"

    @model_validator(mode="after")
    def validate_command(self):
        if not safe_path(self.junit_path) or len(set(self.expected_test_ids)) != len(
            self.expected_test_ids
        ):
            raise ValueError("canonical report path and unique test IDs required")
        return self


class EvidenceTarget(Contract):
    candidate_id: Name
    head_sha: Sha
    source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    spec_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    profile_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class CheckObservation(Contract):
    check_id: Name
    execution_state: Literal["EXECUTED", "TIMEOUT", "START_FAILED"]
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    started_at: float = Field(ge=0, allow_inf_nan=False)
    finished_at: float = Field(ge=0, allow_inf_nan=False)
    log_artifact_id: Name
    junit_artifact_id: Name | None = None

    @model_validator(mode="after")
    def valid_times(self):
        if self.finished_at < self.started_at:
            raise ValueError("check end precedes start")
        return self


class CriterionObservation(Contract):
    criterion_id: Name
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    explanation: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_artifact_ids: list[Name] = Field(default_factory=list, max_length=100)


class FindingLocation(Contract):
    path: Name
    start_line: int = Field(ge=1, le=1000000)
    end_line: int = Field(ge=1, le=1000000)
    symbol: Name | None = None

    @model_validator(mode="after")
    def valid_location(self):
        if not safe_path(self.path) or self.end_line < self.start_line:
            raise ValueError("invalid finding location")
        return self


class FindingObservation(Contract):
    id: Name
    candidate_id: Name
    criterion_id: Name
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal["CODE", "SPEC_CHANGE_REQUIRED"] = "CODE"
    stable_code: Name = "REVIEW_FINDING"
    location: FindingLocation
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_artifact_ids: list[Name] = Field(min_length=1, max_length=100)


class EvidenceReport(Contract):
    target: EvidenceTarget
    isolation: dict
    checks: list[CheckObservation] = Field(default_factory=list, max_length=100)
    criteria: list[CriterionObservation] = Field(default_factory=list, max_length=100)
    findings: list[FindingObservation] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_records(self):
        for ids in (
            [c.check_id for c in self.checks],
            [c.criterion_id for c in self.criteria],
            [f.id for f in self.findings],
        ):
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate evidence record")
        return self


GITHUB_ACTIONS_APP_ID = 15368


class RequiredCICheck(Contract):
    provider: Literal["github"] = "github"
    app_id: int = Field(gt=0)
    check_name: Name
    github_workflow_id: int | None = Field(default=None, strict=True, gt=0)
    github_workflow_path: Name | None = None

    @model_validator(mode="after")
    def workflow_identity(self):
        if self.app_id == GITHUB_ACTIONS_APP_ID:
            if self.github_workflow_id is None or self.github_workflow_path is None:
                raise ValueError("GitHub Actions requires a fixed workflow ID and path")
            if not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", self.github_workflow_path):
                raise ValueError("invalid GitHub Actions workflow path")
        elif self.github_workflow_id is not None or self.github_workflow_path is not None:
            raise ValueError("workflow identity is only supported for GitHub Actions")
        return self


MicroUSD = Annotated[int, Field(strict=True, ge=0, le=10**12)]
Rate = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]{1,9})?$", max_length=32)]


class PricingManifest(Contract):
    version: Name
    provider: Literal["fixture", "openai", "codex_cli"]
    model: Name
    currency: Literal["USD"] = "USD"
    basis: Literal["provider_invoice", "provider_list_price", "budget_estimate"]
    source_url: Annotated[str, Field(min_length=1, max_length=1000)]
    input_usd_per_million: Rate
    cached_input_usd_per_million: Rate
    output_usd_per_million: Rate


class AgentToolCall(Contract):
    call_id: Name
    name: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")]


class ModelCallUsage(Contract):
    call_id: Name
    input_tokens: Annotated[int, Field(strict=True, ge=0, le=10**9)]
    cached_input_tokens: Annotated[int, Field(strict=True, ge=0, le=10**9)] = 0
    output_tokens: Annotated[int, Field(strict=True, ge=0, le=10**9)]
    # Keep the serialized payload of existing deliveries stable for idempotent replay.
    tool_calls: list[AgentToolCall] = Field(
        default_factory=list, max_length=128, exclude_if=lambda calls: not calls
    )

    @model_validator(mode="after")
    def cached_is_subset(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached tokens must be part of total input")
        return self


class UsageDelivery(Contract):
    provider: Name
    delivery_id: Name
    sequence: Annotated[int, Field(strict=True, ge=1, le=10**9)]
    total_microusd: MicroUSD
    currency: Literal["USD"] = "USD"
    final: Annotated[bool, Field(strict=True)]
    pricing_manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    calls: list[ModelCallUsage] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_calls(self):
        if len({c.call_id for c in self.calls}) != len(self.calls):
            raise ValueError("model call IDs must be unique")
        tool_ids = [tool.call_id for call in self.calls for tool in call.tool_calls]
        if len(set(tool_ids)) != len(tool_ids):
            raise ValueError("tool call IDs must be unique")
        return self


class BudgetRequest(Contract):
    expected_run_revision: int = Field(ge=1)
    current_budget_microusd: MicroUSD
    new_budget_microusd: MicroUSD
    reason: Annotated[str, Field(min_length=1, max_length=4000)]


class ProjectPolicy(Contract):
    version: Literal[1] = 1
    allowed_read_paths: list[Name] = Field(
        default_factory=lambda: ["."], min_length=1, max_length=100
    )
    allowed_write_paths: list[Name] = Field(
        default_factory=lambda: ["."], min_length=1, max_length=100
    )
    protected_paths: list[Name] = Field(default_factory=list, max_length=100)
    symlink_paths: list[Name] = Field(default_factory=list, max_length=100)
    agent_configuration: dict[str, str] = Field(default_factory=lambda: {"version": "1"})
    verification: Verification
    execution_image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    agent_adapter: Literal["fake", "codex_cli"] = "fake"
    verification_commands: list[VerificationCommand] = Field(default_factory=list, max_length=100)
    required_ci_checks: list[RequiredCICheck] = Field(default_factory=list, max_length=100)
    protected_manifest: dict[str, str] = Field(default_factory=dict, max_length=10000)
    allow_external_model: bool = False
    require_plan_approval: bool = False
    delivery_mode: Literal["draft_pr", "local_candidate"] = "draft_pr"
    pricing_manifest: PricingManifest | None = None
    run_budget_microusd: int = Field(gt=0, le=10**12)
    job_reservation_microusd: int = Field(gt=0, le=10**12)
    max_infra_attempts_per_job: int = Field(default=3, ge=1, le=3)
    max_job_execution_seconds: int = Field(default=2700, ge=1, le=2700)

    @model_validator(mode="after")
    def valid_paths_and_budget(self):
        if not all(
            path == "." or safe_path(path)
            for path in self.allowed_read_paths + self.allowed_write_paths
        ) or not all(safe_path(path) for path in self.protected_paths + self.symlink_paths):
            raise ValueError("policy paths must be relative canonical paths without wildcards")
        if self.job_reservation_microusd > self.run_budget_microusd:
            raise ValueError("a job reservation must fit within the run budget")
        if self.delivery_mode == "local_candidate" and self.required_ci_checks:
            raise ValueError("local delivery cannot satisfy required external CI checks")
        if any(
            not safe_path(path) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for path, value in self.protected_manifest.items()
        ):
            raise ValueError("protected manifest must contain canonical paths and SHA-256 values")
        ids = [command.check_id for command in self.verification_commands]
        if len(ids) != len(set(ids)) or not set(ids).issubset(self.verification.required_checks):
            raise ValueError("verification command IDs must be unique required checks")
        if any(
            check.github_workflow_path and check.github_workflow_path not in self.protected_manifest
            for check in self.required_ci_checks
        ):
            raise ValueError("GitHub Actions workflow must be in the protected manifest")
        identities = [(c.provider, c.app_id, c.check_name) for c in self.required_ci_checks]
        if len(identities) != len(set(identities)):
            raise ValueError("required CI identities must be unique")
        return self


class Unit(Contract):
    id: Name
    objective: Annotated[str, Field(min_length=1, max_length=16000)]
    acceptance_ids: list[Name] = Field(min_length=1, max_length=100)
    depends_on: list[Name] = Field(default_factory=list, max_length=100)
    proposed_write_paths: list[Name] = Field(min_length=1, max_length=100)


class Plan(Contract):
    units: list[Unit] = Field(min_length=1, max_length=1)
    assumptions: list[Name] = Field(default_factory=list, max_length=100)
    open_questions: list[Name] = Field(default_factory=list, max_length=100)


def path_within(path: str, root: str) -> bool:
    return safe_path(path) if root == "." else path == root or path.startswith(root + "/")


def validate_plan(plan: Plan, spec: TaskSpec, policy: ProjectPolicy) -> str | None:
    if not spec.task.acceptance_criteria:
        return "SPEC_ACCEPTANCE_CRITERIA_REQUIRED"
    unit = plan.units[0]
    if plan.open_questions:
        return "PLAN_HAS_OPEN_QUESTIONS"
    if unit.depends_on:
        return "PLAN_INVALID_DEPENDENCIES"  # One unit cannot depend on another unit in MVP.
    required = {criterion.id for criterion in spec.task.acceptance_criteria}
    if (
        len(unit.acceptance_ids) != len(set(unit.acceptance_ids))
        or set(unit.acceptance_ids) != required
    ):
        return "PLAN_ACCEPTANCE_COVERAGE"
    for path in unit.proposed_write_paths:
        if any(path_within(path, root) for root in policy.symlink_paths):
            return "PLAN_SYMLINK_PATH"
        if (
            not safe_path(path)
            or any(part.lower() == ".git" for part in path.split("/"))
            or not any(path_within(path, root) for root in policy.allowed_write_paths)
        ):
            return "PLAN_WRITE_PATH_DENIED"
        if any(
            path_within(path, root) or path_within(root, path)
            for root in policy.protected_paths
            + list(policy.protected_manifest)
            + (
                [
                    ".github",
                    "migrations",
                    "auth",
                    "billing",
                    "src/auth",
                    "src/billing",
                ]
                if "." not in policy.allowed_write_paths
                else []
            )
        ):
            return "PLAN_PROTECTED_PATH"
    return None


class ProjectCreate(Contract):
    name: Name
    policy: ProjectPolicy


class ProjectPolicyUpdate(Contract):
    expected_policy_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy: ProjectPolicy


class RepositoryCreate(Contract):
    name: Name
    # First slice pins a trusted revision in the registry; no git/network execution in the CP.
    base_ref: Name = "main"
    base_sha: Sha
    remote_url: str | None = Field(default=None, max_length=1000)


class RunStart(Contract):
    spec_version: int = Field(ge=1)
    reuse_existing: bool = False
    clarification_only: bool = False
    strategy: Literal["implement_and_review"] = "implement_and_review"


class ReplanRequest(Contract):
    expected_run_revision: int = Field(ge=1)
    reason: Annotated[str, Field(min_length=1, max_length=4000)]
    requested_task: TaskDefinition | None = None


class ApprovalSubmission(Contract):
    expected_run_revision: int = Field(ge=1, strict=True)
    target_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    decision: Literal["approve", "deny"]
    reason: Annotated[str, Field(min_length=1, max_length=4000)]


class Intervention(Contract):
    action: Literal["approve_plan", "approve_budget", "cancel", "resolve_blocked"]
    subject_id: Name
    expected_run_revision: int = Field(ge=1, strict=True)
    reason: Annotated[str, Field(min_length=1, max_length=4000)]
    target_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    decision: Literal["approve", "deny"] | None = None

    @model_validator(mode="after")
    def scoped_decision(self):
        if self.action in {"approve_plan", "approve_budget"}:
            if self.target_hash is None or self.decision is None:
                raise ValueError("approval requires target_hash and decision")
        elif self.decision is not None:
            raise ValueError("only an approval accepts a decision")
        if self.action == "resolve_blocked" and self.target_hash is None:
            raise ValueError("resolution requires the current intervention target hash")
        if self.action == "cancel" and self.target_hash is not None:
            raise ValueError("cancellation is bound to the run revision")
        if self.action == "cancel" and len(self.reason) > 2000:
            raise ValueError("cancellation reason exceeds its contract limit")
        return self


class PublicationBegin(Contract):
    expected_run_revision: int = Field(ge=1, strict=True)
    target_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PublicationReconcile(Contract):
    attempt: int = Field(ge=1, le=3, strict=True)
    target_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PublishedDraft(Contract):
    repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    number: int = Field(gt=0, strict=True)
    external_id: Name
    url: Annotated[str, Field(max_length=1000)]
    base_ref: Name
    base_sha: Sha
    head_ref: Name
    head_sha: Sha
    merge_sha: Sha | None = None
    draft: Literal[True]
    state: Literal["OPEN"]


class ProviderRequest(Contract):
    method: Literal["GET", "POST"]
    resource: Annotated[str, Field(min_length=1, max_length=1000)]
    status_code: int | None = Field(default=None, ge=100, le=599, strict=True)


class PublicationReport(Contract):
    delivery_id: Name
    target_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    status: Literal["SUCCEEDED", "UNKNOWN", "CONFLICT"]
    reconcile_attempt: int | None = Field(default=None, ge=1, le=3, strict=True)
    pull_request: PublishedDraft | None = None
    observed_head_sha: Sha | None = None
    provider_requests: list[ProviderRequest] = Field(default_factory=list, max_length=1000)
    reason: (
        Literal[
            "REMOTE_HEAD_CONFLICT",
            "BASE_CHANGED",
            "PR_CONFLICT",
            "SOURCE_MISMATCH",
            "PUBLISH_POLICY_DENIED",
            "PUBLISH_UNKNOWN",
            "PROVIDER_UNAVAILABLE",
            "PUSH_REJECTED",
        ]
        | None
    ) = None

    def payload(self):
        # Preserve hashes of initial reports stored before reconciliation was added.
        return self.model_dump(
            exclude={"reconcile_attempt"} if self.reconcile_attempt is None else None
        )

    @model_validator(mode="after")
    def outcome(self):
        if (self.status == "SUCCEEDED") != (self.pull_request is not None):
            raise ValueError("only success includes a verified Draft PR")
        if self.status != "SUCCEEDED" and self.reason is None:
            raise ValueError("a non-success result requires a classified reason")
        return self


class Claim(Contract):
    protocol_version: Literal[1] = 1
    wait_seconds: int = Field(default=0, ge=0, le=20)


class WorkerEnrollmentRequest(Contract):
    kinds: list[
        Literal["PLAN", "IMPLEMENT", "REPAIR", "ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"]
    ] = Field(min_length=1, max_length=6)
    adapter: Literal["fake", "codex_cli"] = "fake"
    ttl_seconds: int = Field(default=600, ge=1, le=3600)
    managed: bool = False
    usage_authority: bool = False

    @model_validator(mode="after")
    def trusted_capabilities(self):
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("duplicate Worker capabilities")
        if not self.managed and (
            self.usage_authority or set(self.kinds) & {"ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"}
        ):
            raise ValueError("trusted capabilities require a managed Worker")
        return self


class WorkerRevocation(Contract):
    reason: Name


class Enroll(Contract):
    token: Annotated[str, Field(min_length=20, max_length=300)]
    name: Name


class Heartbeat(Contract):
    fence: int = Field(ge=1)
    progress: dict[str, str] = Field(default_factory=dict, max_length=20)


class Complete(Contract):
    fence: int = Field(ge=1)
    outcome: Literal["EXECUTED", "CANCELLED"]
    artifact_ids: list[Name] = Field(default_factory=list, max_length=100)
    result: dict = Field(default_factory=dict, max_length=100)
    usage_microusd: MicroUSD | None = 0


class Fail(Contract):
    fence: int = Field(ge=1)
    category: Literal["INFRASTRUCTURE", "POLICY", "INCONCLUSIVE"]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    usage_microusd: MicroUSD | None = 0


class WorkspaceExecutionChange(Contract):
    stopped: bool = Field(strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class Cancel(Contract):
    expected_run_revision: int = Field(ge=1, strict=True)
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class WorkerEventInput(Contract):
    sequence: int = Field(ge=1)
    kind: Literal["PROGRESS", "LOG", "AGENT_STARTED", "AGENT_FINISHED"]
    body: dict = Field(default_factory=dict, max_length=100)


class WorkerEventBatch(Contract):
    fence: int = Field(ge=1)
    events: list[WorkerEventInput] = Field(min_length=1, max_length=100)


class ArtifactReserve(Contract):
    fence: int = Field(ge=1)
    kind: Literal["PATCH", "LOG", "RESULT", "EVIDENCE", "SOURCE", "JUNIT"]
    size_bytes: int = Field(ge=0, le=10**12, strict=True)
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ArtifactFinalize(Contract):
    fence: int = Field(ge=1)


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class CandidateReport(Contract):
    base_sha: Sha
    head_sha: Sha
    parent_sha: Sha | None = None
    tree_sha: Sha
    source_digest: Digest
    spec_hash: Digest
    profile_hash: Digest
    image_digest: ImageDigest
    source_artifact_id: Name
    patch_artifact_id: Name
    commit_object: Annotated[str, Field(min_length=1, max_length=100000)]
    file_manifest: dict[str, Digest] = Field(min_length=1, max_length=10000)
    changed_paths: list[Name] = Field(min_length=1, max_length=10000)
    isolation: dict = Field(max_length=30)

    @model_validator(mode="after")
    def validate_paths(self):
        if not all(safe_path(path) for path in list(self.file_manifest) + self.changed_paths):
            raise ValueError("candidate paths must be canonical")
        return self
