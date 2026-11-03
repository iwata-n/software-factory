"""Offline backup, recovery staging, and artifact retention."""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from alembic import command as migrations
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from sqlalchemy import delete, func, inspect, select, text, update

from software_factory.db import Database, db_now
from software_factory.domain import TERMINAL
from software_factory.models import (
    ApprovalRequest,
    Artifact,
    Attempt,
    AuditEvent,
    Base,
    Enrollment,
    HistoricalCIObservation,
    IdempotencyRecord,
    JiraIssueRevision,
    Job,
    OutboxEvent,
    Principal,
    PublicationOperation,
    Run,
    RunEvent,
    SlackDelivery,
    SpecVersion,
    Task,
    WorkerEvent,
    Workspace,
)
from software_factory.service import event
from software_factory.storage import LocalArtifactStorage, content_hash, identifier

DAY = 86400
GENERATION = re.compile(r"factory-[0-9]{20}-[a-f0-9]{32}\.cms")


def postgres(database, command, *args):
    url = database.engine.url
    params = database.engine.dialect.create_connect_args(url)[1]
    params.pop("context", None)

    password = params.pop("password", None)
    env = os.environ.copy()
    if password is not None:
        env["PGPASSWORD"] = password
    executable = Path(env.get("FACTORY_POSTGRES_BIN", "")) / command
    subprocess.run(
        [str(executable), "--no-password", "--dbname", make_conninfo(**params), *args],
        env=env,
        check=True,
    )


def inventory_rows(connection):
    return [
        [identifier(workspace), identifier(artifact), size, sha]
        for workspace, artifact, size, sha in connection.execute(
            select(
                Artifact.workspace_id,
                Artifact.id,
                Artifact.stored_size_bytes,
                Artifact.stored_sha256,
            )
            .where(Artifact.status.in_(["AVAILABLE", "UPLOADED"]))
            .order_by(Artifact.workspace_id, Artifact.id)
        )
    ]


def inventory(database):
    with database.transaction() as session:
        return inventory_rows(session)


def checked_body(storage, row):
    workspace, artifact, size, sha = row
    body = storage.read(workspace, artifact)
    if len(body) != size or content_hash(body) != sha:
        raise ValueError(f"Artifact integrity check failed for {artifact}")
    return body


