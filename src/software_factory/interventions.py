"""Human commands share business rules, replay identity and atomic audit across API routes."""

from sqlalchemy import select

from .contracts import ApprovalSubmission, Cancel, digest
from .db import db_now
from .models import ApprovalRequest, Attempt, AuditEvent, Job, Project, Repository, Run, Task


class InterventionCommands:
    def __init__(self, service):
        self.service = service

    def subject(self, session, run):
        subject = self.service.budgets.subject(session, run) | {
            "run_revision": run.state_revision,
            "status": run.status,
            "phase": run.phase,
            "stop_reason": run.stop_reason,
            "live_evidence_digest": None,
        }
        if run.current_candidate_id:
            *_, decision = self.service.gates.inputs(session, run)
            subject["live_evidence_digest"] = decision.evidence_digest
        return subject

    def view(self, session, actor, run_id):
        from .service import owned

        run = owned(session, Run, run_id, actor.workspace_id)
        subject = self.subject(session, run)
        return {
            "subject_id": run.id,
            "target": subject,
            "target_hash": digest(subject),
            "approval_actions": ["approve_plan", "approve_budget"],
            "run_actions": ["cancel", "resolve_blocked"],
        }

    def approval(self, token, key, approval_id, data, *, run_id=None, action=None):
        from .service import DomainError, owned

        # Validate route/subject even on a cross-route replay. The transaction below
        # rechecks authorization and the actual decision under the workspace mutex.
        def authorize(session, actor):
            self.service.require_user_access(actor, "OPERATOR")
            request = owned(session, ApprovalRequest, approval_id, actor.workspace_id)
            if run_id is not None:
                owned(session, Run, run_id, actor.workspace_id)
                if request.run_id != run_id:
                    raise DomainError(404, "NOT_FOUND")
            if action is not None and request.action != action:
                raise DomainError(409, "APPROVAL_ACTION_MISMATCH")

        def check_replay(result):
            if run_id is not None and result["run"]["run_id"] != run_id:
                raise DomainError(404, "NOT_FOUND")
            if action is not None and result["approval"]["action"] != action:
                raise DomainError(409, "APPROVAL_ACTION_MISMATCH")

        def command(session, actor):
            authorize(session, actor)
            result = self.service.plans.decide(session, actor, approval_id, data)
            self.service.checkpoint("intervention.decided")
            return result

        return self.service.write(
            token,
            "USER",
            f"approval:{approval_id}:decision",
            key,
            data.model_dump(),
            command,
            replay_check=check_replay,
        )

    def execute(self, token, key, run_id, data):
        from .service import DomainError, owned

        if data.action in {"approve_plan", "approve_budget"}:
            return self.approval(
                token,
                key,
                data.subject_id,
                ApprovalSubmission(
                    expected_run_revision=data.expected_run_revision,
                    target_hash=data.target_hash,
                    decision=data.decision,
                    reason=data.reason,
                ),
                run_id=run_id,
                action=data.action,
            )

        def command(session, actor):
            owned(session, Run, run_id, actor.workspace_id)
            if data.subject_id != run_id:
                raise DomainError(404, "NOT_FOUND")
            if data.action == "cancel":
                return self.service.cancel(
                    session,
                    actor,
                    run_id,
                    Cancel(expected_run_revision=data.expected_run_revision, reason=data.reason),
                )
            return self.resolve(session, actor, run_id, data)

        # Cancellation shares identity with /cancel as well as its business command.
        body = (
            Cancel(
                expected_run_revision=data.expected_run_revision, reason=data.reason
            ).model_dump()
            if data.action == "cancel"
            else data.model_dump()
        )
        if data.action == "cancel" and data.subject_id != run_id:
            # Never replay a valid cancellation through a mismatched URL/subject.
            def authorize(session, actor):
                self.service.require_user_access(actor, "OPERATOR")
                owned(session, Run, run_id, actor.workspace_id)

            self.service.read(token, authorize)
            raise DomainError(404, "NOT_FOUND")
        return self.service.write(
            token,
            "USER",
            f"cancel:{run_id}" if data.action == "cancel" else f"resolve-blocked:{run_id}",
            key,
            body,
            command,
        )

    def require_current_context(self, session, run):
        from .service import DomainError, owned

        task = owned(session, Task, run.task_id, run.workspace_id)
        project = owned(session, Project, task.project_id, run.workspace_id)
        repository = owned(session, Repository, run.repository_id, run.workspace_id)
        if not repository.active:
            raise DomainError(409, "REPOSITORY_REVOKED")
        if (
            task.latest_spec_version != run.spec_version
            or digest(run.snapshot["task_spec"]) != run.snapshot["task_spec_hash"]
            or digest(project.policy) != run.snapshot["policy_hash"]
            or digest(run.snapshot["policy"]) != run.snapshot["policy_hash"]
            or digest(run.snapshot["verification_profile"])
            != run.snapshot["verification_profile_hash"]
            or run.base_sha != run.snapshot["base_sha"]
        ):
            raise DomainError(409, "INTERVENTION_CONTEXT_STALE")

    def resolve(self, session, actor, run_id, data):
        from .service import DomainError, event, owned, run_view

        run = owned(session, Run, run_id, actor.workspace_id)
        from .task_lifecycle import lock_run_task

        lock_run_task(session, run)
        if run.state_revision != data.expected_run_revision:
            raise DomainError(409, "RUN_REVISION_CONFLICT")
        if run.status != "BLOCKED":
            raise DomainError(409, "RUN_NOT_BLOCKED")
        target = self.subject(session, run)
        if digest(target) != data.target_hash:
            raise DomainError(409, "INTERVENTION_TARGET_STALE")
        self.require_current_context(session, run)
        self.service.costs.require_balanced(session, run)
        if run.reserved_microusd or session.scalar(
            select(Attempt.id)
            .join(Job, Job.id == Attempt.job_id)
            .where(Job.run_id == run.id, Attempt.state == "LEASED")
            .limit(1)
        ):
            raise DomainError(409, "EXECUTION_ACTIVE")
        before = run.state_revision
        cause = run.stop_reason
        if cause in {"BUDGET_EXHAUSTED", "BUDGET_EXCEEDED", "USAGE_UNKNOWN"}:
            if run.cost_state != "KNOWN":
                raise DomainError(409, "USAGE_NOT_RECONCILED")
            reservation = run.snapshot["policy"]["job_reservation_microusd"]
            if run.budget_microusd - run.spent_microusd < reservation:
                raise DomainError(409, "BUDGET_NOT_RESOLVED")
            context = run.wait_context or {}
            previous = context.get("previous_wait") or {}
            if previous.get("kind") == "CI":
                if run.phase != "WAIT_CI" or db_now(session) >= previous["deadline_at"]:
                    raise DomainError(409, "CI_WAIT_NOT_RESUMABLE")
                _, current_target, *_, decision = self.service.gates.inputs(session, run)
                if decision.status != "PASS" or current_target != previous["target"]:
                    raise DomainError(409, "EVIDENCE_NOT_RESOLVED")
                run.status, run.stop_reason, run.wait_context = "WAITING_EXTERNAL", None, previous
                reevaluation = {
                    "cost_state": run.cost_state,
                    "deadline_at": previous["deadline_at"],
                }
            else:
                job = session.get(Job, context.get("job_id")) if context.get("job_id") else None
                if job is not None:
                    if job.run_id != run.id or job.workspace_id != run.workspace_id:
                        raise DomainError(409, "RESUME_JOB_CONTEXT_STALE")
                    if job.attempt_no >= run.snapshot["execution"]["max_infra_attempts_per_job"]:
                        raise DomainError(409, "INFRA_ATTEMPTS_EXHAUSTED")
                    allowed = {"VERIFY", "REVIEW"} if run.phase == "VERIFY_REVIEW" else {run.phase}
                    if job.kind not in allowed:
                        raise DomainError(409, "RESUME_PHASE_NOT_ALLOWED")
                    if job.state not in {"BLOCKED", "FAILED", "CANCELLED"}:
                        raise DomainError(409, "RESUME_JOB_NOT_BLOCKED")
                    job.state, job.available_at = "RETRY_WAIT", db_now(session)
                elif not session.scalar(
                    select(Job.id).where(
                        Job.run_id == run.id, Job.state.in_(["READY", "RETRY_WAIT"])
                    )
                ):
                    raise DomainError(409, "RESUME_JOB_MISSING")
                run.status, run.stop_reason, run.wait_context = "QUEUED", None, None
                reevaluation = {"cost_state": run.cost_state, "job_id": job.id if job else None}
        elif cause in {
            "POLICY: SENSITIVE_MODEL_INPUT",
            "POLICY: MODEL_CHANGE_PATH_DENIED",
            "INCONCLUSIVE: MODEL_EDIT_ANCHOR_MISMATCH",
            "INCONCLUSIVE: PROJECT_READ_LIMIT",
        }:
            if run.phase not in {"PLAN", "IMPLEMENT", "REPAIR"}:
                raise DomainError(409, "RESUME_PHASE_NOT_ALLOWED")
            job = session.scalar(
                select(Job)
                .where(
                    Job.workspace_id == run.workspace_id,
                    Job.run_id == run.id,
                    Job.kind == run.phase,
                    Job.state.in_(["FAILED", "BLOCKED"]),
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
            if job is None:
                raise DomainError(409, "RESUME_JOB_MISSING")
            if job.attempt_no >= run.snapshot["execution"]["max_infra_attempts_per_job"]:
                raise DomainError(409, "INFRA_ATTEMPTS_EXHAUSTED")
            if (
                run.budget_microusd - run.spent_microusd
                < run.snapshot["policy"]["job_reservation_microusd"]
            ):
                raise DomainError(409, "BUDGET_NOT_RESOLVED")
            job.state, job.available_at = "RETRY_WAIT", db_now(session)
            run.status, run.stop_reason, run.wait_context = "QUEUED", None, None
            reevaluation = {"job_id": job.id, "cause": cause}
        elif cause in {"MANDATORY_EVIDENCE_MISSING", "EVIDENCE_TARGET_MISMATCH"}:
            if run.phase != "VERIFY_REVIEW":
                raise DomainError(409, "RESUME_PHASE_NOT_ALLOWED")
            *_, decision = self.service.gates.inputs(session, run)
            if (
                decision.status in {"WAIT", "INCONCLUSIVE"}
                or decision.evidence_digest == target["evidence_digest"]
            ):
                raise DomainError(409, "EVIDENCE_NOT_RESOLVED")
            run.status, run.stop_reason = "QUEUED", None
            gate = self.service.gates.local(session, run)
            if not gate:
                raise DomainError(409, "EVIDENCE_NOT_RESOLVED")
            if gate.status == "PASS" and run.phase == "VERIFY_REVIEW":
                publication = session.scalar(
                    select(Job).where(
                        Job.run_id == run.id,
                        Job.kind == "PUBLISH_DRAFT",
                        Job.state.in_(["READY", "PENDING", "RETRY_WAIT"]),
                        Job.payload["gate_id"].as_string() == gate.id,
                        Job.payload["candidate_id"].as_string() == run.current_candidate_id,
                    )
                )
                if publication is None:
                    raise DomainError(409, "RESUME_JOB_MISSING")
                run.phase = "PUBLISH_DRAFT"
            if run.status == "BLOCKED":
                raise DomainError(409, "EVIDENCE_NOT_RESOLVED")
            if run.phase == "VERIFY_REVIEW" and not session.scalar(
                select(Job.id).where(Job.run_id == run.id, Job.state == "RETRY_WAIT")
            ):
                raise DomainError(409, "EVIDENCE_NOT_RESOLVED")
            reevaluation = {"gate_id": gate.id, "status": gate.status}
        else:
            raise DomainError(409, "BLOCK_REQUIRES_SPECIFIC_ACTION")
        event(
            session,
            run,
            "BLOCK_RESOLVED",
            {"actor_id": actor.id, "reason": data.reason, "cause": cause, **reevaluation},
        )
        session.add(
            AuditEvent(
                workspace_id=actor.workspace_id,
                actor_id=actor.id,
                operation="intervention.resolve_blocked",
                body={
                    "action": "resolve_blocked",
                    "target": target,
                    "target_hash": data.target_hash,
                    "before_revision": before,
                    "after_revision": run.state_revision,
                    "reason": data.reason,
                    "reevaluation": reevaluation,
                },
                created_at=db_now(session),
            )
        )
        session.flush()
        self.service.checkpoint("intervention.resolved")
        return {"run": run_view(run), "reevaluation": reevaluation}
