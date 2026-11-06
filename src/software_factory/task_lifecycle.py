"""Task-facing stages, independent of the internal Run execution states."""


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
