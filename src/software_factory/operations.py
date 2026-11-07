"""Operations console and workspace-scoped queries."""

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, select, tuple_
from starlette.concurrency import run_in_threadpool

from .contracts import ProjectPolicy, digest
from .db import db_now
from .models import Attempt, Principal, Project, Repository, Run, RunEvent, Task, Worker
from .service import DomainError, owned
from .task_lifecycle import task_stage

UI_DIR = Path(__file__).with_name("operations_ui")
HEARTBEAT_STALE_SECONDS = 30


def event_view(event):
    return {
        "revision": event.revision,
        "kind": event.kind,
        "body": event.body,
        "created_at": event.created_at,
    }


def event_batch(session, run, revision):
    if revision is not None and revision > run.state_revision:
        raise DomainError(422, "INVALID_EVENT_CURSOR")
    if (
        revision is not None
        and run.events_pruned_through is not None
        and revision <= run.events_pruned_through
    ):
        raise DomainError(410, "EVENT_CURSOR_EXPIRED")
    events = list(
        session.scalars(
            select(RunEvent)
            .where(
                RunEvent.workspace_id == run.workspace_id,
                RunEvent.run_id == run.id,
                RunEvent.revision > (revision or 0),
            )
            .order_by(RunEvent.revision)
            .limit(1000)
        )
    )
    # A caller without a cursor starts at the first retained event.
    # Run revisions start at 1; the first persisted event is revision 2.
    if revision is not None:
        previous = max(1, revision)
        for event in events:
            if event.revision != previous + 1:
                raise DomainError(410, "EVENT_CURSOR_EXPIRED")
            previous = event.revision
        if not events and run.state_revision > previous:
            raise DomainError(410, "EVENT_CURSOR_EXPIRED")
    return [event_view(event) for event in events]


def worker_view(worker, credential, attempt, now):
    if not credential.active:
        credential_status = "REVOKED"
    elif credential.token_expires_at is not None and credential.token_expires_at <= now:
        credential_status = "EXPIRED"
    else:
        credential_status = "ACTIVE"
    lease_status = "NO_LEASE"
    if attempt is not None:
        if attempt.state == "EXPIRED" or (attempt.state == "LEASED" and now >= attempt.expires_at):
            lease_status = "EXECUTION_RIGHT_EXPIRED"
        elif attempt.state == "LEASED":
            lease_status = (
                "HEARTBEAT_OVERDUE"
                if now - attempt.heartbeat_at >= HEARTBEAT_STALE_SECONDS
                else "LEASED"
            )
        elif attempt.stop_confirmation in {"WORKER_REPORTED_STOP", "STOP_UNCONFIRMED"}:
            lease_status = attempt.stop_confirmation
        else:
            lease_status = "LEASE_ENDED"
    return {
        "worker_id": worker.id,
        "name": worker.name,
        "capabilities": worker.capabilities,
        "credential_status": credential_status,
        "lease_status": lease_status,
        "last_heartbeat_at": attempt.heartbeat_at if attempt else None,
        "lease_expires_at": attempt.expires_at if attempt else None,
        "attempt_state": attempt.state if attempt else None,
        "stop_confirmation": attempt.stop_confirmation if attempt else None,
    }


