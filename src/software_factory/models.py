"""Persistence model. Every tenant-owned reference includes workspace_id."""

from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Tenant:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))


class Principal(Tenant, Base):
    __tablename__ = "principals"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("role IN ('USER', 'WORKER')"),
    )
    role: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Enrollment(Tenant, Base):
    __tablename__ = "worker_enrollments"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    capabilities: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    consumed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Worker(Tenant, Base):
    __tablename__ = "workers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "id"], ["principals.workspace_id", "principals.id"]),
    )
    name: Mapped[str] = mapped_column(String(200))
    capabilities: Mapped[list] = mapped_column(JSON)
    managed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Exactly one slot in this first slice; worker row is the slot mutex.


class Project(Tenant, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("workspace_id", "id"),)
    name: Mapped[str] = mapped_column(String(200))
    policy: Mapped[dict] = mapped_column(JSON)


class Repository(Tenant, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "project_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"]
        ),
    )
    project_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200))
    base_ref: Mapped[str] = mapped_column(String(200))
    base_sha: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    remote_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class Task(Tenant, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "repository_id"],
            ["repositories.workspace_id", "repositories.project_id", "repositories.id"],
        ),
    )
    project_id: Mapped[str] = mapped_column(String(36))
    repository_id: Mapped[str] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(200))
    latest_spec_version: Mapped[int] = mapped_column(Integer, default=1)


class SpecVersion(Tenant, Base):
    __tablename__ = "task_spec_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "task_id", "version"),
        ForeignKeyConstraint(["workspace_id", "task_id"], ["tasks.workspace_id", "tasks.id"]),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(String(64))


class Run(Tenant, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "task_id", "spec_version"],
            [
                "task_spec_versions.workspace_id",
                "task_spec_versions.task_id",
                "task_spec_versions.version",
            ],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "repository_id"],
            ["repositories.workspace_id", "repositories.id"],
        ),
        CheckConstraint("budget_microusd > 0 AND reserved_microusd >= 0 AND spent_microusd >= 0"),
        ForeignKeyConstraint(
            ["workspace_id", "id", "current_candidate_id"],
            ["candidates.workspace_id", "candidates.run_id", "candidates.id"],
            name="fk_runs_current_candidate",
            use_alter=True,
        ),
    )
    task_id: Mapped[str] = mapped_column(String(36))
    spec_version: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[str] = mapped_column(String(36))
    base_sha: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    phase: Mapped[str] = mapped_column(String(40), default="PLAN")
    stop_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    state_revision: Mapped[int] = mapped_column(Integer, default=1)
    budget_microusd: Mapped[int] = mapped_column(BigInteger)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    spent_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    wait_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    current_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class PlanVersion(Tenant, Base):
    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "run_id", "version"),
        UniqueConstraint("workspace_id", "run_id", "id", name="uq_plans_workspace_run_id"),
        UniqueConstraint("workspace_id", "producer_job_id", name="uq_plans_producer_job"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "producer_job_id"],
            ["jobs.workspace_id", "jobs.run_id", "jobs.id"],
            name="fk_plan_producer_job",
        ),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    producer_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    validation_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Job(Tenant, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "run_id", "id", name="uq_jobs_workspace_run_id"),
        UniqueConstraint("workspace_id", "run_id", "step_key"),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
        CheckConstraint("attempt_no >= 0 AND fence >= 0"),
        Index("ix_jobs_ready", "workspace_id", "state", "available_at", "created_at", "id"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    step_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="READY")
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    payload: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


class JobDependency(Tenant, Base):
    __tablename__ = "job_dependencies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "job_id", "depends_on_id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "job_id"],
            ["jobs.workspace_id", "jobs.run_id", "jobs.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "depends_on_id"],
            ["jobs.workspace_id", "jobs.run_id", "jobs.id"],
        ),
        CheckConstraint("job_id <> depends_on_id"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    depends_on_id: Mapped[str] = mapped_column(String(36))


class Attempt(Tenant, Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "job_id", "attempt_no"),
        UniqueConstraint("workspace_id", "id", name="uq_attempts_workspace_id"),
        UniqueConstraint("workspace_id", "job_id", "id", name="uq_attempts_workspace_job_id"),
        ForeignKeyConstraint(["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"]),
        ForeignKeyConstraint(["workspace_id", "worker_id"], ["workers.workspace_id", "workers.id"]),
        Index("ix_attempts_expiry", "state", "expires_at"),
    )
    job_id: Mapped[str] = mapped_column(String(36))
    worker_id: Mapped[str] = mapped_column(String(36))
    attempt_no: Mapped[int] = mapped_column(Integer)
    lease_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    fence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="LEASED")
    started_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    deadline_at: Mapped[float] = mapped_column(Float)
    heartbeat_at: Mapped[float] = mapped_column(Float)
    reservation_microusd: Mapped[int] = mapped_column(BigInteger)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stop_confirmation: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RunEvent(Tenant, Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "run_id", "revision"),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(100))
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class OutboxEvent(Tenant, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(100))
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
    # Delivery adapters are a later task; these durable records are not sent yet.


class IdempotencyRecord(Tenant, Base):
    __tablename__ = "api_idempotency_records"
    __table_args__ = (
        UniqueConstraint("workspace_id", "principal_id", "operation", "key"),
        ForeignKeyConstraint(
            ["workspace_id", "principal_id"], ["principals.workspace_id", "principals.id"]
        ),
    )
    principal_id: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class WorkerEvent(Tenant, Base):
    __tablename__ = "worker_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "attempt_id", "sequence"),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["job_attempts.workspace_id", "job_attempts.id"],
        ),
        CheckConstraint("sequence > 0"),
    )
    attempt_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)
    run_revision: Mapped[int] = mapped_column(Integer)


