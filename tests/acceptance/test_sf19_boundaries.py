import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from scripts.factory_maintenance import backup, restore
from software_factory import cli
from software_factory.api import create_app
from software_factory.db import Database
from software_factory.publisher import PublisherUnavailable
from tests.test_assembler import runner as runner
from tests.test_publications import publication as publication
from tests.test_publisher_backend import backend as backend
from tests.test_publisher_supervisor import supervisor

from .helpers import dump
from .test_sf10 import pipeline as pipeline
from .test_sf11 import source_bundle as source_bundle


def initial_unknown(pub, operation):
    response = pub.report(
        operation,
        {
            "delivery_id": "lost-initial-response",
            "target_hash": operation["target_hash"],
            "status": "UNKNOWN",
            "reason": "PUBLISH_UNKNOWN",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["operation"]


def claim(pub, operation, attempt, key):
    return pub.world.post(
        f"/v1/publisher/operations/{operation['operation_id']}/reconcile",
        {"target_hash": operation["target_hash"], "attempt": attempt},
        token=pub.secret,
        key=key,
    )


@pytest.mark.ac("SF-19-AC01", "SF-19-AC06")
def test_lost_create_response_recovers_existing_pr_after_restarts(
    backend, tmp_path, clock, evidence
):
    pub, provider, inputs, remote, _ = backend
    operation = inputs["operation"]
    state_dir = tmp_path / "sf19-publisher"
    agent, _ = supervisor(pub, state_dir, backend=provider)
    agent.journal.save({"phase": "AUTHORIZED", "operation": operation})
    remote["crash_after_create"] = True
    with pytest.raises(RuntimeError, match="after PR creation"):
        agent.run_once()

    agent, _ = supervisor(pub, state_dir, backend=provider)
    assert agent.run_once()["state"] == "UNKNOWN"
    assert not agent.run_once()["processed"]
    clock.advance(1)

    def crash(point):
        if point == "publisher.before_reconcile":
            raise RuntimeError("stopped after claiming reconciliation")

    agent.checkpoint = crash
    with pytest.raises(RuntimeError, match="after claiming"):
        agent.run_once()
    with TestClient(create_app(pub.world.database, reconcile_interval=3600)) as client:
        pub.world.client = client
        agent, _ = supervisor(pub, state_dir, backend=provider, lose="/reports")
        with pytest.raises(PublisherUnavailable, match="CONTROL_PLANE_UNAVAILABLE"):
            agent.run_once()
        first = pub.world.get(f"/v1/runs/{pub.world.run_id}/publication").json()
        agent, _ = supervisor(pub, state_dir, backend=provider)
        assert agent.run_once()["state"] == "SUCCEEDED"
        assert not agent.run_once()["processed"]
        assert pub.world.get(f"/v1/runs/{pub.world.run_id}/publication").json() == first
        assert pub.world.run()["phase"] == "WAIT_CI"

    assert first["operations"][0]["reconcile_attempts"] == 1
    assert first["pull_request"]["number"] == 123
    assert len(first["observations"]) == 2
    assert remote["pushes"] == 1 and remote["pulls"] == [123]
    assert sum(method == "POST" for method, _ in remote["http"]) == 1
    assert {item["method"] for item in provider.trace} == {"GET"}
    evidence(
        "one Draft is recovered across the restart and report replay",
        {"creates": 1, "recovery_creates": 0, "operation": first["operations"][0]["state"]},
    )


@pytest.mark.ac("SF-19-AC02")
@pytest.mark.parametrize(
    "failure",
    ["zero", "multiple", "marker", "run", "repository", "head", "malformed_head", "404", "offline"],
)
def test_unresolved_reads_wait_one_two_four_seconds(backend, tmp_path, clock, evidence, failure):
    pub, provider, inputs, remote, _ = backend
    operation = inputs["operation"]
    original = provider._client

    def observe(request):
        assert request.method == "GET"
        if failure == "offline":
            raise httpx.ReadError("injected loss", request=request)
        if request.url.path.endswith("/pulls"):
            if failure == "404":
                return httpx.Response(404)
            count = {"zero": 0, "multiple": 2}.get(failure, 1)
            return httpx.Response(200, json=[{"number": 123, "id": 2345}] * count)
        response = original.send(request)
        if request.url.path.endswith("/pulls/123"):
            pull = response.json()
            pull["title"] = "Software Factory: " + operation["target"]["run_id"]
            pull["body"] = (
                provider._run_marker(operation["target"])
                + "\n"
                + provider._operation_marker(operation)
            )
            if failure == "marker":
                pull["body"] = provider._run_marker(operation["target"])
            elif failure == "run":
                pull["body"] = provider._operation_marker(operation)
            elif failure == "repository":
                pull["head"]["repo"]["full_name"] = "untrusted/fork"
            elif failure == "head":
                pull["head"]["sha"] = "0" * 40
            elif failure == "malformed_head":
                pull["head"]["sha"] = "not a SHA"
            return httpx.Response(200, json=pull)
        return response

    provider._client = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(observe)
    )
    initial_unknown(pub, operation)
    delays = []
    for attempt, delay in enumerate((1, 2, 4), 1):
        agent, _ = supervisor(pub, tmp_path / "retries", backend=provider)
        assert not agent.run_once()["processed"]
        clock.advance(delay - 0.25)
        assert claim(pub, operation, attempt, f"early-{attempt}").status_code == 409
        assert not agent.run_once()["processed"]
        clock.advance(0.25)
        assert agent.run_once()["state"] == ("UNKNOWN" if attempt < 3 else "BLOCKED")
        delays.append(delay)

    status = pub.world.get(f"/v1/runs/{pub.world.run_id}/publication").json()
    assert status["pull_request"] is None
    assert status["operations"][0]["reconcile_attempts"] == 3
    assert status["operations"][0]["next_reconcile_at"] is None
    assert status["operations"][0]["required_action"]
    assert pub.world.run()["status"] == "BLOCKED"
    assert pub.world.run()["stop_reason"] == "PUBLISH_UNKNOWN"
    assert len(status["observations"]) == 4
    expected_reason = {
        "zero": "PUBLISH_UNKNOWN",
        "multiple": "PUBLISH_UNKNOWN",
        "404": "PROVIDER_UNAVAILABLE",
        "offline": "PROVIDER_UNAVAILABLE",
    }.get(failure, "PR_CONFLICT")
    assert [obs["body"]["reason"] for obs in status["observations"][1:]] == [expected_reason] * 3
    assert not remote["pulls"] and not remote["pushes"]
    assert all(
        item["method"] == "GET"
        for obs in status["observations"]
        for item in obs["body"]["provider_requests"]
    )
    evidence("three due read-only attempts", {"delays": delays, "creates": 0, "failure": failure})


@pytest.mark.ac("SF-19-AC04")
@pytest.mark.parametrize("trial", range(10))
def test_cancel_and_recovered_pr_report_keep_external_history(backend, clock, evidence, trial):
    pub, provider, inputs, _, _ = backend
    world = pub.world
    operation = inputs["operation"]
    provider.publish(inputs)
    initial_unknown(pub, operation)
    clock.advance(1)
    assert claim(pub, operation, 1, "first").json()["state"] == "RECONCILING"
    pull = provider.reconcile(operation)
    report = pub.success(operation) | {"reconcile_attempt": 1, "pull_request": pull}
    revision = world.run()["state_revision"]
    barrier = Barrier(2)

    def race(index):
        barrier.wait(5)
        if index == 0:
            return pub.report(operation, report, key="recovered")
        return world.post(
            f"/v1/runs/{world.run_id}/cancel",
            {"expected_run_revision": revision, "reason": "cancel during reconciliation"},
        )

    with ThreadPoolExecutor(2) as pool:
        recovered, cancel = pool.map(race, [0, 1])
    assert recovered.status_code == 200
    if cancel.status_code == 409:
        assert world.run()["phase"] == "WAIT_CI"
        cancel = world.post(
            f"/v1/runs/{world.run_id}/cancel",
            {"expected_run_revision": world.run()["state_revision"], "reason": "cancel after read"},
        )
    assert cancel.status_code == 202
    assert world.run()["status"] == "CANCELLED"
    snapshot = dump(world.database)
    assert pub.report(operation, report, key="recovered").status_code == 200
    assert dump(world.database) == snapshot
    assert len(snapshot["draft_pull_requests"]) == 1
    assert len(snapshot["publication_observations"]) == 2
    evidence(
        "external publication recorded once, cancelled Run remains cancelled",
        {"trial": trial, "run_status": world.run()["status"], "drafts": 1},
    )


@pytest.mark.ac("SF-19-AC02", "SF-19-AC06")
def test_two_reports_for_one_attempt_cannot_spend_two_retries(publication, clock, evidence):
    pub = publication
    operation = pub.begin().json()
    initial_unknown(pub, operation)
    clock.advance(1)
    barrier = Barrier(2)

    def race(index):
        assert claim(pub, operation, 1, f"claim-{index}").status_code == 200
        barrier.wait(5)
        return pub.report(
            operation,
            {
                "delivery_id": f"retry-{index}",
                "target_hash": operation["target_hash"],
                "status": "UNKNOWN",
                "reconcile_attempt": 1,
                "reason": "PUBLISH_UNKNOWN",
            },
            key=f"retry-{index}",
        )

    with ThreadPoolExecutor(2) as pool:
        assert sorted(r.status_code for r in pool.map(race, [0, 1])) == [200, 409]
    status = pub.world.get(f"/v1/runs/{pub.world.run_id}/publication").json()
    assert len(status["observations"]) == 2
    assert status["operations"][0]["reconcile_attempts"] == 1
    assert status["operations"][0]["next_reconcile_at"] == clock.value + 2
    evidence(
        "one persisted retry for two competing reports and unchanged next-attempt delay",
        {"attempts": 1, "observations": 2, "next_delay_seconds": 2},
    )


def test_restored_publisher_reconciles_existing_pr_without_writing(
    backend, tmp_path, monkeypatch, capsys
):
    publication, provider, inputs, remote, _ = backend
    world = publication.world
    pull = provider.publish(inputs)
    assert remote["pushes"] == 1 and len(remote["pulls"]) == 1
    certificate, key = tmp_path / "recovery.crt", tmp_path / "recovery.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=restore publication",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    artifact_root = world.client.app.state.service.artifacts.storage.root
    bundle = backup(world.database, artifact_root, tmp_path / "backup", certificate)
    name = f"factory_restore_{uuid4().hex}"
    with world.database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    target = Database(
        world.database.engine.url.set(database=name).render_as_string(hide_password=False)
    )
    try:
        report = restore(target, tmp_path / "restored", bundle["backup"], certificate, key)
        assert report["status"] == "STAGED_RECONCILIATION_REQUIRED"
        with TestClient(create_app(target, reconcile_interval=3600)) as client:
            restored = replace(world, database=target, client=client)
            assert (
                restored.get("/v1/publisher/reconciliations", token=publication.secret).status_code
                == 401
            )
            path = f"/v1/workspaces/{world.workspace_id}/execution-control"
            release = {"stopped": False, "expected_revision": 1, "reason": "restore reconciliation"}
            denied = restored.post(path, release)
            assert (
                denied.status_code == 409
                and denied.json()["code"] == "RESTORE_RECONCILIATION_REQUIRED"
            )
            monkeypatch.setattr(cli, "Database", lambda: target)
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "factory",
                    "reissue-publisher",
                    "--publisher-id",
                    publication.identity,
                    "--reason",
                    "recovery exercise",
                ],
            )
            cli.main()
            issued = json.loads(capsys.readouterr().out)
            assert issued["publisher_id"] == publication.identity
            recovered = SimpleNamespace(world=restored, secret=issued["publisher_token"])
            agent, _ = supervisor(recovered, tmp_path / "fresh-publisher-journal", backend=provider)
            assert agent.run_once()["state"] == "SUCCEEDED"
            assert all(request["method"] == "GET" for request in provider.trace)
            assert remote["pushes"] == 1 and len(remote["pulls"]) == 1
            current = restored.get(f"/v1/runs/{world.run_id}/publication").json()
            assert current["pull_request"]["number"] == pull["number"]
            assert restored.run()["phase"] == "WAIT_CI"
            assert restored.post(path, release).status_code == 200
    finally:
        target.engine.dispose()
        with world.database.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            conn.execute(text(f"DROP DATABASE {name}"))
