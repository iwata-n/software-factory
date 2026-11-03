"""Trusted publication commands. Database transactions perform no Git or provider calls."""

import base64
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select

from .candidates import candidate_view
from .contracts import digest
from .db import db_now
from .models import (
    Candidate,
    DraftPullRequest,
    Gate,
    Job,
    PublicationObservation,
    PublicationOperation,
    PublisherGrant,
    Repository,
    Run,
    new_id,
)
from .source_runner import SourcePolicyDenied, canonical_remote


def operation_view(item):
    return {
        "operation_id": item.id,
        "required_action": (
            "Inspect the remote branch and PR before deciding how to proceed."
            if item.state == "BLOCKED" and item.reason == "PUBLISH_UNKNOWN"
            else None
        ),
        **{
            key: getattr(item, key)
            for key in (
                "run_id",
                "repository_id",
                "candidate_id",
                "job_id",
                "gate_id",
                "kind",
                "operation_key",
                "target",
                "target_hash",
                "state",
                "publisher_id",
                "dispatch_revision",
                "started_at",
                "completed_at",
                "reason",
                "created_at",
                "reconcile_attempts",
                "next_reconcile_at",
            )
        },
    }


def draft_view(item):
    return {
        "id": item.id,
        "origin_verified": item.metadata_body.get("origin_verified") is True,
        **{
            key: getattr(item, key)
            for key in (
                "run_id",
                "repository_id",
                "candidate_id",
                "number",
                "external_id",
                "url",
                "base_ref",
                "base_sha",
                "head_ref",
                "head_sha",
                "merge_sha",
                "draft",
                "state",
                "observed_at",
            )
        },
    }


