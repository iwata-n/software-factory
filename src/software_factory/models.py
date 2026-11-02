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


class Worker(Tenant, Base):
    __tablename__ = "workers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id", "id"], ["principals.workspace_id", "principals.id"]),
    )
    name: Mapped[str] = mapped_column(String(200))
    capabilities: Mapped[list] = mapped_column(JSON)
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


class PlanVersion(Tenant, Base):
    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "run_id", "version"),
        ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
    )
    run_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
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
