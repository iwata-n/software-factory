"""Reconcile published candidates without holding a workspace lock during GitHub I/O."""

import os
from uuid import UUID, uuid4

from pydantic import SecretStr, TypeAdapter, ValidationError
from sqlalchemy import func, select

from .db import db_now
from .github_ci import CIReadError, GitHubCIReader
from .models import (
    CIWebhookDelivery,
    DraftPullRequest,
    Gate,
    Repository,
    Run,
    VerifiedRunResult,
    new_id,
)
from .publications import draft_view
from .service import DomainError, event, owned, transition
from .slack_delivery import enqueue_result

POLL_SECONDS = 60
WAITING = (Run.status == "WAITING_EXTERNAL", Run.phase.in_(["WAIT_CI", "FINAL_GATE_READY"]))


def configured_read_tokens():
    try:
        return TypeAdapter(dict[UUID, SecretStr]).validate_json(
            os.environ.get("FACTORY_GITHUB_CI_READ_TOKENS", "{}")
        )
    except ValidationError:
        raise ValueError("Invalid FACTORY_GITHUB_CI_READ_TOKENS configuration") from None


def check_identity(check):
    return check["app_id"], check["check_name"]


def stale_snapshot(previous, observed):
    """Reject regressions even when a delayed webhook triggers another API read."""
    latest = {}
    for check in observed:
        key = check_identity(check)
        if key not in latest or check["run_sequence"] > latest[key]["run_sequence"]:
            latest[key] = check
    for old in previous:
        new = latest.get(check_identity(old))
        if new is None or new["run_sequence"] < old["run_sequence"]:
            return True
        if new["run_sequence"] == old["run_sequence"] and old["status"] != "PENDING":
            if new["status"] == "PENDING":
                return True
            if new["status"] != old["status"] and (
                (new["provider_completed_at"] or "") <= (old["provider_completed_at"] or "")
            ):
                return True
    return False


