import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url

from scripts.factory_maintenance import DAY, backup, collect, inventory, restore
from software_factory.api import create_app
from software_factory.db import Database, db_now
from software_factory.models import Artifact, Attempt, AuditEvent, Run, RunEvent
from software_factory.storage import LocalArtifactStorage
from tests.acceptance.test_sf09 import artifacts as artifacts

pytestmark = pytest.mark.postgres


def test_artifact_collection_at_boundaries(world, artifacts):
    root = world.client.app.state.service.artifacts.storage.root
    evidence = artifacts.ready()
    pending = artifacts.reserve().json()["id"]
    with world.database.transaction() as session:
        now = db_now(session)
        session.execute(update(Artifact).where(Artifact.id == pending).values(expires_at=now))
        session.execute(update(RunEvent).values(created_at=now - 30 * DAY))
    database = Database(
        world.database.engine.url.render_as_string(hide_password=False), clock=lambda: now
    )
    try:
        # A live run protects finalized evidence even when its events are old.
        assert collect(database, root) == {"marked_deleted": 1}
        assert world.get(f"/v1/artifacts/{evidence}/content").content == b"hello"
        with database.transaction() as session:
            session.execute(update(Run).values(status="SUCCEEDED"))
        database.sessions.configure(info={"clock": lambda: now - 0.001})
        assert collect(database, root) == {"marked_deleted": 0}
        database.sessions.configure(info={"clock": lambda: now})
        assert collect(database, root) == {"marked_deleted": 1}
        assert collect(database, root) == {"marked_deleted": 0}
        reply = world.get(f"/v1/artifacts/{evidence}/content")
        assert reply.status_code == 404
        assert "EVIDENCE_RETENTION_EXPIRED" in reply.text
        assert not (root / world.workspace_id / evidence).exists()
        with database.transaction() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.operation == "artifact.collect")
                )
                == 2
            )
    finally:
        database.engine.dispose()


def test_encrypted_backup_restore(world, artifacts, tmp_path):
    root = world.client.app.state.service.artifacts.storage.root
    evidence = artifacts.ready()
    certificate, key = tmp_path / "backup.crt", tmp_path / "backup.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=SF24 test",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        capture_output=True,
        check=True,
    )
    destination = tmp_path / "generations"
    for _ in range(8):
        result = backup(world.database, root, destination, certificate)
    assert len(list(destination.glob("*.cms"))) == 7
    bundle = Path(result["backup"])
    source_url = make_url(os.environ["TEST_DATABASE_URL"])
    admin = Database(source_url.render_as_string(hide_password=False))
    name = f"sf24_restore_{uuid4().hex}"
    with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    target_url = world.database.engine.url.set(database=name)
    target = Database(target_url.render_as_string(hide_password=False))
    target_root = tmp_path / "restored"
    try:
        wrong_cert, wrong_key = tmp_path / "wrong.crt", tmp_path / "wrong.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=wrong",
                "-days",
                "1",
                "-keyout",
                str(wrong_key),
                "-out",
                str(wrong_cert),
            ],
            capture_output=True,
            check=True,
        )
        with pytest.raises(subprocess.CalledProcessError):
            restore(target, target_root, bundle, wrong_cert, wrong_key)
        assert not target_root.exists()

        report = restore(target, target_root, bundle, certificate, key)
        assert report["status"] == "STAGED_RECONCILIATION_REQUIRED"
        assert inventory(target) == inventory(world.database)
        assert LocalArtifactStorage(target_root).read(world.workspace_id, evidence) == b"hello"

        from fastapi.testclient import TestClient

        app = create_app(target)
        with target.transaction() as session:
            attempt = session.scalar(select(Attempt))
            assert attempt.expires_at <= db_now(session)
            assert session.get(Run, world.run_id).reserved_microusd == 100_000
        client = TestClient(app)
        try:
            reply = client.post(
                "/v1/worker/claims",
                json={"protocol_version": 1},
                headers={
                    "Authorization": f"Bearer {world.worker_token}",
                    "Idempotency-Key": "restored-worker-claim",
                },
            )
            assert reply.status_code == 401
        finally:
            client.close()
        with pytest.raises(ValueError, match="empty database"):
            restore(target, tmp_path / "another-target", bundle, certificate, key)
    finally:
        target.engine.dispose()
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f"DROP DATABASE {name}"))
        admin.engine.dispose()
