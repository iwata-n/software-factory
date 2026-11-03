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
from .models import CIWebhookDelivery, Repository
from .service import DomainError, owned

MAX_BODY = 2 * 1024 * 1024
EVENTS = {"check_run", "check_suite", "workflow_run", "ping"}


class GitHubWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: UUID
    repository_id: UUID
    github_repository_id: Annotated[int, Field(strict=True, gt=0)]
    github_repository: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    secret: Annotated[SecretStr, Field(min_length=32)]


def configured_webhooks():
    try:
        return TypeAdapter(dict[UUID, GitHubWebhook]).validate_json(
            os.environ.get("FACTORY_GITHUB_WEBHOOKS", "{}")
        )
    except ValidationError:
        raise ValueError("Invalid FACTORY_GITHUB_WEBHOOKS configuration") from None


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
            session.add(
                CIWebhookDelivery(
                    workspace_id=workspace_id,
                    repository_id=repository_id,
                    integration_id=str(integration_id),
                    delivery_id=str(delivery_id),
                    event=event,
                    fingerprint=fingerprint,
                    received_at=db_now(session),
                )
            )
    return {"accepted": True}


def webhook_router(service, integrations):
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
        expected = (
            "sha256="
            + hmac.new(config.secret.get_secret_value().encode(), body, "sha256").hexdigest()
        )
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
