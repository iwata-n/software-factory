import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url

from scripts.factory_maintenance import DAY, backup, collect, inventory, restore
from software_factory import cli
from software_factory.api import create_app
from software_factory.db import Database, db_now
from software_factory.models import (
    Artifact,
    Attempt,
    AuditEvent,
    IdempotencyRecord,
    Job,
    OutboxEvent,
    Run,
    RunEvent,
    WorkerEvent,
    Workspace,
)
from software_factory.service import event
from software_factory.storage import LocalArtifactStorage
from tests.acceptance.test_sf09 import artifacts as artifacts
from tests.conftest import example_spec

pytestmark = pytest.mark.postgres


@pytest.mark.ac("SF-24-AC06")
def test_artifact_collection_at_boundaries(world, artifacts, evidence):
    root = world.client.app.state.service.artifacts.storage.root
    artifact_id = artifacts.ready()
    pending = artifacts.reserve().json()["id"]
    with world.database.transaction() as session:
        reserved = session.get(Artifact, pending)
        now = reserved.expires_at
        assert now - reserved.created_at == 3600
        session.execute(update(RunEvent).values(created_at=now - 30 * DAY))
        last_revision = session.get(Run, world.run_id).state_revision
    database = Database(
        world.database.engine.url.render_as_string(hide_password=False), clock=lambda: now - 0.001
    )
    try:
        assert collect(database, root)["marked_deleted"] == 0
        with database.transaction() as session:
            assert session.get(Artifact, pending).status == "RESERVED"
        database.sessions.configure(info={"clock": lambda: now})
        # A live run protects finalized evidence even when its events are old.
        assert collect(database, root) == {
            "marked_deleted": 1,
            "events_removed": 0,
            "runs_purged": 0,
            "tasks_purged": 0,
        }
        assert world.get(f"/v1/artifacts/{artifact_id}/content").content == b"hello"
        with database.transaction() as session:
            session.execute(update(Job).values(state="SUCCEEDED"))
            session.execute(update(Attempt).values(state="SUCCEEDED"))
            session.execute(update(Run).values(status="SUCCEEDED", finished_at=now - 30 * DAY))
        database.sessions.configure(info={"clock": lambda: now - 0.001})
        assert collect(database, root) == {
            "marked_deleted": 0,
            "events_removed": 0,
            "runs_purged": 0,
            "tasks_purged": 0,
        }
        database.sessions.configure(info={"clock": lambda: now})
        collected = collect(database, root)
        assert collected["marked_deleted"] == 1 and collected["events_removed"] > 0
        assert collect(database, root) == {
            "marked_deleted": 0,
            "events_removed": 0,
            "runs_purged": 0,
            "tasks_purged": 0,
        }
        assert world.run()["evidence_expired_at"] == now
        assert world.run()["finished_at"] == now - 30 * DAY
        cursor = f"{world.run_id}:{last_revision}"
        response = world.client.get(
            f"/v1/runs/{world.run_id}/events?wait_seconds=0",
            headers={"Authorization": f"Bearer {world.user_token}", "Last-Event-ID": cursor},
        )
        assert response.status_code == 410
        assert world.get(f"/v1/runs/{world.run_id}/event-history").json() == {"events": []}
        reply = world.get(f"/v1/artifacts/{artifact_id}/content")
        assert reply.status_code == 404
        assert "EVIDENCE_RETENTION_EXPIRED" in reply.text
        assert not (root / world.workspace_id / artifact_id).exists()
        with database.transaction() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.operation == "artifact.collect")
                )
                == 2
            )
        evidence(
            "Reservation and finalized evidence expire at their respective boundaries",
            {
                "collection": collected,
                "sse_status": response.status_code,
                "artifact_status": reply.status_code,
            },
        )
    finally:
        database.engine.dispose()


