"""Pure decisions: no clock, database, network, or Agent calls."""

from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    BLOCKED = "BLOCKED"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


@dataclass(frozen=True)
class RunSnapshot:
    status: str
    phase: str
    reason: str | None = None


@dataclass(frozen=True)
class DomainEvent:
    kind: str
    reason: str | None = None


@dataclass(frozen=True)
class Decision:
    status: str
    phase: str
    reason: str | None = None
    next_job: str | None = None


@dataclass(frozen=True)
class TransitionPolicy:
    workflow_version: int = 1


DEFAULT_POLICY = TransitionPolicy()


def decide(
    run: RunSnapshot,
    event: DomainEvent,
    policy: TransitionPolicy = DEFAULT_POLICY,
    *,
    now: float | None = None,
) -> Decision:
    if policy.workflow_version != 1:
        raise ValueError("unsupported workflow version")
    if run.status in TERMINAL:
        return Decision(run.status, run.phase, run.reason)
    if event.kind == "CANCEL_REQUESTED":
        return Decision(RunStatus.CANCELLING, run.phase, "CANCEL_REQUESTED")
    if event.kind == "SPEC_CHANGE_REQUESTED":
        return Decision(RunStatus.CANCELLING, run.phase, "SPEC_CHANGE_REQUIRED")
    if run.status == RunStatus.CANCELLING:
        if event.kind == "EXECUTION_RIGHT_ENDED":
            if run.reason == "SPEC_CHANGE_REQUIRED":
                return Decision(RunStatus.BLOCKED, run.phase, "SPEC_CHANGE_REQUIRED")
            return Decision(RunStatus.CANCELLED, run.phase, "EXECUTION_RIGHT_ENDED")
        return Decision(run.status, run.phase, "CANCEL_REQUESTED")
    if event.kind == "ATTEMPTS_EXHAUSTED":
        return Decision(RunStatus.FAILED, run.phase, "INFRA_ATTEMPTS_EXHAUSTED")
    if event.kind == "BLOCKED":
        return Decision(RunStatus.BLOCKED, run.phase, event.reason)
    if (
        event.kind == "PLAN_APPROVED"
        and run.status == RunStatus.WAITING_EXTERNAL
        and run.phase == "WAIT_PLAN_APPROVAL"
    ):
        return Decision(RunStatus.QUEUED, "IMPLEMENT", next_job="IMPLEMENT")
    if event.kind == "PLAN_REPLAN_REQUESTED" and run.status in {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.BLOCKED,
        RunStatus.WAITING_EXTERNAL,
    }:
        return Decision(RunStatus.QUEUED, "PLAN", next_job="PLAN")
    if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
        raise ValueError(f"cannot advance {run.status}")
    if event.kind == "PLAN_ACCEPTED" and run.phase == "PLAN":
        return Decision(RunStatus.QUEUED, "IMPLEMENT", next_job="IMPLEMENT")
    if event.kind == "PLAN_APPROVAL_REQUIRED" and run.phase == "PLAN":
        return Decision(RunStatus.WAITING_EXTERNAL, "WAIT_PLAN_APPROVAL")
    if event.kind == "IMPLEMENT_EXECUTED" and run.phase == "IMPLEMENT":
        # A fake implementation is not an immutable Candidate or verified code.
        return Decision(RunStatus.BLOCKED, "ASSEMBLE_CANDIDATE", "CHANGE_ARTIFACT_MISSING")
    if event.kind == "CHANGE_ARTIFACT_READY" and run.phase in {"IMPLEMENT", "REPAIR"}:
        return Decision(RunStatus.QUEUED, "ASSEMBLE_CANDIDATE", next_job="ASSEMBLE_CANDIDATE")
    if event.kind == "CANDIDATE_ASSEMBLED" and run.phase == "ASSEMBLE_CANDIDATE":
        return Decision(RunStatus.QUEUED, "VERIFY_REVIEW")
    raise ValueError(f"invalid event {event.kind} for {run.status}/{run.phase}")
