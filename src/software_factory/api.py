import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .contracts import (
    Cancel,
    Claim,
    Complete,
    Enroll,
    Fail,
    Heartbeat,
    ProjectCreate,
    RepositoryCreate,
    RunStart,
    TaskSpec,
    WorkerEventBatch,
)
from .db import Database
from .models import (
    Attempt,
    Job,
    JobDependency,
    PlanVersion,
    Run,
    RunEvent,
    SpecVersion,
    Task,
    Worker,
)
from .resolver import SourceResolver
from .service import DomainError, Service, owned, run_view

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
) -> FastAPI:
    service = Service(database or Database(), resolver=resolver, checkpoint=checkpoint)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def reconciliation_loop():
            import logging

            while True:
                try:
                    await run_in_threadpool(service.reconcile)
                except Exception:
                    logging.getLogger(__name__).error("lease reconciliation failed")
                await asyncio.sleep(reconcile_interval)

        task = asyncio.create_task(reconciliation_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="Software Factory Control Plane",
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Local foundation with Fake Agent only. Runs stop at ASSEMBLE_CANDIDATE; "
            "no verified code, PR publication, or production authentication is provided yet."
        ),
    )
    app.state.service = service

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
            token, "USER", f"projects:{workspace_id}", key, data.model_dump(), command
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
                        "validation_error": p.validation_error,
                    }
                    for p in plans
                ]
            }

        return service.read(token, query)

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
            return {
                "events": [
                    {
                        "revision": e.revision,
                        "kind": e.kind,
                        "body": e.body,
                        "created_at": e.created_at,
                    }
                    for e in events
                ]
            }

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

    @app.post("/v1/worker/enroll", status_code=201, tags=["Worker"])
    def enroll(data: Enroll):
        return service.enroll(data.token, data.name)

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
