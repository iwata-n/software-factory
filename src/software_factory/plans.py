"""Versioned plans, bounded replanning and approvals bound to exact immutable targets."""

from pydantic import ValidationError
from sqlalchemy import func, select

from .contracts import Plan, ProjectPolicy, TaskSpec, digest, validate_plan
from .db import db_now
from .models import (
    ApprovalDecision,
    ApprovalRequest,
    Attempt,
    AuditEvent,
    Gate,
    Job,
    PlanVersion,
    Project,
    Repository,
    Run,
    Task,
    new_id,
)


def approval_view(item):
    return {
        "approval_id": item.id,
        **{
            key: getattr(item, key)
            for key in (
                "run_id",
                "plan_id",
                "action",
                "target",
                "target_hash",
                "status",
                "created_at",
                "expires_at",
            )
        },
    }


class PlanCommands:
    def __init__(self, service):
        self.service = service

    def invalidate(self, session, run):
        for request in session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == run.workspace_id,
                ApprovalRequest.run_id == run.id,
                ApprovalRequest.status == "PENDING",
            )
        ):
            request.status = "INVALIDATED"
        run.wait_context = None

    def plan_jobs(self, session, run):
        return list(
            session.scalars(
                select(Job).where(
                    Job.workspace_id == run.workspace_id, Job.run_id == run.id, Job.kind == "PLAN"
                )
            )
        )

    def schedule_replan(self, session, run, reason, rejected=None):
        from .service import transition

        count = len(self.plan_jobs(session, run)) - 1
        if count >= run.snapshot["execution"]["max_replans"]:
            return False
        self.invalidate(session, run)
        for job in session.scalars(
            select(Job).where(
                Job.workspace_id == run.workspace_id,
                Job.run_id == run.id,
                Job.state.in_(["READY", "RETRY_WAIT", "PENDING"]),
            )
        ):
            job.state = "CANCELLED"
        transition(
            session,
            run,
            "PLAN_REPLAN_REQUESTED",
            reason,
            step_key=f"plan:{count + 1}",
            payload={
                "replan_count": count + 1,
                "rejected_plan_id": rejected,
                "reason": reason,
                "spec_hash": run.snapshot["task_spec_hash"],
                "base_sha": run.base_sha,
            },
        )
        return True

    def complete(self, session, actor, attempt, job, run, raw):
        from .service import transition

        try:
            plan = Plan.model_validate(raw)
            policy = ProjectPolicy.model_validate(run.snapshot["policy"]).model_copy(
                update={
                    "symlink_paths": run.snapshot["policy"].get("symlink_paths", [])
                    + run.snapshot.get("source", {}).get("symlink_paths", [])
                }
            )
            error = validate_plan(plan, TaskSpec.model_validate(run.snapshot["task_spec"]), policy)
        except ValidationError:
            error = "PLAN_SCHEMA_INVALID"
        if raw.get("open_questions"):
            error = "PLAN_HAS_OPEN_QUESTIONS"
        if any(
            key in raw for key in ("task_spec", "objective", "acceptance_criteria", "repository_id")
        ):
            error = "SPEC_CHANGE_REQUIRED"
        number = (
            session.scalar(
                select(func.max(PlanVersion.version)).where(
                    PlanVersion.workspace_id == run.workspace_id, PlanVersion.run_id == run.id
                )
            )
            or 0
        ) + 1
        record = PlanVersion(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            version=number,
            body=raw,
            body_hash=digest(raw),
            validation_error=error,
            producer_job_id=job.id,
        )
        session.add(record)
        session.flush()
        self.service.checkpoint("plan.created")
        if error:
            structural = error in {
                "PLAN_SCHEMA_INVALID",
                "PLAN_ACCEPTANCE_COVERAGE",
                "PLAN_INVALID_DEPENDENCIES",
            }
            structural &= set(raw).issubset({"units", "assumptions", "open_questions"})
            if structural and self.schedule_replan(session, run, error, record.id):
                return
            transition(session, run, "BLOCKED", error)
        elif run.snapshot.get("clarification_only") or run.snapshot["policy"].get(
            "require_plan_approval", False
        ):
            transition(session, run, "PLAN_APPROVAL_REQUIRED")
            self.request_approval(session, run, record)
        else:
            self.adopt(session, run, record, "PLAN_ACCEPTED")

    def adopt(self, session, run, plan, event_kind):
        from .service import transition

        transition(
            session,
            run,
            event_kind,
            payload={"plan_id": plan.id, "plan_hash": plan.body_hash},
            step_key=f"implement:plan:{plan.version}" if plan.version > 1 else None,
            depends_on=(plan.producer_job_id,) if plan.producer_job_id else (),
        )

    def request_approval(self, session, run, plan):
        from .service import event

        now = db_now(session)
        request = ApprovalRequest(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            plan_id=plan.id,
            action="approve_plan",
            status="PENDING",
            created_at=now,
            expires_at=now + 86400,
        )
        gate = session.scalar(
            select(Gate)
            .where(
                Gate.workspace_id == run.workspace_id, Gate.candidate_id == run.current_candidate_id
            )
            .order_by(Gate.created_at.desc(), Gate.id)
            .limit(1)
        )
        event(
            session,
            run,
            "PLAN_APPROVAL_REQUESTED",
            {"approval_id": request.id, "plan_id": plan.id, "expires_at": request.expires_at},
        )
        request.target = {
            "action": "approve_plan",
            "run_id": run.id,
            "run_revision": run.state_revision,
            "candidate_id": run.current_candidate_id,
            "plan_id": plan.id,
            "plan_hash": plan.body_hash,
            "spec_hash": run.snapshot["task_spec_hash"],
            "spec_version": run.spec_version,
            "base_sha": run.base_sha,
            "policy_hash": run.snapshot["policy_hash"],
            "profile_hash": run.snapshot["verification_profile_hash"],
            "policy_version": run.snapshot["policy"]["version"],
            "gate_id": gate.id if gate else None,
            "evidence_digest": gate.evidence_digest if gate else None,
            "budget_microusd": run.budget_microusd,
            "expires_at": request.expires_at,
        }
        request.target_hash = digest(request.target)
        session.add(request)
        run.wait_context = {
            "kind": "PLAN_APPROVAL",
            "approval_id": request.id,
            "plan_id": plan.id,
            "plan_hash": plan.body_hash,
            "expires_at": request.expires_at,
        }
        session.flush()
        self.service.checkpoint("approval.created")
        return request

    def decide(self, session, actor, approval_id, data):
        from .service import DomainError, event, owned, run_view, transition

        request = owned(session, ApprovalRequest, approval_id, actor.workspace_id)
        run = owned(session, Run, request.run_id, actor.workspace_id)
        from .task_lifecycle import lock_run_task

        lock_run_task(session, run)
        if request.action == "approve_budget":
            return self.service.budgets.decide(session, actor, request, data)
        run = owned(session, Run, request.run_id, actor.workspace_id)
        if request.action != "approve_plan":
            raise DomainError(409, "APPROVAL_ACTION_MISMATCH")
        if request.status == "EXPIRED":
            raise DomainError(409, "APPROVAL_EXPIRED")
        if request.status == "INVALIDATED":
            raise DomainError(409, "APPROVAL_TARGET_STALE")
        if request.status != "PENDING":
            raise DomainError(409, "APPROVAL_ALREADY_DECIDED")
        if db_now(session) >= request.expires_at:
            raise DomainError(409, "APPROVAL_EXPIRED")
        if data.target_hash != request.target_hash or digest(request.target) != request.target_hash:
            raise DomainError(409, "APPROVAL_TARGET_MISMATCH")
        target = request.target
        latest = session.scalar(
            select(PlanVersion)
            .where(PlanVersion.workspace_id == actor.workspace_id, PlanVersion.run_id == run.id)
            .order_by(PlanVersion.version.desc())
            .limit(1)
        )
        if (
            latest.id != request.plan_id
            or latest.body_hash != target["plan_hash"]
            or digest(latest.body) != target["plan_hash"]
            or latest.validation_error
        ):
            raise DomainError(409, "APPROVAL_PLAN_STALE")
        task = owned(session, Task, run.task_id, actor.workspace_id)
        project = owned(session, Project, task.project_id, actor.workspace_id)
        if (
            task.latest_spec_version != target["spec_version"]
            or run.snapshot["task_spec_hash"] != target["spec_hash"]
            or digest(run.snapshot["task_spec"]) != target["spec_hash"]
        ):
            raise DomainError(409, "APPROVAL_SPEC_STALE")
        if (
            run.snapshot["verification_profile_hash"] != target["profile_hash"]
            or digest(run.snapshot["verification_profile"]) != target["profile_hash"]
        ):
            raise DomainError(409, "APPROVAL_PROFILE_STALE")
        if (
            run.snapshot["policy_hash"] != target["policy_hash"]
            or digest(project.policy) != target["policy_hash"]
            or run.snapshot["policy"]["version"] != target["policy_version"]
        ):
            raise DomainError(409, "APPROVAL_POLICY_STALE")
        if run.base_sha != target["base_sha"] or run.snapshot["base_sha"] != target["base_sha"]:
            raise DomainError(409, "APPROVAL_BASE_STALE")
        if not owned(session, Repository, run.repository_id, actor.workspace_id).active:
            raise DomainError(409, "APPROVAL_REPOSITORY_REVOKED")
        if run.current_candidate_id != target["candidate_id"]:
            raise DomainError(409, "APPROVAL_CANDIDATE_STALE")
        current_gate = session.scalar(
            select(Gate)
            .where(
                Gate.workspace_id == run.workspace_id, Gate.candidate_id == run.current_candidate_id
            )
            .order_by(Gate.created_at.desc(), Gate.id)
            .limit(1)
        )
        if target["gate_id"] != (current_gate.id if current_gate else None) or target[
            "evidence_digest"
        ] != (current_gate.evidence_digest if current_gate else None):
            raise DomainError(409, "APPROVAL_EVIDENCE_STALE")
        if run.budget_microusd != target["budget_microusd"]:
            raise DomainError(409, "APPROVAL_BUDGET_STALE")
        if (
            run.state_revision != target["run_revision"]
            or data.expected_run_revision != run.state_revision
            or run.status != "WAITING_EXTERNAL"
            or run.phase != "WAIT_PLAN_APPROVAL"
            or (run.wait_context or {}).get("approval_id") != request.id
        ):
            raise DomainError(409, "APPROVAL_RUN_REVISION_STALE")
        before = run.state_revision
        request.status = "APPROVED" if data.decision == "approve" else "DENIED"
        run.wait_context = None
        if data.decision == "approve":
            self.adopt(session, run, latest, "PLAN_APPROVED")
        else:
            transition(session, run, "BLOCKED", "PLAN_REJECTED")
        event(
            session,
            run,
            "APPROVAL_DECIDED",
            {
                "approval_id": request.id,
                "action": request.action,
                "decision": data.decision,
                "actor_id": actor.id,
                "reason": data.reason,
                "target_hash": request.target_hash,
            },
        )
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
                operation="approval.decide",
                body={
                    "action": request.action,
                    "target": target,
                    "decision": data.decision,
                    "reason": data.reason,
                    "before_revision": before,
                    "after_revision": run.state_revision,
                },
                created_at=db_now(session),
            )
        )
        session.flush()
        self.service.checkpoint("approval.decided")
        return {"decision_id": result.id, "approval": approval_view(request), "run": run_view(run)}

    def replan(self, session, actor, run_id, data):
        from .domain import TERMINAL
        from .service import DomainError, owned, run_view, transition

        run = owned(session, Run, run_id, actor.workspace_id)
        from .task_lifecycle import lock_run_task

        lock_run_task(session, run)
        if run.state_revision != data.expected_run_revision:
            raise DomainError(409, "RUN_REVISION_CONFLICT")
        if run.status in TERMINAL or run.status == "CANCELLING":
            raise DomainError(409, "RUN_TERMINAL")
        if run.stop_reason == "SPEC_CHANGE_REQUIRED":
            raise DomainError(409, "SPEC_CHANGE_REQUIRED")
        before_revision = run.state_revision
        active = session.scalar(
            select(Attempt.id)
            .join(Job, Attempt.job_id == Job.id)
            .where(
                Attempt.workspace_id == run.workspace_id,
                Job.run_id == run.id,
                Attempt.state == "LEASED",
            )
            .limit(1)
        )
        original = TaskSpec.model_validate(run.snapshot["task_spec"]).task.model_dump()
        if data.requested_task and data.requested_task.model_dump() != original:
            self.invalidate(session, run)
            if active:
                transition(session, run, "SPEC_CHANGE_REQUESTED")
                for job in session.scalars(
                    select(Job).where(
                        Job.run_id == run.id, Job.state.in_(["READY", "PENDING", "RETRY_WAIT"])
                    )
                ):
                    job.state = "CANCELLED"
            else:
                transition(session, run, "BLOCKED", "SPEC_CHANGE_REQUIRED")
        elif active:
            raise DomainError(409, "EXECUTION_ACTIVE")
        elif not self.schedule_replan(session, run, data.reason):
            transition(session, run, "BLOCKED", "REPLAN_LIMIT_REACHED")
        session.add(
            AuditEvent(
                workspace_id=run.workspace_id,
                actor_id=actor.id,
                operation="plan.replan.requested",
                body={
                    "run_id": run.id,
                    "reason": data.reason,
                    "before_revision": before_revision,
                    "after_revision": run.state_revision,
                    "requested_task_hash": digest(data.requested_task.model_dump())
                    if data.requested_task
                    else None,
                },
                created_at=db_now(session),
            )
        )
        return run_view(run)

    def expire(self, session, workspace_id):
        from .service import owned, transition

        now = db_now(session)
        for request in session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workspace_id == workspace_id,
                ApprovalRequest.status == "PENDING",
                ApprovalRequest.expires_at <= now,
            )
        ):
            request.status = "EXPIRED"
            run = owned(session, Run, request.run_id, workspace_id)
            if request.action == "approve_budget":
                self.service.budgets.expire(session, request, run)
                continue
            if (
                run.status == "WAITING_EXTERNAL"
                and run.phase == "WAIT_PLAN_APPROVAL"
                and (run.wait_context or {}).get("approval_id") == request.id
            ):
                transition(session, run, "BLOCKED", "PLAN_APPROVAL_EXPIRED")