def backup(database, root, destination, certificate):
    if not root.is_dir():
        raise ValueError("Artifact source directory must exist")
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("Backup destination must be outside Artifact storage")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.stat().st_mode & 0o077:
        raise ValueError("Backup destination must be private (0700)")
    with (destination / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with TemporaryDirectory() as work, TemporaryDirectory(dir=destination) as sealed:
            stage = Path(work)
            dump = stage / "database.dump"
            source = LocalArtifactStorage(root)
            manifest = stage / "manifest.json"
            bundle = stage / "bundle.tar"
            with database.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                with connection.begin():
                    # Artifact changes use the same workspace mutex as database changes.
                    connection.execute(
                        select(Workspace.id).order_by(Workspace.id).with_for_update()
                    ).all()
                    started = time.time_ns()
                    snapshot = connection.scalar(text("SELECT pg_export_snapshot()"))
                    postgres(
                        database,
                        "pg_dump",
                        "--snapshot",
                        snapshot,
                        "--format=custom",
                        "--file",
                        str(dump),
                    )
                    rows = inventory_rows(connection)
                    manifest.write_text(
                        json.dumps({"version": 1, "captured_at_ns": started, "artifacts": rows})
                    )
                    with tarfile.open(bundle, "w") as archive:
                        archive.add(manifest, arcname=manifest.name)
                        archive.add(dump, arcname=dump.name)
                        for row in rows:
                            body = checked_body(source, row)
                            workspace, artifact = row[:2]
                            entry = tarfile.TarInfo(f"artifacts/{workspace}/{artifact}")
                            entry.size = len(body)
                            archive.addfile(entry, BytesIO(body))
            dump_size = dump.stat().st_size
            encrypted = Path(sealed) / "bundle.cms"
            subprocess.run(
                [
                    "openssl",
                    "cms",
                    "-encrypt",
                    "-binary",
                    "-stream",
                    "-aes-256-gcm",
                    "-in",
                    str(bundle),
                    "-outform",
                    "DER",
                    "-out",
                    str(encrypted),
                    str(certificate),
                ],
                check=True,
            )
            target = destination / f"factory-{started:020d}-{uuid4().hex}.cms"
            encrypted.chmod(0o600)
            with encrypted.open("rb") as stream:
                os.fsync(stream.fileno())
            encrypted.rename(target)
        generations = sorted(p for p in destination.iterdir() if GENERATION.fullmatch(p.name))
        for expired in generations[:-7]:
            expired.unlink()
        directory = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return {
        "backup": str(target),
        "captured_at_ns": started,
        "artifacts": len(rows),
        "artifact_bytes": sum(row[2] for row in rows),
        "database_dump_bytes": dump_size,
    }


def member(archive, name, limit=None):
    entry = archive.next()
    if entry is None or entry.name != name or not entry.isfile():
        raise ValueError(f"Missing or unexpected backup member {name}")
    if limit is not None and entry.size > limit:
        raise ValueError(f"Backup member exceeds size limit: {name}")
    return archive.extractfile(entry)


def restore(database, root, bundle, certificate, key):
    started = time.time()
    if root.exists() or root.is_symlink():
        raise ValueError("Restore requires a new Artifact directory")
    if not root.parent.is_dir():
        raise ValueError("Restore Artifact parent directory must exist")
    inspector = inspect(database.engine)
    schemas = set(inspector.get_schema_names()) - {"information_schema"}
    if any(inspector.get_table_names(schema=schema) for schema in schemas):
        raise ValueError("Restore requires an empty database")
    with TemporaryDirectory(dir=root.parent) as work:
        stage = Path(work)
        plaintext = stage / "bundle.tar"
        subprocess.run(
            [
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(bundle),
                "-recip",
                str(certificate),
                "-inkey",
                str(key),
                "-out",
                str(plaintext),
            ],
            check=True,
        )
        staged_root = stage / "artifacts"
        storage = LocalArtifactStorage(staged_root)
        dump = stage / "database.dump"
        with tarfile.open(plaintext, "r|") as archive:
            manifest = json.load(member(archive, "manifest.json", 16 * 1024 * 1024))
            if manifest["version"] != 1:
                raise ValueError("Unsupported backup format")
            with dump.open("wb") as output:
                shutil.copyfileobj(member(archive, "database.dump"), output)
            rows = manifest["artifacts"]
            for workspace, artifact, size, sha in rows:
                name = f"artifacts/{identifier(workspace)}/{identifier(artifact)}"
                with member(archive, name, 16 * 1024 * 1024) as stream:
                    body = stream.read()
                if len(body) != size or content_hash(body) != sha:
                    raise ValueError(f"Artifact integrity check failed for {artifact}")
                storage.put(workspace, artifact, body)
            if archive.next() is not None:
                raise ValueError("Unexpected data after backup inventory")
        postgres(
            database,
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            str(dump),
        )
        configuration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with database.engine.begin() as connection:
            configuration.attributes["connection"] = connection
            migrations.upgrade(configuration, "head")
        if inventory(database) != rows:
            raise ValueError("Restored database and Artifact inventory disagree")
        with database.transaction() as session:
            now = db_now(session)
            for workspace in session.scalars(select(Workspace)):
                before = workspace.execution_stop_revision
                workspace.execution_stopped = True
                workspace.restore_reconciliation_required = True
                workspace.execution_stop_revision += 1
                session.add(
                    AuditEvent(
                        workspace_id=workspace.id,
                        actor_id="local-admin",
                        operation="workspace.execution.stop",
                        body={
                            "workspace_id": workspace.id,
                            "reason": "RESTORE_RECONCILIATION_REQUIRED",
                            "before_revision": before,
                            "after_revision": workspace.execution_stop_revision,
                        },
                        created_at=now,
                    )
                )
            for operation in session.scalars(
                select(PublicationOperation).where(PublicationOperation.state == "IN_FLIGHT")
            ):
                operation.state = "UNKNOWN"
                operation.reason = "PUBLISH_UNKNOWN"
                operation.next_reconcile_at = now
                job = session.get(Job, operation.job_id)
                job.state = "PUBLISH_UNKNOWN"
                run = session.get(Run, operation.run_id)
                if run.status in {"QUEUED", "RUNNING"} and run.phase == "PUBLISH_DRAFT":
                    run.status, run.phase = "WAITING_EXTERNAL", "PUBLISH_UNKNOWN"
                event(
                    session,
                    run,
                    "PUBLICATION_RESTORE_RECONCILIATION_REQUIRED",
                    {"operation_id": operation.id},
                )
            session.execute(
                update(Principal)
                .where(Principal.role.in_(["WORKER", "PUBLISHER"]))
                .values(active=False)
            )
            session.execute(
                update(Enrollment).where(Enrollment.consumed_at.is_(None)).values(consumed_at=now)
            )
            session.execute(
                update(Attempt)
                .where(Attempt.state == "LEASED")
                .values(expires_at=func.least(Attempt.expires_at, now))
            )
        staged_root.rename(root)
    return {
        "captured_at_ns": manifest["captured_at_ns"],
        "started_at": started,
        "staging_seconds": time.time() - started,
        "artifacts": len(rows),
        "status": "STAGED_RECONCILIATION_REQUIRED",
    }


def purge_run(session, run, now):
    """Remove one terminal Run in foreign-key order and retire its command receipts."""
    workspace_id, run_id = run.workspace_id, run.id
    tables = Base.metadata.tables

    def scoped(model):
        return (model.workspace_id == workspace_id, model.run_id == run_id)

    outbox_ids = list(
        session.scalars(select(OutboxEvent.id).where(*scoped(OutboxEvent)).with_for_update())
    )
    if session.scalar(
        select(SlackDelivery.outbox_id)
        .where(
            SlackDelivery.outbox_id.in_(outbox_ids),
            SlackDelivery.state.in_(["SENDING", "RETRY", "UNKNOWN"]),
        )
        .limit(1)
    ):
        return False

    job_ids = list(session.scalars(select(Job.id).where(*scoped(Job))))
    attempts = session.execute(
        select(Attempt.id, Attempt.lease_id).where(
            Attempt.workspace_id == workspace_id, Attempt.job_id.in_(job_ids)
        )
    ).all()
    operations = [
        f"cancel:{run_id}",
        f"resolve-blocked:{run_id}",
        f"runs:{run_id}:budget-request",
        f"runs:{run_id}:replan",
    ]
    for attempt_id, lease_id in attempts:
        operations.append(f"attempts:{attempt_id}:usage")
        operations.extend(
            f"{prefix}:{lease_id}" for prefix in ("complete", "fail", "artifact.reserve")
        )
    leases = {attempt.id: attempt.lease_id for attempt in attempts}
    for artifact_id, attempt_id in session.execute(
        select(Artifact.id, Artifact.attempt_id).where(*scoped(Artifact))
    ):
        operations.append(f"artifact.finalize:{artifact_id}")
        session.execute(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.workspace_id == workspace_id,
                IdempotencyRecord.operation == f"artifact.upload:{artifact_id}",
            )
            .values(parent_deleted_at=now, retired_lease_id=leases[attempt_id])
        )
    operations.extend(f"publication:{job_id}:begin" for job_id in job_ids)
    for operation_id in session.scalars(
        select(PublicationOperation.id).where(*scoped(PublicationOperation))
    ):
        operations.extend(
            f"publication:{operation_id}:{action}" for action in ("report", "reconcile")
        )
    operations.extend(
        f"approval:{approval_id}:decision"
        for approval_id in session.scalars(
            select(ApprovalRequest.id).where(*scoped(ApprovalRequest))
        )
    )
    session.execute(
        update(IdempotencyRecord)
        .where(
            IdempotencyRecord.workspace_id == workspace_id,
            IdempotencyRecord.operation.in_(operations),
        )
        .values(parent_deleted_at=now)
    )
    session.execute(
        delete(SlackDelivery).where(
            SlackDelivery.outbox_id.in_(select(OutboxEvent.id).where(*scoped(OutboxEvent)))
        )
    )
    session.execute(
        delete(WorkerEvent).where(
            WorkerEvent.workspace_id == workspace_id,
            WorkerEvent.attempt_id.in_([attempt.id for attempt in attempts]),
        )
    )
    # Drop the Run -> current Candidate edge before deleting the Candidate chain.
    run.current_candidate_id = None
    session.flush()
    for name in (
        "historical_ci_observations",
        "publication_observations",
        "publication_operations",
        "draft_pull_requests",
        "gates",
        "findings",
        "checks",
        "candidates",
        "artifacts",
        "usage_receipts",
        "cost_entries",
        "approval_decisions",
        "budget_versions",
        "approval_requests",
        "plan_versions",
        "job_dependencies",
        "outbox_events",
        "run_events",
        "verified_run_results",
    ):
        table = tables[name]
        session.execute(
            delete(table).where(table.c.workspace_id == workspace_id, table.c.run_id == run_id)
        )
    session.execute(
        delete(Attempt).where(Attempt.workspace_id == workspace_id, Attempt.job_id.in_(job_ids))
    )
    session.execute(delete(Job).where(*scoped(Job)))
    session.delete(run)
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_id="local-admin",
            operation="run.retention.purge",
            body={"run_id": run_id, "reason": "RUN_RETENTION_EXPIRED"},
            created_at=now,
        )
    )

    return True


