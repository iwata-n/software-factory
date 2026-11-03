"""Control-plane gates: only immutable trusted evidence can advance a Candidate."""

from dataclasses import replace

from sqlalchemy import select

from .candidates import candidate_view
from .contracts import digest
from .db import db_now
from .evidence import check_view, finding_view, target_of
from .models import Attempt, Candidate, Check, Finding, Gate, Job, new_id
from .quality import local_gate
from .repair import ci_failure_set, compare_failures, constraint_digest, failure_set


def gate_view(item):
    return {
        "gate_id": item.id,
        **{
            key: getattr(item, key)
            for key in (
                "run_id",
                "candidate_id",
                "stage",
                "status",
                "reason",
                "target",
                "evidence_ids",
                "evidence_digest",
                "policy_version",
                "metadata_body",
                "created_at",
            )
        },
    }


class GateCommands:
    def __init__(self, service):
        self.service = service

    def inputs(self, session, run):
        from .service import DomainError, owned

        if not run.current_candidate_id:
            raise DomainError(409, "CURRENT_CANDIDATE_REQUIRED")
        candidate = owned(session, Candidate, run.current_candidate_id, run.workspace_id)
        target = target_of(candidate) | {
            "spec_hash": run.snapshot["task_spec_hash"],
            "profile_hash": run.snapshot["verification_profile_hash"],
            "image_digest": run.snapshot["execution_image_digest"],
            "base_sha": run.base_sha,
            "repository_id": run.repository_id,
            "policy_hash": run.snapshot["policy_hash"],
        }
        checks = []
        for item, number in session.execute(
            select(Check, Attempt.attempt_no)
            .join(
                Attempt,
                (Attempt.workspace_id == Check.workspace_id) & (Attempt.id == Check.attempt_id),
            )
            .where(Check.workspace_id == run.workspace_id, Check.candidate_id == candidate.id)
        ):
            record = check_view(item)
            record["id"], record["attempt_no"] = record.pop("check_record_id"), number
            checks.append(record)
        findings = []
        for item in session.scalars(
            select(Finding).where(
                Finding.workspace_id == run.workspace_id, Finding.candidate_id == candidate.id
            )
        ):
            record = finding_view(item)
            record["id"] = record.pop("finding_record_id")
            findings.append(record)
        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.workspace_id == run.workspace_id,
                    Job.run_id == run.id,
                    Job.kind.in_(["VERIFY", "REVIEW"]),
                    Job.payload["candidate_id"].as_string() == candidate.id,
                )
            )
        )
        profile = run.snapshot["verification_profile"]
        review_ids = profile.get("review_check_ids", ["review", "acceptance_review"])
        required = [name for name in profile["required_checks"] if name not in review_ids]
        criteria = [c["id"] for c in run.snapshot["task_spec"]["task"]["acceptance_criteria"]]
        pending = any(job.state in {"READY", "PENDING", "LEASED", "RETRY_WAIT"} for job in jobs)
        decision = local_gate(target, required, criteria, checks, findings, jobs_pending=pending)
        if not pending and (
            candidate.base_sha != run.base_sha
            or candidate.spec_hash != run.snapshot["task_spec_hash"]
            or candidate.profile_hash != run.snapshot["verification_profile_hash"]
            or candidate.image_digest != run.snapshot["execution_image_digest"]
            or digest(run.snapshot["verification_profile"]) != candidate.profile_hash
            or digest(run.snapshot["policy"]) != run.snapshot["policy_hash"]
        ):
            decision = replace(decision, status="INCONCLUSIVE", reason="CURRENT_TARGET_CHANGED")
        return candidate, target, jobs, checks, findings, decision

    def local(self, session, run):
        from .service import enqueue, event, transition

        candidate, target, jobs, checks, findings, decision = self.inputs(session, run)
        if decision.status == "WAIT":
            run.status = "RUNNING" if any(job.state == "LEASED" for job in jobs) else "QUEUED"
            return None
        defects = failure_set(checks, findings, decision.evidence_ids)
        progress, stop = self.progress(session, run, candidate, "LOCAL", defects, decision.status)
        if any(
            f.get("category") == "SPEC_CHANGE_REQUIRED" and f["id"] in decision.evidence_ids
            for f in findings
        ):
            stop = "SPEC_CHANGE_REQUIRED"
            decision = replace(decision, status="INCONCLUSIVE", reason=stop)
        if stop == "REPAIR_COMPARISON_INCONCLUSIVE":
            decision = replace(decision, status="INCONCLUSIVE", reason=stop)
        existing = session.scalar(
            select(Gate).where(
                Gate.workspace_id == run.workspace_id,
                Gate.candidate_id == candidate.id,
                Gate.stage == "LOCAL",
                Gate.evidence_digest == decision.evidence_digest,
            )
        )
        if existing:
            return existing
        gate = Gate(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            candidate_id=candidate.id,
            stage="LOCAL",
            status=decision.status,
            reason=decision.reason,
            target=target,
            evidence_ids=list(decision.evidence_ids),
            evidence_digest=decision.evidence_digest,
            policy_version=run.snapshot["policy"]["version"],
            metadata_body={
                "job_ids": sorted(j.id for j in jobs),
                "failure_set": defects,
                "failure_set_version": 1,
                "repair_progress": progress,
            },
            created_at=db_now(session),
        )
        session.add(gate)
        session.flush()
        self.service.checkpoint("gate.created")
        event(
            session,
            run,
            "LOCAL_GATE_DECIDED",
            {
                "gate_id": gate.id,
                "candidate_id": candidate.id,
                "status": gate.status,
                "evidence_digest": gate.evidence_digest,
            },
        )
        payload = {
            "candidate_id": candidate.id,
            "candidate": candidate_view(candidate),
            "source_artifact_id": candidate.source_artifact_id,
            "input_artifact_ids": [candidate.source_artifact_id],
            "gate_id": gate.id,
            "evidence_digest": gate.evidence_digest,
            "policy_hash": target["policy_hash"],
        }
        if stop:
            transition(session, run, "BLOCKED", stop)
        elif decision.status == "PASS":
            run.status, run.phase, run.stop_reason = "QUEUED", "PUBLISH_DRAFT", None
            enqueue(
                session,
                run,
                "PUBLISH_DRAFT",
                depends_on=tuple(j.id for j in jobs),
                step_key=f"publish:{candidate.generation}",
                payload=payload,
            )
        elif decision.status == "ERROR":
            bad_jobs = {
                c["job_id"]
                for c in checks
                if c["id"] in decision.evidence_ids and c["status"] == "ERROR"
            }
            for job in jobs:
                if job.id in bad_jobs:
                    self.service.retry_or_fail(session, job, run)
        elif decision.status == "FAIL":
            self.queue_repair(
                session, run, candidate, gate, jobs, checks, findings, defects, payload
            )
        else:
            transition(session, run, "BLOCKED", decision.reason)
        return gate

    def progress(self, session, run, candidate, stage, defects, status):
        repair_id = candidate.metadata_body.get("repair_job_id")
        if not repair_id:
            return {"status": "INITIAL", "after": defects}, None
        repair = session.get(Job, repair_id)
        previous = repair.payload.get("failure_set") if repair else None
        if status == "ERROR":
            return {"status": "RETRY_INFRASTRUCTURE"}, None
        if (
            repair
            and repair.payload.get("repair_stage") == "FINAL"
            and stage == "LOCAL"
            and status == "PASS"
        ):
            return {"status": "PENDING_FINAL_VALIDATION"}, None
        comparison = (
            compare_failures(previous, defects)
            if repair
            and (repair.payload.get("constraints_digest") == constraint_digest(run.snapshot))
            else "INCONCLUSIVE"
        )
        if status == "INCONCLUSIVE":
            comparison = "INCONCLUSIVE"
        stop = {"NO_PROGRESS": "NO_PROGRESS", "INCONCLUSIVE": "REPAIR_COMPARISON_INCONCLUSIVE"}.get(
            comparison
        )
        return {
            "status": comparison,
            "before": previous,
            "after": defects,
            "repair_job_id": repair_id,
        }, stop

    def queue_repair(self, session, run, candidate, gate, jobs, checks, findings, defects, payload):
        from .service import enqueue, transition

        adopted = [f for f in findings if f["id"] in gate.evidence_ids]
        if any(f.get("category") == "SPEC_CHANGE_REQUIRED" for f in adopted):
            transition(session, run, "BLOCKED", "SPEC_CHANGE_REQUIRED")
            return
        if defects is None or not defects:
            transition(session, run, "BLOCKED", "REPAIR_COMPARISON_INCONCLUSIVE")
            return
        repairs = list(
            session.scalars(
                select(Job).where(
                    Job.workspace_id == run.workspace_id, Job.run_id == run.id, Job.kind == "REPAIR"
                )
            )
        )
        if len(repairs) >= run.snapshot["execution"]["max_repair_rounds"]:
            transition(session, run, "BLOCKED", "REPAIR_LIMIT_REACHED")
            return
        for job in session.scalars(
            select(Job).where(
                Job.run_id == run.id,
                Job.kind == "PUBLISH_DRAFT",
                Job.state.in_(["READY", "PENDING", "RETRY_WAIT"]),
            )
        ):
            job.state = "CANCELLED"
        run.status, run.phase, run.stop_reason, run.wait_context = "QUEUED", "REPAIR", None, None
        artifacts = set(payload["input_artifact_ids"])
        for check in checks:
            if check["id"] in gate.evidence_ids:
                artifacts.update(check["artifact_ids"])
        for finding in adopted:
            artifacts.update(finding["artifact_ids"])
        payload |= {
            "repair_round": len(repairs) + 1,
            "finding_ids": [f["id"] for f in adopted],
            "parent_sha": candidate.head_sha,
            "source_candidate_id": candidate.id,
            "failure_set": defects,
            "failure_set_version": 1,
            "repair_stage": gate.stage,
            "constraints_digest": constraint_digest(run.snapshot),
            "input_artifact_ids": sorted(artifacts),
        }
        if candidate.metadata_body.get("plan_id"):
            payload["plan_id"] = candidate.metadata_body["plan_id"]
        enqueue(
            session,
            run,
            "REPAIR",
            depends_on=tuple(j.id for j in jobs),
            step_key=f"repair:{candidate.generation}",
            payload=payload,
        )

    def require_publication(
        self, session, run, candidate_id, gate_id, evidence_digest, *, recovering=False
    ):
        from .service import DomainError, owned

        gate = owned(session, Gate, gate_id, run.workspace_id)
        if (
            run.current_candidate_id != candidate_id
            or gate.candidate_id != candidate_id
            or gate.run_id != run.id
            or gate.stage != "LOCAL"
            or gate.status != "PASS"
            or gate.evidence_digest != evidence_digest
            or not (
                (run.status == "WAITING_EXTERNAL" and run.phase == "PUBLISH_UNKNOWN")
                if recovering
                else (run.status in {"QUEUED", "RUNNING"} and run.phase == "PUBLISH_DRAFT")
            )
        ):
            raise DomainError(409, "PUBLICATION_GATE_STALE")
        _, target, _, _, _, current = self.inputs(session, run)
        if (
            target != gate.target
            or current.status != "PASS"
            or current.evidence_digest != gate.evidence_digest
        ):
            raise DomainError(409, "PUBLICATION_GATE_STALE")
        return gate

    def begin_ci_wait(self, session, run, gate_id, draft, *, recovering=False):
        from .service import DomainError, event, owned

        if (run.wait_context or {}).get("quality_gate"):
            if run.wait_context["local_gate_id"] == gate_id and run.wait_context["draft"] == draft:
                return run.wait_context
            raise DomainError(409, "CI_WAIT_ALREADY_STARTED")
        gate = owned(session, Gate, gate_id, run.workspace_id)
        self.require_publication(
            session,
            run,
            run.current_candidate_id,
            gate_id,
            gate.evidence_digest,
            recovering=recovering,
        )
        run.wait_context = {
            "kind": "CI",
            "quality_gate": True,
            "deadline_at": db_now(session) + 1800,
            "local_gate_id": gate.id,
            "target": gate.target,
            "draft": draft,
            "required_ci": run.snapshot["verification_profile"].get("required_ci_checks", []),
            "checks": [],
        }
        run.status, run.phase = "WAITING_EXTERNAL", "WAIT_CI"
        event(
            session,
            run,
            "CI_WAIT_STARTED",
            {
                "deadline_at": run.wait_context["deadline_at"],
                "candidate_id": run.current_candidate_id,
            },
        )
        return run.wait_context

    def final(self, session, run, checks=None):
        from .quality import final_gate
        from .service import DomainError, event, owned, transition

        context = run.wait_context or {}
        if (
            run.status != "WAITING_EXTERNAL"
            or run.phase != "WAIT_CI"
            or not context.get("quality_gate")
        ):
            raise DomainError(409, "CI_WAIT_NOT_ACTIVE")
        local = owned(session, Gate, context["local_gate_id"], run.workspace_id)
        _, current_target, _, _, _, current_local = self.inputs(session, run)
        if current_target != context["target"] or current_local.status != "PASS":
            transition(session, run, "BLOCKED", "PUBLICATION_GATE_STALE")
            return None
        if checks is not None:
            run.wait_context = context | {"checks": checks}
            context = run.wait_context
        local_input = {
            "id": local.id,
            "status": local.status,
            "target": local.target,
            "evidence_digest": local.evidence_digest,
        }
        decision = final_gate(
            context["target"],
            local_input,
            context["draft"],
            context["required_ci"],
            context["checks"],
            now=db_now(session),
            deadline=context["deadline_at"],
        )
        if decision.status == "WAIT":
            return None
        candidate, _, jobs, local_checks, findings, _ = self.inputs(session, run)
        local_defects = failure_set(local_checks, findings, local.evidence_ids)
        ci_defects = ci_failure_set(
            [c for c in context["checks"] if c["id"] in decision.evidence_ids]
        )
        defects = (
            local_defects + ci_defects
            if local_defects is not None and ci_defects is not None
            else None
        )
        if decision.reason == "CI_TIMEOUT":
            progress, stop = {"status": "CI_TIMEOUT"}, None
        else:
            progress, stop = self.progress(
                session, run, candidate, "FINAL", defects, decision.status
            )
        if stop == "REPAIR_COMPARISON_INCONCLUSIVE":
            decision = replace(decision, status="INCONCLUSIVE", reason=stop)
        existing = session.scalar(
            select(Gate).where(
                Gate.workspace_id == run.workspace_id,
                Gate.candidate_id == run.current_candidate_id,
                Gate.stage == "FINAL",
                Gate.evidence_digest == decision.evidence_digest,
            )
        )
        if existing:
            return existing
        gate = Gate(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            candidate_id=run.current_candidate_id,
            stage="FINAL",
            status=decision.status,
            reason=decision.reason,
            target=context["target"],
            evidence_ids=list(decision.evidence_ids),
            evidence_digest=decision.evidence_digest,
            policy_version=local.policy_version,
            metadata_body={
                "draft": context["draft"],
                "checks": context["checks"],
                "deadline_at": context["deadline_at"],
                "failure_set": defects,
                "failure_set_version": 1,
                "repair_progress": progress,
            },
            created_at=db_now(session),
        )
        session.add(gate)
        session.flush()
        self.service.checkpoint("gate.created")
        event(session, run, "FINAL_GATE_DECIDED", {"gate_id": gate.id, "status": gate.status})
        if stop:
            transition(session, run, "BLOCKED", stop)
        elif decision.status == "PASS":
            # SF-19 verifies publication reconciliation before assigning Run SUCCEEDED.
            run.phase = "FINAL_GATE_READY"
        elif decision.status == "ERROR":
            count = context.get("infra_attempt", 1)
            if count >= run.snapshot["execution"]["max_infra_attempts_per_job"]:
                transition(session, run, "BLOCKED", "CI_INFRA_ATTEMPTS_EXHAUSTED")
            else:
                run.wait_context = context | {
                    "infra_attempt": count + 1,
                    "next_reconcile_at": db_now(session) + 2**count,
                }
                event(
                    session,
                    run,
                    "CI_INFRA_RETRY_REQUESTED",
                    {
                        "gate_id": gate.id,
                        "candidate_id": run.current_candidate_id,
                        "target": context["target"],
                        "attempt": count + 1,
                        "not_before": run.wait_context["next_reconcile_at"],
                    },
                )
        elif decision.status == "FAIL":
            payload = {
                "candidate_id": candidate.id,
                "candidate": candidate_view(candidate),
                "source_artifact_id": candidate.source_artifact_id,
                "input_artifact_ids": [candidate.source_artifact_id],
                "gate_id": gate.id,
                "evidence_digest": gate.evidence_digest,
                "policy_hash": gate.target["policy_hash"],
            }
            self.queue_repair(
                session, run, candidate, gate, jobs, local_checks, findings, defects, payload
            )
        else:
            transition(session, run, "BLOCKED", decision.reason)
        return gate

    def deny_mutation(self, token, gate_id):
        from .models import AuditEvent, Principal
        from .service import DomainError, owned, token_hash

        with self.service.db.transaction() as session:
            actor = session.scalar(
                select(Principal).where(Principal.token_hash == token_hash(token))
            )
            if actor is None or not actor.active:
                raise DomainError(401, "UNAUTHENTICATED")
            actor = self.service.principal(session, token, actor.role)
            owned(session, Gate, gate_id, actor.workspace_id)
            session.add(
                AuditEvent(
                    workspace_id=actor.workspace_id,
                    actor_id=actor.id,
                    operation="gate.change.denied",
                    body={"gate_id": gate_id},
                    created_at=db_now(session),
                )
            )
            status = 409 if actor.role == "USER" else 403
        raise DomainError(status, "GATE_IMMUTABLE")