def operations_router(service, token_type):
    router = APIRouter()

    @router.get("/operations", include_in_schema=False)
    def operations_page():
        return FileResponse(
            UI_DIR / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                    "form-action 'none'"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.get("/operations/{asset}", include_in_schema=False)
    def operations_asset(asset: str):
        if asset not in {"board.js", "board.css", "a2ui.js"}:
            return Response(status_code=404)
        return FileResponse(UI_DIR / asset, headers={"X-Content-Type-Options": "nosniff"})

    @router.get("/v1/operations/context", tags=["Operations"])
    def operations_context(token: token_type, response: Response):
        response.headers["Cache-Control"] = "no-store"

        def query(session, actor):
            return {
                "access": actor.user_access,
                "execution": service.execution_control(session, actor, actor.workspace_id),
            }

        return service.read(token, query)

    @router.get("/v1/operations/task-targets", tags=["Operations"])
    def task_targets(token: token_type, response: Response):
        response.headers["Cache-Control"] = "no-store"

        def query(session, actor):
            service.require_user_access(actor, "OPERATOR")
            rows = session.execute(
                select(Project, Repository)
                .join(Repository, Repository.project_id == Project.id)
                .where(
                    Project.workspace_id == actor.workspace_id,
                    Repository.workspace_id == actor.workspace_id,
                    Repository.active.is_(True),
                )
                .order_by(Project.name, Project.id, Repository.name, Repository.id)
            )
            projects = {}
            for project, repository in rows:
                if project.id not in projects:
                    policy = ProjectPolicy.model_validate(project.policy)
                    projects[project.id] = {
                        "id": project.id,
                        "name": project.name,
                        "verification": policy.verification.model_dump(),
                        "allowed_write_paths": policy.allowed_write_paths,
                        "allowed_read_paths": policy.allowed_read_paths,
                        "policy_hash": digest(project.policy),
                        "repositories": [],
                    }
                projects[project.id]["repositories"].append(
                    {"id": repository.id, "name": repository.name, "base_ref": repository.base_ref}
                )
            return {"projects": list(projects.values())}

        return service.read(token, query)

    @router.get("/v1/workspaces/{workspace_id}/workers", tags=["Operations"])
    def workers_get(workspace_id: str, token: token_type, response: Response):
        def query(session, actor):
            if workspace_id != actor.workspace_id:
                raise DomainError(404, "NOT_FOUND")
            latest_attempt = (
                select(Attempt.id)
                .where(Attempt.workspace_id == Worker.workspace_id, Attempt.worker_id == Worker.id)
                .order_by(Attempt.started_at.desc(), Attempt.id.desc())
                .limit(1)
                .correlate(Worker)
                .scalar_subquery()
            )
            workers = session.execute(
                select(Worker, Principal, Attempt)
                .join(
                    Principal,
                    (Principal.workspace_id == Worker.workspace_id) & (Principal.id == Worker.id),
                )
                .outerjoin(Attempt, Attempt.id == latest_attempt)
                .where(Worker.workspace_id == workspace_id)
                .order_by(Worker.name, Worker.id)
            )
            now = db_now(session)
            return {
                "workers": [
                    worker_view(worker, credential, attempt, now)
                    for worker, credential, attempt in workers
                ]
            }

        response.headers["Cache-Control"] = "no-store"
        return service.read(token, query)

    @router.get("/v1/operations/tasks", tags=["Tasks"])
    def tasks_list(
        token: token_type,
        response: Response,
        before: str | None = Query(default=None, max_length=36),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        def query(session, actor):
            latest_run = (
                select(Run.id)
                .where(Run.workspace_id == actor.workspace_id, Run.task_id == Task.id)
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(1)
                .correlate(Task)
                .scalar_subquery()
            )
            touched = func.coalesce(Task.retention_touched_at, 0)
            statement = (
                select(Task, Run)
                .select_from(Task)
                .outerjoin(Run, Run.id == latest_run)
                .where(Task.workspace_id == actor.workspace_id)
            )
            if before:
                anchor = owned(session, Task, before, actor.workspace_id)
                statement = statement.where(
                    tuple_(touched, Task.id) < (anchor.retention_touched_at or 0, anchor.id)
                )
            rows = session.execute(
                statement.order_by(touched.desc(), Task.id.desc()).limit(limit + 1)
            ).all()
            return {
                "tasks": [
                    (
                        {
                            "run_id": run.id,
                            "spec_version": run.spec_version,
                            "status": run.status,
                            "phase": run.phase,
                            "state_revision": run.state_revision,
                            "stop_reason": run.stop_reason,
                        }
                        if run
                        else {"run_id": None}
                    )
                    | {
                        "task_id": task.id,
                        "task_title": task.title,
                        "task_spec_version": task.latest_spec_version,
                        "task_status": task_stage(task, run),
                    }
                    for task, run in rows[:limit]
                ],
                "next_before": rows[limit - 1][0].id if len(rows) > limit else None,
            }

        response.headers["Cache-Control"] = "no-store"
        return service.read(token, query)

    @router.get("/v1/runs", tags=["Runs"])
    def runs_list(
        token: token_type,
        response: Response,
        before: str | None = Query(default=None, max_length=36),
        limit: int = Query(default=50, ge=1, le=100),
        latest_per_task: bool = False,
    ):
        def query(session, actor):
            statement = (
                select(
                    Run.id.label("run_id"),
                    Task.title.label("task_title"),
                    Run.status,
                    Run.phase,
                    Run.state_revision,
                    Run.stop_reason,
                    Run.spec_version,
                    func.coalesce(Run.snapshot["clarification_only"].as_boolean(), False).label(
                        "clarification_only"
                    ),
                    func.coalesce(Run.snapshot["execution_requested"].as_boolean(), False).label(
                        "execution_requested"
                    ),
                )
                .join(Task, (Task.id == Run.task_id) & (Task.workspace_id == Run.workspace_id))
                .where(Run.workspace_id == actor.workspace_id)
            )
            if latest_per_task:
                # Choose the latest Run before applying the page cursor, so older Runs
                # for an already listed task cannot reappear on a later page.
                latest_ids = (
                    select(Run.id)
                    .where(Run.workspace_id == actor.workspace_id)
                    .distinct(Run.task_id)
                    .order_by(Run.task_id, Run.created_at.desc(), Run.id.desc())
                )
                statement = statement.where(Run.id.in_(latest_ids))
            if before is not None:
                anchor = owned(session, Run, before, actor.workspace_id)
                statement = statement.where(
                    tuple_(Run.created_at, Run.id) < (anchor.created_at, anchor.id)
                )
            rows = (
                session.execute(
                    statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
                )
                .mappings()
                .all()
            )
            return {
                "runs": [dict(row) for row in rows[:limit]],
                "next_before": rows[limit - 1]["run_id"] if len(rows) > limit else None,
            }

        response.headers["Cache-Control"] = "no-store"
        return service.read(token, query)

    @router.get("/v1/runs/{run_id}/events", tags=["Runs"])
    async def run_events(
        run_id: str,
        token: token_type,
        last_event_id: Annotated[str | None, Header(max_length=80)] = None,
        wait_seconds: Annotated[int, Query(ge=0, le=25)] = 25,
    ):
        def read_batch(cursor, *, initial=False):
            def query(session, actor):
                run = owned(session, Run, run_id, actor.workspace_id)
                if initial and last_event_id is not None:
                    owner, separator, number = last_event_id.partition(":")
                    if not separator or not number.isascii() or not number.isdecimal():
                        raise DomainError(422, "INVALID_EVENT_CURSOR")
                    if owner != run_id:
                        raise DomainError(404, "NOT_FOUND")
                    cursor_value = int(number)
                else:
                    cursor_value = cursor
                return cursor_value, event_batch(session, run, cursor_value)

            return service.read(token, query)

        recovery = {
            "code": "EVENT_CURSOR_EXPIRED",
            "history": f"/v1/runs/{run_id}/event-history",
            "resume": "Reload retained history and reconnect without Last-Event-ID.",
        }
        try:
            revision, first = await run_in_threadpool(read_batch, None, initial=True)
        except DomainError as error:
            if error.status != 410:
                raise
            return JSONResponse(status_code=410, content=recovery)

        async def stream():
            cursor, batch = revision, first
            deadline = time.monotonic() + wait_seconds
            while True:
                if not batch:
                    yield ": keepalive\n\n"
                for item in batch:
                    cursor = item["revision"]
                    yield f"id: {run_id}:{cursor}\ndata: {json.dumps(item)}\n\n"
                if time.monotonic() >= deadline:
                    return
                if len(batch) < 1000:
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                try:
                    cursor, batch = await run_in_threadpool(read_batch, cursor)
                except DomainError as error:
                    if error.status == 410:
                        yield f"event: reset\ndata: {json.dumps(recovery)}\n\n"
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return router
