"""Real GitHub transport evaluation using fixture agents, not the real-agent MVP batch.

Run through scripts/with_postgres.py after committing/pushing the evaluated branch.
Only codex/factory/<run UUID> and one Draft PR are written; no merge or deployment.
"""

import argparse
import base64
import difflib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from acceptance_support import source_manifest
from alembic import command
from alembic.config import Config
from factory_maintenance import backup, restore
from live_github_webhook import github_webhook_ingress
from sqlalchemy import MetaData, select, text
from sqlalchemy.engine import make_url

from software_factory.adapters import AgentResult, Capabilities, FakeAgent, ProducedArtifact
from software_factory.contracts import digest
from software_factory.db import Database
from software_factory.models import CIWebhookDelivery, PublicationOperation, Run, RunEvent
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.publisher import PublicationJournal, PublisherSupervisor
from software_factory.publisher_backend import GitHubDraftPublisher
from software_factory.sandbox import DockerRunner
from software_factory.service import Service
from software_factory.source_runner import SourcePolicy
from software_factory.worker import Supervisor

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/iwata-n/software-factory.git"
REPOSITORY = "iwata-n/software-factory"
TARGET = "examples/evaluation/app.py"
EVALUATION_WORKFLOW = ".github/workflows/factory-evaluation.yml"


def process_json(argv, *, environment=None):
    return json.loads(subprocess.check_output(argv, env=environment, text=True))


def github(path):
    return process_json(["gh", "api", ("repos/" + REPOSITORY + "/" + path).rstrip("/")])


def port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def wait_ready(url, process):
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("evaluation service stopped before becoming ready")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError("evaluation service startup timeout")


def post(client, path, body):
    response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    response.raise_for_status()
    return response.json()


def save_database(output):
    records = {}
    database = Database()
    with database.engine.connect() as connection:
        metadata = MetaData()
        metadata.reflect(bind=connection)
        for name, table in metadata.tables.items():
            records[name] = [dict(row) for row in connection.execute(select(table)).mappings()]
    (output / "database.json").write_text(json.dumps(records, indent=2, default=str))
    database.engine.dispose()
    return records


class FixtureAgent(FakeAgent):
    capabilities = Capabilities(kinds=("PLAN", "IMPLEMENT", "REPAIR"))

    def __init__(self, before, after):
        super().__init__()
        self.before, self.after = before, after

    def start(self, job):
        if job["kind"] == "PLAN":
            self._result = AgentResult(
                "EXECUTED",
                {
                    "units": [
                        {
                            "id": "unit-1",
                            "objective": job["snapshot"]["task_spec"]["task"]["objective"],
                            "acceptance_ids": ["AC-1", "AC-2"],
                            "depends_on": [],
                            "proposed_write_paths": [TARGET],
                        }
                    ],
                    "assumptions": [],
                    "open_questions": [],
                },
            )
        else:
            patch = "".join(
                difflib.unified_diff(
                    self.before.splitlines(True),
                    self.after.splitlines(True),
                    fromfile="a/" + TARGET,
                    tofile="b/" + TARGET,
                )
            ).encode()
            self._result = AgentResult(
                "EXECUTED",
                {"summary": "publication transport fixture", "simulation": True},
                artifacts=(ProducedArtifact("patch", "PATCH", patch),),
            )


class FixtureReviewer:
    def review(self, context, cancelled):
        if cancelled.is_set():
            raise RuntimeError("fixture review cancelled")
        return {
            "criteria": [
                {
                    "criterion_id": key,
                    "status": "PASS",
                    "explanation": "Transport fixture review with independent golden verification",
                    "evidence_artifact_ids": [
                        context["source_artifact_id"],
                        "$artifact/review-log",
                    ],
                }
                for key in ("AC-1", "AC-2")
            ]
        }