@pytest.mark.ac("SF-24-AC06")
def test_retention_prunes_only_old_history_prefix_and_keeps_terminal_anchor(
    world, artifacts, evidence
):
    root = world.client.app.state.service.artifacts.storage.root
    lease = artifacts.lease
    run_path = f"/v1/runs/{world.run_id}"
    recorded = world.post(
        f"/v1/worker/leases/{lease['id']}/events",
        {
            "fence": lease["fence"],
            "events": [{"sequence": 1, "kind": "LOG", "body": {"line": "test"}}],
        },
        token=world.worker_token,
    )
    assert recorded.status_code == 200
    assert (
        world.post(
            run_path + "/cancel",
            {"expected_run_revision": world.run()["state_revision"], "reason": "retention fixture"},
        ).status_code
        == 202
    )
    assert (
        world.post(
            f"/v1/worker/leases/{lease['id']}/complete",
            {"fence": lease["fence"], "outcome": "CANCELLED"},
            token=world.worker_token,
        ).status_code
        == 200
    )
    finished = world.run()["finished_at"]
    assert finished is not None
    database = Database(
        world.database.engine.url.render_as_string(hide_password=False),
        clock=lambda: finished + DAY,
    )
    try:
        with database.transaction() as session:
            run = session.get(Run, world.run_id)
            old_revision = run.state_revision
            event(session, run, "LATE_AUDIT_FIXTURE", {})
            late_revision = run.state_revision
        database.sessions.configure(info={"clock": lambda: finished + 30 * DAY})
        collected = collect(database, root)
        assert collected["events_removed"] > 0
        with database.transaction() as session:
            assert session.scalar(select(WorkerEvent.id)) is None
            assert (
                session.scalar(select(OutboxEvent.id).where(OutboxEvent.kind == "WORKER_EVENT"))
                is None
            )
        assert world.run()["finished_at"] == finished
        history = world.get(run_path + "/event-history").json()["events"]
        assert [item["revision"] for item in history] == [late_revision]
        headers = {"Authorization": f"Bearer {world.user_token}"}
        endpoint = run_path + "/events?wait_seconds=0"
        assert (
            world.client.get(
                endpoint, headers=headers | {"Last-Event-ID": f"{world.run_id}:{old_revision}"}
            ).status_code
            == 410
        )
        assert (
            world.client.get(
                endpoint, headers=headers | {"Last-Event-ID": f"{world.run_id}:{late_revision}"}
            ).status_code
            == 200
        )
        assert collect(database, root) == {
            "marked_deleted": 0,
            "events_removed": 0,
            "runs_purged": 0,
            "tasks_purged": 0,
        }
        evidence(
            "Collection preserves the newer event and terminal history anchor",
            {"run_id": world.run_id, "old_revision": old_revision, "retained_history": True},
        )
    finally:
        database.engine.dispose()


@pytest.mark.ac("SF-24-AC05", "SF-24-AC06")
def test_encrypted_backup_restore(world, artifacts, tmp_path, monkeypatch, capsys, evidence):
    root = world.client.app.state.service.artifacts.storage.root
    artifact_id = artifacts.ready()
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
    generations = []
    for _ in range(8):
        result = backup(world.database, root, destination, certificate)
        generations.append(Path(result["backup"]))
    assert not generations[0].exists()
    assert set(destination.glob("*.cms")) == set(generations[1:])
    evidence(
        "Eight encrypted generations retain precisely the seven newest",
        {"created": len(generations), "retained": len(list(destination.glob("*.cms")))},
    )
    bundle = Path(result["backup"])
    assert bundle.stat().st_mode & 0o077 == 0
    assert result["artifact_bytes"] == 5
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
        assert LocalArtifactStorage(target_root).read(world.workspace_id, artifact_id) == b"hello"

        from fastapi.testclient import TestClient

        app = create_app(target)
        with target.transaction() as session:
            attempt = session.scalar(select(Attempt))
            assert attempt.expires_at <= db_now(session)
            assert session.get(Run, world.run_id).reserved_microusd == 100_000
            assert session.get(Workspace, world.workspace_id).execution_stopped is True
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
            monkeypatch.setattr(cli, "Database", lambda: target)
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "factory",
                    "reissue-worker",
                    "--worker-id",
                    world.worker_id,
                    "--reason",
                    "restore",
                ],
            )
            cli.main()
            issued = json.loads(capsys.readouterr().out)
            assert issued["worker_id"] == world.worker_id
            headers = {"Authorization": "Bearer " + issued["worker_token"]}
            lease = artifacts.lease
            stale = client.post(
                f"/v1/worker/leases/{lease['id']}/heartbeat",
                json={"fence": lease["fence"]},
                headers=headers,
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "LEASE_EXPIRED"
            assert (
                client.post(
                    "/v1/worker/claims", json={"protocol_version": 1}, headers=headers
                ).status_code
                == 204
            )
        finally:
            client.close()
        with pytest.raises(ValueError, match="empty database"):
            restore(target, tmp_path / "another-target", bundle, certificate, key)
    finally:
        target.engine.dispose()
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f"DROP DATABASE {name}"))
        admin.engine.dispose()