class PublicationCommands:
    def __init__(self, service):
        self.service = service

    def grant(self, session, actor, repository_id):
        from .service import DomainError

        if not session.scalar(
            select(PublisherGrant.id).where(
                PublisherGrant.workspace_id == actor.workspace_id,
                PublisherGrant.publisher_id == actor.id,
                PublisherGrant.repository_id == repository_id,
            )
        ):
            raise DomainError(404, "NOT_FOUND")

    def subject(self, session, run, job):
        from .service import DomainError, owned

        candidate = owned(session, Candidate, job.payload["candidate_id"], run.workspace_id)
        gate = owned(session, Gate, job.payload["gate_id"], run.workspace_id)
        repository = owned(session, Repository, run.repository_id, run.workspace_id)
        remote = (run.snapshot.get("source") or {}).get("remote_url")
        if not remote or remote != repository.remote_url:
            raise DomainError(409, "PUBLICATION_REPOSITORY_STALE")
        try:
            remote = canonical_remote(remote)
        except SourcePolicyDenied:
            raise DomainError(409, "PUBLICATION_REPOSITORY_DENIED") from None
        if urlsplit(remote).hostname != "github.com":
            raise DomainError(409, "PUBLISH_PROVIDER_UNSUPPORTED")
        previous = session.scalar(
            select(DraftPullRequest).where(
                DraftPullRequest.workspace_id == run.workspace_id, DraftPullRequest.run_id == run.id
            )
        )
        return {
            "run_id": run.id,
            "run_revision": run.state_revision,
            "job_id": job.id,
            "repository_id": run.repository_id,
            "remote_url": remote,
            "repository": urlsplit(remote).path.strip("/").removesuffix(".git"),
            "base_ref": run.snapshot["task_spec"]["task"]["base_ref"].removeprefix("refs/heads/"),
            "head_ref": "codex/factory/" + run.id,
            "expected_remote_head": previous.head_sha if previous else None,
            "pull_request_number": previous.number if previous else None,
            "pull_request_external_id": previous.external_id if previous else None,
            "candidate": candidate_view(candidate),
            "gate_id": gate.id,
            "evidence_digest": gate.evidence_digest,
            "policy": run.snapshot["policy"],
            "policy_hash": run.snapshot["policy_hash"],
            "profile_hash": run.snapshot["verification_profile_hash"],
            "spec_hash": run.snapshot["task_spec_hash"],
        }

    def queue(self, session, actor):
        values = []
        for job, run in session.execute(
            select(Job, Run)
            .join(Run, (Run.workspace_id == Job.workspace_id) & (Run.id == Job.run_id))
            .join(
                PublisherGrant,
                (
                    (PublisherGrant.workspace_id == Run.workspace_id)
                    & (PublisherGrant.repository_id == Run.repository_id)
                    & (PublisherGrant.publisher_id == actor.id)
                ),
            )
            .where(
                Job.workspace_id == actor.workspace_id,
                Job.kind == "PUBLISH_DRAFT",
                Job.state.in_(["READY", "RETRY_WAIT"]),
                Run.phase == "PUBLISH_DRAFT",
                Run.status.in_(["QUEUED", "RUNNING"]),
            )
            .order_by(Job.created_at, Job.id)
            .limit(50)
        ):
            target = self.subject(session, run, job)
            values.append({"job_id": job.id, "target": target, "target_hash": digest(target)})
        return {"operations": values}

    def begin(self, session, actor, job_id, data):
        from .service import DomainError, event, owned

        job = owned(session, Job, job_id, actor.workspace_id)
        run = owned(session, Run, job.run_id, actor.workspace_id)
        self.grant(session, actor, run.repository_id)
        if job.kind != "PUBLISH_DRAFT" or job.state not in {"READY", "RETRY_WAIT"}:
            raise DomainError(409, "PUBLICATION_NOT_PENDING")
        if run.state_revision != data.expected_run_revision:
            raise DomainError(409, "RUN_REVISION_CONFLICT")
        self.service.interventions.require_current_context(session, run)
        self.service.costs.require_balanced(session, run)
        if (
            run.cost_state != "KNOWN"
            or run.reserved_microusd
            or run.spent_microusd > run.budget_microusd
        ):
            raise DomainError(409, "PUBLICATION_COST_UNRESOLVED")
        self.service.gates.require_publication(
            session,
            run,
            job.payload["candidate_id"],
            job.payload["gate_id"],
            job.payload["evidence_digest"],
        )
        target = self.subject(session, run, job)
        if data.target_hash != digest(target):
            raise DomainError(409, "PUBLICATION_TARGET_STALE")
        previous = session.scalar(
            select(PublicationOperation).where(
                PublicationOperation.workspace_id == run.workspace_id,
                PublicationOperation.repository_id == run.repository_id,
                PublicationOperation.run_id == run.id,
                PublicationOperation.candidate_id == target["candidate"]["candidate_id"],
                PublicationOperation.kind == "PUBLISH_DRAFT",
            )
        )
        if previous:
            raise DomainError(409, "PUBLICATION_ALREADY_STARTED")
        now = db_now(session)
        item = PublicationOperation(
            id=new_id(),
            workspace_id=run.workspace_id,
            repository_id=run.repository_id,
            run_id=run.id,
            candidate_id=target["candidate"]["candidate_id"],
            job_id=job.id,
            gate_id=target["gate_id"],
            kind="PUBLISH_DRAFT",
            operation_key=digest(
                {
                    "repository_id": run.repository_id,
                    "run_id": run.id,
                    "candidate_id": target["candidate"]["candidate_id"],
                    "kind": "PUBLISH_DRAFT",
                }
            ),
            target=target,
            target_hash=data.target_hash,
            state="IN_FLIGHT",
            publisher_id=actor.id,
            started_at=now,
            created_at=now,
        )
        session.add(item)
        job.state = "DISPATCHING"
        event(
            session,
            run,
            "PUBLICATION_IN_FLIGHT",
            {
                "operation_id": item.id,
                "candidate_id": item.candidate_id,
                "publisher_id": actor.id,
                "target_hash": item.target_hash,
            },
        )
        item.dispatch_revision = run.state_revision
        session.flush()
        self.service.checkpoint("publication.in_flight")
        return operation_view(item)

    def assigned(self, session, actor, operation_id):
        from .service import DomainError, owned

        item = owned(session, PublicationOperation, operation_id, actor.workspace_id)
        self.grant(session, actor, item.repository_id)
        if item.publisher_id != actor.id:
            raise DomainError(404, "NOT_FOUND")
        if digest(item.target) != item.target_hash:
            raise DomainError(409, "PUBLICATION_TARGET_CORRUPTED")
        return item

    def reconciliation_queue(self, session, actor):
        operations = session.scalars(
            select(PublicationOperation)
            .join(
                PublisherGrant,
                and_(
                    PublisherGrant.workspace_id == PublicationOperation.workspace_id,
                    PublisherGrant.repository_id == PublicationOperation.repository_id,
                    PublisherGrant.publisher_id == actor.id,
                ),
            )
            .where(
                PublicationOperation.workspace_id == actor.workspace_id,
                PublicationOperation.publisher_id == actor.id,
                or_(
                    PublicationOperation.state == "RECONCILING",
                    and_(
                        PublicationOperation.state == "UNKNOWN",
                        PublicationOperation.next_reconcile_at <= db_now(session),
                    ),
                ),
            )
            .order_by(PublicationOperation.next_reconcile_at, PublicationOperation.id)
            .limit(50)
        )
        return {"operations": [operation_view(item) for item in operations]}

    def claim_reconciliation(self, session, actor, operation_id, data):
        from .service import DomainError, event, owned

        item = self.assigned(session, actor, operation_id)
        if data.target_hash != item.target_hash:
            raise DomainError(409, "PUBLICATION_TARGET_STALE")
        if item.state == "RECONCILING" and data.attempt == item.reconcile_attempts:
            return operation_view(item)
        if (
            item.state != "UNKNOWN"
            or item.next_reconcile_at is None
            or item.next_reconcile_at > db_now(session)
            or data.attempt != item.reconcile_attempts + 1
        ):
            raise DomainError(409, "PUBLICATION_RECONCILIATION_NOT_DUE")
        item.state = "RECONCILING"
        item.reconcile_attempts = data.attempt
        event(
            session,
            owned(session, Run, item.run_id, actor.workspace_id),
            "PUBLICATION_RECONCILING",
            {"operation_id": item.id, "attempt": data.attempt},
        )
        return operation_view(item)

    def inputs(self, session, actor, operation_id):
        from .service import DomainError, owned

        item = self.assigned(session, actor, operation_id)
        if item.state != "IN_FLIGHT":
            raise DomainError(409, "PUBLICATION_NOT_IN_FLIGHT")
        owned(session, Repository, item.repository_id, actor.workspace_id)
        chain, current = [], item.candidate_id
        while current:
            candidate = owned(session, Candidate, current, actor.workspace_id)
            if candidate.run_id != item.run_id or len(chain) >= 3:
                raise DomainError(409, "PUBLICATION_PARENT_CHAIN_INVALID")
            archive = self.service.artifacts.content(session, actor, candidate.source_artifact_id)
            chain.append(
                {
                    "candidate": candidate_view(candidate),
                    "archive": base64.b64encode(archive).decode(),
                }
            )
            current = candidate.metadata_body.get("parent_candidate_id")
        return {"operation": operation_view(item), "sources": list(reversed(chain))}

    def report(self, session, actor, operation_id, data):
        from .service import DomainError, event, owned, run_view, transition

        item = self.assigned(session, actor, operation_id)
        if data.target_hash != item.target_hash:
            raise DomainError(409, "PUBLICATION_TARGET_STALE")
        payload = data.payload()
        previous = session.scalar(
            select(PublicationObservation).where(
                PublicationObservation.workspace_id == actor.workspace_id,
                PublicationObservation.operation_id == item.id,
                PublicationObservation.delivery_id == data.delivery_id,
            )
        )
        run = owned(session, Run, item.run_id, actor.workspace_id)
        if previous:
            if previous.payload_hash != digest(payload):
                raise DomainError(409, "PUBLICATION_DELIVERY_CONFLICT")
            return {"operation": operation_view(item), "run": run_view(run)}
        reconciling = data.reconcile_attempt is not None
        if reconciling:
            if item.state != "RECONCILING" or data.reconcile_attempt != item.reconcile_attempts:
                raise DomainError(409, "PUBLICATION_RECONCILIATION_STALE")
            if data.status == "CONFLICT" or any(r.method != "GET" for r in data.provider_requests):
                raise DomainError(409, "PUBLICATION_RECONCILIATION_INVALID")
        elif item.state != "IN_FLIGHT":
            raise DomainError(409, "PUBLICATION_NOT_IN_FLIGHT")
        target = item.target
        now = db_now(session)
        draft = None
        if data.pull_request:
            report = data.pull_request
            if (
                report.repository != target["repository"]
                or report.url != f"https://github.com/{report.repository}/pull/{report.number}"
                or report.base_ref != target["base_ref"]
                or report.head_ref != target["head_ref"]
                or report.base_sha != target["candidate"]["base_sha"]
                or report.head_sha != target["candidate"]["head_sha"]
            ):
                raise DomainError(409, "PUBLICATION_RESULT_TARGET_MISMATCH")
            draft = session.scalar(
                select(DraftPullRequest).where(
                    DraftPullRequest.workspace_id == actor.workspace_id,
                    DraftPullRequest.run_id == item.run_id,
                )
            )
            if draft and (
                draft.number != report.number
                or draft.external_id != report.external_id
                or draft.head_sha != target["expected_remote_head"]
            ):
                raise DomainError(409, "PUBLICATION_PR_CONFLICT")
            if not draft:
                draft = DraftPullRequest(
                    id=new_id(),
                    workspace_id=item.workspace_id,
                    run_id=item.run_id,
                    repository_id=item.repository_id,
                )
                session.add(draft)
            for key, value in report.model_dump(exclude={"repository"}).items():
                setattr(draft, key, value)
            draft.candidate_id = item.candidate_id
            draft.metadata_body = {"origin_verified": True, "operation_id": item.id}
            draft.observed_at = now
        exhausted = reconciling and data.status == "UNKNOWN" and item.reconcile_attempts == 3
        item.state = "BLOCKED" if exhausted or data.status == "CONFLICT" else data.status
        item.reason = "PUBLISH_UNKNOWN" if exhausted else data.reason
        item.completed_at = now if item.state in {"BLOCKED", "SUCCEEDED"} else None
        item.next_reconcile_at = (
            now + 2**item.reconcile_attempts if item.state == "UNKNOWN" else None
        )
        session.add(
            PublicationObservation(
                workspace_id=item.workspace_id,
                run_id=item.run_id,
                operation_id=item.id,
                publisher_id=actor.id,
                delivery_id=data.delivery_id,
                payload_hash=digest(payload),
                body=payload,
                created_at=now,
            )
        )
        job = owned(session, Job, item.job_id, actor.workspace_id)
        job.state = "PUBLISH_UNKNOWN" if item.state == "UNKNOWN" else item.state
        event(
            session,
            run,
            "PUBLICATION_RESULT_RECORDED",
            {
                "operation_id": item.id,
                "status": item.state,
                "candidate_id": item.candidate_id,
                "draft_pr_id": draft.id if draft else None,
            },
        )
        if run.current_candidate_id == item.candidate_id and (
            (run.status == "WAITING_EXTERNAL" and run.phase == "PUBLISH_UNKNOWN")
            if reconciling
            else (run.status in {"QUEUED", "RUNNING"} and run.phase == "PUBLISH_DRAFT")
        ):
            if item.state == "BLOCKED":
                transition(session, run, "BLOCKED", item.reason)
            elif item.state == "UNKNOWN":
                run.status, run.phase = "WAITING_EXTERNAL", "PUBLISH_UNKNOWN"
            elif self.service.repository_allowed(session, run):
                try:
                    self.service.interventions.require_current_context(session, run)
                    self.service.gates.require_publication(
                        session,
                        run,
                        item.candidate_id,
                        item.gate_id,
                        target["evidence_digest"],
                        recovering=reconciling,
                    )
                except DomainError:
                    transition(session, run, "BLOCKED", "PUBLICATION_GATE_STALE")
                else:
                    self.service.gates.begin_ci_wait(
                        session, run, item.gate_id, draft_view(draft), recovering=reconciling
                    )
        session.flush()
        self.service.checkpoint("publication.reported")
        return {"operation": operation_view(item), "run": run_view(run)}
