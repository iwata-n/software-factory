"""Authenticated input from a trusted Jira relay, using a dedicated structured field."""

import hmac
import json
import os
from typing import Annotated

from fastapi import APIRouter, Header
from pydantic import Field, ValidationError
from sqlalchemy import select

from .contracts import Contract, Source, TaskSpec, digest
from .models import JiraIssueRevision, Principal, Project, Task
from .security import redact
from .service import DomainError, Service, owned


class JiraMapping(Contract):
    integration_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,36}$")]
    site: Annotated[str, Field(pattern=r"^https://[a-z0-9-]{1,63}\.atlassian\.net$")]
    project_key: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{0,19}$")]
    workspace_id: Annotated[str, Field(min_length=1, max_length=36)]
    project_id: Annotated[str, Field(min_length=1, max_length=36)]
    principal_id: Annotated[str, Field(min_length=1, max_length=36)]
    field_id: Annotated[str, Field(pattern=r"^customfield_[0-9]{1,16}$")]


class JiraInput(Contract):
    site: Annotated[str, Field(max_length=100)]
    project_key: Annotated[str, Field(max_length=20)]
    issue_id: Annotated[str, Field(pattern=r"^[0-9]{1,32}$")]
    issue_key: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{0,19}-[0-9]{1,20}$")]
    source_version: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    fields: dict


def configured_jira() -> tuple[JiraMapping, str] | None:
    raw = os.environ.get("FACTORY_JIRA_MAPPING")
    if raw is None:
        return None
    try:
        mapping = JiraMapping.model_validate_json(raw)
    except ValidationError:
        raise ValueError("Invalid FACTORY_JIRA_MAPPING") from None
    secret = os.environ.get("FACTORY_JIRA_RELAY_TOKEN", "")
    if len(secret) < 32:
        raise ValueError("FACTORY_JIRA_RELAY_TOKEN must have at least 32 characters")
    return mapping, secret


def structured_field_text(raw):
    """Decode plain text blocks from Jira Cloud multiline custom fields."""
    if not isinstance(raw, dict) or raw.get("type") != "doc":
        return raw
    blocks = raw.get("content")
    if raw.get("version") != 1 or not isinstance(blocks, list):
        return None
    paragraphs = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in ("paragraph", "codeBlock"):
            return None
        content = block.get("content", [])
        if not isinstance(content, list):
            return None
        lines = []
        for node in content:
            if not isinstance(node, dict):
                return None
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                lines.append(node["text"])
            elif node.get("type") == "hardBreak":
                lines.append("\n")
            else:
                return None
        paragraphs.append("".join(lines))
    return "\n".join(paragraphs)


def normalize(raw, secret: str) -> tuple[str, TaskSpec | None]:
    raw = structured_field_text(raw)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return "NEEDS_INPUT", None
    if not isinstance(raw, dict):
        return "NEEDS_INPUT", None
    if redact(raw, (secret,)) != raw:
        return "QUARANTINED", None
    try:
        spec = TaskSpec.model_validate(raw)
    except ValidationError:
        return "NEEDS_INPUT", None
    # Source provenance lives in the revision ledger. Keep the executable spec canonical.
    return "IMPORTED", spec.model_copy(
        update={"task": spec.task.model_copy(update={"source": Source()})}
    )


def receive(service: Service, mapping: JiraMapping, delivery: JiraInput, secret: str) -> dict:
    if (
        delivery.site != mapping.site
        or delivery.project_key != mapping.project_key
        or not delivery.issue_key.startswith(f"{mapping.project_key}-")
        or set(delivery.fields) - {mapping.field_id}
    ):
        raise DomainError(403, "JIRA_MAPPING_DENIED")

    with service.db.transaction() as session:
        service.lock_workspace(session, mapping.workspace_id)
        actor = session.get(Principal, mapping.principal_id, populate_existing=True)
        if (
            not actor
            or not actor.active
            or actor.role != "USER"
            or actor.user_access not in {"OPERATOR", "ADMIN"}
            or actor.workspace_id != mapping.workspace_id
        ):
            raise DomainError(403, "JIRA_INTEGRATION_REVOKED")
        owned(session, Project, mapping.project_id, mapping.workspace_id)
        state, spec = normalize(delivery.fields.get(mapping.field_id), secret)
        issue = select(JiraIssueRevision).where(
            JiraIssueRevision.workspace_id == mapping.workspace_id,
            JiraIssueRevision.integration_id == mapping.integration_id,
            JiraIssueRevision.issue_id == delivery.issue_id,
        )
        latest = session.scalar(issue.order_by(JiraIssueRevision.source_version.desc()).limit(1))
        if latest and not latest.issue_url.startswith(
            f"{mapping.site}/browse/{mapping.project_key}-"
        ):
            raise DomainError(403, "JIRA_MAPPING_DENIED")
        if session.scalar(issue.where(JiraIssueRevision.state == "RETIRED").limit(1)):
            raise DomainError(404, "JIRA_TASK_RETIRED")
        imported = session.scalar(
            issue.where(JiraIssueRevision.state == "IMPORTED")
            .order_by(JiraIssueRevision.source_version.desc())
            .limit(1)
        )
        if imported:
            task = owned(session, Task, imported.task_id, mapping.workspace_id)
            if task.project_id != mapping.project_id:
                raise DomainError(403, "JIRA_MAPPING_DENIED")

        issue_url = f"{mapping.site}/browse/{delivery.issue_key}"
        fingerprint = digest({"url": issue_url, "spec": spec.model_dump()}) if spec else None
        revision = session.scalar(
            issue.where(JiraIssueRevision.source_version == delivery.source_version)
        )
        if revision is not None:
            if revision.fingerprint != fingerprint or revision.issue_url != issue_url:
                raise DomainError(409, "JIRA_VERSION_CONFLICT")
        else:
            result = {}
            if latest and latest.source_version > delivery.source_version:
                state = "STALE"
            elif spec:
                result = (
                    service.new_spec(session, actor, imported.task_id, spec)
                    if imported
                    else service.create_task(session, actor, mapping.project_id, spec)
                )
            revision = JiraIssueRevision(
                workspace_id=mapping.workspace_id,
                integration_id=mapping.integration_id,
                issue_id=delivery.issue_id,
                source_version=delivery.source_version,
                issue_url=issue_url,
                state=state,
                fingerprint=fingerprint,
                task_id=result.get("task_id"),
                spec_version=result.get("spec_version"),
            )
            session.add(revision)
        return {
            "state": revision.state,
            "issue_url": revision.issue_url,
            "source_version": revision.source_version,
            "task_id": revision.task_id,
            "spec_version": revision.spec_version,
        }


def jira_router(service: Service, mapping: JiraMapping, secret: str) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/integrations/jira/{integration_id}/issues", tags=["Integrations"])
    def receive_issue(
        integration_id: str,
        delivery: JiraInput,
        authorization: Annotated[str | None, Header()] = None,
    ):
        expected = f"Bearer {secret}".encode()
        if integration_id != mapping.integration_id or not hmac.compare_digest(
            (authorization or "").encode(), expected
        ):
            raise DomainError(401, "JIRA_UNAUTHENTICATED")
        return receive(service, mapping, delivery, secret)

    return router
