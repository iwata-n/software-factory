"""Real CLI, HTTP, process restart, and a worker with no database credential."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.postgres


def module_command(module, *args, env=None):
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def start_control_plane(port):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "software_factory.api:app_factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.2).status_code == 200:
                    return process
            except httpx.TransportError:
                pass
            if process.poll() is not None:
                raise AssertionError("Control Plane stopped before becoming healthy")
            time.sleep(0.05)
        raise AssertionError("Control Plane health check timed out")
    except BaseException:
        process.terminate()
        process.communicate(timeout=10)
        raise


def test_cli_demo_and_separate_worker_survive_control_plane_restart(database):
    bootstrap = json.loads(module_command("software_factory.cli", "bootstrap", "--name", "smoke"))
    enrollment = json.loads(
        module_command(
            "software_factory.cli",
            "issue-enrollment",
            "--workspace-id",
            bootstrap["workspace_id"],
        )
    )
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    api_url = f"http://127.0.0.1:{port}"
    process = start_control_plane(port)
    try:
        worker_registration = httpx.post(
            api_url + "/v1/worker/enroll",
            json={
                "token": enrollment["enrollment_token"],
                "name": "smoke-worker",
            },
        )
        assert worker_registration.status_code == 201
        client_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "FACTORY_DATABASE_URL",
                "TEST_DATABASE_URL",
                "FACTORY_USER_TOKEN",
                "FACTORY_WORKER_TOKEN",
            }
        }
        demo_env = client_env | {"FACTORY_USER_TOKEN": bootstrap["user_token"]}
        demo = subprocess.run(
            [
                sys.executable,
                "scripts/create_demo.py",
                "--workspace-id",
                bootstrap["workspace_id"],
                "--api-url",
                api_url,
            ],
            env=demo_env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert demo.returncode == 0, demo.stderr
        run_url = json.loads(demo.stdout)["run_url"]
        worker_env = client_env | {
            "FACTORY_WORKER_TOKEN": worker_registration.json()["worker_token"]
        }
        assert (
            len(
                module_command(
                    "software_factory.worker",
                    "--once",
                    "--api-url",
                    api_url,
                    env=worker_env,
                )
                .strip()
                .splitlines()
            )
            == 1
        )
        headers = {"Authorization": f"Bearer {bootstrap['user_token']}"}
        before = httpx.get(run_url, headers=headers).json()
        assert before["phase"] == "IMPLEMENT"
        process.terminate()
        process.communicate(timeout=10)
        process = start_control_plane(port)
        assert httpx.get(run_url, headers=headers).json() == before
        assert (
            len(
                module_command(
                    "software_factory.worker",
                    "--drain",
                    "--api-url",
                    api_url,
                    env=worker_env,
                )
                .strip()
                .splitlines()
            )
            == 1
        )
        after = httpx.get(run_url, headers=headers).json()
        assert after["status"] == "BLOCKED"
        assert after["stop_reason"] == "CANDIDATE_PIPELINE_NOT_IMPLEMENTED"
        revoked = json.loads(
            module_command(
                "software_factory.cli",
                "revoke",
                "--principal-id",
                worker_registration.json()["worker_id"],
            )
        )
        assert revoked["revoked"]
        assert (
            httpx.post(
                api_url + "/v1/worker/claims",
                json={"protocol_version": 1},
                headers={"Authorization": f"Bearer {worker_env['FACTORY_WORKER_TOKEN']}"},
            ).status_code
            == 401
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)