@pytest.mark.ac("SF-24-AC06")
def test_run_purge_preserves_command_receipt_for_ninety_more_days(world, artifacts, evidence):
    with world.database.transaction() as session:
        receipt = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.operation == f"runs:{world.task_id}",
                IdempotencyRecord.workspace_id == world.workspace_id,
            )
        )
        start_key, start_response = receipt.key, dict(receipt.response)
    root = world.client.app.state.service.artifacts.storage.root
    artifact = artifacts.reserve().json()["id"]
    uploaded = artifacts.upload(artifact, key="retention-upload")
    assert uploaded.status_code == 200
    assert artifacts.finalize(artifact).status_code == 200
    task_path = f"/v1/tasks/{world.task_id}"
    spec = example_spec(world.repository_id)
    updated = world.post(task_path + "/spec-versions", spec, key="retention-spec")
    assert updated.status_code == 201
    run_path = f"/v1/runs/{world.run_id}"
    payload = {"expected_run_revision": world.run()["state_revision"], "reason": "retention"}
    cancelled = world.post(run_path + "/cancel", payload, key="retention-cancel")
    assert cancelled.status_code == 202
    lease = artifacts.lease
    assert (
        world.post(
            f"/v1/worker/leases/{lease['id']}/complete",
            {"fence": lease["fence"], "outcome": "CANCELLED"},
            token=world.worker_token,
        ).status_code
        == 200
    )
    finished = world.run()["finished_at"]
    purge_at = finished + 90 * DAY
    database = Database(
        world.database.engine.url.render_as_string(hide_password=False),
        clock=lambda: purge_at - 1,
    )
    try:
        assert collect(database, root)["runs_purged"] == 0
        assert world.get(run_path).status_code == 200
        database.sessions.configure(info={"clock": lambda: purge_at})
        collected = collect(database, root)
        assert collected["runs_purged"] == collected["tasks_purged"] == 1
        assert world.get(run_path).status_code == world.get(task_path).status_code == 404
        assert (
            world.post(task_path + "/runs", {"spec_version": 1}, key=start_key).json()
            == start_response
        )
        assert (
            world.post(
                task_path + "/runs", {"spec_version": 1}, key="new-start-after-purge"
            ).status_code
            == 404
        )
        with world.database.transaction() as session:
            assert session.scalar(select(Run.id).where(Run.task_id == world.task_id)) is None
        assert (
            world.post(task_path + "/spec-versions", spec, key="retention-spec").json()
            == updated.json()
        )
        assert (
            world.post(task_path + "/spec-versions", spec, key="new-after-purge").status_code == 404
        )
        changed = spec | {"task": spec["task"] | {"title": "Changed after purge"}}
        assert (
            world.post(task_path + "/spec-versions", changed, key="retention-spec").status_code
            == 409
        )
        assert artifacts.upload(artifact, key="retention-upload").json() == uploaded.json()
        assert (
            artifacts.upload(artifact, body=b"changed", key="retention-upload").status_code == 409
        )
        assert world.get(f"/v1/artifacts/{artifact}/content").status_code == 404
        assert not (root / world.workspace_id / artifact).exists()
        assert (
            world.post(run_path + "/cancel", payload, key="retention-cancel").json()
            == cancelled.json()
        )
        assert (
            world.post(run_path + "/cancel", payload, key="new-cancel-after-purge").status_code
            == 404
        )
        assert (
            world.post(
                run_path + "/cancel", payload, key="retention-cancel", token=world.other_user_token
            ).status_code
            == 404
        )
        database.sessions.configure(info={"clock": lambda: purge_at + 90 * DAY - 1})
        assert collect(database, root)["runs_purged"] == 0
        assert world.post(run_path + "/cancel", payload, key="retention-cancel").status_code == 202
        database.sessions.configure(info={"clock": lambda: purge_at + 90 * DAY})
        assert collect(database, root)["runs_purged"] == 0
        assert world.post(run_path + "/cancel", payload, key="retention-cancel").status_code == 404
        assert (
            world.post(task_path + "/spec-versions", spec, key="retention-spec").status_code == 404
        )
        assert artifacts.upload(artifact, key="retention-upload").status_code == 404
        evidence(
            "Run and Task are purged at 90 days and command receipts expire separately",
            {"collection": collected, "expired_upload_status": 404},
        )
    finally:
        database.engine.dispose()
