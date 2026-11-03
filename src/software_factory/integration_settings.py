"""Workspace-scoped credentials used by the web integration flows."""

import json
import os
import secrets
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import Field, SecretStr
from sqlalchemy import select

from .contracts import Contract, digest
from .db import db_now
from .jira_input import JiraMapping, receive
from .jira_relay import read_issue
from .models import AuditEvent, IntegrationSettings, Principal, Project
from .service import DomainError, owned
from .slack_delivery import SlackClient, SlackDispatcher

Provider = Literal["jira", "slack", "github"]
STATE_SECONDS = 600
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


class ConnectionInput(Contract):
    mode: Literal["token", "oauth"]
    token: Annotated[SecretStr, Field(min_length=1, max_length=4096)] | None = None
    client_id: Annotated[str, Field(min_length=1, max_length=250)] | None = None
    client_secret: Annotated[SecretStr, Field(min_length=1, max_length=4096)] | None = None


class JiraSettings(ConnectionInput):
    site: Annotated[str, Field(pattern=r"^https://[a-z0-9-]{1,63}\.atlassian\.net$")]
    project_key: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{0,19}$")]
    field_id: Annotated[str, Field(pattern=r"^customfield_[0-9]{1,16}$")]
    project_id: UUID
    email: Annotated[str, Field(min_length=1, max_length=254)] | None = None


class SlackSettings(ConnectionInput):
    channel: Annotated[str, Field(pattern=r"^[CG][A-Z0-9]{8,30}$")]
    team_id: Annotated[str, Field(pattern=r"^T[A-Z0-9]{8,30}$")] | None = None


class GitHubSettings(Contract):
    token: Annotated[SecretStr, Field(min_length=1, max_length=4096)]


class WebhookSecret(Contract):
    secret: Annotated[SecretStr, Field(min_length=32, max_length=4096)]


class JiraImport(Contract):
    issue_key: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{0,19}-[0-9]{1,20}$")]


def oauth_origin():
    raw = os.getenv("FACTORY_OAUTH_ORIGIN", "")
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or raw != f"https://{parsed.netloc}"
    ):
        raise ValueError("FACTORY_OAUTH_ORIGIN must be an HTTPS origin without a path")
    return raw


class Vault:
    def __init__(self, db, key):
        self.db = db
        self.cipher = Fernet(key.encode()) if key else None

    def require(self):
        if self.cipher is None:
            raise DomainError(503, "INTEGRATION_VAULT_NOT_CONFIGURED")
        return self.cipher

    def decode(self, row):
        try:
            record = json.loads(self.require().decrypt(row.ciphertext))
            if (record["workspace_id"], record["provider"]) != (row.workspace_id, row.provider):
                raise ValueError
            return record["value"]
        except (InvalidToken, ValueError, KeyError):
            raise DomainError(503, "INTEGRATION_VAULT_UNREADABLE") from None

    def encode(self, row, value):
        row.ciphertext = self.require().encrypt(
            json.dumps(
                {"workspace_id": row.workspace_id, "provider": row.provider, "value": value}
            ).encode()
        )

    def webhook_secret(self, workspace, integration_id):
        if not self.cipher:
            return None
        with self.db.transaction() as session:
            row = session.get(IntegrationSettings, (workspace, "github"))
            return (
                self.decode(row).get("webhook_secrets", {}).get(str(integration_id))
                if row
                else None
            )

    def active(self, workspace, provider, client):
        with self.db.transaction() as session:
            row = session.get(IntegrationSettings, (workspace, provider), with_for_update=True)
            if row is None:
                return None
            value = self.decode(row)
            if not value.get("token"):
                return None
            if value.get("expires_at", float("inf")) <= db_now(session) + 60:
                value.update(exchange(client, provider, value, db_now(session)))
                self.encode(row, value)
            return value


