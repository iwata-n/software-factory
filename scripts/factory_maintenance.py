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

from psycopg.conninfo import make_conninfo
from sqlalchemy import func, inspect, select, update

from software_factory.db import Database, db_now
from software_factory.domain import TERMINAL
from software_factory.models import (
    Artifact,
    Attempt,
    AuditEvent,
    Enrollment,
    Principal,
    PublicationOperation,
    Run,
    RunEvent,
)
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


def inventory(database):
    with database.transaction() as session:
        return [
            [identifier(workspace), identifier(artifact), size, sha]
            for workspace, artifact, size, sha in session.execute(
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
            started = time.time_ns()
            dump = stage / "database.dump"
            postgres(database, "pg_dump", "--format=custom", "--file", str(dump))
            rows = inventory(database)
            source = LocalArtifactStorage(root)
            manifest = stage / "manifest.json"
            manifest.write_text(
                json.dumps({"version": 1, "captured_at_ns": started, "artifacts": rows})
            )
            bundle = stage / "bundle.tar"
            with tarfile.open(bundle, "w") as archive:
                archive.add(manifest, arcname=manifest.name)
                archive.add(dump, arcname=dump.name)
                for row in rows:
                    body = checked_body(source, row)
                    workspace, artifact = row[:2]
                    entry = tarfile.TarInfo(f"artifacts/{workspace}/{artifact}")
                    entry.size = len(body)
                    archive.addfile(entry, BytesIO(body))
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
            with encrypted.open("rb") as stream:
                os.fsync(stream.fileno())
            encrypted.rename(target)
        generations = sorted(p for p in destination.iterdir() if GENERATION.fullmatch(p.name))
        for expired in generations[:-7]:
            expired.unlink()
    return {"backup": str(target), "captured_at_ns": started, "artifacts": len(rows)}


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
        if inventory(database) != rows:
            raise ValueError("Restored database and Artifact inventory disagree")
        with database.transaction() as session:
            now = db_now(session)
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


def collect(database, root):
    if not root.is_dir():
        raise ValueError("Artifact source directory must exist")
    storage = LocalArtifactStorage(root)
    with database.transaction() as session:
        now = db_now(session)
        last_event = (
            select(func.max(RunEvent.created_at))
            .where(RunEvent.workspace_id == Run.workspace_id, RunEvent.run_id == Run.id)
            .correlate(Run)
            .scalar_subquery()
        )
        unresolved = (
            select(PublicationOperation.id)
            .where(
                PublicationOperation.workspace_id == Run.workspace_id,
                PublicationOperation.run_id == Run.id,
                PublicationOperation.state.in_(["PENDING", "IN_FLIGHT", "UNKNOWN"]),
            )
            .exists()
        )
        expired = session.scalars(
            select(Artifact)
            .join(Run, (Run.workspace_id == Artifact.workspace_id) & (Run.id == Artifact.run_id))
            .where(
                (Artifact.status.in_(["RESERVED", "UPLOADED"]) & (Artifact.expires_at <= now))
                | (
                    (Artifact.status == "AVAILABLE")
                    & Run.status.in_(TERMINAL)
                    & (last_event <= now - 30 * DAY)
                    & ~unresolved
                )
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
    # Commit the tombstone first. A failed unlink can be retried on the next invocation.
    with database.transaction() as session:
        for workspace, artifact in session.execute(
            select(Artifact.workspace_id, Artifact.id).where(Artifact.status == "DELETED")
        ):
            storage.delete(workspace, artifact)
    return {"marked_deleted": marked}


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
