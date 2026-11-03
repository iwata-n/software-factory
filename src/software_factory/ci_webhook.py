"""Authenticate GitHub delivery signals and persist a deduplicated inbox."""

import hashlib
import hmac
import json
import os
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .db import db_now
from .github_ci import SHA, GitHubCIReader, check_status
from .models import (
    Candidate,
    CIWebhookDelivery,
    Gate,
    HistoricalCIObservation,
    Repository,
    Run,
    new_id,
)
from .service import DomainError, owned

MAX_BODY = 2 * 1024 * 1024
EVENTS = {"check_run", "check_suite", "workflow_run", "ping"}


class GitHubWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    repository_id: UUID
    github_repository_id: Annotated[int, Field(strict=True, gt=0)]
    github_repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    secret: Annotated[SecretStr, Field(min_length=32)] | None = None


def configured_webhooks():
    try:
        return TypeAdapter(dict[UUID, GitHubWebhook]).validate_json(
            os.environ.get("FACTORY_GITHUB_WEBHOOKS", "{}")
        )
    except ValidationError:
        raise ValueError("Invalid FACTORY_GITHUB_WEBHOOKS configuration") from None


def remember_historical_check(session, config, inbox, payload):
    # This is an authenticated provider report. Current Gate decisions only read the GitHub API.
    check = payload.get("check_run")
    if inbox.event != "check_run" or not isinstance(check, dict):
        return
    app = check.get("app")
    pulls = check.get("pull_requests")
    check_id, tested = check.get("id"), check.get("head_sha")
    if (
        type(check_id) is not int
        or check_id <= 0
        or not isinstance(tested, str)
        or not SHA.fullmatch(tested)
        or not isinstance(app, dict)
        or type(app.get("id")) is not int
        or not isinstance(check.get("name"), str)
        or not isinstance(check.get("status"), str)
        or (check.get("conclusion") is not None and not isinstance(check["conclusion"], str))
        or not isinstance(pulls, list)
        or not pulls
    ):
        return
    # A prior Final Gate pins the PR tuple even after the same Draft PR advances.
    rows = session.execute(
        select(Gate, Run)
        .join(Run, (Run.workspace_id == Gate.workspace_id) & (Run.id == Gate.run_id))
        .join(
            Candidate,
            (Candidate.workspace_id == Gate.workspace_id) & (Candidate.id == Gate.candidate_id),
        )
        .where(
            Run.workspace_id == inbox.workspace_id,
            Run.repository_id == inbox.repository_id,
            Run.evidence_expired_at.is_(None),
            Run.current_candidate_id.is_distinct_from(Gate.candidate_id),
            Gate.stage == "FINAL",
            (Candidate.head_sha == tested)
            | (Gate.metadata_body["draft"]["merge_sha"].as_string() == tested),
        )
    )
    seen = set()
    for gate, run in rows:
        draft = gate.metadata_body.get("draft")
        required = run.snapshot["verification_profile"].get("required_ci_checks", [])
        if (
            gate.candidate_id in seen
            or not draft
            or draft["origin_verified"] is not True
            or not any(
                item["provider"] == "github"
                and item["app_id"] == app["id"]
                and item["check_name"] == check["name"]
                for item in required
            )
            or not any(
                matches_historical_pull(pr, draft, config.github_repository_id) for pr in pulls
            )
        ):
            continue
        seen.add(gate.candidate_id)
        session.add(
            HistoricalCIObservation(
                workspace_id=inbox.workspace_id,
                run_id=gate.run_id,
                candidate_id=gate.candidate_id,
                inbox_id=inbox.id,
                received_at=inbox.received_at,
                body={
                    "delivery_id": inbox.delivery_id,
                    "source": "SIGNED_GITHUB_WEBHOOK",
                    "gate_eligible": False,
                    "provider_check_id": check_id,
                    "check_name": check["name"],
                    "app_id": app["id"],
                    "reported_status": check_status(
                        {"status": check["status"], "conclusion": check.get("conclusion")}
                    ),
                    "provider_reported_sha": tested,
                    "head_sha": draft["head_sha"],
                    "base_sha": draft["base_sha"],
                    "pull_request_number": draft["number"],
                },
            )
        )


