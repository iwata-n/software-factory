"""Local administrative commands. Token values are printed only at issuance."""

import argparse
import json

from sqlalchemy import select

from .db import Database, db_now
from .models import (
    AuditEvent,
    Enrollment,
    Principal,
    Publisher,
    PublisherGrant,
    Repository,
    Workspace,
    new_id,
)
from .security import redact
from .service import WORKER_TOKEN_SECONDS, Service, issue_token, token_hash


def main():
    parser = argparse.ArgumentParser(description="Software Factory local administration")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap", help="Create a workspace and a local user credential")
    bootstrap.add_argument("--name", required=True)
    user = sub.add_parser("issue-user", help="Issue a local Workspace user credential")
    user.add_argument("--workspace-id", required=True)
    user.add_argument("--access", choices=["ADMIN", "OPERATOR", "VIEWER"], required=True)
    publisher = sub.add_parser(
        "issue-publisher", help="Issue a dedicated trusted Publisher credential"
    )
    publisher.add_argument("--workspace-id", required=True)
    publisher.add_argument("--name", required=True)
    publisher.add_argument("--repository-id", action="append", required=True)
    enrollment = sub.add_parser("issue-enrollment", help="Issue a single-use worker enrollment")
    enrollment.add_argument("--workspace-id", required=True)
    enrollment.add_argument("--ttl-seconds", type=int, default=600)
    enrollment.add_argument(
        "--managed", action="store_true", help="Trust this locally managed runner"
    )
    enrollment.add_argument("--adapter", choices=["fake", "codex_cli"], default="fake")
    enrollment.add_argument(
        "--usage-authority",
        action="store_true",
        help="Allow a managed supervisor to attest metered provider usage",
    )
    enrollment.add_argument(
        "--kinds",
        nargs="+",
        choices=["PLAN", "IMPLEMENT", "REPAIR", "ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"],
    )
    revoke = sub.add_parser("revoke", help="Revoke a local user/worker credential")
    revoke.add_argument("--principal-id", required=True)
    revoke.add_argument("--reason", default="Local administrator revoked credential")
    reissue = sub.add_parser(
        "reissue-worker", help="Reissue an inactive Worker in a stopped Workspace"
    )
    reissue.add_argument("--worker-id", required=True)
    reissue.add_argument("--reason", required=True)
    revoke_repository = sub.add_parser(
        "revoke-repository", help="Revoke repository execution access"
    )
    revoke_repository.add_argument("--repository-id", required=True)
    sub.add_parser("reconcile", help="Recover expired leases once")
    sub.add_parser("serve", help="Start loopback-only Control Plane")
    args = parser.parse_args()
    if args.command == "serve":
        from .api import main as serve

        serve()
        return
    database = Database()
    service = Service(database)
    if args.command == "reconcile":
        print(json.dumps({"expired_attempts": service.reconcile()}))
        return
    with database.transaction() as session:
        if args.command == "bootstrap":
            workspace = Workspace(name=args.name)
            session.add(workspace)
            session.flush()
            secret = issue_token()
            principal = Principal(
                workspace_id=workspace.id, role="USER", token_hash=token_hash(secret)
            )
            session.add(principal)
            session.flush()
            result = {
                "workspace_id": workspace.id,
                "principal_id": principal.id,
                "user_token": secret,
            }
        elif args.command == "issue-user":
            workspace = session.get(Workspace, args.workspace_id)
            if workspace is None:
                parser.error("workspace not found")
            service.lock_workspace(session, workspace.id)
            secret = issue_token()
            principal = Principal(
                workspace_id=workspace.id,
                role="USER",
                user_access=args.access,
                token_hash=token_hash(secret),
            )
            session.add(principal)
            session.flush()
            session.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    actor_id="local-admin",
                    operation="user.issued",
                    body={"principal_id": principal.id, "access": args.access},
                    created_at=db_now(session),
                )
            )
            result = {"principal_id": principal.id, "user_token": secret, "access": args.access}
        elif args.command == "issue-publisher":
            workspace = session.get(Workspace, args.workspace_id)
            if workspace is None:
                parser.error("workspace not found")
            service.lock_workspace(session, workspace.id)
            repositories = [session.get(Repository, rid) for rid in set(args.repository_id)]
            if any(repo is None or repo.workspace_id != workspace.id for repo in repositories):
                parser.error("repository is not in this workspace")
            secret, identity = issue_token(), new_id()
            session.add(
                Principal(
                    id=identity,
                    workspace_id=workspace.id,
                    role="PUBLISHER",
                    token_hash=token_hash(secret),
                )
            )
            session.flush()
            session.add(Publisher(id=identity, workspace_id=workspace.id, name=args.name))
            session.flush()
            for repository in repositories:
                session.add(
                    PublisherGrant(
                        workspace_id=workspace.id,
                        publisher_id=identity,
                        repository_id=repository.id,
                    )
                )
            session.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    actor_id="local-admin",
                    operation="publisher.issued",
                    body={
                        "publisher_id": identity,
                        "repository_ids": sorted(set(args.repository_id)),
                    },
                    created_at=db_now(session),
                )
            )
            result = {"publisher_id": identity, "publisher_token": secret}
        elif args.command == "issue-enrollment":
            kinds = args.kinds or ["PLAN", "IMPLEMENT"]
            if args.usage_authority and not args.managed:
                parser.error("usage authority requires --managed")
            if not args.managed and set(kinds) & {"ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"}:
                parser.error("trusted stages require --managed")
            if not 1 <= args.ttl_seconds <= 3600:
                parser.error("ttl-seconds must be between 1 and 3600")
            workspace = session.get(Workspace, args.workspace_id)
            if workspace is None:
                parser.error("workspace not found")
            service.lock_workspace(session, workspace.id)
            secret = issue_token()
            session.add(
                Enrollment(
                    workspace_id=workspace.id,
                    token_hash=token_hash(secret),
                    capabilities=[*kinds, f"adapter:{args.adapter}"]
                    + (["usage:trusted"] if args.usage_authority else []),
                    managed=args.managed,
                    expires_at=db_now(session) + args.ttl_seconds,
                )
            )
            result = {"enrollment_token": secret}
        elif args.command == "reissue-worker":
            worker = session.get(Principal, args.worker_id)
            if worker is None or worker.role != "WORKER":
                parser.error("Worker not found")
            service.lock_workspace(session, worker.workspace_id)
            session.refresh(worker)
            workspace = session.get(Workspace, worker.workspace_id)
            if worker.active or not workspace.execution_stopped:
                parser.error("Worker reissue requires an inactive Worker and a stopped Workspace")
            secret, now = issue_token(), db_now(session)
            worker.token_hash = token_hash(secret)
            worker.token_expires_at = now + WORKER_TOKEN_SECONDS
            worker.previous_token_hash = None
            worker.previous_token_expires_at = None
            worker.active = True
            session.add(
                AuditEvent(
                    workspace_id=worker.workspace_id,
                    actor_id="local-admin",
                    operation="worker.credential.reissue",
                    body={"worker_id": worker.id, "reason": redact(args.reason)},
                    created_at=now,
                )
            )
            result = {
                "worker_id": worker.id,
                "worker_token": secret,
                "token_expires_at": worker.token_expires_at,
                "protocol_version": 1,
            }
        elif args.command == "revoke-repository":
            repository = session.get(Repository, args.repository_id)
            if repository is None:
                parser.error("repository not found")
            service.lock_workspace(session, repository.workspace_id)
            repository.active = False
            session.add(
                AuditEvent(
                    workspace_id=repository.workspace_id,
                    actor_id="local-admin",
                    operation="repository.revoke",
                    body={"repository_id": repository.id},
                    created_at=db_now(session),
                )
            )
            result = {"revoked": True, "repository_id": repository.id}
        else:
            principal = session.scalar(select(Principal).where(Principal.id == args.principal_id))
            if principal is None:
                parser.error("principal not found")
            service.lock_workspace(session, principal.workspace_id)
            session.refresh(principal)
            previous_active = principal.active
            principal.active = False
            session.add(
                AuditEvent(
                    workspace_id=principal.workspace_id,
                    actor_id="local-admin",
                    operation="principal.revoke",
                    body={
                        "principal_id": principal.id,
                        "before_active": previous_active,
                        "after_active": False,
                        "reason": redact(args.reason),
                    },
                    created_at=db_now(session),
                )
            )
            result = {"revoked": True, "principal_id": principal.id}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
