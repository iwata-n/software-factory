"""Transactional application commands; external execution never occurs under DB locks."""

import hashlib
import os
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from typing import TypeVar

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .contracts import (
    Cancel,
    Complete,
    Fail,
    ProjectCreate,
    ProjectPolicy,
    ProjectPolicyUpdate,
    RepositoryCreate,
    RunStart,
    TaskSpec,
    WorkerEventBatch,
    digest,
)
from .db import Database, db_now
from .domain import TERMINAL, DomainEvent, RunSnapshot, RunStatus, decide
from .models import (
    ApprovalRequest,
    Attempt,
    AuditEvent,
    Enrollment,
    IdempotencyRecord,
    Job,
    JobDependency,
    OutboxEvent,
    PlanVersion,
    Principal,
    Project,
    PublicationOperation,
    Repository,
    Run,
    RunEvent,
    SpecVersion,
    Task,
    TaskChatTurn,
    WebSession,
    Worker,
    WorkerEvent,
    Workspace,
    new_id,
)
from .resolver import (
    ReferenceNotFound,
    ResolutionUnavailable,
    SourceReference,
    SourceResolver,
    configured_resolver,
    valid_sha,
)
from .security import redact
from .slack_delivery import enqueue_result
from .task_lifecycle import active_task, lock_run_task, run_stage
from .worker_credentials import rotated_token

WEB_SESSION_PREFIX = "web."
LEASE_SECONDS = 90
WORKER_TOKEN_SECONDS = 24 * 60 * 60
WORKER_ROTATION_GRACE_SECONDS = 60
T = TypeVar("T")
request_secrets: ContextVar[tuple[str, ...]] = ContextVar("request_secrets", default=())


def scrub(value):
    return redact(value, request_secrets.get())


def scrub_project_body(body, known_secrets=()):
    """Validated file-hash maps contain paths/hashes, not credential-name/value pairs."""
    policy = dict(body["policy"])
    manifest = policy.pop("protected_manifest", {})
    cleaned = redact({**body, "policy": policy}, known_secrets)
    cleaned["policy"]["protected_manifest"] = {
        redact(path, known_secrets): value for path, value in manifest.items()
    }
    return cleaned


class DomainError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def owned(session: Session, model: type[T], item_id: str, workspace_id: str) -> T:
    if model is Task:
        return active_task(session, item_id, workspace_id)
    item = session.scalar(
        select(model).where(model.id == item_id, model.workspace_id == workspace_id)
    )
    if item is None:
        raise DomainError(404, "NOT_FOUND")
    return item


def run_view(run: Run) -> dict:
    return {
        "task_status": run_stage(run),
        "run_id": run.id,
        "task_id": run.task_id,
        "spec_version": run.spec_version,
        "status": run.status,
        "phase": run.phase,
        "state_revision": run.state_revision,
        "stop_reason": run.stop_reason,
        "finished_at": run.finished_at,
        "evidence_expired_at": run.evidence_expired_at,
        "base_sha": run.base_sha,
        "snapshot": run.snapshot,
        "budget_microusd": run.budget_microusd,
        "reserved_microusd": run.reserved_microusd,
        "spent_microusd": run.spent_microusd,
        "wait_context": run.wait_context,
        "current_candidate_id": run.current_candidate_id,
        "cost_state": run.cost_state,
        "clarification_only": run.snapshot.get("clarification_only", False),
        "execution_requested": run.snapshot.get("execution_requested", False),
        "budget_version": run.budget_version,
        "available_microusd": run.budget_microusd - run.spent_microusd - run.reserved_microusd,
    }


def event(session: Session, run: Run, kind: str, body: dict) -> float:
    run.state_revision += 1
    created_at = db_now(session)
    fields = dict(
        workspace_id=run.workspace_id,
        run_id=run.id,
        kind=kind,
        body=scrub(body),
        created_at=created_at,
    )
    session.add(RunEvent(**fields, revision=run.state_revision))
    session.add(OutboxEvent(**fields))
    return created_at


def enqueue(
    session: Session,
    run: Run,
    kind: str,
    *,
    depends_on: tuple[str, ...] = (),
    priority: int = 0,
    step_key: str | None = None,
    payload: dict | None = None,
) -> Job:
    now = db_now(session)
    dependencies = [owned(session, Job, dep, run.workspace_id) for dep in depends_on]
    if any(dep.run_id != run.id for dep in dependencies):
        raise DomainError(422, "JOB_DEPENDENCY_RUN_MISMATCH")
    job = Job(
        id=new_id(),
        workspace_id=run.workspace_id,
        run_id=run.id,
        kind=kind,
        step_key=step_key or f"{kind.lower()}:0",
        available_at=now,
        created_at=now,
        priority=priority,
        payload=payload or {},
        state="READY" if all(dep.state == "SUCCEEDED" for dep in dependencies) else "PENDING",
    )
    session.add(job)
    session.flush()
    for dep in dependencies:
        session.add(
            JobDependency(
                workspace_id=run.workspace_id,
                run_id=run.id,
                job_id=job.id,
                depends_on_id=dep.id,
            )
        )
    return job


def transition(
    session: Session,
    run: Run,
    kind: str,
    reason: str | None = None,
    *,
    payload=None,
    step_key=None,
    depends_on=None,
) -> None:
    previous_phase = run.phase
    previous_status = run.status
    decision = decide(
        RunSnapshot(run.status, run.phase, run.stop_reason), DomainEvent(kind, reason)
    )
    run.status, run.phase, run.stop_reason = decision.status, decision.phase, decision.reason
    if decision.next_job:
        previous = session.scalar(
            select(Job.id)
            .where(
                Job.workspace_id == run.workspace_id,
                Job.run_id == run.id,
                Job.kind == previous_phase,
                Job.state == "SUCCEEDED",
            )
            .order_by(Job.created_at.desc(), Job.id)
            .limit(1)
        )
        enqueue(
            session,
            run,
            decision.next_job,
            depends_on=depends_on if depends_on is not None else ((previous,) if previous else ()),
            payload=payload,
            step_key=step_key,
        )
    recorded_at = event(session, run, kind, {"reason": decision.reason, "phase": run.phase})
    if run.status in TERMINAL and previous_status not in TERMINAL:
        run.finished_at = recorded_at
    if run.status != previous_status and run.status in {"FAILED", "BLOCKED"}:
        enqueue_result(session, run)