class CIPoller:
    def __init__(self, service, integrations, tokens):
        self.service = service
        self.bindings = {}
        for integration_id, token in tokens.items():
            config = integrations.get(integration_id)
            if config is None or not token.get_secret_value():
                raise ValueError("GitHub CI read token requires a matching webhook integration")
            key = str(config.workspace_id), str(config.repository_id)
            if key in self.bindings:
                raise ValueError("Multiple GitHub CI read integrations for one repository")
            self.bindings[key] = integration_id, config, token

    def poll(self, client):
        if not self.bindings:
            return
        with self.service.db.transaction() as session:
            runs = session.execute(
                select(Run.workspace_id, Run.id, Run.repository_id).where(*WAITING)
            ).all()
        for workspace_id, run_id, repository_id in runs:
            binding = self.bindings.get((workspace_id, repository_id))
            if binding is None:
                continue
            integration_id, config, token = binding
            context = self.claim(workspace_id, run_id, integration_id, config)
            if context is None:
                continue
            reader = GitHubCIReader(
                client,
                config.github_repository,
                config.github_repository_id,
                token.get_secret_value(),
            )
            try:
                checks, error = reader.read(context), None
            except CIReadError as exc:
                checks, error = None, str(exc)
            self.finish(workspace_id, run_id, config, context, checks, error)

    def active_context(self, session, workspace_id, run_id, config):
        run = session.scalar(
            select(Run).where(Run.workspace_id == workspace_id, Run.id == run_id, *WAITING)
        )
        if run is None or run.repository_id != str(config.repository_id):
            return None, None
        context = run.wait_context or {}
        if not context.get("quality_gate") or not context["required_ci"]:
            return None, None
        if run.current_candidate_id != context["target"]["candidate_id"]:
            return None, None
        if not self.service.repository_allowed(session, run):
            return None, None
        repo = owned(session, Repository, run.repository_id, workspace_id)
        remote = (repo.remote_url or "").rstrip("/").removesuffix(".git").casefold()
        if remote != f"https://github.com/{config.github_repository}".casefold():
            return None, None
        return run, context

    def claim(self, workspace_id, run_id, integration_id, config):
        with self.service.db.transaction() as session:
            self.service.lock_workspace(session, workspace_id)
            run, context = self.active_context(session, workspace_id, run_id, config)
            if context is None:
                return None
            now = db_now(session)
            if now >= context["deadline_at"]:
                self.expire(session, run)
                return None
            poll = context.get("github_ci_poll", {})
            if now < max(poll.get("retry_at", 0), context.get("next_reconcile_at", 0)):
                return None
            signal = session.scalar(
                select(func.max(CIWebhookDelivery.received_at)).where(
                    CIWebhookDelivery.workspace_id == workspace_id,
                    CIWebhookDelivery.repository_id == run.repository_id,
                    CIWebhookDelivery.integration_id == str(integration_id),
                    CIWebhookDelivery.event != "ping",
                )
            )
            triggered = signal is not None and signal > poll.get("signal", -1)
            if poll.get("phase") == run.phase and now < poll.get("next_at", 0) and not triggered:
                return None
            run.wait_context = context | {
                "github_ci_poll": {
                    "claim": str(uuid4()),
                    "phase": run.phase,
                    "revision": run.state_revision,
                    "next_at": now + POLL_SECONDS,
                    "signal": signal if signal is not None else -1,
                }
            }
            return run.wait_context

    def finish(self, workspace_id, run_id, config, claimed, checks, error):
        with self.service.db.transaction() as session:
            self.service.lock_workspace(session, workspace_id)
            run, context = self.active_context(session, workspace_id, run_id, config)
            if context is None or any(
                context.get(key) != claimed[key]
                for key in ("github_ci_poll", "draft", "target", "required_ci", "deadline_at")
            ):
                return
            claim = claimed["github_ci_poll"]
            if (run.phase, run.state_revision) != (claim["phase"], claim["revision"]):
                return
            if db_now(session) >= context["deadline_at"]:
                self.expire(session, run)
                return
            previous = [check for check in context["checks"] if "provider_run_id" in check]
            if error is None and stale_snapshot(previous, checks):
                error = "GITHUB_CI_STALE_SNAPSHOT"
            if error == "GITHUB_CI_TARGET_CHANGED" and run.phase == "FINAL_GATE_READY":
                transition(session, run, "BLOCKED", error)
                return
            if error is not None:
                poll = context["github_ci_poll"]
                run.wait_context = context | {
                    "github_ci_poll": poll | {"retry_at": poll["next_at"]}
                }
                event(session, run, "GITHUB_CI_READ_DEFERRED", {"reason": error})
                return
            if checks != context["checks"]:
                event(session, run, "GITHUB_CI_RECONCILED", {"check_count": len(checks)})
            confirming = run.phase == "FINAL_GATE_READY"
            if confirming and not self.current_publication(session, run, context):
                transition(session, run, "BLOCKED", "PUBLICATION_GATE_STALE")
                return
            run.phase = "WAIT_CI"
            gate = self.service.gates.final(session, run, checks)
            if confirming and gate is not None and gate.status == "PASS":
                self.save_result(session, run, context, gate, checks)

    def expire(self, session, run):
        run.phase = "WAIT_CI"
        self.service.gates.final(session, run)

    def current_publication(self, session, run, context):
        try:
            self.service.interventions.require_current_context(session, run)
        except DomainError:
            return False
        draft = session.scalar(
            select(DraftPullRequest).where(
                DraftPullRequest.workspace_id == run.workspace_id, DraftPullRequest.run_id == run.id
            )
        )
        local = owned(session, Gate, context["local_gate_id"], run.workspace_id)
        _, target, _, _, _, current = self.service.gates.inputs(session, run)
        return (
            draft is not None
            and draft_view(draft) == context["draft"]
            and local.run_id == run.id
            and local.stage == "LOCAL"
            and local.status == current.status == "PASS"
            and local.target == target == context["target"]
            and local.evidence_digest == current.evidence_digest
        )

    def save_result(self, session, run, context, gate, checks):
        result_checks = [check for check in checks if check["id"] in gate.evidence_ids]
        snapshot = {
            "kind": "VERIFIED_DRAFT_PR",
            "candidate_id": run.current_candidate_id,
            "target": gate.target,
            "policy_version": gate.policy_version,
            "local_gate_id": context["local_gate_id"],
            "final_gate_id": gate.id,
            "evidence_digest": gate.evidence_digest,
            "draft_pr": context["draft"],
            "required_ci": context["required_ci"],
            "checks": result_checks,
        }
        result = VerifiedRunResult(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            body=snapshot,
            created_at=db_now(session),
        )
        session.add(result)
        run.status, run.phase, run.stop_reason = "SUCCEEDED", "COMPLETE", None
        run.wait_context = None
        event(session, run, "VERIFIED_DRAFT_PR", {"result_id": result.id, "gate_id": gate.id})
        enqueue_result(
            session,
            run,
            pr_url=context["draft"]["url"],
            ci_urls=[check["url"] for check in result_checks],
        )
        session.flush()
        self.service.checkpoint("verified_result.saved")
