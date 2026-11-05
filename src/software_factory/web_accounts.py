"""Local web accounts backed by the existing Workspace user roles."""

import hashlib
import hmac
import secrets
from threading import BoundedSemaphore
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .contracts import Contract
from .db import db_now
from .models import AuditEvent, Principal, WebAccount, WebSession
from .service import WEB_SESSION_PREFIX, DomainError, issue_token, token_hash

SESSION_SECONDS = 7 * 24 * 60 * 60
USERNAME = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")]
PASSWORD = Annotated[SecretStr, Field(min_length=12, max_length=256)]
HASH_SLOTS = BoundedSemaphore(4)


class Login(Contract):
    username: USERNAME
    password: Annotated[SecretStr, Field(min_length=1, max_length=256)]


class NewAccount(Login):
    password: PASSWORD
    access: Literal["ADMIN", "OPERATOR", "VIEWER"]


class AccountChange(Contract):
    active: bool


class PasswordReset(Contract):
    password: PASSWORD


def browser_secure(request):
    loopback = {"localhost", "127.0.0.1", "::1"}
    return request.url.scheme == "https" or (
        request.url.hostname in loopback and request.client and request.client.host in loopback
    )


def browser_write(request):
    if not browser_secure(request):
        raise DomainError(403, "HTTPS_REQUIRED")
    if request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}":
        raise DomainError(403, "ORIGIN_DENIED")


def cookie_name(request):
    # A host-only cookie is normally shared across ports. Keep local previews independent.
    port = request.url.port
    return f"factory_session_{port}" if port else "factory_session"


def cookie_options(request):
    return {
        "path": "/v1",
        "httponly": True,
        "secure": request.url.scheme == "https",
        "samesite": "strict",
    }


def browser_token(request):
    if not browser_secure(request):
        return None
    token = request.cookies.get(cookie_name(request), "")
    if token.startswith(WEB_SESSION_PREFIX) and len(token) == len(WEB_SESSION_PREFIX) + 43:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            browser_write(request)
        return token
    return None


def password_hash(password, salt):
    if not HASH_SLOTS.acquire(blocking=False):
        raise DomainError(429, "AUTH_BUSY")
    try:
        return hashlib.scrypt(
            password.encode(), salt=salt, n=2**16, r=8, p=1, dklen=32, maxmem=96 * 1024**2
        )
    finally:
        HASH_SLOTS.release()


def account_view(account, actor):
    return {
        "id": actor.id,
        "username": account.username,
        "access": actor.user_access,
        "active": actor.active,
    }


def revoke_sessions(session, principal_id):
    session.execute(delete(WebSession).where(WebSession.principal_id == principal_id))