def exchange(client, provider, value, now, *, code=None, redirect_uri=None):
    credentials = {"client_id": value["client_id"], "client_secret": value["client_secret"]}
    if code is None:
        credentials.update(grant_type="refresh_token", refresh_token=value.get("refresh_token"))
        if not credentials["refresh_token"]:
            raise DomainError(409, "OAUTH_RECONNECT_REQUIRED")
    else:
        credentials.update(code=code, redirect_uri=redirect_uri)
        if provider == "jira":
            credentials["grant_type"] = "authorization_code"
    try:
        if provider == "jira":
            reply = client.post(ATLASSIAN_TOKEN_URL, json=credentials)
        else:
            reply = client.post(SLACK_TOKEN_URL, data=credentials)
        reply.raise_for_status()
        data = reply.json()
        if provider == "slack" and (
            data.get("ok") is not True
            or data.get("token_type") != "bot"
            or (code is not None and data.get("team", {}).get("id") != value["team_id"])
        ):
            raise ValueError
        token = data["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError
        result = {"token": token}
        if "expires_in" in data:
            seconds = int(data["expires_in"])
            if seconds <= 0 or not data.get("refresh_token"):
                raise ValueError
            result.update(expires_at=now + seconds, refresh_token=data["refresh_token"])
        if provider == "jira" and code is not None:
            resources = client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {token}"},
            )
            resources.raise_for_status()
            result["cloud_id"] = str(
                UUID(
                    next(
                        resource["id"]
                        for resource in resources.json()
                        if resource["url"] == value["site"]
                        and "read:jira-work" in resource["scopes"]
                    )
                )
            )
        return result
    except (httpx.HTTPError, KeyError, ValueError, TypeError, StopIteration):
        raise DomainError(502, "OAUTH_EXCHANGE_FAILED") from None


def provider_client():
    return httpx.Client(timeout=10, trust_env=False)


