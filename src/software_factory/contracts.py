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
    title: Name
    repository_id: Name
    source: Source = Field(default_factory=Source)
    objective: Annotated[str, Field(min_length=1, max_length=16000)]
    base_ref: Name = "main"
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=100)
    constraints: list[Name] = Field(default_factory=list, max_length=100)

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


class RequiredCICheck(Contract):
    provider: Literal["github"] = "github"
    app_id: int = Field(gt=0)
    check_name: Name


class ProjectPolicy(Contract):
    version: Literal[1] = 1
    allowed_write_paths: list[Name] = Field(min_length=1, max_length=100)
    protected_paths: list[Name] = Field(
        default_factory=lambda: [".github", "migrations", "auth", "billing"]
    )
    symlink_paths: list[Name] = Field(default_factory=list, max_length=100)
    agent_configuration: dict[str, str] = Field(default_factory=lambda: {"version": "1"})
    verification: Verification
    execution_image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    agent_adapter: Literal["fake", "codex_cli"] = "fake"
    verification_commands: list[VerificationCommand] = Field(default_factory=list, max_length=100)
    required_ci_checks: list[RequiredCICheck] = Field(default_factory=list, max_length=100)
    protected_manifest: dict[str, str] = Field(default_factory=dict, max_length=10000)
    allow_external_model: bool = False
    run_budget_microusd: int = Field(gt=0, le=10**12)
    job_reservation_microusd: int = Field(gt=0, le=10**12)
    max_infra_attempts_per_job: int = Field(default=3, ge=1, le=3)
    max_job_execution_seconds: int = Field(default=2700, ge=1, le=2700)

    @model_validator(mode="after")
    def valid_paths_and_budget(self):
        if not all(
            safe_path(path)
            for path in self.allowed_write_paths + self.protected_paths + self.symlink_paths
        ):
            raise ValueError("policy paths must be relative canonical paths without wildcards")
        if self.job_reservation_microusd > self.run_budget_microusd:
            raise ValueError("a job reservation must fit within the run budget")
        if any(
            not safe_path(path) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for path, value in self.protected_manifest.items()
        ):
            raise ValueError("protected manifest must contain canonical paths and SHA-256 values")
        ids = [command.check_id for command in self.verification_commands]
        if len(ids) != len(set(ids)) or not set(ids).issubset(self.verification.required_checks):
            raise ValueError("verification command IDs must be unique required checks")
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
    return path == root or path.startswith(root + "/")


def validate_plan(plan: Plan, spec: TaskSpec, policy: ProjectPolicy) -> str | None:
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
        if not safe_path(path) or not any(
            path_within(path, root) for root in policy.allowed_write_paths
        ):
            return "PLAN_WRITE_PATH_DENIED"
        if any(
            path_within(path, root) or path_within(root, path)
            for root in policy.protected_paths
            + [
                ".github",
                "migrations",
                "auth",
                "billing",
                "src/auth",
                "src/billing",
            ]
        ):
            return "PLAN_PROTECTED_PATH"
    return None


class ProjectCreate(Contract):
    name: Name
    policy: ProjectPolicy


class RepositoryCreate(Contract):
    name: Name
    # First slice pins a trusted revision in the registry; no git/network execution in the CP.
    base_ref: Name = "main"
    base_sha: Sha
    remote_url: str | None = Field(default=None, max_length=1000)


class RunStart(Contract):
    spec_version: int = Field(ge=1)
    strategy: Literal["implement_and_review"] = "implement_and_review"


class Claim(Contract):
    protocol_version: Literal[1] = 1
    wait_seconds: int = Field(default=0, ge=0, le=20)


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
    usage_microusd: int = Field(default=0, ge=0, le=10**12)


class Fail(Contract):
    fence: int = Field(ge=1)
    category: Literal["INFRASTRUCTURE", "POLICY", "INCONCLUSIVE"]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    usage_microusd: int = Field(default=0, ge=0, le=10**12)


class Cancel(Contract):
    expected_run_revision: int = Field(ge=1)
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