def web_accounts_router(service, token_type):
    router = APIRouter(prefix="/v1/auth", tags=["Web accounts"])

    def admin(session, token, *, write=False):
        authenticate = service.locked_principal if write else service.principal
        actor = authenticate(session, token, "USER")
        service.require_user_access(actor, "ADMIN")
        return actor

    def lookup(session, admin_actor, identifier):
        row = session.execute(
            select(WebAccount, Principal)
            .join(Principal, Principal.id == WebAccount.principal_id)
            .where(
                Principal.workspace_id == admin_actor.workspace_id, Principal.id == str(identifier)
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise DomainError(404, "NOT_FOUND")
        return row

    def audit(session, actor, target, operation):
        session.add(
            AuditEvent(
                workspace_id=actor.workspace_id,
                actor_id=actor.id,
                operation=operation,
                body={"principal_id": target.id, "access": target.user_access},
                created_at=db_now(session),
            )
        )

    @router.post("/login")
    def login(data: Login, request: Request, response: Response):
        browser_write(request)
        with service.db.transaction() as session:
            row = session.execute(
                select(WebAccount, Principal)
                .join(Principal, Principal.id == WebAccount.principal_id)
                .where(WebAccount.username == data.username.lower())
                .with_for_update()
            ).one_or_none()
            account, actor = row or (None, None)
            now = db_now(session)
            blocked = account and account.login_after > now
            valid = hmac.compare_digest(
                password_hash(
                    data.password.get_secret_value(),
                    account.password_salt if account else b"\0" * 16,
                ),
                account.password_hash if account else b"\0" * 32,
            )
            if account and actor.active and not blocked:
                if valid:
                    account.login_failures = 0
                    account.login_after = 0
                    previous = browser_token(request)
                    session.execute(
                        delete(WebSession).where(
                            (WebSession.token_hash == token_hash(previous or ""))
                            | (
                                (WebSession.principal_id == actor.id)
                                & (WebSession.expires_at <= now)
                            )
                        )
                    )
                    token = WEB_SESSION_PREFIX + issue_token()
                    session.add(
                        WebSession(
                            token_hash=token_hash(token),
                            principal_id=actor.id,
                            expires_at=now + SESSION_SECONDS,
                        )
                    )
                else:
                    account.login_failures += 1
                    if account.login_failures >= 5:
                        account.login_after = now + min(900, 2 ** min(account.login_failures, 10))
            if not (account and actor.active and not blocked and valid):
                token = None
        if token is None:
            raise DomainError(401, "LOGIN_FAILED")
        response.set_cookie(
            cookie_name(request), token, max_age=SESSION_SECONDS, **cookie_options(request)
        )
        response.headers["Cache-Control"] = "no-store"
        return account_view(account, actor)

    @router.get("/session")
    def current_session(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        lan = request.app.state.lan_access
        if lan and (token := lan.token_for(request)):
            return service.read(
                token,
                lambda session, actor: {
                    "id": actor.id,
                    "username": "LAN 共有",
                    "access": actor.user_access,
                    "active": actor.active,
                    "authentication": "lan",
                },
            )
        token = browser_token(request)
        if token is not None:
            try:
                return service.read(
                    token,
                    lambda session, actor: account_view(session.get(WebAccount, actor.id), actor),
                )
            except DomainError as error:
                if error.status != 401:
                    raise
                response.delete_cookie(cookie_name(request), **cookie_options(request))
        response.status_code = 204
        return None

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response):
        browser_write(request)
        if token := browser_token(request):
            with service.db.transaction() as session:
                session.execute(
                    delete(WebSession).where(WebSession.token_hash == token_hash(token))
                )
        response.delete_cookie(cookie_name(request), **cookie_options(request))
        response.headers["Cache-Control"] = "no-store"

    @router.get("/accounts")
    def accounts(token: token_type, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with service.db.transaction() as session:
            actor = admin(session, token)
            rows = session.execute(
                select(WebAccount, Principal)
                .join(Principal, Principal.id == WebAccount.principal_id)
                .where(Principal.workspace_id == actor.workspace_id)
                .order_by(WebAccount.username)
            )
            return {
                "accounts": [account_view(account, principal) for account, principal in rows],
                "current_id": actor.id,
            }

    @router.post("/accounts", status_code=201)
    def create_account(data: NewAccount, request: Request, token: token_type):
        browser_write(request)
        try:
            with service.db.transaction() as session:
                actor = admin(session, token, write=True)
                target = Principal(
                    workspace_id=actor.workspace_id,
                    role="USER",
                    user_access=data.access,
                    token_hash=token_hash(issue_token()),
                )
                session.add(target)
                session.flush()
                salt = secrets.token_bytes(16)
                account = WebAccount(
                    principal_id=target.id,
                    username=data.username.lower(),
                    password_salt=salt,
                    password_hash=password_hash(data.password.get_secret_value(), salt),
                )
                session.add(account)
                audit(session, actor, target, "web_account.create")
                return account_view(account, target)
        except IntegrityError:
            raise DomainError(409, "USERNAME_TAKEN") from None

    @router.patch("/accounts/{identifier}")
    def change_account(identifier: UUID, data: AccountChange, request: Request, token: token_type):
        browser_write(request)
        with service.db.transaction() as session:
            actor = admin(session, token, write=True)
            account, target = lookup(session, actor, identifier)
            if target.id == actor.id:
                raise DomainError(409, "SELF_DISABLE_DENIED")
            if target.active != data.active:
                target.active = data.active
                revoke_sessions(session, target.id)
                audit(
                    session,
                    actor,
                    target,
                    "web_account.enable" if data.active else "web_account.disable",
                )
            return account_view(account, target)

    @router.put("/accounts/{identifier}/password", status_code=204)
    def reset_password(identifier: UUID, data: PasswordReset, request: Request, token: token_type):
        browser_write(request)
        with service.db.transaction() as session:
            actor = admin(session, token, write=True)
            account, target = lookup(session, actor, identifier)
            salt = secrets.token_bytes(16)
            account.password_salt = salt
            account.password_hash = password_hash(data.password.get_secret_value(), salt)
            account.login_failures = 0
            account.login_after = 0
            revoke_sessions(session, target.id)
            audit(session, actor, target, "web_account.reset_password")

    return router
