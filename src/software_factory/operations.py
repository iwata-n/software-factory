"""Read-only operations console and workspace-scoped Run index."""

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select, tuple_
from starlette.concurrency import run_in_threadpool

from .models import Run, RunEvent, Task
from .service import DomainError, owned

UI_DIR = Path(__file__).with_name("operations_ui")


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
        if asset not in {"board.js", "board.css"}:
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

    @router.get("/v1/runs", tags=["Runs"])
    def runs_list(
        token: token_type,
        response: Response,
        before: str | None = Query(default=None, max_length=36),
        limit: int = Query(default=50, ge=1, le=100),
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
                )
                .join(Task, (Task.id == Run.task_id) & (Task.workspace_id == Run.workspace_id))
                .where(Run.workspace_id == actor.workspace_id)
            )
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