def purge_tasks(session, now):
    has_runs = (
        select(Run.id).where(Run.workspace_id == Task.workspace_id, Run.task_id == Task.id).exists()
    )
    tasks = list(
        session.scalars(select(Task).where(Task.retention_touched_at <= now - 90 * DAY, ~has_runs))
    )
    for task in tasks:
        session.execute(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.workspace_id == task.workspace_id,
                IdempotencyRecord.operation.in_([f"specs:{task.id}", f"runs:{task.id}"]),
            )
            .values(parent_deleted_at=now)
        )
        session.execute(
            update(JiraIssueRevision)
            .where(
                JiraIssueRevision.workspace_id == task.workspace_id,
                JiraIssueRevision.task_id == task.id,
            )
            .values(state="RETIRED", task_id=None, spec_version=None, fingerprint=None)
        )
        session.execute(
            delete(SpecVersion).where(
                SpecVersion.workspace_id == task.workspace_id, SpecVersion.task_id == task.id
            )
        )
        session.delete(task)
        session.add(
            AuditEvent(
                workspace_id=task.workspace_id,
                actor_id="local-admin",
                operation="task.retention.purge",
                body={"task_id": task.id, "reason": "TASK_RETENTION_EXPIRED"},
                created_at=now,
            )
        )
    return len(tasks)


