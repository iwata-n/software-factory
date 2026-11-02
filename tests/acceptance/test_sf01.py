import json
import os
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, text

from scripts.acceptance_support import ROOT, sha
from software_factory.api import create_app
from software_factory.contracts import digest
from software_factory.db import Database

from .helpers import dump, schema_database


@pytest.fixture(scope="module")
def checkout(tmp_path_factory):
    directory = tmp_path_factory.mktemp("fresh-checkout")
    for name in ("src", "scripts", "migrations", "docs"):
        shutil.copytree(ROOT / name, directory / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock", "README.md", "alembic.ini", ".python-version"):
        shutil.copy2(ROOT / name, directory / name)
    before = sha((directory / "uv.lock").read_bytes())
    installed = subprocess.run(
        ["uv", "sync", "--locked", "--python", sys.executable],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stderr
    assert sha((directory / "uv.lock").read_bytes()) == before
    return directory


@pytest.mark.ac("SF-01-AC01")
def test_locked_fresh_install(checkout, evidence):
    python = checkout / ".venv/bin/python"
    imported = subprocess.run(
        [str(python), "-c", "import software_factory; import sys; print(sys.version_info[:2])"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    assert imported.stdout.strip() == "(3, 11)"
    for name in ("factory", "factory-worker"):
        assert (
            subprocess.run(
                [str(checkout / ".venv/bin" / name), "--help"],
                cwd=checkout,
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    evidence(
        "fresh Python 3.11 install and both entry points succeed",
        imported.stdout.strip(),
        lock_digest=sha((checkout / "uv.lock").read_bytes()),
    )


@pytest.mark.ac("SF-01-AC02")
def test_empty_upgrade_is_scoped_and_matches_models(database, evidence):
    before = dump(database)
    with schema_database(database) as fresh:
        command.check(Config("alembic.ini"))
        with fresh.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        assert dump(database) == before
        evidence("head=0002, model diff=0, parent schema unchanged", sorted(dump(fresh)))


@pytest.mark.ac("SF-01-AC03")
def test_previous_revision_data_survives_upgrade(database, evidence):
    from tests.conftest import example_policy, example_spec

    with schema_database(database, "0001") as old:
        with old.engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection)
            wid, pid, rid, tid, sid, runid = [str(uuid4()) for _ in range(6)]
            body = example_spec(rid)
            records = {
                "workspaces": {"id": wid, "name": "legacy"},
                "projects": {
                    "id": pid,
                    "workspace_id": wid,
                    "name": "project",
                    "policy": example_policy(),
                },
                "repositories": {
                    "id": rid,
                    "workspace_id": wid,
                    "project_id": pid,
                    "name": "repo",
                    "base_ref": "main",
                    "base_sha": "a" * 40,
                },
                "tasks": {
                    "id": tid,
                    "workspace_id": wid,
                    "project_id": pid,
                    "repository_id": rid,
                    "title": "legacy",
                    "latest_spec_version": 1,
                },
                "task_spec_versions": {
                    "id": sid,
                    "workspace_id": wid,
                    "task_id": tid,
                    "version": 1,
                    "body": body,
                    "spec_hash": digest(body),
                },
                "runs": {
                    "id": runid,
                    "workspace_id": wid,
                    "task_id": tid,
                    "spec_version": 1,
                    "repository_id": rid,
                    "base_sha": "a" * 40,
                    "snapshot": {"immutable": body},
                    "status": "BLOCKED",
                    "phase": "PLAN",
                    "stop_reason": "legacy reason",
                    "state_revision": 7,
                    "budget_microusd": 1_000_000,
                    "reserved_microusd": 0,
                    "spent_microusd": 25000,
                    "created_at": 100.0,
                },
            }
            for name, values in records.items():
                connection.execute(metadata.tables[name].insert().values(**values))
        before = dump(old)
        command.upgrade(Config("alembic.ini"), "head")
        after = dump(old)
        for name, rows in before.items():
            if name != "alembic_version":
                assert (
                    [{key: row[key] for key in rows[0]} for row in after[name]] == rows
                    if rows
                    else not after[name]
                )
        assert after["repositories"][0]["active"] is True
        command.upgrade(Config("alembic.ini"), "head")
        assert dump(old) == after
        command.check(Config("alembic.ini"))
        evidence(
            "legacy task/spec/run/money unchanged; upgrade repeat is a no-op",
            {"before_hash": digest(before), "after_hash": digest(after), "retained_spent": 25000},
        )


@pytest.mark.ac("SF-01-AC04")
def test_health_openapi_and_real_connection_failure(database, tmp_path, evidence):
    client = TestClient(create_app(database))
    assert client.get("/healthz").status_code == 200
    assert "/v1/worker/leases/{lease_id}/events" in client.get("/openapi.json").json()["paths"]
    failing = Database(
        database.engine.url.update_query_dict(
            {
                "host": str(tmp_path / "absent-socket"),
                "connect_timeout": "1",
            }
        ).render_as_string(hide_password=False)
    )
    try:
        assert TestClient(create_app(failing)).get("/healthz").status_code == 503
    finally:
        failing.engine.dispose()
    with pytest.raises(ValueError):
        Database("sqlite://")
    evidence(
        {"healthy": 200, "db_unreachable": 503, "sqlite": "rejected"},
        {"healthy": 200, "db_unreachable": 503, "sqlite": "rejected"},
    )


@pytest.mark.ac("SF-01-AC05", "C-01")
def test_lint_and_format(evidence):
    for command_line in (
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
    ):
        result = subprocess.run(command_line, cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        evidence("command exits 0", result.stdout.strip(), command=command_line)


@pytest.mark.ac("SF-01-AC06")
def test_readme_startup_in_new_checkout(checkout, database, evidence):
    python = str(checkout / ".venv/bin/python")
    env = os.environ | {
        "FACTORY_DATABASE_URL": database.engine.url.render_as_string(hide_password=False)
    }

    def cli(*args):
        result = subprocess.run(
            [python, "-m", "software_factory.cli", *args],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return json.loads(result.stdout)

    bootstrap = cli("bootstrap", "--name", "readme-reproduction")
    enrollment = cli("issue-enrollment", "--workspace-id", bootstrap["workspace_id"])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env["FACTORY_PORT"] = str(port)
    process = subprocess.Popen(
        [python, "-m", "software_factory.cli", "serve"],
        cwd=checkout,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        api = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(api + "/healthz", timeout=0.2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("README server did not start")
        worker = httpx.post(
            api + "/v1/worker/enroll",
            json={"token": enrollment["enrollment_token"], "name": "readme"},
        )
        assert worker.status_code == 201
        client_env = {
            k: v
            for k, v in env.items()
            if k not in {"FACTORY_DATABASE_URL", "TEST_DATABASE_URL", "GH_TOKEN", "GITHUB_TOKEN"}
        }
        demo = subprocess.run(
            [
                python,
                "scripts/create_demo.py",
                "--workspace-id",
                bootstrap["workspace_id"],
                "--api-url",
                api,
            ],
            cwd=checkout,
            env=client_env | {"FACTORY_USER_TOKEN": bootstrap["user_token"]},
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        runid = json.loads(demo.stdout)["run_id"]
        subprocess.run(
            [str(checkout / ".venv/bin/factory-worker"), "--drain", "--api-url", api],
            cwd=checkout,
            env=client_env | {"FACTORY_WORKER_TOKEN": worker.json()["worker_token"]},
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        run = httpx.get(
            api + "/v1/runs/" + runid,
            headers={"Authorization": f"Bearer {bootstrap['user_token']}"},
        ).json()
        assert run["stop_reason"] == "CANDIDATE_PIPELINE_NOT_IMPLEMENTED"
        evidence(
            "README CLI/API/worker flow succeeds in fresh install",
            run["stop_reason"],
            run_id=runid,
            external_requirement="completed GitHub CI and downloaded artifact still require audit",
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)
