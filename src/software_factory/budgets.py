"""Budget increases require a reasoned decision on one fixed target; no quality override."""

from sqlalchemy import select

from .contracts import digest
from .db import db_now
from .models import (
    ApprovalDecision,
    ApprovalRequest,
    Attempt,
    AuditEvent,
    BudgetVersion,
    Gate,
    Job,
    PlanVersion,
    Project,
    Run,
    Task,
    new_id,
)

BUDGET_REASONS = {
    "BUDGET_EXHAUSTED",
    "BUDGET_EXCEEDED",
    "USAGE_UNKNOWN",
    "BUDGET_APPROVAL_EXPIRED",
    "BUDGET_REJECTED",
    "BUDGET_APPROVAL_STALE",
}


class BudgetCommands:
    def __init__(self, service):
        self.service = service

    def subject(self, session, run):
        plan = session.scalar(
            select(PlanVersion)
            .where(PlanVersion.workspace_id == run.workspace_id, PlanVersion.run_id == run.id)
            .order_by(PlanVersion.version.desc())
            .limit(1)
        )
        gate = session.scalar(
            select(Gate)
            .where(
                Gate.workspace_id == run.workspace_id, Gate.candidate_id == run.current_candidate_id
            )
            .order_by(Gate.created_at.desc(), Gate.id)
            .limit(1)
        )
        return {
            "run_id": run.id,
            "candidate_id": run.current_candidate_id,
            "plan_id": plan.id if plan else None,
            "plan_hash": plan.body_hash if plan else None,
            "gate_id": gate.id if gate else None,
            "evidence_digest": gate.evidence_digest if gate else None,
            "spec_hash": run.snapshot["task_spec_hash"],
            "spec_version": run.spec_version,
            "base_sha": run.base_sha,
            "profile_hash": run.snapshot["verification_profile_hash"],
            "policy_hash": run.snapshot["policy_hash"],
            "policy_version": run.snapshot["policy"]["version"],
            "current_budget_microusd": run.budget_microusd,
            "budget_version": run.budget_version,
            "spent_microusd": run.spent_microusd,
            "reserved_microusd": run.reserved_microusd,
            "cost_state": run.cost_state,
        }

    def request(self, session, actor, run_id, data):
        from .domain import TERMINAL
        from .plans import approval_view
        from .service import DomainError, event, owned, run_view

        run = owned(session, Run, run_id, actor.workspace_id)
        if (
            run.state_revision != data.expected_run_revision
            or run.budget_microusd != data.current_budget_microusd
        ):
            raise DomainError(409, "BUDGET_TARGET_STALE")
        if run.status in TERMINAL or run.status == "CANCELLING":
            raise DomainError(409, "RUN_TERMINAL")
        if run.status == "BLOCKED" and run.stop_reason not in BUDGET_REASONS:
            raise DomainError(409, "BUDGET_CANNOT_OVERRIDE_BLOCK")
        self.service.costs.require_balanced(session, run)
        if data.new_budget_microusd < run.spent_microusd + run.reserved_microusd:
            raise DomainError(422, "BUDGET_BELOW_OBLIGATIONS")
        if data.new_budget_microusd <= run.budget_microusd:
            raise DomainError(422, "BUDGET_INCREASE_REQUIRED")
        if (
            session.scalar(
                select(Attempt.id)
                .join(Job, Job.id == Attempt.job_id)
                .where(Job.run_id == run.id, Attempt.state == "LEASED")
                .limit(1)
            )
            or run.reserved_microusd
        ):
            raise DomainError(409, "EXECUTION_ACTIVE")
        resume = {
            "status": run.status,
            "phase": run.phase,
            "reason": run.stop_reason,
            "wait_context": run.wait_context,
        }
        if (run.wait_context or {}).get("kind") == "BUDGET_APPROVAL":
            previous = owned(
                session, ApprovalRequest, run.wait_context["approval_id"], actor.workspace_id
            )
            if (
                previous.status == "PENDING"
                and previous.target["run_revision"] == run.state_revision
            ):
                raise DomainError(409, "APPROVAL_PENDING")
            resume = previous.target["resume"]
        self.service.plans.invalidate(session, run)
        now = db_now(session)
        request = ApprovalRequest(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            action="approve_budget",
            target={},
            target_hash="",
            status="PENDING",
            created_at=now,
            expires_at=now + 86400,
        )
        run.status, run.phase, run.stop_reason = "WAITING_EXTERNAL", "WAIT_BUDGET_APPROVAL", None
        event(
            session,
            run,
            "BUDGET_APPROVAL_REQUESTED",
            {
                "approval_id": request.id,
                "current_budget_microusd": run.budget_microusd,
                "new_budget_microusd": data.new_budget_microusd,
            },
        )
        request.target = self.subject(session, run) | {
            "action": "approve_budget",
            "run_revision": run.state_revision,
            "new_budget_microusd": data.new_budget_microusd,
            "expires_at": request.expires_at,
            "request_reason": data.reason,
            "resume": resume,
        }
        request.target_hash = digest(request.target)
        session.add(request)
        run.wait_context = {"kind": "BUDGET_APPROVAL", "approval_id": request.id}
        session.flush()
        self.service.checkpoint("budget.requested")
        return {"approval": approval_view(request), "run": run_view(run)}

    def decide(self, session, actor, request, data):
        from .plans import approval_view
        from .service import DomainError, event, owned, run_view, transition

        run = owned(session, Run, request.run_id, actor.workspace_id)
        if request.status == "EXPIRED" or db_now(session) >= request.expires_at:
            raise DomainError(409, "APPROVAL_EXPIRED")
        if request.status == "INVALIDATED":
            raise DomainError(409, "APPROVAL_TARGET_STALE")
        if request.status != "PENDING":
            raise DomainError(409, "APPROVAL_ALREADY_DECIDED")
        if data.target_hash != request.target_hash or digest(request.target) != request.target_hash:
            raise DomainError(409, "APPROVAL_TARGET_MISMATCH")
        if (
            run.status != "WAITING_EXTERNAL"
            or run.phase != "WAIT_BUDGET_APPROVAL"
            or (run.wait_context or {}).get("approval_id") != request.id
            or data.expected_run_revision != run.state_revision
            or request.target["run_revision"] != run.state_revision
        ):
            raise DomainError(409, "BUDGET_TARGET_STALE")
        self.service.costs.require_balanced(session, run)
        subject = self.subject(session, run)
        if any(request.target[key] != value for key, value in subject.items()):
            raise DomainError(409, "BUDGET_TARGET_STALE")
        task = owned(session, Task, run.task_id, actor.workspace_id)
        project = owned(session, Project, task.project_id, actor.workspace_id)
        if (
            task.latest_spec_version != run.spec_version
            or digest(project.policy) != run.snapshot["policy_hash"]
        ):
            raise DomainError(409, "BUDGET_POLICY_OR_SPEC_STALE")
        limit = request.target["new_budget_microusd"]
        if limit < run.spent_microusd + run.reserved_microusd:
            raise DomainError(422, "BUDGET_BELOW_OBLIGATIONS")
        before = run.state_revision
        request.status = "APPROVED" if data.decision == "approve" else "DENIED"
        if data.decision == "approve":
            run.budget_microusd = limit
            run.budget_version += 1
            session.add(
                BudgetVersion(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    version=run.budget_version,
                    limit_microusd=limit,
                    approval_id=request.id,
                    reason=data.reason,
                    created_at=db_now(session),
                )
            )
            self.resume(session, run, request.target["resume"])
            event(
                session,
                run,
                "BUDGET_CHANGED",
                {
                    "approval_id": request.id,
                    "version": run.budget_version,
                    "limit_microusd": limit,
                    "reason": data.reason,
                },
            )
        else:
            run.phase = request.target["resume"]["phase"]
            run.wait_context = request.target["resume"]["wait_context"]
            transition(session, run, "BLOCKED", "BUDGET_REJECTED")
        event(
            session,
            run,
            "APPROVAL_DECIDED",
            {
                "approval_id": request.id,
                "action": "approve_budget",
                "decision": data.decision,
                "actor_id": actor.id,
                "reason": data.reason,
                "target_hash": request.target_hash,
            },
        )
        if run.phase == "WAIT_PLAN_APPROVAL" and data.decision == "approve":
            plan = owned(session, PlanVersion, request.target["plan_id"], run.workspace_id)
            self.service.plans.request_approval(session, run, plan)
        result = ApprovalDecision(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            approval_id=request.id,
            actor_id=actor.id,
            decision=data.decision,
            reason=data.reason,
            target_hash=request.target_hash,
            before_revision=before,
            after_revision=run.state_revision,
            created_at=db_now(session),
        )
        session.add(result)
        session.add(
            AuditEvent(
                workspace_id=run.workspace_id,
                actor_id=actor.id,
                operation="budget.decide",
                body={
                    "action": "approve_budget",
                    "target": request.target,
                    "decision": data.decision,
                    "reason": data.reason,
                    "before_revision": before,
                    "after_revision": run.state_revision,
                },
                created_at=db_now(session),
            )
        )
        session.flush()
        self.service.checkpoint("budget.decided")
        return {"decision_id": result.id, "approval": approval_view(request), "run": run_view(run)}

    def resume(self, session, run, resume):
        from .service import transition

        run.phase = resume["phase"]
        run.wait_context = resume["wait_context"]
        previous = (run.wait_context or {}).get("previous_wait")
        if previous and previous.get("kind") in {"CI", "PLAN_APPROVAL"}:
            run.wait_context = previous
            run.status, run.stop_reason = "WAITING_EXTERNAL", None
            return
        if (run.wait_context or {}).get("kind") in {"CI", "PLAN_APPROVAL"}:
            run.status, run.stop_reason = "WAITING_EXTERNAL", None
            return
        if resume["status"] == "WAITING_EXTERNAL":
            run.status, run.stop_reason = "WAITING_EXTERNAL", None
            return
        if resume["reason"] == "USAGE_UNKNOWN" and run.cost_state == "UNKNOWN":
            run.status, run.stop_reason = "BLOCKED", "USAGE_UNKNOWN"
            return
        blocked_job = (run.wait_context or {}).get("job_id")
        if blocked_job:
            job = session.get(Job, blocked_job)
            if job and job.state in {"BLOCKED", "FAILED", "CANCELLED"}:
                if job.attempt_no >= run.snapshot["execution"]["max_infra_attempts_per_job"]:
                    transition(session, run, "BLOCKED", "INFRA_ATTEMPTS_EXHAUSTED")
                    return
                job.state, job.available_at = "RETRY_WAIT", db_now(session)
        run.status, run.stop_reason, run.wait_context = "QUEUED", None, None

    def expire(self, session, request, run):
        from .service import transition

        if (
            run.phase == "WAIT_BUDGET_APPROVAL"
            and (run.wait_context or {}).get("approval_id") == request.id
        ):
            transition(session, run, "BLOCKED", "BUDGET_APPROVAL_EXPIRED")