class AuditEvent(Tenant, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(200))
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Artifact(Tenant, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "run_id", "id", name="uq_artifacts_workspace_run_id"),
        UniqueConstraint(
            "workspace_id", "attempt_id", "id", name="uq_artifacts_workspace_attempt_id"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "job_id"], ["jobs.workspace_id", "jobs.run_id", "jobs.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id", "attempt_id"],
            ["job_attempts.workspace_id", "job_attempts.job_id", "job_attempts.id"],
        ),
        CheckConstraint("size_bytes >= 0 AND size_bytes <= 16777216"),
        CheckConstraint("status IN ('RESERVED', 'UPLOADED', 'AVAILABLE', 'DELETED')"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    attempt_id: Mapped[str] = mapped_column(String(36))
    fence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="RESERVED")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    stored_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stored_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    finalized_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Candidate(Tenant, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "run_id", "id", name="uq_candidates_workspace_run_id"),
        UniqueConstraint("workspace_id", "run_id", "generation"),
        UniqueConstraint("workspace_id", "assembly_job_id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "assembly_job_id"],
            ["jobs.workspace_id", "jobs.run_id", "jobs.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "assembly_job_id", "producer_attempt_id"],
            ["job_attempts.workspace_id", "job_attempts.job_id", "job_attempts.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "patch_artifact_id"],
            ["artifacts.workspace_id", "artifacts.run_id", "artifacts.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "producer_attempt_id", "source_artifact_id"],
            ["artifacts.workspace_id", "artifacts.attempt_id", "artifacts.id"],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    assembly_job_id: Mapped[str] = mapped_column(String(36))
    producer_attempt_id: Mapped[str] = mapped_column(String(36))
    patch_artifact_id: Mapped[str] = mapped_column(String(36))
    source_artifact_id: Mapped[str] = mapped_column(String(36))
    base_sha: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64))
    tree_sha: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(64))
    spec_hash: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    image_digest: Mapped[str] = mapped_column(String(71))
    metadata_body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Check(Tenant, Base):
    __tablename__ = "checks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "attempt_id", "kind", "key"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "job_id"], ["jobs.workspace_id", "jobs.run_id", "jobs.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "candidate_id"],
            ["candidates.workspace_id", "candidates.run_id", "candidates.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id", "attempt_id"],
            ["job_attempts.workspace_id", "job_attempts.job_id", "job_attempts.id"],
        ),
        CheckConstraint("status IN ('PASS','FAIL','ERROR','INCONCLUSIVE')"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    candidate_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    attempt_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(40))
    eligible: Mapped[bool] = mapped_column(Boolean)
    target: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float] = mapped_column(Float)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_ids: Mapped[list] = mapped_column(JSON)
    counts: Mapped[dict] = mapped_column(JSON)
    artifact_ids: Mapped[list] = mapped_column(JSON)
    metadata_body: Mapped[dict] = mapped_column(JSON)


class Finding(Tenant, Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "attempt_id", "finding_key"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "job_id"], ["jobs.workspace_id", "jobs.run_id", "jobs.id"]
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "candidate_id"],
            ["candidates.workspace_id", "candidates.run_id", "candidates.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id", "attempt_id"],
            ["job_attempts.workspace_id", "job_attempts.job_id", "job_attempts.id"],
        ),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    candidate_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    attempt_id: Mapped[str] = mapped_column(String(36))
    finding_key: Mapped[str] = mapped_column(String(200))
    criterion_id: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(40), default="CODE", server_default="CODE")
    stable_code: Mapped[str] = mapped_column(
        String(200), default="REVIEW_FINDING", server_default="REVIEW_FINDING"
    )
    location: Mapped[dict] = mapped_column(JSON)
    description: Mapped[str] = mapped_column(String(4000))
    artifact_ids: Mapped[list] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(40))
    eligible: Mapped[bool] = mapped_column(Boolean)


class Gate(Tenant, Base):
    __tablename__ = "gates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "candidate_id", "stage", "evidence_digest"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "candidate_id"],
            ["candidates.workspace_id", "candidates.run_id", "candidates.id"],
        ),
        CheckConstraint("status IN ('PASS','FAIL','ERROR','INCONCLUSIVE')"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    candidate_id: Mapped[str] = mapped_column(String(36))
    stage: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(200))
    target: Mapped[dict] = mapped_column(JSON)
    evidence_ids: Mapped[list] = mapped_column(JSON)
    evidence_digest: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    metadata_body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class ApprovalRequest(Tenant, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint(
            "workspace_id", "run_id", "id", name="uq_approval_requests_workspace_run_id"
        ),
        UniqueConstraint("workspace_id", "run_id", "action", "target_hash"),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "plan_id"],
            ["plan_versions.workspace_id", "plan_versions.run_id", "plan_versions.id"],
        ),
        CheckConstraint("status IN ('PENDING','APPROVED','DENIED','EXPIRED','INVALIDATED')"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    target: Mapped[dict] = mapped_column(JSON)
    target_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


class ApprovalDecision(Tenant, Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "approval_id"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "approval_id"],
            ["approval_requests.workspace_id", "approval_requests.run_id", "approval_requests.id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "actor_id"], ["principals.workspace_id", "principals.id"]
        ),
        CheckConstraint("decision IN ('approve','deny')"),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    approval_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(String(36))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(4000))
    target_hash: Mapped[str] = mapped_column(String(64))
    before_revision: Mapped[int] = mapped_column(Integer)
    after_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
