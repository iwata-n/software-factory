import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .ci_poll import CIPoller, configured_read_tokens
from .ci_webhook import configured_webhooks, webhook_router
from .contracts import (
    ApprovalSubmission,
    ArtifactFinalize,
    ArtifactReserve,
    BudgetRequest,
    Cancel,
    Claim,
    Complete,
    Enroll,
    Fail,
    Heartbeat,
    Intervention,
    ProjectCreate,
    PublicationBegin,
    PublicationReconcile,
    PublicationReport,
    ReplanRequest,
    RepositoryCreate,
    RunStart,
    TaskSpec,
    UsageDelivery,
    WorkerEnrollmentRequest,
    WorkerEventBatch,
    WorkerRevocation,
    WorkspaceExecutionChange,
)
from .db import Database
from .jira_input import configured_jira, jira_router
from .models import (
    ApprovalRequest,
    Artifact,
    Attempt,
    Candidate,
    Check,
    Finding,
    Gate,
    Job,
    JobDependency,
    OutboxEvent,
    PlanVersion,
    Repository,
    Run,
    RunEvent,
    SlackDelivery,
    SpecVersion,
    Task,
    VerifiedRunResult,
    Worker,
)
from .operations import event_view, operations_router
from .resolver import SourceResolver
from .service import DomainError, Service, owned, run_view
from .slack_delivery import NOTIFICATION

security = HTTPBearer(auto_error=False)


def bearer(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise DomainError(401, "UNAUTHENTICATED")
    return credentials.credentials


Token = Annotated[str, Depends(bearer)]
Key = Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)]


