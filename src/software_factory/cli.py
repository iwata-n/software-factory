"""Local administrative commands. Token values are printed only at issuance."""

import argparse
import json

from sqlalchemy import select

from .db import Database, db_now
from .models import AuditEvent, Enrollment, Principal, Repository, Workspace
from .service import Service, issue_token, token_hash


def main():
    parser = argparse.ArgumentParser(description="Software Factory local administration")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap", help="Create a workspace and a local user credential")
    bootstrap.add_argument("--name", required=True)
    enrollment = sub.add_parser("issue-enrollment", help="Issue a single-use worker enrollment")
    enrollment.add_argument("--workspace-id", required=True)
    enrollment.add_argument("--ttl-seconds", type=int, default=600)
    enrollment.add_argument(
        "--managed", action="store_true", help="Trust this locally managed runner"
    )
    enrollment.add_argument("--adapter", choices=["fake", "codex_cli"], default="fake")
    enrollment.add_argument(
        "--kinds",
        nargs="+",
        choices=["PLAN", "IMPLEMENT", "REPAIR", "ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"],
    )
    revoke = sub.add_parser("revoke", help="Revoke a local user/worker credential")
    revoke.add_argument("--principal-id", required=True)
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
        elif args.command == "issue-enrollment":
            kinds = args.kinds or ["PLAN", "IMPLEMENT"]
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
                    capabilities=[*kinds, f"adapter:{args.adapter}"],
                    managed=args.managed,
                    expires_at=db_now(session) + args.ttl_seconds,
                )
            )
            result = {"enrollment_token": secret}
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
            principal.active = False
            session.add(
                AuditEvent(
                    workspace_id=principal.workspace_id,
                    actor_id="local-admin",
                    operation="principal.revoke",
                    body={"principal_id": principal.id},
                    created_at=db_now(session),
                )
            )
            result = {"revoked": True, "principal_id": principal.id}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
