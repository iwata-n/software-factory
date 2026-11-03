"""Read-only operations console and workspace-scoped Run index."""

from pathlib import Path

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import select, tuple_

from .models import Run, Task
from .service import owned

UI_DIR = Path(__file__).with_name("operations_ui")


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

    return router