class Service:
    def __init__(
        self,
        database: Database,
        *,
        resolver: SourceResolver | None = None,
        checkpoint=None,
        storage=None,
    ):
        from .artifacts import ArtifactCommands
        from .candidates import CandidateCommands

        self.db = database
        self.resolver = resolver or configured_resolver()
        self.checkpoint = checkpoint or (lambda name: None)
        self.artifacts = ArtifactCommands(self, storage)
        self.candidates = CandidateCommands(self)
        from .evidence import EvidenceCommands

        self.evidence = EvidenceCommands(self)
        from .gates import GateCommands

        self.gates = GateCommands(self)
        from .plans import PlanCommands

        self.plans = PlanCommands(self)
        from .costs import CostCommands

        self.costs = CostCommands(self)
        from .budgets import BudgetCommands

        self.budgets = BudgetCommands(self)
        from .interventions import InterventionCommands

        self.interventions = InterventionCommands(self)
        from .publications import PublicationCommands

        self.publications = PublicationCommands(self)

    def principal(self, session: Session, token: str, role: str) -> Principal:
        hashed = token_hash(token)
        if token.startswith(WEB_SESSION_PREFIX):
            actor = session.scalar(
                select(Principal)
                .join(WebSession, WebSession.principal_id == Principal.id)
                .where(
                    WebSession.token_hash == hashed,
                    WebSession.expires_at > db_now(session),
                    Principal.active.is_(True),
                    Principal.role == "USER",
                )
                .execution_options(populate_existing=True)
            )
            if actor is None:
                raise DomainError(401, "UNAUTHENTICATED")
            if role != "USER":
                raise DomainError(403, "PRINCIPAL_ROLE_DENIED")
            return actor
        actor = session.scalar(
            select(Principal)
            .where(or_(Principal.token_hash == hashed, Principal.previous_token_hash == hashed))
            .execution_options(populate_existing=True)
        )
        if actor is None or not actor.active:
            raise DomainError(401, "UNAUTHENTICATED")
        expiry = (
            actor.token_expires_at
            if actor.token_hash == hashed
            else actor.previous_token_expires_at
        )
        if expiry is not None and db_now(session) >= expiry:
            raise DomainError(401, "UNAUTHENTICATED")
        if actor.role != role:
            raise DomainError(403, "PRINCIPAL_ROLE_DENIED")
        return actor

    @staticmethod
    def require_user_access(actor: Principal, access: str) -> None:
        if actor.role != "USER" or actor.user_access not in (
            {"ADMIN"} if access == "ADMIN" else {"ADMIN", "OPERATOR"}
        ):
            raise DomainError(403, "USER_ACCESS_DENIED")

    def lock_workspace(self, session: Session, workspace_id: str) -> None:
        # Initial single-slot deployment deliberately serializes tenant mutations.
        # This mutex covers idempotency, worker slots, run slots and reservations.
        # Replace it with finer locks only together with equivalent concurrency tests.
        session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())

    def locked_principal(self, session: Session, token: str, role: str) -> Principal:
        actor = self.principal(session, token, role)
        self.lock_workspace(session, actor.workspace_id)
        # Recheck the credential itself after waiting for revocation or rotation.
        return self.principal(session, token, role)

    @staticmethod
    def execution_control(session, actor, workspace_id):
        if workspace_id != actor.workspace_id:
            raise DomainError(404, "NOT_FOUND")
        workspace = session.get(Workspace, workspace_id)
        return {
            "workspace_id": workspace.id,
            "stopped": workspace.execution_stopped,
            "revision": workspace.execution_stop_revision,
        }

    def set_execution_control(self, session, actor, workspace_id, data):
        current = self.execution_control(session, actor, workspace_id)
        if data.expected_revision != current["revision"]:
            raise DomainError(409, "EXECUTION_CONTROL_REVISION_CONFLICT")
        if data.stopped == current["stopped"]:
            raise DomainError(409, "EXECUTION_CONTROL_ALREADY_SET")
        workspace = session.get(Workspace, workspace_id)
        if not data.stopped and workspace.restore_reconciliation_required:
            unresolved = session.scalar(
                select(PublicationOperation.id)
                .where(
                    PublicationOperation.workspace_id == workspace_id,
                    PublicationOperation.state.in_(["IN_FLIGHT", "UNKNOWN", "RECONCILING"]),
                )
                .limit(1)
            )
            if unresolved:
                raise DomainError(409, "RESTORE_RECONCILIATION_REQUIRED")
            workspace.restore_reconciliation_required = False
        workspace.execution_stopped = data.stopped
        workspace.execution_stop_revision += 1
        if data.stopped:
            dispatched = select(Job.run_id).where(
                Job.workspace_id == workspace_id, Job.state == "DISPATCHING"
            )
            active = list(
                session.scalars(
                    select(Run).where(
                        Run.workspace_id == workspace_id,
                        Run.status.not_in([*TERMINAL, "CANCELLING"]),
                        or_(
                            Run.status == "RUNNING",
                            and_(
                                Run.status == "WAITING_EXTERNAL",
                                Run.phase.in_(["WAIT_CI", "FINAL_GATE_READY", "PUBLISH_UNKNOWN"]),
                            ),
                            Run.id.in_(dispatched),
                        ),
                    )
                )
            )
            for run in active:
                self.cancel(
                    session,
                    actor,
                    run.id,
                    Cancel(expected_run_revision=run.state_revision, reason=data.reason),
                    emergency=True,
                )
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_id=actor.id,
                operation="workspace.execution.stop"
                if data.stopped
                else "workspace.execution.release",
                body={
                    "workspace_id": workspace_id,
                    "reason": data.reason,
                    "before_revision": current["revision"],
                    "after_revision": workspace.execution_stop_revision,
                },
                created_at=db_now(session),
            )
        )
        return self.execution_control(session, actor, workspace_id)

    def read(self, token: str, fn: Callable[[Session, Principal], dict], *, role="USER") -> dict:
        with self.db.transaction() as session:
            actor = self.principal(session, token, role)
            return fn(session, actor)

    def write(
        self,
        token: str,
        role: str,
        operation: str,
        key: str | None,
        body: dict,
        fn: Callable[[Session, Principal], dict],
        *,
        user_access: str = "OPERATOR",
        replay_check: Callable[[dict], None] | None = None,
    ) -> dict:
        with self.db.transaction() as session:
            actor = self.principal(session, token, role)
            if role == "USER":
                self.require_user_access(actor, user_access)
            if not key or len(key) > 200 or redact(key, (token,)) != key:
                raise DomainError(422, "IDEMPOTENCY_KEY_REQUIRED")
            self.lock_workspace(session, actor.workspace_id)
            actor = self.principal(session, token, role)
            if role == "USER":
                self.require_user_access(actor, user_access)
            if role in {"USER", "PUBLISHER"}:
                cleaned = (
                    scrub_project_body(body, (token,))
                    if operation.startswith("projects:")
                    else redact(body, (token,))
                )
                if cleaned != body:
                    raise DomainError(422, "SENSITIVE_INPUT")
            request_hash = digest(body)
            self.check_task_command(session, actor, operation)
            previous = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.workspace_id == actor.workspace_id,
                    IdempotencyRecord.principal_id == actor.id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key == key,
                )
            )
            if previous:
                if replay_check:
                    replay_check(previous.response)
                if previous.request_hash != request_hash:
                    raise DomainError(409, "IDEMPOTENCY_CONFLICT")
                return previous.response
            context = request_secrets.set((token,))
            try:
                response = fn(session, actor)
            finally:
                request_secrets.reset(context)
            session.add(
                IdempotencyRecord(
                    workspace_id=actor.workspace_id,
                    principal_id=actor.id,
                    operation=operation,
                    key=key,
                    request_hash=request_hash,
                    response=response,
                )
            )
            session.add(
                AuditEvent(
                    workspace_id=actor.workspace_id,
                    actor_id=actor.id,
                    operation=operation,
                    body={"request_hash": request_hash, "response_hash": digest(response)},
                    created_at=db_now(session),
                )
            )
            session.flush()
            self.checkpoint("write.before_commit")
            return response

    @staticmethod
    def check_task_command(session, actor, operation):
        # Validate the parent even when a pre-deletion idempotency receipt exists.
        parts = operation.split(":")
        if len(parts) < 2:
            return
        kind, identifier = parts[:2]
        if kind in {"runs", "specs"} and len(parts) == 2:
            active_task(session, identifier, actor.workspace_id, lock=True)
        elif kind in {"chat", "attention", "cancel", "resolve-blocked", "runs"}:
            run = owned(session, Run, identifier, actor.workspace_id)
            lock_run_task(session, run)
        elif kind == "approval":
            request = owned(session, ApprovalRequest, identifier, actor.workspace_id)
            run = owned(session, Run, request.run_id, actor.workspace_id)
            lock_run_task(session, run)

    def create_project(self, session: Session, actor: Principal, data: ProjectCreate) -> dict:
        project = Project(
            workspace_id=actor.workspace_id,
            name=data.name,
            policy=scrub_project_body(data.model_dump(), request_secrets.get())["policy"],
        )
        session.add(project)
        session.flush()
        return {"project_id": project.id}

    def create_repository(
        self,
        session: Session,
        actor: Principal,
        project_id: str,
        data: RepositoryCreate,
    ) -> dict:
        owned(session, Project, project_id, actor.workspace_id)
        if data.remote_url:
            from .source_runner import SourcePolicy, SourcePolicyDenied

            try:
                SourcePolicy(
                    tuple(filter(None, os.environ.get("FACTORY_ALLOWED_REMOTES", "").split(",")))
                ).authorize(data.remote_url)
            except SourcePolicyDenied:
                raise DomainError(422, "SOURCE_REPOSITORY_NOT_ALLOWED") from None
        repo = Repository(
            workspace_id=actor.workspace_id, project_id=project_id, **data.model_dump()
        )
        session.add(repo)
        session.flush()
        return {"repository_id": repo.id}

    def check_spec(
        self,
        session: Session,
        actor: Principal,
        project_id: str,
        spec: TaskSpec,
    ) -> None:
        project = owned(session, Project, project_id, actor.workspace_id)
        repo = owned(session, Repository, spec.task.repository_id, actor.workspace_id)
        if repo.project_id != project.id:
            raise DomainError(404, "NOT_FOUND")
        policy = ProjectPolicy.model_validate(project.policy)
        if spec.verification.profile_id != policy.verification.profile_id:
            raise DomainError(422, "VERIFICATION_PROFILE_MISMATCH")
        if not set(policy.verification.required_checks).issubset(spec.verification.required_checks):
            raise DomainError(422, "REQUIRED_CHECK_REMOVAL")

    def update_project_policy(self, session, actor, project_id: str, data: ProjectPolicyUpdate):
        project = owned(session, Project, project_id, actor.workspace_id)
        if digest(project.policy) != data.expected_policy_hash:
            raise DomainError(409, "PROJECT_POLICY_STALE")
        body = {"name": project.name, "policy": data.policy.model_dump()}
        project.policy = scrub_project_body(body, request_secrets.get())["policy"]
        session.add(
            AuditEvent(
                workspace_id=actor.workspace_id,
                actor_id=actor.id,
                operation="project.policy.update",
                body={
                    "project_id": project.id,
                    "previous_hash": data.expected_policy_hash,
                    "policy_hash": digest(project.policy),
                },
                created_at=db_now(session),
            )
        )
        return {"project_id": project.id, "policy_hash": digest(project.policy)}

    def create_task(
        self,
        session: Session,
        actor: Principal,
        project_id: str,
        spec: TaskSpec,
    ) -> dict:
        self.check_spec(session, actor, project_id, spec)
        task = Task(
            workspace_id=actor.workspace_id,
            project_id=project_id,
            repository_id=spec.task.repository_id,
            title=spec.task.title,
        )
        session.add(task)
        session.flush()
        return self.add_spec(session, task, spec, 1)

    def add_spec(self, session: Session, task: Task, spec: TaskSpec, version: int) -> dict:
        body = spec.model_dump()
        if scrub(body) != body:
            raise DomainError(422, "SENSITIVE_INPUT")
        session.add(
            SpecVersion(
                workspace_id=task.workspace_id,
                task_id=task.id,
                version=version,
                body=body,
                spec_hash=digest(body),
            )
        )
        task.latest_spec_version = version
        task.retention_touched_at = db_now(session)
        return {"task_id": task.id, "spec_version": version, "spec_hash": digest(body)}

    def delete_task_request(self, token, task_id):
        # The Workspace mutex is also held by Run starts, chat claims and human
        # interventions. Keep the existing lock order: Workspace, Task, Run/chat.
        with self.db.transaction() as session:
            actor = self.locked_principal(session, token, "USER")
            self.require_user_access(actor, "OPERATOR")
            task = session.scalar(
                select(Task)
                .where(Task.id == task_id, Task.workspace_id == actor.workspace_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise DomainError(404, "NOT_FOUND")
            if task.deleted_at is not None:
                return {"task_id": task.id, "deleted_at": task.deleted_at}
            active_run = session.scalar(
                select(Run.id)
                .where(
                    Run.workspace_id == actor.workspace_id,
                    Run.task_id == task.id,
                    Run.status.in_(["QUEUED", "RUNNING", "WAITING_EXTERNAL", "CANCELLING"]),
                )
                .limit(1)
            )
            if active_run is not None:
                raise DomainError(409, "TASK_DELETE_ACTIVE_RUN")
            now = db_now(session)
            active_chat = session.scalar(
                select(TaskChatTurn.id)
                .join(
                    Run,
                    (Run.id == TaskChatTurn.run_id)
                    & (Run.workspace_id == TaskChatTurn.workspace_id),
                )
                .where(
                    Run.workspace_id == actor.workspace_id,
                    Run.task_id == task.id,
                    TaskChatTurn.status.in_(["QUEUED", "RUNNING"]),
                    TaskChatTurn.deadline_at > now,
                )
                .limit(1)
            )
            if active_chat is not None:
                raise DomainError(409, "TASK_DELETE_ACTIVE_CHAT")
            task.deleted_at, task.deleted_by = now, actor.id
            session.add(
                AuditEvent(
                    workspace_id=actor.workspace_id,
                    actor_id=actor.id,
                    operation="task.delete",
                    body={"task_id": task.id, "deleted_at": now},
                    created_at=now,
                )
            )
            session.flush()
            self.checkpoint("task.deleted")
            return {"task_id": task.id, "deleted_at": now}

    def new_spec(
        self,
        session: Session,
        actor: Principal,
        task_id: str,
        spec: TaskSpec,
    ) -> dict:
        task = active_task(session, task_id, actor.workspace_id, lock=True)
        self.check_spec(session, actor, task.project_id, spec)
        if spec.task.repository_id != task.repository_id:
            raise DomainError(422, "REPOSITORY_CHANGE_REQUIRES_NEW_TASK")
        task.clarified_spec_version = None
        return self.add_spec(session, task, spec, task.latest_spec_version + 1)

    def start_run_request(self, token: str, task_id: str, data: RunStart, key: str | None) -> dict:
        operation = f"runs:{task_id}"
        # Preserve request hashes for Run starts recorded before this option existed.
        body = data.model_dump(
            exclude={
                name for name in ("reuse_existing", "clarification_only") if not getattr(data, name)
            }
        )
        with self.db.transaction() as session:
            actor = self.principal(session, token, "USER")
            self.require_user_access(actor, "OPERATOR")
            if not key or len(key) > 200 or redact(key, (token,)) != key:
                raise DomainError(422, "IDEMPOTENCY_KEY_REQUIRED")
            previous = session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.workspace_id == actor.workspace_id,
                    IdempotencyRecord.principal_id == actor.id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key == key,
                )
            )
            active_task(session, task_id, actor.workspace_id)
            if previous:
                if previous.request_hash != digest(body):
                    raise DomainError(409, "IDEMPOTENCY_CONFLICT")
                return previous.response
            task = owned(session, Task, task_id, actor.workspace_id)
            spec = session.scalar(
                select(SpecVersion).where(
                    SpecVersion.workspace_id == actor.workspace_id,
                    SpecVersion.task_id == task.id,
                    SpecVersion.version == data.spec_version,
                )
            )
            if spec is None:
                raise DomainError(404, "SPEC_NOT_FOUND")
            if not data.clarification_only and not spec.body["task"]["acceptance_criteria"]:
                raise DomainError(409, "SPEC_ACCEPTANCE_CRITERIA_REQUIRED")
            repo = owned(session, Repository, task.repository_id, actor.workspace_id)
            if not repo.active:
                raise DomainError(403, "REPOSITORY_ACCESS_REVOKED")
            reference = SourceReference(
                repo.id,
                spec.body["task"]["base_ref"],
                repo.base_ref,
                repo.base_sha,
                repo.remote_url,
            )
            workspace_id, actor_id = actor.workspace_id, actor.id
        # No connection, transaction, or lock survives source resolution.
        try:
            source_details = None
            if reference.remote_url and hasattr(self.resolver, "resolve_details"):
                source_details = self.resolver.resolve_details(reference)
                resolved_sha = source_details["sha"]
            else:
                resolved_sha = self.resolver.resolve(reference)
            if not valid_sha(resolved_sha):
                raise DomainError(422, "INVALID_BASE_SHA")
        except (ReferenceNotFound, ResolutionUnavailable, DomainError) as error:
            code = (
                error.code
                if isinstance(error, DomainError)
                else (
                    getattr(error, "code", "BASE_REF_NOT_REGISTERED")
                    if isinstance(error, ReferenceNotFound)
                    else "SOURCE_RESOLUTION_UNAVAILABLE"
                )
            )
            with self.db.transaction() as session:
                self.lock_workspace(session, workspace_id)
                session.add(
                    AuditEvent(
                        workspace_id=workspace_id,
                        actor_id=actor_id,
                        operation="source.resolve.failed",
                        body={"task_id": task_id, "code": code},
                        created_at=db_now(session),
                    )
                )
            raise DomainError(
                503 if isinstance(error, ResolutionUnavailable) else 422, code
            ) from None
        return self.write(
            token,
            "USER",
            operation,
            key,
            body,
            lambda session, actor: self.start_run(
                session, actor, task_id, data, resolved_sha, source_details
            ),
        )

    def start_run(
        self,
        session: Session,
        actor: Principal,
        task_id: str,
        data: RunStart,
        resolved_sha: str | None = None,
        source_details: dict | None = None,
    ) -> dict:
        task = active_task(session, task_id, actor.workspace_id, lock=True)
        version = session.scalar(
            select(SpecVersion).where(
                SpecVersion.workspace_id == actor.workspace_id,
                SpecVersion.task_id == task.id,
                SpecVersion.version == data.spec_version,
            )
        )
        if version is None:
            raise DomainError(404, "SPEC_NOT_FOUND")
        spec = TaskSpec.model_validate(version.body)
        if not data.clarification_only and not spec.task.acceptance_criteria:
            raise DomainError(409, "SPEC_ACCEPTANCE_CRITERIA_REQUIRED")
        self.check_spec(session, actor, task.project_id, spec)
        repo = owned(session, Repository, task.repository_id, actor.workspace_id)
        if not repo.active:
            raise DomainError(403, "REPOSITORY_ACCESS_REVOKED")
        if not repo.remote_url and repo.base_ref != spec.task.base_ref:
            raise DomainError(422, "BASE_REF_NOT_REGISTERED")
        project = owned(session, Project, task.project_id, actor.workspace_id)
        policy = ProjectPolicy.model_validate(project.policy)
        if data.reuse_existing:
            existing = session.scalar(
                select(Run)
                .where(
                    Run.workspace_id == actor.workspace_id,
                    Run.task_id == task.id,
                    Run.spec_version == version.version,
                    Run.snapshot["workflow_type"].as_string() == data.strategy,
                    Run.snapshot["policy_hash"].as_string() == digest(project.policy),
                    (Run.snapshot["clarification_only"].as_boolean().is_(True))
                    if data.clarification_only
                    else (Run.snapshot["clarification_only"].as_boolean().is_not(True)),
                )
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(1)
            )
            if existing:
                return run_view(existing)
        base_sha = resolved_sha if resolved_sha is not None else repo.base_sha
        if not valid_sha(base_sha):
            raise DomainError(422, "INVALID_BASE_SHA")
        execution = spec.execution.model_dump()
        for name in ("max_infra_attempts_per_job", "max_job_execution_seconds"):
            execution[name] = min(execution[name], getattr(policy, name))
        snapshot = {
            "task_spec": version.body,
            "task_spec_hash": version.spec_hash,
            "repository_id": repo.id,
            "base_sha": base_sha,
            "workflow_type": data.strategy,
            "workflow_version": 1,
            "policy": project.policy,
            "policy_hash": digest(project.policy),
            "verification_profile": spec.verification.model_dump(),
            "verification_profile_hash": digest(spec.verification.model_dump()),
            "execution": execution,
            "agent_adapter": policy.agent_adapter,
            "agent_configuration": policy.agent_configuration,
            "execution_image_digest": policy.execution_image_digest,
            "simulation": policy.agent_adapter == "fake",
            "clarification_only": data.clarification_only,
            "execution_requested": not data.clarification_only,
        }
        if policy.verification_commands or policy.required_ci_checks:
            snapshot["verification_profile"] = {
                **spec.verification.model_dump(),
                "schema_version": 1,
                "commands": [command.model_dump() for command in policy.verification_commands],
                "protected_manifest": policy.protected_manifest,
                "required_ci_checks": [check.model_dump() for check in policy.required_ci_checks],
                "review_check_ids": [
                    name
                    for name in spec.verification.required_checks
                    if name in {"review", "acceptance_review"}
                ],
            }
            snapshot["verification_profile_hash"] = digest(snapshot["verification_profile"])
        if repo.remote_url:
            snapshot["source"] = {"remote_url": repo.remote_url, "base_sha": base_sha}
            if source_details:
                snapshot["source"].update(
                    {
                        name: source_details[name]
                        for name in (
                            "tree",
                            "source_digest",
                            "symlink_paths",
                            "image_digest",
                            "source_origin",
                        )
                        if name in source_details
                    }
                )
        task.retention_touched_at = db_now(session)
        run = Run(
            workspace_id=actor.workspace_id,
            task_id=task.id,
            spec_version=version.version,
            repository_id=repo.id,
            base_sha=base_sha,
            snapshot=snapshot,
            budget_microusd=policy.run_budget_microusd,
            created_at=db_now(session),
        )
        session.add(run)
        session.flush()
        self.costs.initial_budget(session, run)
        event(session, run, "RUN_COMPILED", {"snapshot_hash": digest(snapshot)})
        if not spec.task.acceptance_criteria:
            # A draft starts a specification interview before an executable Plan exists.
            body = {
                "units": [],
                "assumptions": [],
                "open_questions": ["このタスクで実現したい利用場面や振る舞いは何ですか？"],
            }
            session.add(
                PlanVersion(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    version=1,
                    body=body,
                    body_hash=digest(body),
                    validation_error="PLAN_HAS_OPEN_QUESTIONS",
                )
            )
            session.flush()
            transition(session, run, "BLOCKED", "PLAN_HAS_OPEN_QUESTIONS")
        else:
            enqueue(session, run, "PLAN")
        return run_view(run)

    def repository_allowed(self, session: Session, run: Run) -> bool:
        task = session.get(Task, run.task_id)
        if task is None or task.deleted_at is not None:
            return False
        repo = owned(session, Repository, run.repository_id, run.workspace_id)
        if repo.active:
            return True
        if run.status not in TERMINAL and run.status != RunStatus.CANCELLING:
            if run.stop_reason != "REPOSITORY_ACCESS_REVOKED":
                transition(session, run, "BLOCKED", "REPOSITORY_ACCESS_REVOKED")
        return False

    def refresh_dependencies(self, session: Session, workspace_id: str) -> None:
        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.workspace_id == workspace_id,
                    Job.state.in_(["PENDING", "READY", "RETRY_WAIT"]),
                )
            )
        )
        for job in jobs:
            dependencies = list(
                session.scalars(
                    select(Job)
                    .join(
                        JobDependency,
                        (JobDependency.workspace_id == Job.workspace_id)
                        & (JobDependency.depends_on_id == Job.id),
                    )
                    .where(
                        JobDependency.workspace_id == workspace_id, JobDependency.job_id == job.id
                    )
                )
            )
            if any(dep.state != "SUCCEEDED" for dep in dependencies):
                job.state = "PENDING"
            elif job.state == "PENDING":
                job.state = "READY"

    def issue_worker_enrollment(self, token, workspace_id, data):
        with self.db.transaction() as session:
            actor = self.locked_principal(session, token, "USER")
            self.require_user_access(actor, "ADMIN")
            if workspace_id != actor.workspace_id:
                raise DomainError(404, "NOT_FOUND")
            now = db_now(session)
            secret = issue_token()
            enrollment = Enrollment(
                workspace_id=workspace_id,
                token_hash=token_hash(secret),
                capabilities=[*data.kinds, f"adapter:{data.adapter}"]
                + (["usage:trusted"] if data.usage_authority else []),
                managed=data.managed,
                expires_at=now + data.ttl_seconds,
            )
            session.add(enrollment)
            session.flush()
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_id=actor.id,
                    operation="worker.enrollment.issued",
                    body={"enrollment_id": enrollment.id, "expires_at": enrollment.expires_at},
                    created_at=now,
                )
            )
            return {"enrollment_token": secret, "expires_at": enrollment.expires_at}

    def revoke_worker(self, session, actor, workspace_id, worker_id, reason):
        if workspace_id != actor.workspace_id:
            raise DomainError(404, "NOT_FOUND")
        owned(session, Worker, worker_id, workspace_id)
        principal = owned(session, Principal, worker_id, workspace_id)
        before = principal.active
        before_revision = principal.credential_revision
        if before:
            principal.active = False
            principal.credential_revision += 1
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_id=actor.id,
                operation="worker.credential.revoke",
                body={
                    "worker_id": worker_id,
                    "before_active": before,
                    "after_active": False,
                    "before_revision": before_revision,
                    "after_revision": principal.credential_revision,
                    "reason": reason,
                },
                created_at=db_now(session),
            )
        )
        return {"worker_id": worker_id, "revoked": True}

    def enroll(self, enrollment_token: str, name: str) -> dict:
        with self.db.transaction() as session:
            enrollment = session.scalar(
                select(Enrollment).where(Enrollment.token_hash == token_hash(enrollment_token))
            )
            if enrollment is None:
                raise DomainError(401, "ENROLLMENT_INVALID")
            self.lock_workspace(session, enrollment.workspace_id)
            session.refresh(enrollment)
            now = db_now(session)
            if enrollment.consumed_at is not None or enrollment.expires_at <= now:
                raise DomainError(401, "ENROLLMENT_INVALID")
            secret, worker_id = issue_token(), new_id()
            session.add(
                Principal(
                    id=worker_id,
                    workspace_id=enrollment.workspace_id,
                    role="WORKER",
                    token_hash=token_hash(secret),
                    token_expires_at=now + WORKER_TOKEN_SECONDS,
                )
            )
            session.flush()
            session.add(
                Worker(
                    id=worker_id,
                    workspace_id=enrollment.workspace_id,
                    name=name,
                    capabilities=enrollment.capabilities,
                    managed=enrollment.managed,
                )
            )
            enrollment.consumed_at = now
            return {
                "worker_id": worker_id,
                "worker_token": secret,
                "token_expires_at": now + WORKER_TOKEN_SECONDS,
                "protocol_version": 1,
            }

    def worker_credential_metadata(self, session, actor, token):
        current = actor.token_hash == token_hash(token)
        expires = actor.token_expires_at if current else actor.previous_token_expires_at
        return {
            "worker_id": actor.id,
            "current": current,
            "expires_in_seconds": max(0, expires - db_now(session)) if expires is not None else 0,
        }

    def rotate_worker_token(self, token: str, key: str | None) -> dict:
        with self.db.transaction() as session:
            actor = self.locked_principal(session, token, "WORKER")
            if not key or len(key) > 200 or redact(key, (token,)) != key:
                raise DomainError(422, "IDEMPOTENCY_KEY_REQUIRED")
            # A lost response can be replayed during grace without storing either secret.
            secret = rotated_token(token, key)
            if actor.token_hash != token_hash(token):
                if actor.token_hash != token_hash(secret):
                    raise DomainError(409, "WORKER_ROTATION_REQUIRES_CURRENT_TOKEN")
                return self.worker_credential(actor, secret)
            now = db_now(session)
            if (actor.previous_token_expires_at or 0) > now:
                raise DomainError(409, "WORKER_ROTATION_GRACE_ACTIVE")
            before_revision = actor.credential_revision
            actor.credential_revision += 1
            actor.previous_token_hash = actor.token_hash
            actor.previous_token_expires_at = now + WORKER_ROTATION_GRACE_SECONDS
            actor.token_hash = token_hash(secret)
            actor.token_expires_at = now + WORKER_TOKEN_SECONDS
            session.add(
                AuditEvent(
                    workspace_id=actor.workspace_id,
                    actor_id=actor.id,
                    operation="worker.credential.rotate",
                    body={
                        "worker_id": actor.id,
                        "before_revision": before_revision,
                        "after_revision": actor.credential_revision,
                        "previous_token_expires_at": actor.previous_token_expires_at,
                        "token_expires_at": actor.token_expires_at,
                    },
                    created_at=now,
                )
            )
            return self.worker_credential(actor, secret)

    @staticmethod
    def worker_credential(actor: Principal, secret: str) -> dict:
        return {
            "worker_id": actor.id,
            "worker_token": secret,
            "token_expires_at": actor.token_expires_at,
            "previous_token_expires_at": actor.previous_token_expires_at,
        }

    def claim(self, token: str) -> dict | None:
        with self.db.transaction() as session:
            actor = self.locked_principal(session, token, "WORKER")
            worker = owned(session, Worker, actor.id, actor.workspace_id)
            self.reconcile_workspace(session, actor.workspace_id)
            if session.get(Workspace, actor.workspace_id).execution_stopped:
                return None
            self.refresh_dependencies(session, actor.workspace_id)
            now = db_now(session)
            busy = session.scalar(
                select(Attempt.id)
                .where(
                    Attempt.workspace_id == actor.workspace_id,
                    Attempt.worker_id == actor.id,
                    Attempt.state == "LEASED",
                )
                .limit(1)
            )
            if busy or session.scalar(
                select(TaskChatTurn.id)
                .where(
                    TaskChatTurn.workspace_id == actor.workspace_id,
                    TaskChatTurn.worker_id == actor.id,
                    TaskChatTurn.status == "RUNNING",
                    TaskChatTurn.deadline_at > now,
                )
                .limit(1)
            ):
                return None
            jobs = session.scalars(
                select(Job)
                .where(
                    Job.workspace_id == actor.workspace_id,
                    Job.state.in_(["READY", "RETRY_WAIT"]),
                    Job.available_at <= now,
                )
                .order_by(Job.priority.desc(), Job.available_at, Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
            )
            for job in jobs:
                if job.kind == "PUBLISH_DRAFT":
                    continue  # Dedicated Publisher credentials never belong to a Worker.
                run = owned(session, Run, job.run_id, actor.workspace_id)
                if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    continue
                if not self.repository_allowed(session, run):
                    continue
                lock_run_task(session, run)
                adapter = run.snapshot["agent_adapter"]
                if (
                    job.kind not in worker.capabilities
                    or f"adapter:{adapter}" not in worker.capabilities
                ):
                    continue
                if job.kind in {"ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"} and not worker.managed:
                    continue
                run_busy = session.scalar(
                    select(Job.id)
                    .where(
                        Job.workspace_id == run.workspace_id,
                        Job.run_id == run.id,
                        Job.state == "LEASED",
                    )
                    .limit(1)
                )
                if run_busy:
                    continue
                execution = run.snapshot["execution"]
                if job.attempt_no >= execution["max_infra_attempts_per_job"]:
                    job.state = "FAILED"
                    transition(session, run, "ATTEMPTS_EXHAUSTED")
                    continue
                reservation = run.snapshot["policy"]["job_reservation_microusd"]
                self.costs.require_balanced(session, run)
                if run.spent_microusd + run.reserved_microusd + reservation > run.budget_microusd:
                    transition(session, run, "BLOCKED", "BUDGET_EXHAUSTED")
                    continue
                job.attempt_no += 1
                job.fence += 1
                job.state = "LEASED"
                deadline = now + execution["max_job_execution_seconds"]
                attempt = Attempt(
                    workspace_id=actor.workspace_id,
                    job_id=job.id,
                    worker_id=actor.id,
                    attempt_no=job.attempt_no,
                    fence=job.fence,
                    started_at=now,
                    expires_at=min(now + LEASE_SECONDS, deadline),
                    deadline_at=deadline,
                    heartbeat_at=now,
                    reservation_microusd=reservation,
                )
                session.add(attempt)
                session.flush()
                self.checkpoint("claim.attempt")
                self.costs.reserve(session, run, attempt)
                session.flush()
                self.checkpoint("claim.reservation")
                run.status = RunStatus.RUNNING
                event(
                    session,
                    run,
                    "JOB_CLAIMED",
                    {
                        "job_id": job.id,
                        "attempt_no": job.attempt_no,
                        "worker_id": actor.id,
                    },
                )
                session.add(
                    AuditEvent(
                        workspace_id=actor.workspace_id,
                        actor_id=actor.id,
                        operation="job.claim",
                        body={"job_id": job.id, "attempt_id": attempt.id, "fence": attempt.fence},
                        created_at=db_now(session),
                    )
                )
                session.flush()
                self.checkpoint("claim.event")
                plan_query = (
                    select(PlanVersion)
                    .where(
                        PlanVersion.workspace_id == run.workspace_id,
                        PlanVersion.run_id == run.id,
                    )
                    .order_by(PlanVersion.version.desc())
                )
                if job.payload.get("plan_id"):
                    plan_query = plan_query.where(PlanVersion.id == job.payload["plan_id"])
                plan = session.scalar(plan_query.limit(1))
                if job.payload.get("plan_id") and (
                    plan is None
                    or plan.validation_error is not None
                    or plan.body_hash != job.payload.get("plan_hash", plan.body_hash)
                ):
                    raise DomainError(409, "JOB_PLAN_CONTEXT_INVALID")
                attempt.input_digest = digest(
                    {
                        "snapshot": run.snapshot,
                        "kind": job.kind,
                        "plan": plan.body if plan else None,
                        "input": job.payload,
                    }
                )
                return {
                    "protocol_version": 1,
                    "job": {
                        "id": job.id,
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "kind": job.kind,
                        "spec_version": run.spec_version,
                        "repository_id": run.repository_id,
                        "base_sha": run.base_sha,
                        "execution": {"timeout_seconds": execution["max_job_execution_seconds"]},
                        "snapshot": run.snapshot,
                        "plan": plan.body if plan else None,
                        "input": job.payload,
                    },
                    "lease": {
                        "id": attempt.lease_id,
                        "fence": attempt.fence,
                        "expires_in_seconds": max(0, attempt.expires_at - db_now(session)),
                    },
                }
            return None

    def lease(
        self,
        session: Session,
        actor: Principal,
        lease_id: str,
        fence: int,
    ) -> tuple[Attempt, Job, Run]:
        attempt = session.scalar(
            select(Attempt).where(
                Attempt.workspace_id == actor.workspace_id,
                Attempt.lease_id == lease_id,
                Attempt.worker_id == actor.id,
            )
        )
        if attempt is None:
            raise DomainError(404, "NOT_FOUND")
        job = owned(session, Job, attempt.job_id, actor.workspace_id)
        run = owned(session, Run, job.run_id, actor.workspace_id)
        if fence != attempt.fence or fence != job.fence or attempt.attempt_no != job.attempt_no:
            raise DomainError(409, "STALE_FENCE")
        if attempt.state != "LEASED" or attempt.expires_at <= db_now(session):
            raise DomainError(409, "LEASE_EXPIRED")
        return attempt, job, run

    def heartbeat(self, token: str, lease_id: str, fence: int) -> dict:
        with self.db.transaction() as session:
            actor = self.locked_principal(session, token, "WORKER")
            attempt, _, run = self.lease(session, actor, lease_id, fence)
            now = db_now(session)
            attempt.heartbeat_at = now
            cancelling = run.status == RunStatus.CANCELLING or not self.repository_allowed(
                session, run
            )
            if not cancelling:
                attempt.expires_at = min(now + LEASE_SECONDS, attempt.deadline_at)
            return {
                "lease_expires_in_seconds": max(0, attempt.expires_at - db_now(session)),
                "cancel_requested": cancelling,
            }

    def fetch_source(self, token: str, lease_id: str, fence: int) -> dict:
        def authorize():
            with self.db.transaction() as session:
                actor = self.principal(session, token, "WORKER")
                _, _, run = self.artifacts.active(session, actor, lease_id, fence)
                source = run.snapshot.get("source")
                if not source:
                    raise DomainError(409, "SOURCE_NOT_CONFIGURED")
                return dict(source)

        source = authorize()
        if not hasattr(self.resolver, "fetch"):
            raise DomainError(503, "SOURCE_RUNNER_UNAVAILABLE")
        try:
            result = self.resolver.fetch(source["remote_url"], source["base_sha"])
        except ReferenceNotFound as error:
            raise DomainError(409, getattr(error, "code", "SOURCE_FETCH_REJECTED")) from None
        except ResolutionUnavailable:
            raise DomainError(503, "SOURCE_FETCH_UNAVAILABLE") from None
        # Revocation, cancellation, generation and deadline are checked after the remote wait too.
        authorize()
        if result.get("sha") != source["base_sha"]:
            raise DomainError(409, "SOURCE_SHA_MISMATCH")
        if source.get("source_digest") and result.get("source_digest") != source["source_digest"]:
            raise DomainError(409, "SOURCE_DIGEST_MISMATCH")
        return result

    def settle(
        self, session, run: Run, attempt: Attempt, usage: int | None, *, uncertain: bool = False
    ) -> bool:
        return self.costs.settle(session, run, attempt, usage, uncertain=uncertain)

    def complete(
        self,
        session: Session,
        actor: Principal,
        lease_id: str,
        data: Complete,
    ) -> dict:
        attempt, job, run = self.lease(session, actor, lease_id, data.fence)
        allowed = self.repository_allowed(session, run)
        if not allowed:
            self.settle(session, run, attempt, data.usage_microusd)
            attempt.state, job.state = "CANCELLED", "CANCELLED"
            attempt.stop_confirmation = "WORKER_REPORTED_STOP"
            attempt.result = scrub(data.model_dump())
            if run.status == RunStatus.CANCELLING:
                self.finish_cancellation(session, run)
            return {"accepted": True, "attempt_id": attempt.id, "run": run_view(run)}
        if run.status not in {RunStatus.RUNNING, RunStatus.CANCELLING}:
            raise DomainError(409, "INVALID_RUN_STATE")
        artifacts = self.artifacts.validate_completion(session, actor, attempt, data.artifact_ids)
        if data.outcome == "CANCELLED" and run.status != RunStatus.CANCELLING:
            raise DomainError(409, "CANCELLATION_NOT_REQUESTED")
        report = None
        if run.status != RunStatus.CANCELLING:
            phase = "VERIFY_REVIEW" if job.kind in {"VERIFY", "REVIEW"} else job.kind
            if run.phase != phase:
                raise DomainError(409, "JOB_PHASE_MISMATCH")
            if job.kind == "ASSEMBLE_CANDIDATE":
                report = self.candidates.validate(
                    session, actor, attempt, job, run, data.result, data.artifact_ids
                )
            elif job.kind in {"VERIFY", "REVIEW"}:
                report = self.evidence.validate(
                    session, actor, attempt, job, run, data.result, data.artifact_ids
                )
        usage_known = self.settle(session, run, attempt, data.usage_microusd)
        clean_result = scrub(data.result)
        attempt.result = scrub(data.model_dump())
        attempt.stop_confirmation = "WORKER_REPORTED_STOP"
        if run.status == RunStatus.CANCELLING:
            attempt.state, job.state = "CANCELLED", "CANCELLED"
            self.finish_cancellation(session, run)
        else:
            attempt.state, job.state = "SUCCEEDED", "SUCCEEDED"
            if not usage_known:
                job.state = "BLOCKED"
                run.wait_context = {
                    "kind": "COST_BLOCKED",
                    "job_id": job.id,
                    "cause": "USAGE_UNKNOWN",
                }
                transition(session, run, "BLOCKED", "USAGE_UNKNOWN")
            elif run.spent_microusd > run.budget_microusd:
                job.state = "BLOCKED"
                run.wait_context = {
                    "kind": "COST_BLOCKED",
                    "job_id": job.id,
                    "cause": "BUDGET_EXCEEDED",
                }
                transition(session, run, "BLOCKED", "BUDGET_EXCEEDED")
            elif job.kind == "PLAN":
                self.plans.complete(session, actor, attempt, job, run, clean_result)
            elif job.kind in {"IMPLEMENT", "REPAIR"}:
                patches = [artifact for artifact in artifacts if artifact.kind == "PATCH"]
                if len(patches) > 1:
                    raise DomainError(409, "SINGLE_PATCH_REQUIRED")
                if not patches:
                    if job.kind == "REPAIR":
                        transition(session, run, "BLOCKED", "CHANGE_ARTIFACT_MISSING")
                    else:
                        transition(session, run, "IMPLEMENT_EXECUTED")
                else:
                    payload = {
                        "patch_artifact_id": patches[0].id,
                        "input_artifact_ids": [patches[0].id],
                        "parent_sha": run.base_sha,
                    }
                    if job.payload.get("plan_id"):
                        payload["plan_id"] = job.payload["plan_id"]
                    if job.kind == "REPAIR":
                        payload |= {
                            key: job.payload[key]
                            for key in (
                                "candidate_id",
                                "candidate",
                                "source_candidate_id",
                                "source_artifact_id",
                                "parent_sha",
                                "repair_round",
                            )
                        }
                        payload["repair_job_id"] = job.id
                        payload["input_artifact_ids"].append(job.payload["source_artifact_id"])
                    transition(
                        session,
                        run,
                        "CHANGE_ARTIFACT_READY",
                        payload=payload,
                        step_key=(
                            f"assemble_candidate:{job.payload['repair_round']}"
                            if job.kind == "REPAIR"
                            else None
                        ),
                    )
            elif job.kind == "ASSEMBLE_CANDIDATE":
                if (
                    job.payload.get("source_candidate_id")
                    and report.source_digest == job.payload["candidate"]["source_digest"]
                ):
                    transition(session, run, "BLOCKED", "NO_CODE_CHANGE")
                else:
                    transition(session, run, "CANDIDATE_ASSEMBLED")
                    self.candidates.create(session, actor, attempt, job, run, report)
            elif job.kind in {"VERIFY", "REVIEW"}:
                self.evidence.save(session, actor, attempt, job, run, report)
            else:
                raise DomainError(409, "UNSUPPORTED_COMPLETION_KIND")
        return {"accepted": True, "attempt_id": attempt.id, "run": run_view(run)}

    def retry_or_fail(self, session: Session, job: Job, run: Run) -> None:
        if job.attempt_no >= run.snapshot["execution"]["max_infra_attempts_per_job"]:
            job.state = "FAILED"
            transition(session, run, "ATTEMPTS_EXHAUSTED")
        else:
            job.state = "RETRY_WAIT"
            job.available_at = db_now(session) + min(2**job.attempt_no, 30)
            run.status = RunStatus.QUEUED
            event(session, run, "INFRA_RETRY_SCHEDULED", {"job_id": job.id})

    def fail(self, session: Session, actor: Principal, lease_id: str, data: Fail) -> dict:
        attempt, job, run = self.lease(session, actor, lease_id, data.fence)
        attempt.state = "FAILED"
        attempt.result = scrub(data.model_dump())
        stop_unconfirmed = data.reason == "STOP_UNCONFIRMED"
        attempt.stop_confirmation = (
            "STOP_UNCONFIRMED" if stop_unconfirmed else "WORKER_REPORTED_STOP"
        )
        if stop_unconfirmed:
            self.revoke_worker(session, actor, actor.workspace_id, actor.id, data.reason)
        usage_known = self.settle(session, run, attempt, data.usage_microusd)
        allowed = self.repository_allowed(session, run)
        if run.status == RunStatus.CANCELLING:
            job.state = "CANCELLED"
            self.finish_cancellation(session, run)
        elif not allowed:
            job.state = "CANCELLED"
        elif stop_unconfirmed:
            job.state = "FAILED"
            transition(session, run, "BLOCKED", "STOP_UNCONFIRMED")
        elif not usage_known:
            job.state = "BLOCKED"
            run.wait_context = {"kind": "COST_BLOCKED", "job_id": job.id, "cause": "USAGE_UNKNOWN"}
            transition(session, run, "BLOCKED", "USAGE_UNKNOWN")
        elif run.spent_microusd > run.budget_microusd:
            job.state = "FAILED"
            run.wait_context = {
                "kind": "COST_BLOCKED",
                "job_id": job.id,
                "cause": "BUDGET_EXCEEDED",
            }
            transition(session, run, "BLOCKED", "BUDGET_EXCEEDED")
        elif data.category == "INFRASTRUCTURE":
            self.retry_or_fail(session, job, run)
        elif data.category == "POLICY" and job.kind == "ASSEMBLE_CANDIDATE":
            job.state = "FAILED"
            reason = (
                data.reason
                if data.reason in {"PROTECTED_VERIFICATION_CHANGE", "NO_CODE_CHANGE"}
                else "POLICY_DENIED"
            )
            transition(session, run, "BLOCKED", reason)
        else:
            job.state = "FAILED"
            transition(session, run, "BLOCKED", scrub(f"{data.category}: {data.reason}"))
        return {"accepted": True, "attempt_id": attempt.id, "run": run_view(run)}

    @staticmethod
    def finish_cancellation(session, run):
        if not session.scalar(
            select(Job.id)
            .where(
                Job.workspace_id == run.workspace_id, Job.run_id == run.id, Job.state == "LEASED"
            )
            .limit(1)
        ):
            transition(session, run, "EXECUTION_RIGHT_ENDED")

    def cancel(
        self,
        session: Session,
        actor: Principal,
        run_id: str,
        data: Cancel,
        *,
        emergency: bool = False,
    ) -> dict:
        run = owned(session, Run, run_id, actor.workspace_id)
        if run.state_revision != data.expected_run_revision:
            raise DomainError(409, "RUN_REVISION_CONFLICT")
        if run.status in TERMINAL:
            raise DomainError(409, "RUN_TERMINAL")
        before = run.state_revision
        target = {
            "run_id": run.id,
            "candidate_id": run.current_candidate_id,
            "spec_hash": run.snapshot["task_spec_hash"],
            "policy_hash": run.snapshot["policy_hash"],
            "profile_hash": run.snapshot["verification_profile_hash"],
            "run_revision": before,
        }
        for request in session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == run.workspace_id,
                ApprovalRequest.run_id == run.id,
                ApprovalRequest.status == "PENDING",
            )
        ):
            request.status = "INVALIDATED"
        transition(session, run, "EMERGENCY_STOP_REQUESTED" if emergency else "CANCEL_REQUESTED")
        event(session, run, "USER_CANCEL_REASON", {"principal_id": actor.id, "reason": data.reason})
        for job in session.scalars(
            select(Job).where(
                Job.workspace_id == actor.workspace_id,
                Job.run_id == run.id,
            )
        ):
            if job.state in {"READY", "RETRY_WAIT", "PENDING"}:
                job.state = "CANCELLED"
        self.finish_cancellation(session, run)
        session.add(
            AuditEvent(
                workspace_id=actor.workspace_id,
                actor_id=actor.id,
                operation="intervention.cancel",
                body={
                    "action": "cancel",
                    "target": target,
                    "before_revision": before,
                    "after_revision": run.state_revision,
                    "reason": data.reason,
                },
                created_at=db_now(session),
            )
        )
        session.flush()
        self.checkpoint("intervention.cancelled")
        return run_view(run)

    def reconcile_workspace(self, session: Session, workspace_id: str) -> int:
        self.artifacts.collect(session, workspace_id)
        self.plans.expire(session, workspace_id)
        now = db_now(session)
        attempts = list(
            session.scalars(
                select(Attempt).where(
                    Attempt.workspace_id == workspace_id,
                    Attempt.state == "LEASED",
                    Attempt.expires_at <= now,
                )
            )
        )
        for attempt in attempts:
            job = owned(session, Job, attempt.job_id, workspace_id)
            run = owned(session, Run, job.run_id, workspace_id)
            attempt.state = "EXPIRED"
            attempt.stop_confirmation = "EXECUTION_RIGHT_EXPIRED"
            self.settle(session, run, attempt, 0, uncertain=True)
            event(
                session,
                run,
                "LEASE_EXPIRED",
                {
                    "attempt_id": attempt.id,
                    "physical_stop_confirmed": False,
                },
            )
            if run.status == RunStatus.CANCELLING:
                job.state = "CANCELLED"
                self.finish_cancellation(session, run)
            elif self.repository_allowed(session, run) and run.status not in TERMINAL:
                self.retry_or_fail(session, job, run)
            else:
                job.state = "CANCELLED"
        for run in session.scalars(
            select(Run).where(
                Run.workspace_id == workspace_id,
                Run.phase == "VERIFY_REVIEW",
                Run.status.in_(["QUEUED", "RUNNING"]),
                Run.current_candidate_id.is_not(None),
            )
        ):
            self.gates.local(session, run)
        for run in session.scalars(
            select(Run).where(
                Run.workspace_id == workspace_id,
                Run.phase == "WAIT_CI",
                Run.status == "WAITING_EXTERNAL",
            )
        ):
            if (run.wait_context or {}).get("quality_gate"):
                self.gates.final(session, run)
        return len(attempts)

    def worker_events(self, token: str, lease_id: str, data: WorkerEventBatch) -> dict:
        with self.db.transaction() as session:
            actor = self.locked_principal(session, token, "WORKER")
            attempt, _, run = self.lease(session, actor, lease_id, data.fence)
            receipts = []
            for item in data.events:
                payload = item.model_dump()
                payload_hash = digest(payload)
                previous = session.scalar(
                    select(WorkerEvent).where(
                        WorkerEvent.workspace_id == actor.workspace_id,
                        WorkerEvent.attempt_id == attempt.id,
                        WorkerEvent.sequence == item.sequence,
                    )
                )
                if previous:
                    if previous.payload_hash != payload_hash:
                        raise DomainError(409, "EVENT_SEQUENCE_CONFLICT")
                    receipts.append({"sequence": item.sequence, "revision": previous.run_revision})
                    continue
                cleaned = redact(payload, (token,))
                event(
                    session,
                    run,
                    "WORKER_EVENT",
                    {
                        "attempt_id": attempt.id,
                        "sequence": item.sequence,
                        "event": cleaned,
                    },
                )
                session.add(
                    WorkerEvent(
                        workspace_id=actor.workspace_id,
                        attempt_id=attempt.id,
                        sequence=item.sequence,
                        payload_hash=payload_hash,
                        body=cleaned,
                        run_revision=run.state_revision,
                    )
                )
                session.flush()
                receipts.append({"sequence": item.sequence, "revision": run.state_revision})
            return {"accepted": receipts}

    def reconcile(self) -> int:
        count = 0
        with self.db.transaction() as session:
            workspace_ids = list(session.scalars(select(Workspace.id)))
        for workspace_id in workspace_ids:
            with self.db.transaction() as session:
                self.lock_workspace(session, workspace_id)
                count += self.reconcile_workspace(session, workspace_id)
        return count