def create_app(
    database: Database | None = None,
    *,
    reconcile_interval: float = 5,
    resolver: SourceResolver | None = None,
    checkpoint=None,
    storage=None,
) -> FastAPI:
    service = Service(
        database or Database(), resolver=resolver, checkpoint=checkpoint, storage=storage
    )

    integrations = configured_webhooks()
    ci_poller = CIPoller(service, integrations, configured_read_tokens())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def reconciliation_loop():
            while True:
                try:
                    await run_in_threadpool(service.reconcile)
                except Exception:
                    logging.getLogger(__name__).error("lease reconciliation failed")
                await asyncio.sleep(reconcile_interval)

        def poll_ci():
            with httpx.Client(timeout=10, trust_env=False) as client:
                ci_poller.poll(client)

        async def ci_loop():
            while True:
                try:
                    await run_in_threadpool(poll_ci)
                except Exception:
                    logging.getLogger(__name__).error("GitHub CI reconciliation failed")
                await asyncio.sleep(min(reconcile_interval, 5))

        tasks = [asyncio.create_task(reconciliation_loop())]
        if ci_poller.bindings:
            tasks.append(asyncio.create_task(ci_loop()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title="Software Factory Control Plane",
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Local control plane with immutable Candidates and independent evidence. "
            "Default Fake Agent produces no patch. Publication and real-agent "
            "integration are tracked by the implementation plan."
        ),
    )
    app.state.service = service
    app.include_router(webhook_router(service, integrations))
    if jira := configured_jira():
        app.include_router(jira_router(service, *jira))
    app.include_router(operations_router(service, Token))

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse(status_code=error.status, content={"code": error.code}, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Pydantic's default response echoes invalid input, including enrollment credentials.
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_INPUT",
                "errors": [{"location": e["loc"][0], "type": e["type"]} for e in error.errors()],
            },
        )

    @app.get("/healthz", tags=["Operations"])
    def health():
        try:
            with service.db.transaction() as session:
                session.execute(select(1))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok", "mode": "fake-agent-foundation"}

    @app.post("/v1/workspaces/{workspace_id}/projects", status_code=201, tags=["Projects"])
    def project_create(workspace_id: str, data: ProjectCreate, token: Token, key: Key = None):
        def command(session, actor):
            if actor.workspace_id != workspace_id:
                raise DomainError(404, "NOT_FOUND")
            return service.create_project(session, actor, data)

        return service.write(
            token,
            "USER",
            f"projects:{workspace_id}",
            key,
            data.model_dump(),
            command,
            user_access="ADMIN",
        )

    @app.post("/v1/projects/{project_id}/repositories", status_code=201, tags=["Projects"])
    def repository_create(project_id: str, data: RepositoryCreate, token: Token, key: Key = None):
        return service.write(
            token,
            "USER",
            f"repositories:{project_id}",
            key,
            data.model_dump(),
            lambda s, a: service.create_repository(s, a, project_id, data),
            user_access="ADMIN",
        )

    @app.post("/v1/projects/{project_id}/tasks", status_code=201, tags=["Tasks"])
    def task_create(project_id: str, data: TaskSpec, token: Token, key: Key = None):
        return service.write(
            token,
            "USER",
            f"tasks:{project_id}",
            key,
            data.model_dump(),
            lambda s, a: service.create_task(s, a, project_id, data),
        )

    @app.get("/v1/tasks/{task_id}", tags=["Tasks"])
    def task_get(task_id: str, token: Token):
        def query(session, actor):
            task = owned(session, Task, task_id, actor.workspace_id)
            specs = session.scalars(
                select(SpecVersion)
                .where(
                    SpecVersion.workspace_id == actor.workspace_id,
                    SpecVersion.task_id == task.id,
                )
                .order_by(SpecVersion.version)
            )
            runs = session.scalars(
                select(Run)
                .where(
                    Run.workspace_id == actor.workspace_id,
                    Run.task_id == task.id,
                )
                .order_by(Run.created_at)
            )
            return {
                "task_id": task.id,
                "title": task.title,
                "spec_versions": [
                    {"version": v.version, "hash": v.spec_hash, "body": v.body} for v in specs
                ],
                "runs": [run_view(run) for run in runs],
            }

        return service.read(token, query)

    @app.post("/v1/tasks/{task_id}/spec-versions", status_code=201, tags=["Tasks"])
    def spec_create(task_id: str, data: TaskSpec, token: Token, key: Key = None):
        return service.write(
            token,
            "USER",
            f"specs:{task_id}",
            key,
            data.model_dump(),
            lambda s, a: service.new_spec(s, a, task_id, data),
        )

    @app.post("/v1/tasks/{task_id}/runs", status_code=202, tags=["Runs"])
    def run_start(task_id: str, data: RunStart, token: Token, response: Response, key: Key = None):
        result = service.start_run_request(token, task_id, data, key)
        response.headers["Location"] = f"/v1/runs/{result['run_id']}"
        return result

    @app.get("/v1/runs/{run_id}", tags=["Runs"])
    def run_get(run_id: str, token: Token):
        return service.read(token, lambda s, a: run_view(owned(s, Run, run_id, a.workspace_id)))

    def verified_result(session, actor, run_id):
        run = owned(session, Run, run_id, actor.workspace_id)
        result = session.scalar(
            select(VerifiedRunResult).where(
                VerifiedRunResult.workspace_id == run.workspace_id,
                VerifiedRunResult.run_id == run.id,
            )
        )
        if result is None:
            raise DomainError(404, "NOT_FOUND")
        return run, result

    @app.get("/v1/runs/{run_id}/result", tags=["Runs"])
    def run_result(run_id: str, token: Token):
        def query(session, actor):
            _, result = verified_result(session, actor, run_id)
            return {"result_id": result.id, "verified_at": result.created_at, **result.body}

        return service.read(token, query)

    @app.get("/v1/runs/{run_id}/result/current-pr", tags=["Runs"])
    def run_result_current_pr(run_id: str, token: Token, response: Response):
        def query(session, actor):
            run, result = verified_result(session, actor, run_id)
            repo = owned(session, Repository, run.repository_id, actor.workspace_id)
            binding = ci_poller.bindings.get((run.workspace_id, run.repository_id))
            if not repo.active or (binding and not ci_poller.matches_repository(repo, binding[1])):
                return None, result.body["draft_pr"]
            return binding, result.body["draft_pr"]

        binding, draft = service.read(token, query)
        response.headers["Cache-Control"] = "no-store"
        with httpx.Client(timeout=5, trust_env=False) as client:
            return ci_poller.observe_result_pr(client, binding, draft)

    @app.get("/v1/runs/{run_id}/notifications", tags=["Integrations"])
    def run_notifications(run_id: str, token: Token):
        def query(session, actor):
            run = owned(session, Run, run_id, actor.workspace_id)
            rows = session.execute(
                select(OutboxEvent, SlackDelivery)
                .outerjoin(SlackDelivery, SlackDelivery.outbox_id == OutboxEvent.id)
                .where(
                    OutboxEvent.workspace_id == run.workspace_id,
                    OutboxEvent.run_id == run.id,
                    OutboxEvent.kind == NOTIFICATION,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
            return {
                "notifications": [
                    {
                        "delivery_key": outbox.id,
                        "run_status": outbox.body["status"],
                        "state": delivery.state if delivery else "PENDING",
                        "attempts": delivery.attempts if delivery else 0,
                        "message_id": delivery.message_id if delivery else None,
                        "reason": delivery.reason if delivery else None,
                    }
                    for outbox, delivery in rows
                ]
            }

        return service.read(token, query)

    @app.get("/v1/runs/{run_id}/plan", tags=["Runs"])
    def plan_get(run_id: str, token: Token):
        def query(session, actor):
            owned(session, Run, run_id, actor.workspace_id)
            plans = session.scalars(
                select(PlanVersion)
                .where(
                    PlanVersion.workspace_id == actor.workspace_id,
                    PlanVersion.run_id == run_id,
                )
                .order_by(PlanVersion.version)
            )
            return {
                "plans": [
                    {
                        "plan_id": p.id,
                        "version": p.version,
                        "body": p.body,
                        "body_hash": p.body_hash,
                        "producer_job_id": p.producer_job_id,
                        "validation_error": p.validation_error,
                    }
                    for p in plans
                ]
            }

        return service.read(token, query)

    @app.get("/v1/runs/{run_id}/approvals", tags=["Approvals"])
    def approvals_get(run_id: str, token: Token):
        from .plans import approval_view

        def query(s, a):
            owned(s, Run, run_id, a.workspace_id)
            return [
                approval_view(row)
                for row in s.scalars(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.workspace_id == a.workspace_id,
                        ApprovalRequest.run_id == run_id,
                    )
                    .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
                )
            ]

        return service.read(token, query)

    @app.get("/v1/approvals/{approval_id}", tags=["Approvals"])
    def approval_get(approval_id: str, token: Token):
        from .plans import approval_view

        return service.read(
            token,
            lambda s, a: approval_view(owned(s, ApprovalRequest, approval_id, a.workspace_id)),
        )

    @app.post("/v1/approvals/{approval_id}/decisions", tags=["Approvals"])
    def approval_decide(approval_id: str, data: ApprovalSubmission, token: Token, key: Key = None):
        return service.interventions.approval(token, key, approval_id, data)

    @app.get("/v1/runs/{run_id}/interventions", tags=["Interventions"])
    def intervention_target(run_id: str, token: Token):
        return service.read(token, lambda s, a: service.interventions.view(s, a, run_id))

    @app.post("/v1/runs/{run_id}/interventions", tags=["Interventions"])
    def intervention(
        run_id: str, data: Intervention, token: Token, response: Response, key: Key = None
    ):
        if data.action == "cancel":
            response.status_code = 202
        return service.interventions.execute(token, key, run_id, data)

    @app.get("/v1/runs/{run_id}/costs", tags=["Costs"])
    def costs_get(run_id: str, token: Token):
        from .costs import entry_view, receipt_view
        from .models import CostEntry, UsageReceipt

        def query(s, a):
            run = owned(s, Run, run_id, a.workspace_id)
            return {
                **service.costs.view(s, run),
                "entries": [
                    entry_view(e)
                    for e in s.scalars(
                        select(CostEntry)
                        .where(CostEntry.workspace_id == a.workspace_id, CostEntry.run_id == run.id)
                        .order_by(CostEntry.created_at, CostEntry.id)
                    )
                ],
                "receipts": [
                    receipt_view(e)
                    for e in s.scalars(
                        select(UsageReceipt)
                        .where(
                            UsageReceipt.workspace_id == a.workspace_id,
                            UsageReceipt.run_id == run.id,
                        )
                        .order_by(UsageReceipt.created_at, UsageReceipt.id)
                    )
                ],
            }

        return service.read(token, query)

    @app.get("/v1/publisher/queue", tags=["Publisher"])
    def publisher_queue(token: Token):
        return service.read(token, service.publications.queue, role="PUBLISHER")

    @app.post("/v1/publisher/jobs/{job_id}/begin", tags=["Publisher"])
    def publication_begin(job_id: str, data: PublicationBegin, token: Token, key: Key = None):
        return service.write(
            token,
            "PUBLISHER",
            f"publication:{job_id}:begin",
            key,
            data.model_dump(),
            lambda s, a: service.publications.begin(s, a, job_id, data),
        )

    @app.get("/v1/publisher/reconciliations", tags=["Publisher"])
    def publication_reconciliations(token: Token):
        return service.read(token, service.publications.reconciliation_queue, role="PUBLISHER")

    @app.post("/v1/publisher/operations/{operation_id}/reconcile", tags=["Publisher"])
    def publication_reconcile(
        operation_id: str, data: PublicationReconcile, token: Token, key: Key = None
    ):
        return service.write(
            token,
            "PUBLISHER",
            f"publication:{operation_id}:reconcile",
            key,
            data.model_dump(),
            lambda s, a: service.publications.claim_reconciliation(s, a, operation_id, data),
        )

    @app.get("/v1/publisher/operations/{operation_id}/inputs", tags=["Publisher"])
    def publication_inputs(operation_id: str, token: Token):
        return service.read(
            token, lambda s, a: service.publications.inputs(s, a, operation_id), role="PUBLISHER"
        )

    @app.post("/v1/publisher/operations/{operation_id}/reports", tags=["Publisher"])
    def publication_report(
        operation_id: str, data: PublicationReport, token: Token, key: Key = None
    ):
        return service.write(
            token,
            "PUBLISHER",
            f"publication:{operation_id}:report",
            key,
            data.payload(),
            lambda s, a: service.publications.report(s, a, operation_id, data),
        )

    @app.get("/v1/runs/{run_id}/publication", tags=["Runs"])
    def publication_get(run_id: str, token: Token):
        from .models import DraftPullRequest, PublicationObservation, PublicationOperation
        from .publications import draft_view, operation_view

        def query(session, actor):
            run = owned(session, Run, run_id, actor.workspace_id)
            operations = session.scalars(
                select(PublicationOperation)
                .where(
                    PublicationOperation.workspace_id == actor.workspace_id,
                    PublicationOperation.run_id == run.id,
                )
                .order_by(PublicationOperation.created_at, PublicationOperation.id)
            )
            draft = session.scalar(
                select(DraftPullRequest).where(
                    DraftPullRequest.workspace_id == actor.workspace_id,
                    DraftPullRequest.run_id == run.id,
                )
            )
            return {
                "operations": [operation_view(item) for item in operations],
                "pull_request": draft_view(draft) if draft else None,
                "observations": [
                    {
                        "observation_id": item.id,
                        "operation_id": item.operation_id,
                        "publisher_id": item.publisher_id,
                        "body": item.body,
                        "created_at": item.created_at,
                    }
                    for item in session.scalars(
                        select(PublicationObservation)
                        .where(
                            PublicationObservation.workspace_id == actor.workspace_id,
                            PublicationObservation.run_id == run.id,
                        )
                        .order_by(PublicationObservation.created_at, PublicationObservation.id)
                    )
                ],
            }

        return service.read(token, query)

    @app.post("/v1/runs/{run_id}/budget-requests", tags=["Costs"])
    def budget_request(run_id: str, data: BudgetRequest, token: Token, key: Key = None):
        return service.write(
            token,
            "USER",
            f"runs:{run_id}:budget-request",
            key,
            data.model_dump(),
            lambda s, a: service.budgets.request(s, a, run_id, data),
        )

    @app.post("/v1/worker/attempts/{attempt_id}/usage", tags=["Costs"])
    def usage_post(attempt_id: str, data: UsageDelivery, token: Token, key: Key = None):
        return service.write(
            token,
            "WORKER",
            f"attempts:{attempt_id}:usage",
            key,
            data.model_dump(),
            lambda s, a: service.costs.receive(s, a, attempt_id, data),
        )

    @app.post("/v1/runs/{run_id}/replan", status_code=202, tags=["Runs"])
    def run_replan(
        run_id: str, data: ReplanRequest, token: Token, response: Response, key: Key = None
    ):
        result = service.write(
            token,
            "USER",
            f"runs:{run_id}:replan",
            key,
            data.model_dump(),
            lambda s, a: service.plans.replan(s, a, run_id, data),
        )
        response.headers["Location"] = f"/v1/runs/{run_id}"
        return result

    @app.get("/v1/runs/{run_id}/candidates", tags=["Candidates"])
    def candidates_get(run_id: str, token: Token):
        from .candidates import candidate_view

        def query(s, a):
            owned(s, Run, run_id, a.workspace_id)
            return {
                "candidates": [
                    candidate_view(item)
                    for item in s.scalars(
                        select(Candidate)
                        .where(Candidate.workspace_id == a.workspace_id, Candidate.run_id == run_id)
                        .order_by(Candidate.generation)
                    )
                ]
            }

        return service.read(token, query)

    @app.get("/v1/candidates/{candidate_id}", tags=["Candidates"])
    def candidate_get(candidate_id: str, token: Token):
        from .candidates import candidate_view

        return service.read(
            token, lambda s, a: candidate_view(owned(s, Candidate, candidate_id, a.workspace_id))
        )

    @app.get("/v1/candidates/{candidate_id}/checks", tags=["Candidates"])
    def checks_get(candidate_id: str, token: Token):
        from .evidence import check_view

        def query(s, a):
            owned(s, Candidate, candidate_id, a.workspace_id)
            return [
                check_view(c)
                for c in s.scalars(
                    select(Check)
                    .where(Check.workspace_id == a.workspace_id, Check.candidate_id == candidate_id)
                    .order_by(Check.kind, Check.key, Check.id)
                )
            ]

        return service.read(token, query)

    @app.get("/v1/candidates/{candidate_id}/findings", tags=["Candidates"])
    def findings_get(candidate_id: str, token: Token):
        from .evidence import finding_view

        def query(s, a):
            owned(s, Candidate, candidate_id, a.workspace_id)
            return [
                finding_view(f)
                for f in s.scalars(
                    select(Finding)
                    .where(
                        Finding.workspace_id == a.workspace_id, Finding.candidate_id == candidate_id
                    )
                    .order_by(Finding.finding_key, Finding.id)
                )
            ]

        return service.read(token, query)

    @app.get("/v1/candidates/{candidate_id}/verification", tags=["Candidates"])
    def verification_get(candidate_id: str, token: Token):
        from .evidence import check_view, finding_view
        from .gates import gate_view

        def query(s, a):
            owned(s, Candidate, candidate_id, a.workspace_id)
            result = {}
            for model, name, view in (
                (Check, "checks", check_view),
                (Finding, "findings", finding_view),
                (Gate, "gates", gate_view),
            ):
                result[name] = [
                    view(row)
                    for row in s.scalars(
                        select(model)
                        .where(
                            model.workspace_id == a.workspace_id, model.candidate_id == candidate_id
                        )
                        .order_by(model.id)
                    )
                ]
            return result

        return service.read(token, query)

    @app.get("/v1/gates/{gate_id}", tags=["Candidates"])
    def gate_get(gate_id: str, token: Token):
        from .gates import gate_view

        return service.read(token, lambda s, a: gate_view(owned(s, Gate, gate_id, a.workspace_id)))

    @app.post("/v1/gates/{gate_id}/override", tags=["Candidates"])
    @app.put("/v1/gates/{gate_id}", tags=["Candidates"])
    @app.patch("/v1/gates/{gate_id}", tags=["Candidates"])
    @app.delete("/v1/gates/{gate_id}", tags=["Candidates"])
    def gate_mutation(gate_id: str, token: Token):
        return service.gates.deny_mutation(token, gate_id)

    @app.put("/v1/candidates/{candidate_id}", tags=["Candidates"])
    @app.patch("/v1/candidates/{candidate_id}", tags=["Candidates"])
    @app.delete("/v1/candidates/{candidate_id}", tags=["Candidates"])
    def candidate_mutation(candidate_id: str, token: Token):
        def reject(s, a):
            owned(s, Candidate, candidate_id, a.workspace_id)
            raise DomainError(409, "CANDIDATE_IMMUTABLE")

        return service.read(token, reject)

    @app.get("/v1/worker/leases/{lease_id}/inputs/{artifact_id}/content", tags=["Artifacts"])
    def input_content(lease_id: str, artifact_id: str, fence: int, token: Token):
        with service.db.transaction() as s:
            actor = service.principal(s, token, "WORKER")
            _, job, run = service.artifacts.active(s, actor, lease_id, fence)
            artifact = owned(s, Artifact, artifact_id, actor.workspace_id)
            if artifact.run_id != run.id or artifact_id not in job.payload.get(
                "input_artifact_ids", []
            ):
                raise DomainError(404, "NOT_FOUND")
            body = service.artifacts.content(s, actor, artifact_id)
        return Response(
            content=body,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/v1/runs/{run_id}/graph", tags=["Runs"])
    def graph_get(run_id: str, token: Token):
        def query(session, actor):
            owned(session, Run, run_id, actor.workspace_id)
            jobs = list(
                session.scalars(
                    select(Job)
                    .where(
                        Job.workspace_id == actor.workspace_id,
                        Job.run_id == run_id,
                    )
                    .order_by(Job.created_at, Job.id)
                )
            )
            result = []
            for job in jobs:
                attempts = session.scalars(
                    select(Attempt)
                    .where(
                        Attempt.workspace_id == actor.workspace_id,
                        Attempt.job_id == job.id,
                    )
                    .order_by(Attempt.attempt_no)
                )
                result.append(
                    {
                        "job_id": job.id,
                        "kind": job.kind,
                        "state": job.state,
                        "step_key": job.step_key,
                        "attempt_no": job.attempt_no,
                        "priority": job.priority,
                        "attempts": [
                            {
                                "attempt_id": a.id,
                                "state": a.state,
                                "fence": a.fence,
                                "worker_id": a.worker_id,
                                "expires_at": a.expires_at,
                                "heartbeat_at": a.heartbeat_at,
                                "stop_confirmation": a.stop_confirmation,
                                "input_digest": a.input_digest,
                            }
                            for a in attempts
                        ],
                    }
                )
            edges = session.scalars(
                select(JobDependency)
                .where(
                    JobDependency.workspace_id == actor.workspace_id,
                    JobDependency.run_id == run_id,
                )
                .order_by(JobDependency.id)
            )
            return {
                "jobs": result,
                "edges": [{"from": edge.depends_on_id, "to": edge.job_id} for edge in edges],
            }

        return service.read(token, query)

    @app.get("/v1/runs/{run_id}/event-history", tags=["Runs"])
    def event_history(run_id: str, token: Token, after_revision: int = 0):
        def query(session, actor):
            owned(session, Run, run_id, actor.workspace_id)
            events = session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.workspace_id == actor.workspace_id,
                    RunEvent.run_id == run_id,
                    RunEvent.revision > after_revision,
                )
                .order_by(RunEvent.revision)
                .limit(1000)
            )
            return {"events": [event_view(item) for item in events]}

        return service.read(token, query)

    @app.post("/v1/runs/{run_id}/cancel", status_code=202, tags=["Runs"])
    def run_cancel(run_id: str, data: Cancel, token: Token, key: Key = None):
        return service.write(
            token,
            "USER",
            f"cancel:{run_id}",
            key,
            data.model_dump(),
            lambda s, a: service.cancel(s, a, run_id, data),
        )

    @app.get("/v1/workspaces/{workspace_id}/execution-control", tags=["Operations"])
    def execution_control_get(workspace_id: str, token: Token):
        return service.read(token, lambda s, a: service.execution_control(s, a, workspace_id))

    @app.post("/v1/workspaces/{workspace_id}/execution-control", tags=["Operations"])
    def execution_control_post(
        workspace_id: str, data: WorkspaceExecutionChange, token: Token, key: Key = None
    ):
        return service.write(
            token,
            "USER",
            f"execution-control:{workspace_id}",
            key,
            data.model_dump(),
            lambda s, a: service.set_execution_control(s, a, workspace_id, data),
            user_access="ADMIN",
        )

    @app.get("/v1/workspaces/{workspace_id}/workers", tags=["Operations"])
    def workers_get(workspace_id: str, token: Token):
        def query(session, actor):
            if workspace_id != actor.workspace_id:
                raise DomainError(404, "NOT_FOUND")
            workers = session.scalars(select(Worker).where(Worker.workspace_id == workspace_id))
            return {
                "workers": [
                    {"worker_id": w.id, "name": w.name, "capabilities": w.capabilities}
                    for w in workers
                ]
            }

        return service.read(token, query)

    @app.post(
        "/v1/workspaces/{workspace_id}/worker-enrollments", status_code=201, tags=["Operations"]
    )
    def worker_enrollment(
        workspace_id: str, data: WorkerEnrollmentRequest, token: Token, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        return service.issue_worker_enrollment(token, workspace_id, data)

    @app.post("/v1/workspaces/{workspace_id}/workers/{worker_id}/revoke", tags=["Operations"])
    def worker_revoke(
        workspace_id: str, worker_id: str, data: WorkerRevocation, token: Token, key: Key = None
    ):
        return service.write(
            token,
            "USER",
            f"workers:{workspace_id}:{worker_id}:revoke",
            key,
            data.model_dump(),
            lambda s, a: service.revoke_worker(s, a, workspace_id, worker_id, data.reason),
            user_access="ADMIN",
        )

    @app.post("/v1/worker/enroll", status_code=201, tags=["Worker"])
    def enroll(data: Enroll, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return service.enroll(data.token, data.name)

    @app.post("/v1/worker/credentials/rotate", tags=["Worker"])
    def rotate_worker_credential(token: Token, response: Response, key: Key = None):
        response.headers["Cache-Control"] = "no-store"
        return service.rotate_worker_token(token, key)

    @app.post("/v1/worker/claims", tags=["Worker"])
    def claim(data: Claim, token: Token):
        deadline = time.monotonic() + data.wait_seconds
        while True:
            result = service.claim(token)
            if result:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Response(status_code=204)
            service.checkpoint("poll.wait")
            time.sleep(min(0.2, remaining))  # No transaction or DB lock survives this wait.

    @app.post("/v1/worker/leases/{lease_id}/heartbeat", tags=["Worker"])
    def heartbeat(lease_id: str, data: Heartbeat, token: Token):
        return service.heartbeat(token, lease_id, data.fence)

    @app.get("/v1/worker/leases/{lease_id}/source", tags=["Worker"])
    def source_get(lease_id: str, fence: int, token: Token):
        return service.fetch_source(token, lease_id, fence)

    @app.post("/v1/worker/leases/{lease_id}/artifacts", status_code=201, tags=["Artifacts"])
    def artifact_reserve(lease_id: str, data: ArtifactReserve, token: Token, key: Key = None):
        return service.write(
            token,
            "WORKER",
            f"artifact.reserve:{lease_id}",
            key,
            data.model_dump(),
            lambda s, a: service.artifacts.reserve(s, a, lease_id, data),
        )

    @app.put("/v1/worker/leases/{lease_id}/artifacts/{artifact_id}/content", tags=["Artifacts"])
    async def artifact_upload(
        lease_id: str, artifact_id: str, fence: int, request: Request, token: Token, key: Key = None
    ):
        from .artifacts import MAX_ARTIFACT_BYTES
        from .storage import content_hash

        # Authenticate and validate ownership before receiving a possibly large body.
        def authorize():
            with service.db.transaction() as s:
                actor = service.principal(s, token, "WORKER")
                item = owned(s, Artifact, artifact_id, actor.workspace_id)
                attempt = s.scalar(
                    select(Attempt).where(
                        Attempt.workspace_id == actor.workspace_id,
                        Attempt.id == item.attempt_id,
                        Attempt.worker_id == actor.id,
                        Attempt.lease_id == lease_id,
                    )
                )
                if attempt is None:
                    raise DomainError(404, "NOT_FOUND")
                if not key:
                    raise DomainError(422, "IDEMPOTENCY_KEY_REQUIRED")

        await run_in_threadpool(authorize)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_ARTIFACT_BYTES:
                raise DomainError(413, "ARTIFACT_TOO_LARGE")
            body.extend(chunk)
        data = bytes(body)
        return await run_in_threadpool(
            service.write,
            token,
            "WORKER",
            f"artifact.upload:{artifact_id}",
            key,
            {"fence": fence, "size_bytes": len(data), "sha256": content_hash(data)},
            lambda s, a: service.artifacts.upload(s, a, lease_id, artifact_id, fence, data),
        )

    @app.post("/v1/worker/leases/{lease_id}/artifacts/{artifact_id}/finalize", tags=["Artifacts"])
    def artifact_finalize(
        lease_id: str, artifact_id: str, data: ArtifactFinalize, token: Token, key: Key = None
    ):
        return service.write(
            token,
            "WORKER",
            f"artifact.finalize:{artifact_id}",
            key,
            data.model_dump(),
            lambda s, a: service.artifacts.finalize(s, a, lease_id, artifact_id, data),
        )

    @app.get("/v1/runs/{run_id}/artifacts", tags=["Artifacts"])
    def artifact_list(run_id: str, token: Token):
        from .artifacts import view

        def query(s, a):
            owned(s, Run, run_id, a.workspace_id)
            return {
                "artifacts": [
                    view(item)
                    for item in s.scalars(
                        select(Artifact)
                        .where(
                            Artifact.workspace_id == a.workspace_id,
                            Artifact.run_id == run_id,
                            Artifact.status != "DELETED",
                        )
                        .order_by(Artifact.created_at, Artifact.id)
                    )
                ]
            }

        return service.read(token, query)

    @app.get("/v1/artifacts/{artifact_id}", tags=["Artifacts"])
    def artifact_get(artifact_id: str, token: Token):
        from .artifacts import view

        def query(s, a):
            item = owned(s, Artifact, artifact_id, a.workspace_id)
            if item.status == "DELETED":
                raise DomainError(404, "NOT_FOUND")
            return view(item)

        return service.read(token, query)

    @app.get("/v1/artifacts/{artifact_id}/content", tags=["Artifacts"])
    def artifact_content(artifact_id: str, token: Token):
        body = service.read(token, lambda s, a: service.artifacts.content(s, a, artifact_id))
        return Response(
            content=body,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/v1/worker/leases/{lease_id}/artifacts/{artifact_id}/content", tags=["Artifacts"])
    def worker_artifact_content(lease_id: str, artifact_id: str, fence: int, token: Token):
        with service.db.transaction() as s:
            actor = service.principal(s, token, "WORKER")
            service.artifacts.owned(s, actor, lease_id, artifact_id, fence)
            body = service.artifacts.content(s, actor, artifact_id)
        return Response(
            content=body,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.post("/v1/worker/leases/{lease_id}/complete", tags=["Worker"])
    def complete(lease_id: str, data: Complete, token: Token, key: Key = None):
        return service.write(
            token,
            "WORKER",
            f"complete:{lease_id}",
            key,
            data.model_dump(),
            lambda s, a: service.complete(s, a, lease_id, data),
        )

    @app.post("/v1/worker/leases/{lease_id}/events", tags=["Worker"])
    def worker_events(lease_id: str, data: WorkerEventBatch, token: Token):
        return service.worker_events(token, lease_id, data)

    @app.post("/v1/worker/leases/{lease_id}/fail", tags=["Worker"])
    def fail(lease_id: str, data: Fail, token: Token, key: Key = None):
        return service.write(
            token,
            "WORKER",
            f"fail:{lease_id}",
            key,
            data.model_dump(),
            lambda s, a: service.fail(s, a, lease_id, data),
        )

    return app


def app_factory() -> FastAPI:
    return create_app()


def main():
    import uvicorn

    uvicorn.run(
        "software_factory.api:app_factory",
        factory=True,
        host="127.0.0.1",
        port=int(os.environ.get("FACTORY_PORT", "8000")),
    )
