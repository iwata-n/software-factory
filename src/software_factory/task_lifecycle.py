"""Task-facing stages, independent of the internal Run execution states."""

from sqlalchemy import select

from .models import Task


def active_task(session, task_id, workspace_id, *, lock=False):
    from .service import DomainError

    statement = select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
    if lock:
        # Mutation callers hold the existing Workspace mutex before taking this lock.
        statement = statement.with_for_update().execution_options(populate_existing=True)
    task = session.scalar(statement)
    if task is None or task.deleted_at is not None:
        raise DomainError(404, "NOT_FOUND")
    return task


def lock_run_task(session, run):
    return active_task(session, run.task_id, run.workspace_id, lock=True)


def run_stage(run):
    if run.status in {"SUCCEEDED", "CANCELLED"}:
        return "DONE"
    if run.status == "FAILED":
        return "BLOCKED"
    if run.status == "BLOCKED":
        return (
            "CLARIFYING"
            if run.stop_reason in {"PLAN_HAS_OPEN_QUESTIONS", "SPEC_CHANGE_REQUIRED"}
            else "BLOCKED"
        )
    if run.phase == "WAIT_PLAN_APPROVAL" and run.status == "WAITING_EXTERNAL":
        return "READY"
    if run.status == "CANCELLING":
        return "RUNNING"
    if run.snapshot.get("execution_requested"):
        return "RUNNING"
    if run.snapshot.get("clarification_only") and run.phase == "PLAN":
        return "CLARIFYING"
    if run.phase == "PLAN":
        return "DRAFT" if run.status == "QUEUED" else "CLARIFYING"
    return "RUNNING"


def task_stage(task, run):
    if run and run.spec_version == task.latest_spec_version:
        return run_stage(run)
    if (
        run
        and run.status in {"QUEUED", "RUNNING", "WAITING_EXTERNAL", "CANCELLING"}
        and run_stage(run) == "RUNNING"
    ):
        return "RUNNING"
    if task.clarified_spec_version == task.latest_spec_version:
        return "READY"
    return "DRAFT"