def matches_historical_pull(pull, draft, repository_id):
    try:
        return GitHubCIReader.matches_pr(pull, draft, repository_id)
    except (KeyError, TypeError):
        return False


def save_delivery(service, config, integration_id, delivery_id, event, body):
    try:
        payload = json.loads(body.decode("utf-8"))
        repository = payload["repository"]
        github_id, github_name = repository["id"], repository["full_name"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise DomainError(422, "INVALID_GITHUB_PAYLOAD") from None
    if type(github_id) is not int or not isinstance(github_name, str):
        raise DomainError(422, "INVALID_GITHUB_PAYLOAD")
    if github_id != config.github_repository_id or github_name.casefold() != (
        config.github_repository.casefold()
    ):
        raise DomainError(404, "NOT_FOUND")

    workspace_id, repository_id = str(config.workspace_id), str(config.repository_id)
    fingerprint = hashlib.sha256(event.encode() + b"\0" + body).hexdigest()
    with service.db.transaction() as session:
        service.lock_workspace(session, workspace_id)
        local = owned(session, Repository, repository_id, workspace_id)
        expected_remote = f"https://github.com/{config.github_repository}".casefold()
        if (
            not local.active
            or (local.remote_url or "").casefold().removesuffix(".git").rstrip("/")
            != expected_remote
        ):
            raise DomainError(404, "NOT_FOUND")
        previous = session.scalar(
            select(CIWebhookDelivery).where(
                CIWebhookDelivery.workspace_id == workspace_id,
                CIWebhookDelivery.repository_id == repository_id,
                CIWebhookDelivery.integration_id == str(integration_id),
                CIWebhookDelivery.delivery_id == str(delivery_id),
            )
        )
        if previous:
            if previous.fingerprint != fingerprint:
                raise DomainError(409, "WEBHOOK_DELIVERY_CONFLICT")
        else:
            inbox = CIWebhookDelivery(
                id=new_id(),
                workspace_id=workspace_id,
                repository_id=repository_id,
                integration_id=str(integration_id),
                delivery_id=str(delivery_id),
                event=event,
                fingerprint=fingerprint,
                received_at=db_now(session),
            )
            session.add(inbox)
            session.flush()
            remember_historical_check(session, config, inbox, payload)
    return {"accepted": True}


def webhook_router(service, integrations, vault=None):
    router = APIRouter()

    @router.post("/v1/webhooks/github/{integration_id}", status_code=202, tags=["CI"])
    async def github_webhook(integration_id: UUID, request: Request):
        config = integrations.get(integration_id)
        if config is None:
            raise DomainError(404, "NOT_FOUND")
        signature = request.headers.get("X-Hub-Signature-256", "")
        if len(signature) != 71 or not signature.startswith("sha256="):
            raise DomainError(401, "INVALID_WEBHOOK_SIGNATURE")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise DomainError(413, "WEBHOOK_BODY_TOO_LARGE")
            body.extend(chunk)
        secret = None
        if vault:
            secret = await run_in_threadpool(
                vault.webhook_secret, str(config.workspace_id), integration_id
            )
        if not secret and config.secret:
            secret = config.secret.get_secret_value()
        if not secret:
            raise DomainError(401, "INVALID_WEBHOOK_SIGNATURE")
        expected = "sha256=" + hmac.new(secret.encode(), body, "sha256").hexdigest()
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise DomainError(401, "INVALID_WEBHOOK_SIGNATURE")

        event = request.headers.get("X-GitHub-Event", "")
        if event not in EVENTS:
            raise DomainError(422, "UNSUPPORTED_GITHUB_EVENT")
        try:
            delivery_id = UUID(request.headers.get("X-GitHub-Delivery", ""))
        except ValueError:
            raise DomainError(422, "INVALID_GITHUB_DELIVERY") from None
        return await run_in_threadpool(
            save_delivery, service, config, integration_id, delivery_id, event, bytes(body)
        )

    return router