def verify_live_ci(
    user,
    run_id,
    publication,
    state,
    output,
    identity,
    restart_control_plane,
    *,
    expect_failure=False,
):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        run = state()
        if expect_failure and run["phase"] == "REPAIR" and run["status"] == "QUEUED":
            return verify_live_ci_failure(
                user, run, publication, state, output, identity, restart_control_plane
            )
        if run["status"] == "SUCCEEDED":
            assert not expect_failure, "Failed required CI must not finalize a Run"
            break
        if run["status"] != "WAITING_EXTERNAL" or run["phase"] not in {
            "WAIT_CI",
            "FINAL_GATE_READY",
        }:
            raise RuntimeError(f"Unexpected evaluation state: {run['status']} / {run['phase']}")
        time.sleep(2)
    else:
        raise RuntimeError("Live GitHub CI did not finish within 900 seconds")

    def receipt():
        result = user.get(f"/v1/runs/{run_id}/result")
        result.raise_for_status()
        notifications = user.get(f"/v1/runs/{run_id}/notifications")
        notifications.raise_for_status()
        return result.json(), notifications.json()

    result, notifications = receipt()
    draft = publication["pull_request"]
    (check,) = result["checks"]
    (notification,) = notifications["notifications"]
    assert result["kind"] == "VERIFIED_DRAFT_PR"
    assert result["draft_pr"] == draft
    assert check["status"] == "PASS" and check["tested_sha"] == draft["head_sha"]
    assert notification["run_status"] == "SUCCEEDED" and notification["state"] == "PENDING"
    restart_control_plane()
    assert state()["status"] == "SUCCEEDED" and receipt() == (result, notifications)
    observed_pr = user.get(f"/v1/runs/{run_id}/result/current-pr")
    observed_pr.raise_for_status()
    assert observed_pr.headers["Cache-Control"] == "no-store"
    current_pr = observed_pr.json()
    assert current_pr == {
        "status": "OBSERVED",
        "state": "OPEN",
        "draft": True,
        "head_sha": draft["head_sha"],
        "base_sha": draft["base_sha"],
        "target_matches": True,
    }
    assert receipt() == (result, notifications)
    (output / "evaluation.json").write_text(
        json.dumps(
            {
                "evaluation": "SF18_LIVE_GITHUB_POLL",
                "revision": identity["revision"],
                "run_id": run_id,
                "local_agent_and_reviewer": "fixture",
                "ci_input_origin": "actual_github",
                "notification": notification,
                "result": result,
                "current_pr_after_restart": current_pr,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(json.dumps({"run_id": run_id, "pr": draft["url"], "ci": check["url"]}), flush=True)


def verify_live_pr_drift(user, run_id, output, identity, restart_control_plane):
    path = f"/v1/runs/{run_id}/result"
    original = user.get(path).raise_for_status().json()
    draft = original["draft_pr"]
    head_ref = f"codex/factory/{run_id}"
    assert draft["head_ref"] == head_ref
    assert draft["base_ref"].startswith("codex/factory-evaluation/")

    def mutate(method, resource, body):
        return json.loads(
            subprocess.check_output(
                ["gh", "api", "--method", method, f"repos/{REPOSITORY}/{resource}", "--input", "-"],
                input=json.dumps(body).encode(),
                timeout=25,
            )
        )

    commit = github(f"git/commits/{draft['head_sha']}")
    new_head = mutate(
        "POST",
        "git/commits",
        {
            "message": "Synthetic operator follow-up after Factory verification",
            "tree": commit["tree"]["sha"],
            "parents": [draft["head_sha"]],
        },
    )["sha"]
    assert new_head != draft["head_sha"]
    mutate("PATCH", f"git/refs/heads/{head_ref}", {"sha": new_head, "force": False})
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        moved = user.get(path + "/current-pr").raise_for_status().json()
        runs = github(f"actions/runs?head_sha={new_head}&event=pull_request")["workflow_runs"]
        current_ci = next(
            (
                run
                for run in runs
                if run["path"] == EVALUATION_WORKFLOW and run["status"] == "completed"
            ),
            None,
        )
        if moved.get("head_sha") == new_head and current_ci:
            break
        time.sleep(3)
    else:
        raise RuntimeError("GitHub did not expose the changed head and completed CI")
    assert not moved["target_matches"] and current_ci["head_sha"] == new_head
    assert user.get(path).raise_for_status().json() == original
    closed = mutate("PATCH", f"pulls/{draft['number']}", {"state": "closed"})
    assert closed["state"] == "closed" and not closed["merged"]
    restart_control_plane()
    current = user.get(path + "/current-pr").raise_for_status().json()
    assert current["state"] == "CLOSED" and not current["target_matches"]
    assert current["head_sha"] == new_head
    assert user.get(path).raise_for_status().json() == original
    report = {
        "revision": identity["revision"],
        "run_id": run_id,
        "pr_url": draft["url"],
        "historical_head": draft["head_sha"],
        "current_head": new_head,
        "current_ci": {"url": current_ci["html_url"], "conclusion": current_ci["conclusion"]},
        "current_pr": current,
        "historical_result_unchanged": True,
        "merged": closed["merged"],
    }
    (output / "current-pr-drift.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


def verify_live_ci_failure(user, run, publication, state, output, identity, restart_control_plane):
    run_id = run["run_id"]
    candidate_id = run["current_candidate_id"]
    verification_path = f"/v1/candidates/{candidate_id}/verification"

    def evidence():
        verification = user.get(verification_path)
        verification.raise_for_status()
        result = user.get(f"/v1/runs/{run_id}/result")
        assert result.status_code == 404
        notifications = user.get(f"/v1/runs/{run_id}/notifications")
        notifications.raise_for_status()
        assert not any(
            item["run_status"] == "SUCCEEDED" for item in notifications.json()["notifications"]
        )
        return verification.json()["gates"]

    gates = evidence()
    (local,) = [gate for gate in gates if gate["stage"] == "LOCAL"]
    (final,) = [gate for gate in gates if gate["stage"] == "FINAL"]
    assert local["status"] == "PASS"
    assert final["status"] == "FAIL" and final["reason"] == "CI_FAILED"
    (check,) = final["metadata_body"]["checks"]
    draft = publication["pull_request"]
    assert check["status"] == "FAIL" and check["tested_sha"] == draft["head_sha"]
    restart_control_plane()
    assert state()["phase"] == "REPAIR" and evidence() == gates
    summary = {
        "evaluation": "SF18_LIVE_GITHUB_POLL_FAILURE",
        "revision": identity["revision"],
        "run_id": run_id,
        "run_phase": run["phase"],
        "draft_pr": draft,
        "local_gate": local["status"],
        "final_gate": final,
        "check": check,
        "success_receipt": False,
        "success_notification": False,
    }
    (output / "evaluation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"run_id": run_id, "pr": draft["url"], "ci": check["url"]}), flush=True)


def crash_publisher_before_report():
    """Run the isolated fault-injection child with credentials supplied only on stdin."""
    settings = json.load(sys.stdin)

    class InterruptedPublisher(GitHubDraftPublisher):
        def publish(self, inputs):
            super().publish(inputs)
            os.kill(os.getpid(), signal.SIGKILL)

    with httpx.Client(
        base_url=settings["cp_url"],
        trust_env=False,
        headers={"Authorization": f"Bearer {settings['publisher_token']}"},
    ) as client:
        PublisherSupervisor(
            client,
            InterruptedPublisher(SourcePolicy((REMOTE,)), settings["github_token"]),
            PublicationJournal(Path(settings["journal"])),
        ).run_once()
    raise RuntimeError("Publisher did not reach the lost-response injection point")


def interrupt_live_publication(journal_path, cp_url, publisher_token, github_token):
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "from evaluate_publication import crash_publisher_before_report; "
            "crash_publisher_before_report()",
        ],
        cwd=ROOT,
        env={
            **{key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ},
            "PYTHONPATH": str(ROOT / "src"),
        },
        input=json.dumps(
            {
                "cp_url": cp_url,
                "publisher_token": publisher_token,
                "github_token": github_token,
                "journal": str(journal_path),
            }
        ),
        text=True,
        capture_output=True,
        timeout=180,
    )
    if child.returncode != -signal.SIGKILL:
        raise RuntimeError("Publisher did not terminate at the lost-response injection point")
    pending = PublicationJournal(journal_path).read()
    assert pending["phase"] == "STARTED" and "report" not in pending
    operation = pending["operation"]
    with Database().transaction() as session:
        stored = session.get(PublicationOperation, operation["operation_id"])
        assert stored.state == "IN_FLIGHT"
    return operation


def matching_live_drafts(operation):
    target = operation["target"]
    params = httpx.QueryParams(
        state="all",
        head=f"{REPOSITORY.split('/')[0]}:{target['head_ref']}",
        base=target["base_ref"],
    )
    return github(f"pulls?{params}")


def verify_live_restart(output, identity, publisher, run_id, cp_url, publish, on_unknown=None):
    github_token = subprocess.check_output(
        ["gh", "auth", "token", "--hostname", "github.com"], text=True
    ).strip()
    journal = output / "interrupted-publisher"
    operation = interrupt_live_publication(
        journal, cp_url, publisher["publisher_token"], github_token
    )
    (before,) = matching_live_drafts(operation)
    assert before["draft"] and before["head"]["sha"] == operation["target"]["candidate"]["head_sha"]
    restarted, publication = publish(journal)
    assert restarted["state"] == "UNKNOWN"
    (unknown,) = publication["operations"]
    assert unknown["state"] == "UNKNOWN" and unknown["next_reconcile_at"] is not None
    if on_unknown is not None:
        on_unknown()
    with httpx.Client(
        base_url=cp_url,
        trust_env=False,
        headers={"Authorization": f"Bearer {publisher['publisher_token']}"},
    ) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reply = client.get("/v1/publisher/reconciliations")
            reply.raise_for_status()
            if reply.json()["operations"]:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("read-only recovery did not become due")
    recovered, publication = publish(journal)
    assert recovered["state"] == "SUCCEEDED"
    (completed,) = publication["operations"]
    assert completed["operation_id"] == unknown["operation_id"] == operation["operation_id"]
    assert completed["state"] == "SUCCEEDED" and completed["reconcile_attempts"] == 1
    observed = publication["pull_request"]
    assert observed["number"] == before["number"] and observed["head_sha"] == before["head"]["sha"]
    (after,) = matching_live_drafts(operation)
    assert (after["id"], after["head"]["sha"]) == (before["id"], before["head"]["sha"])
    reads = publication["observations"][-1]["body"]["provider_requests"]
    assert reads and all(item["method"] == "GET" for item in reads)
    with Database().transaction() as session:
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.revision)
        )
        recovered_states = [
            "RECONCILING" if event.kind == "PUBLICATION_RECONCILING" else event.body["status"]
            for event in events
            if event.kind in {"PUBLICATION_RECONCILING", "PUBLICATION_RESULT_RECORDED"}
            and event.body.get("operation_id") == operation["operation_id"]
        ]
    assert recovered_states == ["UNKNOWN", "RECONCILING", "SUCCEEDED"]
    evidence = {
        "revision": identity["revision"],
        "run_id": run_id,
        "publisher_exit_signal": "SIGKILL",
        "journal_phase_at_crash": "STARTED",
        "states": ["IN_FLIGHT", *recovered_states],
        "operation_id": operation["operation_id"],
        "operation_key": operation["operation_key"],
        "base_sha": operation["target"]["candidate"]["base_sha"],
        "pull_request": observed["url"],
        "head_sha": observed["head_sha"],
        "provider_requests": reads,
        "matching_prs_before": 1,
        "matching_prs_after": 1,
    }
    (output / "evaluation.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence), flush=True)
    return publication


def verify_live_restore(output, identity, account, publisher, run_id, cp_url, cp_environment):
    """Kill the Publisher before result persistence, then recover from an encrypted backup."""
    github_token = subprocess.check_output(
        ["gh", "auth", "token", "--hostname", "github.com"], text=True
    ).strip()
    original_token = publisher["publisher_token"]
    backend = GitHubDraftPublisher(SourcePolicy((REMOTE,)), github_token)
    operation = interrupt_live_publication(
        output / "interrupted-publisher", cp_url, original_token, github_token
    )
    source = Database()
    (pull,) = matching_live_drafts(operation)
    assert pull["draft"] and not pull.get("merged")
    assert pull["head"]["sha"] == operation["target"]["candidate"]["head_sha"]
    before = [(pull["id"], pull["number"], pull["head"]["sha"])]
    name = "sf24_publication_" + uuid4().hex
    with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    target_url = source.engine.url.set(database=name).render_as_string(hide_password=False)
    target = Database(target_url)
    try:
        with TemporaryDirectory() as directory:
            private = Path(directory)
            certificate, key = private / "recovery.crt", private / "recovery.key"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-subj",
                    "/CN=SF24 live recovery",
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
            captured = backup(source, output / "artifacts", private / "backup", certificate)
            restored_root = output / "restored-artifacts"
            staging = restore(target, restored_root, captured["backup"], certificate, key)
            assert staging["status"] == "STAGED_RECONCILIATION_REQUIRED"
        restored_port = port()
        restored_url = f"http://127.0.0.1:{restored_port}"
        env = cp_environment | {
            "FACTORY_DATABASE_URL": target_url,
            "FACTORY_PORT": str(restored_port),
            "FACTORY_ARTIFACT_ROOT": str(restored_root),
        }
        issued = process_json(
            [
                sys.executable,
                "-m",
                "software_factory.cli",
                "reissue-publisher",
                "--publisher-id",
                publisher["publisher_id"],
                "--reason",
                "Live recovery fixture",
            ],
            environment=env,
        )
        assert issued["publisher_id"] == publisher["publisher_id"]
        with (output / "restored-control-plane.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "software_factory.cli", "serve"],
                env=env,
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                wait_ready(restored_url + "/healthz", process)
                with httpx.Client(base_url=restored_url, trust_env=False) as client:
                    old = client.get(
                        "/v1/publisher/reconciliations",
                        headers={"Authorization": f"Bearer {original_token}"},
                    )
                    assert old.status_code == 401
                    client.headers["Authorization"] = f"Bearer {account['user_token']}"
                    path = f"/v1/workspaces/{account['workspace_id']}/execution-control"
                    revision = client.get(path).json()["revision"]
                    action = {
                        "stopped": False,
                        "expected_revision": revision,
                        "reason": "Live recovery fixture",
                    }
                    denied = client.post(path, json=action, headers={"Idempotency-Key": "release"})
                    assert denied.status_code == 409
                    assert denied.json()["code"] == "RESTORE_RECONCILIATION_REQUIRED"
                    with httpx.Client(
                        base_url=restored_url,
                        trust_env=False,
                        headers={"Authorization": f"Bearer {issued['publisher_token']}"},
                    ) as restored_publisher:
                        recovery = PublisherSupervisor(
                            restored_publisher,
                            backend,
                            PublicationJournal(output / "restored-publisher"),
                        ).run_once()
                    assert recovery["state"] == "SUCCEEDED"
                    reads = list(backend.trace)
                    assert reads and all(item["method"] == "GET" for item in reads)
                    result = client.get(f"/v1/runs/{run_id}/publication").json()
                    assert result["pull_request"]["number"] == pull["number"]
                    assert result["pull_request"]["head_sha"] == pull["head"]["sha"]
                    post(client, path, action)
                after = [
                    (p["id"], p["number"], p["head"]["sha"])
                    for p in matching_live_drafts(operation)
                ]
                assert after == before
            finally:
                process.terminate()
                process.wait(timeout=10)
        evidence = {
            "evaluation": "SF24_LIVE_PUBLICATION_RESTORE",
            "revision": identity["revision"],
            "run_id": run_id,
            "pull_request": pull["html_url"],
            "head_sha": pull["head"]["sha"],
            "publisher_exit_signal": "SIGKILL",
            "journal_phase_at_crash": "STARTED",
            "pre_restore_operation": "IN_FLIGHT",
            "recovered_operation": recovery["state"],
            "provider_requests": reads,
            "matching_prs_before": len(before),
            "matching_prs_after": len(after),
            "old_token_status": old.status_code,
            "release_before_reconciliation_status": denied.status_code,
        }
        encoded = json.dumps(evidence, indent=2) + "\n"
        assert not any(
            token in encoded
            for token in (
                github_token,
                original_token,
                issued["publisher_token"],
                account["user_token"],
            )
        )
        (output / "evaluation.json").write_text(encoded)
        print(encoded, flush=True)
    finally:
        target.engine.dispose()
        with source.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f"DROP DATABASE {name}"))
        source.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--actions-workflow-id", type=int)
    parser.add_argument("--fixture-base-sha")
    parser.add_argument("--restore-publication", action="store_true")
    parser.add_argument("--restart-publisher", action="store_true")
    parser.add_argument("--restart-control-plane", action="store_true")
    parser.add_argument("--drift-current-pr", action="store_true")
    parser.add_argument("--expect-ci-failure", action="store_true")
    parser.add_argument("--live-webhook", action="store_true")
    args = parser.parse_args()
    live_ci = args.actions_workflow_id is not None
    recovery = args.restore_publication or args.restart_publisher or args.restart_control_plane
    if (
        recovery or args.expect_ci_failure or args.live_webhook or args.drift_current_pr
    ) and not live_ci:
        parser.error("restore and failure rehearsals require the fixed Actions evaluation profile")
    if recovery and (
        args.expect_ci_failure
        or args.live_webhook
        or sum((args.restore_publication, args.restart_publisher, args.restart_control_plane)) > 1
    ):
        parser.error("select one rehearsal at a time")
    if args.drift_current_pr and (recovery or args.expect_ci_failure or args.live_webhook):
        parser.error("PR drift rehearsal requires the successful polling profile")
    if live_ci != bool(args.fixture_base_sha):
        parser.error("--actions-workflow-id and --fixture-base-sha must be specified together")
    if live_ci and (
        args.actions_workflow_id <= 0 or not args.base_ref.startswith("codex/factory-evaluation/")
    ):
        parser.error("live CI requires a positive workflow ID and an evaluation-only base ref")
    url = make_url(os.environ["FACTORY_DATABASE_URL"])
    if not str(url.query.get("host", "")).startswith("/private/tmp/factory-pg-"):
        raise SystemExit("use with_postgres.py and its disposable PostgreSQL")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise SystemExit("commit the evaluated implementation and fixtures before this evaluation")
    identity = source_manifest()
    remote_sha = github("git/ref/heads/" + args.base_ref)["object"]["sha"]
    if remote_sha != (args.fixture_base_sha or identity["base_sha"]):
        raise SystemExit("evaluation base ref does not match the fixed SHA")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    source_port, cp_port = port(), port()
    while cp_port == source_port:
        cp_port = port()
    cp_url = f"http://127.0.0.1:{cp_port}"
    source_url = f"http://127.0.0.1:{source_port}"
    source_secret = uuid4().hex + uuid4().hex
    known_secrets = [source_secret]
    base_environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {"PATH", "HOME", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    }
    source_environment = base_environment | {
        "FACTORY_ALLOWED_REMOTES": REMOTE,
        "FACTORY_SOURCE_RUNNER_TOKEN": source_secret,
        "FACTORY_SOURCE_RUNNER_PORT": str(source_port),
        "FACTORY_RUNNER_IMAGE_DIGEST": os.environ["FACTORY_RUNNER_IMAGE_DIGEST"],
        "FACTORY_RUNNER_STATE_ROOT": str(output / "source-runner"),
    }
    cp_environment = base_environment | {
        "FACTORY_DATABASE_URL": os.environ["FACTORY_DATABASE_URL"],
        "FACTORY_PORT": str(cp_port),
        "FACTORY_SOURCE_RUNNER_URL": source_url,
        "FACTORY_SOURCE_RUNNER_TOKEN": source_secret,
        "FACTORY_ALLOWED_REMOTES": REMOTE,
        "FACTORY_ARTIFACT_ROOT": str(output / "artifacts"),
    }
    processes = []
    credential_file = output / "publisher.token"
    with ExitStack() as files:
        try:
            for module, environment, name in (
                ("software_factory.source_runner", source_environment, "source"),
                ("software_factory.cli", cp_environment, "control-plane"),
            ):
                log = files.enter_context((output / (name + ".log")).open("wb"))
                argv = [sys.executable, "-m", module] + (
                    ["serve"] if name == "control-plane" else []
                )
                processes.append(
                    subprocess.Popen(argv, env=environment, cwd=ROOT, stdout=log, stderr=log)
                )
            wait_ready(source_url + "/openapi.json", processes[0])
            wait_ready(cp_url + "/healthz", processes[1])
            account = process_json(
                [
                    sys.executable,
                    "-m",
                    "software_factory.cli",
                    "bootstrap",
                    "--name",
                    "SF-17 publication transport fixture",
                ],
                environment=cp_environment,
            )
            user = httpx.Client(
                # Credentials are checked against exported evidence before any upload.
                base_url=cp_url,
                timeout=180,
                trust_env=False,
                headers={"Authorization": "Bearer " + account["user_token"]},
            )
            known_secrets.append(account["user_token"])
            checks = ROOT / "examples/evaluation/checks"
            protected = {
                p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in checks.glob("*.py")
            }
            if live_ci:
                protected[EVALUATION_WORKFLOW] = hashlib.sha256(
                    (ROOT / EVALUATION_WORKFLOW).read_bytes()
                ).hexdigest()
            policy = {
                "allowed_write_paths": [TARGET],
                "protected_paths": [
                    ".github",
                    "migrations",
                    "auth",
                    "billing",
                    "examples/evaluation/checks",
                ],
                "protected_manifest": protected,
                "verification": {
                    "profile_id": "sf17-empty-state",
                    "required_checks": ["unit_tests", "review"],
                },
                "execution_image_digest": os.environ["FACTORY_RUNNER_IMAGE_DIGEST"],
                "run_budget_microusd": 1000000,
                "job_reservation_microusd": 100000,
                "require_plan_approval": True,
                "verification_commands": [
                    {
                        "check_id": "unit_tests",
                        "argv": [
                            "python",
                            "-m",
                            "pytest",
                            "/candidate/examples/evaluation/checks/check_empty.py",
                            "/candidate/examples/evaluation/checks/check_compat.py",
                            "--rootdir=/candidate",
                            "-q",
                            "-p",
                            "no:cacheprovider",
                            "--junitxml=.factory/unit.xml",
                        ],
                        "junit_path": ".factory/unit.xml",
                        "timeout_seconds": 60,
                        "expected_test_ids": [
                            "examples.evaluation.checks.check_empty::" + name
                            for name in ("test_empty", "test_one_item", "test_many_items")
                        ]
                        + ["examples.evaluation.checks.check_compat::test_existing_label_api"],
                    }
                ],
                "required_ci_checks": [
                    {"provider": "github", "app_id": 123, "check_name": name}
                    for name in ("publication-transport-a", "publication-transport-b")
                ],
            }
            if live_ci:
                policy["required_ci_checks"] = [
                    {
                        "provider": "github",
                        "app_id": 15368,
                        # The Agent fixture fixes empty-state behavior only.
                        "check_name": (
                            "factory-eval-count" if args.expect_ci_failure else "factory-eval-empty"
                        ),
                        "github_workflow_id": args.actions_workflow_id,
                        "github_workflow_path": EVALUATION_WORKFLOW,
                    }
                ]
            project = post(
                user,
                f"/v1/workspaces/{account['workspace_id']}/projects",
                {"name": "SF17 real GitHub transport, fixture agents and CI", "policy": policy},
            )
            repository = post(
                user,
                f"/v1/projects/{project['project_id']}/repositories",
                {
                    "name": "Software Factory evaluation",
                    "base_ref": args.base_ref,
                    "base_sha": remote_sha,
                    "remote_url": REMOTE,
                },
            )
            spec = {
                "schema_version": 1,
                "task": {
                    "title": "SF17 transport fixture: empty state",
                    "repository_id": repository["repository_id"],
                    "base_ref": args.base_ref,
                    "objective": "Empty items: データがありません; keep nonempty behavior",
                    "acceptance_criteria": [
                        {"id": "AC-1", "description": "Empty input returns データがありません"},
                        {
                            "id": "AC-2",
                            "description": "Nonempty and stable_label APIs retain behavior",
                        },
                    ],
                    "constraints": ["Only app.py may change; golden checks are immutable"],
                },
                "verification": policy["verification"],
            }
            task = post(user, f"/v1/projects/{project['project_id']}/tasks", spec)
            run = post(user, f"/v1/tasks/{task['task_id']}/runs", {"spec_version": 1})
            run_id = run["run_id"]

            def state():
                reply = user.get(f"/v1/runs/{run_id}")
                reply.raise_for_status()
                return reply.json()

            def worker(kinds, managed=False):
                argv = [
                    sys.executable,
                    "-m",
                    "software_factory.cli",
                    "issue-enrollment",
                    "--workspace-id",
                    account["workspace_id"],
                    "--kinds",
                    *kinds,
                ]
                if managed:
                    argv += ["--managed"]
                enrollment = process_json(argv, environment=cp_environment)
                result = httpx.post(
                    cp_url + "/v1/worker/enroll",
                    json={
                        "token": enrollment["enrollment_token"],
                        "name": "SF17 controlled fixture worker",
                    },
                ).json()
                known_secrets.append(result["worker_token"])
                return httpx.Client(
                    base_url=cp_url,
                    timeout=180,
                    trust_env=False,
                    headers={"Authorization": "Bearer " + result["worker_token"]},
                )

            agent_client = worker(["PLAN", "IMPLEMENT", "REPAIR"])
            managed_client = worker(["ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"], managed=True)
            os.environ["FACTORY_RUNNER_STATE_ROOT"] = str(output / "stage-runner")
            runner = DockerRunner(os.environ["FACTORY_RUNNER_IMAGE_DIGEST"])
            before = (ROOT / TARGET).read_text()
            first = before.replace(
                'return ", ".join(items)',
                'return ", ".join(items) if items else "データがありません"',
            )
            fixture = FixtureAgent(before, first)

            def restart_control_plane():
                process = processes[-1]
                if args.restart_control_plane:
                    process.kill()
                else:
                    process.terminate()
                process.wait(timeout=10)
                log = files.enter_context((output / "control-plane-restarts.log").open("ab"))
                processes[-1] = subprocess.Popen(
                    [sys.executable, "-m", "software_factory.cli", "serve"],
                    env=cp_environment,
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                )
                wait_ready(cp_url + "/healthz", processes[-1])
                return [process.pid, processes[-1].pid], process.returncode

            restart_samples = {}

            def snapshot():
                values = {}
                for name in ("", "/publication", "/result", "/notifications", "/event-history"):
                    reply = user.get(f"/v1/runs/{run_id}{name}")
                    assert reply.status_code == (
                        404 if name == "/result" and values[""]["status"] != "SUCCEEDED" else 200
                    ), reply.text
                    values[name] = reply.json()
                return values

            def capture_restart():
                before = snapshot()
                pids, exit_code = restart_control_plane()
                after = snapshot()
                assert exit_code == -signal.SIGKILL and pids[0] != pids[1]
                assert after == before
                run = before[""]
                (operation,) = before["/publication"]["operations"]
                restart_samples[run["phase"] if run["status"] != "SUCCEEDED" else "SUCCEEDED"] = {
                    "pids": pids,
                    "exit_code": exit_code,
                    "before_digest": digest(before),
                    "after_digest": digest(after),
                    "operation_key": operation["operation_key"],
                    "next_reconcile_at": operation["next_reconcile_at"],
                    "ci_deadline_at": (run.get("wait_context") or {}).get("deadline_at"),
                    "pull_request": (before["/publication"]["pull_request"] or {}).get("url"),
                    "result_id": before["/result"].get("result_id"),
                    "notification_keys": [
                        row["delivery_key"] for row in before["/notifications"]["notifications"]
                    ],
                }

            assert Supervisor(agent_client, fixture, heartbeat_interval=0.1).run_once()["accepted"]
            approvals = user.get(f"/v1/runs/{run_id}/approvals").json()
            request = approvals[0]
            assert state()["phase"] == "WAIT_PLAN_APPROVAL" and state()["reserved_microusd"] == 0
            post(
                user,
                f"/v1/approvals/{request['approval_id']}/decisions",
                {
                    "expected_run_revision": request["target"]["run_revision"],
                    "target_hash": request["target_hash"],
                    "decision": "approve",
                    "reason": "Approve the fixed transport evaluation Plan",
                },
            )
            publisher = process_json(
                [
                    sys.executable,
                    "-m",
                    "software_factory.cli",
                    "issue-publisher",
                    "--workspace-id",
                    account["workspace_id"],
                    "--repository-id",
                    repository["repository_id"],
                    "--name",
                    "SF17 dedicated evaluation Publisher",
                ],
                environment=cp_environment,
            )
            credential_file.write_text(publisher["publisher_token"])
            known_secrets.append(publisher["publisher_token"])
            credential_file.chmod(0o600)

            def managed_stages():
                adapter = PipelineAdapter(runner, reviewer=FixtureReviewer())
                for _ in range(3):
                    result = Supervisor(managed_client, adapter, heartbeat_interval=0.1).run_once()
                    if not result or not result.get("accepted"):
                        raise RuntimeError("managed transport stage did not complete")
                if state()["phase"] != "PUBLISH_DRAFT":
                    raise RuntimeError(
                        "transport candidate did not pass the actual local golden checks"
                    )

            def publish(state_dir=output / "publisher"):
                result = process_json(
                    [
                        sys.executable,
                        "-m",
                        "software_factory.publisher",
                        "--control-plane-url",
                        cp_url,
                        "--publisher-token-file",
                        str(credential_file),
                        "--github-use-gh",
                        "--allowed-remote",
                        REMOTE,
                        "--state-dir",
                        str(state_dir),
                        "--once",
                    ],
                    environment=base_environment,
                )
                record = user.get(f"/v1/runs/{run_id}/publication").json()
                (output / f"publication-{len(record['operations'])}.json").write_text(
                    json.dumps(record, indent=2, ensure_ascii=False)
                )
                return result, record

            assert Supervisor(agent_client, fixture, heartbeat_interval=0.1).run_once()["accepted"]
            managed_stages()
            if args.restart_publisher:
                verify_live_restart(output, identity, publisher, run_id, cp_url, publish)
                return
            if args.restore_publication:
                verify_live_restore(
                    output, identity, account, publisher, run_id, cp_url, cp_environment
                )
                return
            if args.restart_control_plane:
                publication = verify_live_restart(
                    output, identity, publisher, run_id, cp_url, publish, capture_restart
                )
                (output / "evaluation.json").rename(output / "sf19-restart-verification.json")
                capture_restart()
            elif not args.live_webhook:
                result, publication = publish()
                assert result["state"] == "SUCCEEDED" and state()["phase"] == "WAIT_CI"
            if live_ci:
                integration_id = str(uuid4())
                webhook_secret = uuid4().hex + uuid4().hex
                provider_token = subprocess.check_output(
                    ["gh", "auth", "token", "--hostname", "github.com"], text=True
                ).strip()
                known_secrets.extend([webhook_secret, provider_token])
                cp_environment["FACTORY_GITHUB_WEBHOOKS"] = json.dumps(
                    {
                        integration_id: {
                            "workspace_id": account["workspace_id"],
                            "repository_id": repository["repository_id"],
                            "github_repository_id": github("")["id"],
                            "github_repository": REPOSITORY,
                            "secret": webhook_secret,
                        }
                    }
                )
                cp_environment["FACTORY_GITHUB_CI_READ_TOKENS"] = json.dumps(
                    {integration_id: provider_token}
                )

                restart_control_plane()
                if args.live_webhook:
                    hook_path = f"/v1/webhooks/github/{integration_id}"
                    files.enter_context(
                        github_webhook_ingress(REPOSITORY, cp_url, hook_path, webhook_secret)
                    )
                    result, publication = publish()
                    assert result["state"] == "SUCCEEDED"
                verify_live_ci(
                    user,
                    run_id,
                    publication,
                    state,
                    output,
                    identity,
                    capture_restart if args.restart_control_plane else restart_control_plane,
                    expect_failure=args.expect_ci_failure,
                )
                with Database().transaction() as session:
                    deliveries = list(
                        session.scalars(
                            select(CIWebhookDelivery)
                            .where(CIWebhookDelivery.integration_id == integration_id)
                            .order_by(CIWebhookDelivery.received_at)
                        )
                    )
                    triggers = list(
                        session.scalars(
                            select(RunEvent)
                            .where(
                                RunEvent.run_id == run_id,
                                RunEvent.kind == "GITHUB_CI_RECONCILED",
                            )
                            .order_by(RunEvent.revision)
                        )
                    )
                proof = {
                    "deliveries": [
                        {
                            "id": row.delivery_id,
                            "event": row.event,
                            "received_at": row.received_at,
                        }
                        for row in deliveries
                    ],
                    "reconciliations": [row.body for row in triggers],
                }
                expected_trigger = "WEBHOOK" if args.live_webhook else "POLL"
                assert any(row.event != "ping" for row in deliveries) == args.live_webhook
                assert triggers and triggers[-1].body.get("trigger") == expected_trigger
                (output / "ci-trigger.json").write_text(json.dumps(proof, indent=2) + "\n")
                if args.restart_control_plane:
                    before = snapshot()
                    (operation,) = publication["operations"]
                    body = publication["observations"][-1]["body"]
                    report_key = "publish-report:" + digest(body)
                    replay = user.post(
                        f"/v1/publisher/operations/{operation['operation_id']}/reports",
                        json=body,
                        headers={
                            "Authorization": "Bearer " + publisher["publisher_token"],
                            "Idempotency-Key": report_key,
                        },
                    )
                    replay.raise_for_status()
                    assert replay.json()["operation"]["operation_id"] == operation["operation_id"]
                    after = snapshot()
                    assert after == before
                    assert set(restart_samples) == {"PUBLISH_UNKNOWN", "WAIT_CI", "SUCCEEDED"}
                    (output / "sf19-verification.json").write_text(
                        json.dumps(
                            {
                                "revision": identity["revision"],
                                "run_id": run_id,
                                "stages": restart_samples,
                                "publisher_report_key": report_key,
                                "after_replay_digest": digest(after),
                                "evaluation": json.loads((output / "evaluation.json").read_text()),
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                if args.drift_current_pr:
                    verify_live_pr_drift(user, run_id, output, identity, restart_control_plane)
                return
            first_publication = publication
            database = Database()
            service = Service(database)

            def fixture_ci(failures):
                context = state()["wait_context"]
                checks = []
                for index, required in enumerate(context["required_ci"]):
                    checks.append(
                        {
                            "id": f"contract-ci-{state()['current_candidate_id']}-{index}",
                            **required,
                            "origin_verified": True,
                            "status": "FAIL" if index < failures else "PASS",
                            "base_sha": context["target"]["base_sha"],
                            "head_sha": context["target"]["head_sha"],
                            "tested_sha": context["target"]["head_sha"],
                            "merge_parents": [],
                        }
                    )
                with database.transaction() as session:
                    service.lock_workspace(session, account["workspace_id"])
                    gate = service.gates.final(session, session.get(Run, run_id), checks)
                    assert gate.status == "FAIL"
                assert state()["phase"] == "REPAIR"

            def repair(old, new):
                assert Supervisor(
                    agent_client, FixtureAgent(old, new), heartbeat_interval=0.1
                ).run_once()["accepted"]
                managed_stages()

            fixture_ci(2)
            second = first + "\n# Publication transport update fixture.\n"
            repair(first, second)
            result, publication = publish()
            assert result["state"] == "SUCCEEDED", {
                "state": result["state"],
                "reason": publication["operations"][-1]["reason"],
            }
            second_publication = publication
            assert (
                publication["pull_request"]["number"] == first_publication["pull_request"]["number"]
            )
            assert (
                publication["pull_request"]["head_sha"]
                != first_publication["pull_request"]["head_sha"]
            )
            fixture_ci(1)
            third = second + "# Publication transport competing-head fixture.\n"
            repair(second, third)

            operation = second_publication["operations"][-1]
            sources, current = [], operation["candidate_id"]
            while current:
                candidate = user.get(f"/v1/candidates/{current}").json()
                archive = user.get(f"/v1/artifacts/{candidate['source_artifact_id']}/content")
                archive.raise_for_status()
                sources.append(
                    {"candidate": candidate, "archive": base64.b64encode(archive.content).decode()}
                )
                current = candidate["metadata_body"].get("parent_candidate_id")
            provider_token = subprocess.check_output(
                ["gh", "auth", "token", "--hostname", "github.com"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            backend = GitHubDraftPublisher(SourcePolicy((REMOTE,)), provider_token)
            known_secrets.append(provider_token)
            with backend.git.prepare(
                {"operation": operation, "sources": list(reversed(sources))}
            ) as root:
                parent = operation["target"]["candidate"]
                human_sha = (
                    backend.git._git(
                        root,
                        "-c",
                        "user.name=Human concurrency fixture",
                        "-c",
                        "user.email=human-fixture@localhost",
                        "commit-tree",
                        parent["tree_sha"],
                        "-p",
                        parent["head_sha"],
                        body=b"Simulated human commit for publication race\n",
                    )
                    .decode()
                    .strip()
                )
                ref = "refs/heads/" + operation["target"]["head_ref"]
                backend.git._git(
                    root,
                    "push",
                    "--porcelain",
                    "--no-verify",
                    "--force-with-lease=" + ref + ":" + parent["head_sha"],
                    REMOTE,
                    human_sha + ":" + ref,
                    network=True,
                )
            result, publication = publish()
            assert result["state"] == "BLOCKED" and state()["stop_reason"] == "REMOTE_HEAD_CONFLICT"
            actual = github("pulls/" + str(publication["pull_request"]["number"]))
            assert actual["head"]["sha"] == human_sha and actual["draft"] is True
            records = save_database(output)
            artifact_manifest = {
                p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (output / "artifacts").rglob("*")
                if p.is_file()
            }
            provider_requests = [
                request
                for item in records["publication_observations"]
                for request in item["body"].get("provider_requests", [])
            ]
            report = {
                "schema_version": 1,
                "evaluation": "SF17_PUBLICATION_TRANSPORT",
                "code_digest": identity["code_digest"],
                "revision": identity["revision"],
                "repository": REPOSITORY,
                "base_sha": remote_sha,
                "base_ref": args.base_ref,
                "workspace_id": account["workspace_id"],
                "run_id": run_id,
                "agent_origin": "fixture",
                "review_origin": "fixture",
                "ci_input_origin": "contract_fixture",
                "publication_origin": "actual_github",
                "runner_image_digest": os.environ["FACTORY_RUNNER_IMAGE_DIGEST"],
                "first_publication": first_publication,
                "second_publication": second_publication,
                "final_publication": publication,
                "human_fixture_sha": human_sha,
                "actual_pull_request": actual,
                "artifact_manifest": artifact_manifest,
                "database_sha256": hashlib.sha256(
                    (output / "database.json").read_bytes()
                ).hexdigest(),
                "final_run": state(),
                "provider_requests": provider_requests,
                "merge_calls": sum(r["resource"].endswith("/merge") for r in provider_requests),
                "deploy_calls": sum("/deployments" in r["resource"] for r in provider_requests),
                "created_at": time.time(),
            }
            (output / "evaluation.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False)
            )
            for name in list(artifact_manifest) + ["database.json", "evaluation.json"]:
                content = (output / name).read_bytes()
                if any(secret.encode() in content for secret in known_secrets):
                    raise RuntimeError("live credential in evaluation evidence; upload denied")
            print(
                json.dumps(
                    {
                        "report": str(output / "evaluation.json"),
                        "pull_request": actual["html_url"],
                        "state": state()["status"],
                        "preserved_human_sha": human_sha,
                    }
                ),
                flush=True,
            )
        finally:
            if not (output / "evaluation.json").exists():
                try:
                    save_database(output)
                    (output / "evaluation-error.json").write_text(
                        json.dumps(
                            {
                                "evaluation": "SF17_PUBLICATION_TRANSPORT",
                                "result": "INCOMPLETE",
                                "revision": identity["revision"],
                                "code_digest": identity["code_digest"],
                                "created_at": time.time(),
                                "base_ref": args.base_ref,
                            },
                            indent=2,
                        )
                    )
                except Exception:
                    (output / "export-error.txt").write_text("failed to preserve the disposable DB")
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            credential_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