def collect(database, root):
    if not root.is_dir():
        raise ValueError("Artifact source directory must exist")
    storage = LocalArtifactStorage(root)
    with database.transaction() as session:
        # Match Control Plane mutation ordering even when an operator invokes collect online.
        list(session.scalars(select(Workspace.id).order_by(Workspace.id).with_for_update()))
        now = db_now(session)
        cutoff = now - 30 * DAY
        unresolved = (
            select(PublicationOperation.id)
            .where(
                PublicationOperation.workspace_id == Run.workspace_id,
                PublicationOperation.run_id == Run.id,
                PublicationOperation.state.in_(["PENDING", "IN_FLIGHT", "UNKNOWN", "RECONCILING"]),
            )
            .exists()
        )
        leased = (
            select(Job.id)
            .where(
                Job.workspace_id == Run.workspace_id, Job.run_id == Run.id, Job.state == "LEASED"
            )
            .exists()
        )
        eligible = (Run.status.in_(TERMINAL), Run.finished_at <= cutoff, ~unresolved, ~leased)
        expired = session.scalars(
            select(Artifact)
            .join(Run, (Run.workspace_id == Artifact.workspace_id) & (Run.id == Artifact.run_id))
            .where(
                (Artifact.status.in_(["RESERVED", "UPLOADED"]) & (Artifact.expires_at <= now))
                | ((Artifact.status == "AVAILABLE") & Run.id.in_(select(Run.id).where(*eligible)))
            )
        )
        marked = 0
        for artifact in expired:
            reason = (
                "EVIDENCE_RETENTION_EXPIRED"
                if artifact.status == "AVAILABLE"
                else "RESERVATION_EXPIRED"
            )
            artifact.status = "DELETED"
            session.add(
                AuditEvent(
                    workspace_id=artifact.workspace_id,
                    actor_id="local-admin",
                    operation="artifact.collect",
                    body={"artifact_id": artifact.id, "reason": reason},
                    created_at=now,
                )
            )
            marked += 1
        session.execute(text("SET LOCAL factory.retention = 'on'"))
        session.execute(
            delete(HistoricalCIObservation).where(
                HistoricalCIObservation.run_id.in_(select(Run.id).where(*eligible)),
                HistoricalCIObservation.received_at <= cutoff,
            )
        )
        removed_events = 0
        for run in session.scalars(select(Run).where(*eligible)):
            stream = (
                RunEvent.workspace_id == run.workspace_id,
                RunEvent.run_id == run.id,
            )
            first_retained = session.scalar(
                select(func.min(RunEvent.revision)).where(*stream, RunEvent.created_at > cutoff)
            )
            old = delete(RunEvent).where(*stream, RunEvent.created_at <= cutoff)
            if first_retained is not None:
                old = old.where(RunEvent.revision < first_retained)
            deleted = old.returning(RunEvent.revision).cte()
            count, last_deleted = session.execute(
                select(func.count(), func.max(deleted.c.revision)).select_from(deleted)
            ).one()
            if last_deleted is not None:
                run.events_pruned_through = max(run.events_pruned_through or 0, last_deleted)
                attempts = (
                    select(Attempt.id)
                    .join(
                        Job, (Job.workspace_id == Attempt.workspace_id) & (Job.id == Attempt.job_id)
                    )
                    .where(Job.workspace_id == run.workspace_id, Job.run_id == run.id)
                )
                session.execute(
                    delete(WorkerEvent).where(
                        WorkerEvent.workspace_id == run.workspace_id,
                        WorkerEvent.attempt_id.in_(attempts),
                        WorkerEvent.run_revision <= last_deleted,
                    )
                )
                removed_events += count
            session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.workspace_id == run.workspace_id,
                    OutboxEvent.run_id == run.id,
                    OutboxEvent.kind == "WORKER_EVENT",
                    OutboxEvent.created_at <= cutoff,
                )
            )
            if run.evidence_expired_at is None or count:
                if run.evidence_expired_at is None:
                    run.evidence_expired_at = now
                session.add(
                    AuditEvent(
                        workspace_id=run.workspace_id,
                        actor_id="local-admin",
                        operation="run.evidence.collect",
                        body={"run_id": run.id, "events_removed": count},
                        created_at=now,
                    )
                )
    # Commit the tombstone first. A failed unlink can be retried on the next invocation.
    with database.transaction() as session:
        for workspace, artifact in session.execute(
            select(Artifact.workspace_id, Artifact.id).where(Artifact.status == "DELETED")
        ):
            storage.delete(workspace, artifact)
    with database.transaction() as session:
        list(session.scalars(select(Workspace.id).order_by(Workspace.id).with_for_update()))
        now = db_now(session)
        session.execute(text("SET LOCAL factory.retention = 'on'"))
        uncollected = (
            select(Artifact.id)
            .where(
                Artifact.workspace_id == Run.workspace_id,
                Artifact.run_id == Run.id,
                Artifact.status != "DELETED",
            )
            .exists()
        )
        pending_approval = (
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.workspace_id == Run.workspace_id,
                ApprovalRequest.run_id == Run.id,
                ApprovalRequest.status == "PENDING",
            )
            .exists()
        )
        runs = list(
            session.scalars(
                select(Run).where(
                    *eligible,
                    Run.finished_at <= now - 90 * DAY,
                    ~uncollected,
                    ~pending_approval,
                )
            )
        )
        purged = sum(purge_run(session, run, now) for run in runs)
        purged_tasks = purge_tasks(session, now)
        session.execute(delete(AuditEvent).where(AuditEvent.created_at <= now - 90 * DAY))
        session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.parent_deleted_at <= now - 90 * DAY)
        )
    return {
        "marked_deleted": marked,
        "events_removed": removed_events,
        "runs_purged": purged,
        "tasks_purged": purged_tasks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    save = commands.add_parser("backup")
    save.add_argument("destination", type=Path)
    save.add_argument("--certificate", type=Path, required=True)
    load = commands.add_parser("restore")
    load.add_argument("bundle", type=Path)
    load.add_argument("--certificate", type=Path, required=True)
    load.add_argument("--key", type=Path, required=True)
    commands.add_parser("collect")
    args = parser.parse_args()
    database = Database()
    root = args.artifact_root.absolute()
    if args.operation == "backup":
        result = backup(database, root, args.destination.absolute(), args.certificate)
    elif args.operation == "restore":
        result = restore(database, root, args.bundle, args.certificate, args.key)
    else:
        result = collect(database, root)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