def integration_router(service, token_type, vault, origin, webhooks):
    router = APIRouter(prefix="/v1/operations/integrations")

    def secure(request):
        loopback = {"localhost", "127.0.0.1", "::1"}
        if request.url.scheme != "https" and (
            request.url.hostname not in loopback
            or not request.client
            or request.client.host not in loopback
        ):
            raise DomainError(403, "INTEGRATION_HTTPS_REQUIRED")

    def admin(session, token, *, lock=False):
        authenticate = service.locked_principal if lock else service.principal
        actor = authenticate(session, token, "USER")
        service.require_user_access(actor, "ADMIN")
        return actor

    def redirect_uri(provider):
        return f"{origin}{router.prefix}/{provider}/oauth/callback"

    @router.get("")
    def settings(token: token_type, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with service.db.transaction() as session:
            actor = admin(session, token)
            result = {}
            for provider in ("jira", "slack", "github"):
                row = session.get(IntegrationSettings, (actor.workspace_id, provider))
                value = vault.decode(row) if row and vault.cipher else {}
                result[provider] = {
                    key: value[key]
                    for key in (
                        "mode",
                        "site",
                        "project_key",
                        "project_id",
                        "field_id",
                        "channel",
                        "team_id",
                        "client_id",
                    )
                    if key in value
                } | {"connected": bool(value.get("token"))}
                if provider == "github":
                    result[provider]["webhooks"] = [
                        {
                            "id": str(identifier),
                            "repository": config.github_repository,
                            "configured": str(identifier) in value.get("webhook_secrets", {}),
                            "environment_configured": bool(config.secret),
                        }
                        for identifier, config in webhooks.items()
                        if str(config.workspace_id) == actor.workspace_id
                    ]
            projects = session.scalars(
                select(Project)
                .where(Project.workspace_id == actor.workspace_id)
                .order_by(Project.name, Project.id)
            ).all()
            return {
                "projects": [{"id": row.id, "name": row.name} for row in projects],
                "available": vault.cipher is not None,
                "oauth_available": bool(origin),
                "providers": result,
            }

    def save(provider, token, request, data):
        secure(request)
        vault.require()
        value = data.model_dump(mode="json", exclude={"token", "client_secret"}, exclude_none=True)
        if provider == "github":
            value.update(mode="token", token=data.token.get_secret_value())
        elif data.mode == "token" and data.token and (provider != "jira" or data.email):
            value["token"] = data.token.get_secret_value()
        elif (
            data.mode == "oauth"
            and origin
            and data.client_id
            and data.client_secret
            and (provider != "slack" or data.team_id)
        ):
            value["client_secret"] = data.client_secret.get_secret_value()
        else:
            raise DomainError(422, "INTEGRATION_CREDENTIALS_INCOMPLETE")
        with service.db.transaction() as session:
            actor = admin(session, token, lock=True)
            if provider == "jira":
                owned(session, Project, str(data.project_id), actor.workspace_id)
            key = actor.workspace_id, provider
            row = session.get(IntegrationSettings, key, with_for_update=True)
            if row is None:
                row = IntegrationSettings(workspace_id=actor.workspace_id, provider=provider)
                session.add(row)
            if provider == "github" and row.ciphertext:
                value["webhook_secrets"] = vault.decode(row).get("webhook_secrets", {})
            row.oauth_state_hash = None
            vault.encode(row, value)
            audit(session, actor, provider, "save")
        return {"connected": bool(value.get("token"))}

    @router.put("/jira")
    def save_jira(request: Request, data: JiraSettings, token: token_type):
        return save("jira", token, request, data)

    @router.put("/slack")
    def save_slack(request: Request, data: SlackSettings, token: token_type):
        return save("slack", token, request, data)

    @router.put("/github")
    def save_github(request: Request, data: GitHubSettings, token: token_type):
        return save("github", token, request, data)

    @router.delete("/{provider}")
    def remove(provider: Provider, request: Request, token: token_type):
        secure(request)
        with service.db.transaction() as session:
            actor = admin(session, token, lock=True)
            row = session.get(
                IntegrationSettings, (actor.workspace_id, provider), with_for_update=True
            )
            if row:
                if provider == "github" and (
                    webhook_secrets := vault.decode(row).get("webhook_secrets")
                ):
                    vault.encode(row, {"webhook_secrets": webhook_secrets})
                else:
                    session.delete(row)
                audit(session, actor, provider, "remove")
        return Response(status_code=204)

    def update_webhook(integration_id, request, token, secret):
        secure(request)
        vault.require()
        with service.db.transaction() as session:
            actor = admin(session, token, lock=True)
            config = webhooks.get(integration_id)
            if config is None or str(config.workspace_id) != actor.workspace_id:
                raise DomainError(404, "NOT_FOUND")
            row = session.get(
                IntegrationSettings, (actor.workspace_id, "github"), with_for_update=True
            )
            if row is None:
                if secret is None:
                    return Response(status_code=204)
                row = IntegrationSettings(workspace_id=actor.workspace_id, provider="github")
                session.add(row)
                value = {}
            else:
                value = vault.decode(row)
            saved = value.setdefault("webhook_secrets", {})
            if secret is None:
                saved.pop(str(integration_id), None)
            else:
                saved[str(integration_id)] = secret
            vault.encode(row, value)
            audit(session, actor, "github", "webhook_remove" if secret is None else "webhook_save")
        return Response(status_code=204)

    @router.put("/github/webhooks/{integration_id}")
    def save_webhook(
        integration_id: UUID, data: WebhookSecret, request: Request, token: token_type
    ):
        return update_webhook(integration_id, request, token, data.secret.get_secret_value())

    @router.delete("/github/webhooks/{integration_id}")
    def remove_webhook(integration_id: UUID, request: Request, token: token_type):
        return update_webhook(integration_id, request, token, None)

    @router.post("/{provider}/oauth/start")
    def start(
        provider: Literal["jira", "slack"], request: Request, response: Response, token: token_type
    ):
        secure(request)
        if not origin:
            raise DomainError(503, "OAUTH_ORIGIN_NOT_CONFIGURED")
        if f"{request.url.scheme}://{request.url.netloc}" != origin:
            raise DomainError(403, "OAUTH_ORIGIN_MISMATCH")
        state = secrets.token_urlsafe(32)
        with service.db.transaction() as session:
            actor = admin(session, token, lock=True)
            row = session.get(
                IntegrationSettings, (actor.workspace_id, provider), with_for_update=True
            )
            value = vault.decode(row) if row else {}
            if value.get("mode") != "oauth":
                raise DomainError(409, "OAUTH_CLIENT_NOT_CONFIGURED")
            row.oauth_state_hash = digest(state)
            value["pending"] = {"actor": actor.id, "expires_at": db_now(session) + STATE_SECONDS}
            vault.encode(row, value)
        params = {
            "client_id": value["client_id"],
            "redirect_uri": redirect_uri(provider),
            "state": state,
        }
        if provider == "jira":
            endpoint = "https://auth.atlassian.com/authorize"
            params.update(
                audience="api.atlassian.com",
                scope="read:jira-work offline_access",
                response_type="code",
                prompt="consent",
            )
        else:
            endpoint = "https://slack.com/oauth/v2/authorize"
            params.update(scope="chat:write,channels:history,groups:history", team=value["team_id"])
        response.set_cookie(
            f"factory_oauth_{provider}",
            state,
            max_age=STATE_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path=router.prefix,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"url": f"{endpoint}?{urlencode(params)}"}

    @router.get("/{provider}/oauth/callback", include_in_schema=False)
    def callback(
        provider: Literal["jira", "slack"], request: Request, state: str = "", code: str = ""
    ):
        cookie = f"factory_oauth_{provider}"
        status = "failed"
        cookie_state = request.cookies.get(cookie, "")
        if (
            origin
            and f"{request.url.scheme}://{request.url.netloc}" == origin
            and state
            and len(state) <= 128
            and state.isascii()
            and cookie_state.isascii()
            and secrets.compare_digest(state, cookie_state)
        ):
            try:
                with service.db.transaction() as session:
                    row = session.scalar(
                        select(IntegrationSettings)
                        .where(
                            IntegrationSettings.provider == provider,
                            IntegrationSettings.oauth_state_hash == digest(state),
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise DomainError(400, "OAUTH_STATE_INVALID")
                    value = vault.decode(row)
                    pending = value.pop("pending")
                    actor = session.get(Principal, pending["actor"])
                    row.oauth_state_hash = None
                    vault.encode(row, value)
                    if (
                        actor
                        and actor.active
                        and actor.role == "USER"
                        and actor.user_access == "ADMIN"
                        and actor.workspace_id == row.workspace_id
                        and pending["expires_at"] >= db_now(session)
                        and 0 < len(code) <= 4096
                    ):
                        # Commit state consumption even if the provider refuses the exchange.
                        try:
                            with provider_client() as client:
                                value.update(
                                    exchange(
                                        client,
                                        provider,
                                        value,
                                        db_now(session),
                                        code=code,
                                        redirect_uri=redirect_uri(provider),
                                    )
                                )
                            vault.encode(row, value)
                            audit(session, actor, provider, "connect")
                            status = "connected"
                        except DomainError:
                            pass
            except DomainError:
                pass
        reply = RedirectResponse(
            f"/operations?integration={provider}&oauth={status}",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        reply.delete_cookie(cookie, path=router.prefix, secure=True, httponly=True, samesite="lax")
        return reply

    @router.post("/jira/import")
    def import_jira(data: JiraImport, request: Request, token: token_type):
        secure(request)
        with service.db.transaction() as session:
            actor = admin(session, token)
        with provider_client() as client:
            value = vault.active(actor.workspace_id, "jira", client)
            if value is None:
                raise DomainError(409, "JIRA_NOT_CONNECTED")
            mapping = JiraMapping(
                integration_id="web-jira-" + digest([value["site"], value["project_key"]])[:24],
                site=value["site"],
                project_key=value["project_key"],
                workspace_id=actor.workspace_id,
                project_id=value["project_id"],
                principal_id=actor.id,
                field_id=value["field_id"],
            )
            try:
                delivery = read_issue(
                    client,
                    mapping,
                    data.issue_key,
                    value.get("email"),
                    value["token"],
                    cloud_id=value.get("cloud_id"),
                )
            except (httpx.HTTPError, ValueError):
                raise DomainError(502, "JIRA_READ_FAILED") from None
        return receive(service, mapping, delivery, value["token"])

    @router.post("/slack/deliver")
    def deliver_slack(request: Request, token: token_type):
        secure(request)
        with service.db.transaction() as session:
            actor = admin(session, token)
        with provider_client() as client:
            value = vault.active(actor.workspace_id, "slack", client)
            if value is None:
                raise DomainError(409, "SLACK_NOT_CONNECTED")
            dispatcher = SlackDispatcher(
                service.db,
                SlackClient(client, value["token"]),
                actor.workspace_id,
                value["channel"],
            )
            return {"state": dispatcher.deliver_one()}

    return router


def audit(session, actor, provider, action):
    session.add(
        AuditEvent(
            workspace_id=actor.workspace_id,
            actor_id=actor.id,
            operation=f"integration.settings.{action}",
            body={"provider": provider},
            created_at=db_now(session),
        )
    )
