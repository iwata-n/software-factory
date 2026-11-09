"""Run a loopback Software Factory workspace against its GitHub repository."""

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from evaluate_codex import available_port, post, wait_ready
from sqlalchemy.engine import make_url

from software_factory.codex_engine import CodexCLI
from software_factory.codex_worker_pool import (
    DEFAULT_CODEX_WORKERS,
    MAX_CODEX_WORKERS,
    CodexWorkerPool,
)
from software_factory.db import Database
from software_factory.models import Principal, Workspace

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/iwata-n/software-factory.git"
CHECK = "project_syntax"
# The verifier is supplied by the operator policy, outside the candidate tree.
TEST = (
    "from pathlib import Path\n"
    "import subprocess\n"
    "def test_project():\n"
    "    root = Path('/candidate')\n"
    "    sources = sorted(root.rglob('*.py'))\n"
    "    assert sources, 'Python source files are missing'\n"
    "    for path in sources:\n"
    "        compile(path.read_text(encoding='utf-8'), str(path), 'exec')\n"
    "    for path in sorted(root.rglob('*.js')):\n"
    "        subprocess.run(['node', '--input-type=module', '--check'], "
    "input=path.read_bytes(), check=True, capture_output=True)\n"
)
PYTEST = (
    "import sys,base64,pytest;open('test_project.py','wb').write("
    "base64.b64decode(''.join(sys.argv[1:])));"
    "sys.exit(pytest.main(['test_project.py','--junitxml=.factory/project.xml','-q']))"
)
_TEST_ENCODED = base64.b64encode(TEST.encode()).decode()
# Command arguments are capped at 200 characters by the policy contract.
CHECK_ARGV = ["python", "-I", "-c", PYTEST] + [
    _TEST_ENCODED[start : start + 200] for start in range(0, len(_TEST_ENCODED), 200)
]


def start(stack, output, name, module, env, *args):
    with (output / f"{name}.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", module, *args], cwd=ROOT, env=env, stdout=log, stderr=log
        )
    stack.callback(stop, process)
    return process


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def start_managed_worker(
    stack, output, name, worker_env, server_env, workspace, url, kinds, arguments
):
    enrollment = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-m",
                "software_factory.cli",
                "issue-enrollment",
                "--workspace-id",
                workspace,
                "--managed",
                "--usage-authority",
                "--adapter",
                "codex_cli",
                "--kinds",
                *kinds,
            ],
            cwd=ROOT,
            env=server_env,
        )
    )
    with httpx.Client(base_url=url, timeout=30, trust_env=False) as client:
        credential = post(
            client,
            "/v1/worker/enroll",
            {
                "token": enrollment["enrollment_token"],
                "name": f"Self-host {name}",
            },
        )
    process = start(
        stack,
        output,
        name,
        "software_factory.worker",
        worker_env | {"FACTORY_WORKER_TOKEN": credential["worker_token"]},
        *arguments,
    )
    process.factory_worker_id = credential["worker_id"]
    return process


def start_codex_pool(
    stack, output, worker_env, server_env, workspace, url, binary, manifest, *, task_chat=True
):
    database = Database(server_env["FACTORY_DATABASE_URL"])
    stack.callback(database.engine.dispose)
    generations = {}

    def desired():
        with database.transaction() as session:
            return session.get(Workspace, workspace).codex_worker_count

    def retire(process):
        with database.transaction() as session:
            actor = session.get(Principal, process.factory_worker_id)
            if actor and actor.workspace_id == workspace:
                actor.active = False

    def provision(slot, drain):
        generations[slot] = generations.get(slot, 0) + 1
        name = f"codex_cli-{slot + 1}-{generations[slot]}"
        return start_managed_worker(
            stack,
            output,
            name,
            worker_env | {"FACTORY_RUNNER_STATE_ROOT": str(output / f"codex-state-{slot + 1}")},
            server_env,
            workspace,
            url,
            ["PLAN", "IMPLEMENT", "REPAIR"],
            [
                "--api-url",
                url,
                "--adapter",
                "codex_cli",
                "--codex-binary",
                str(binary),
                "--codex-manifest",
                str(manifest),
                "--stop-when-idle-file",
                str(drain),
                *(["--task-chat"] if task_chat else []),
            ],
        )

    return CodexWorkerPool(output / "codex-pool", desired, provision, retire)


def monitor_services(server, services, restart, *, tick=None):
    """A failed source runner/worker must not take the operations UI offline."""
    failures, retry_at = [0] * len(services), [0.0] * len(services)
    while server.poll() is None:
        if tick:
            try:
                tick()
            except Exception as error:
                print(json.dumps({"pool_refresh_failed": type(error).__name__}), flush=True)
        for index, process in enumerate(services):
            if process.poll() is None or time.monotonic() < retry_at[index]:
                continue
            failures[index] += 1
            retry_at[index] = time.monotonic() + min(30, 2 ** min(failures[index], 5))
            try:
                services[index] = restart(index)
                print(
                    json.dumps({"service_restarted": index, "generation": failures[index]}),
                    flush=True,
                )
            except Exception as error:
                print(
                    json.dumps({"service_restart_failed": index, "error": type(error).__name__}),
                    flush=True,
                )
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--codex-binary", required=True, type=Path)
    parser.add_argument("--codex-manifest", required=True, type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--codex-workers",
        type=int,
        choices=range(1, MAX_CODEX_WORKERS + 1),
        default=DEFAULT_CODEX_WORKERS,
        help="Initial number of parallel isolated Codex workers (default: 10)",
    )
    parser.add_argument("--codex-login-binary", default=shutil.which("codex"))
    parser.add_argument("--admin-username", help="Provision a web administrator for this preview")
    args = parser.parse_args()
    db = os.environ["FACTORY_DATABASE_URL"]
    if not str(make_url(db).query.get("host", "")).startswith("/private/tmp/factory-pg-"):
        parser.error("Use scripts/with_postgres.py for a dedicated database")
    engine = CodexCLI.from_file(args.codex_binary, args.codex_manifest, require_isolation=True)
    image = engine.manifest.isolation.image_digest
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    common = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
        )
        if key in os.environ
    } | {"PYTHONPATH": str(ROOT / "src")}
    port = args.port or available_port()
    source_port = available_port()
    while source_port == port:
        source_port = available_port()
    source_url = f"http://127.0.0.1:{source_port}"
    url = f"http://127.0.0.1:{port}"
    source_token = secrets.token_urlsafe(32)
    source_env = common | {
        "FACTORY_ALLOWED_REMOTES": REMOTE,
        "FACTORY_SOURCE_RUNNER_TOKEN": source_token,
        "FACTORY_SOURCE_RUNNER_PORT": str(source_port),
        "FACTORY_RUNNER_IMAGE_DIGEST": image,
        "FACTORY_RUNNER_STATE_ROOT": str(output / "source"),
    }
    server_env = common | {
        "FACTORY_DATABASE_URL": db,
        "FACTORY_PORT": str(port),
        "FACTORY_SOURCE_RUNNER_URL": source_url,
        "FACTORY_SOURCE_RUNNER_TOKEN": source_token,
        "FACTORY_ALLOWED_REMOTES": REMOTE,
        "FACTORY_ARTIFACT_ROOT": str(output / "artifacts"),
    }

    def admin(*arguments):
        return json.loads(
            subprocess.check_output(
                [sys.executable, "-m", "software_factory.cli", *arguments], cwd=ROOT, env=server_env
            )
        )

    with ExitStack() as stack:
        source_process = start(
            stack, output, "source", "software_factory.source_runner", source_env
        )
        wait_ready(source_url + "/openapi.json", source_process)
        with httpx.Client(
            base_url=source_url,
            timeout=120,
            trust_env=False,
            headers={"Authorization": f"Bearer {source_token}"},
        ) as source:
            base = post(
                source, "/v1/sources/resolve", {"remote_url": REMOTE, "base_ref": args.base_ref}
            )
        account = admin("bootstrap", "--name", "Software Factory")
        workspace = account["workspace_id"]
        database = Database(db)
        with database.transaction() as session:
            session.get(Workspace, workspace).codex_worker_count = args.codex_workers
        database.engine.dispose()
        codex_root = {}
        if args.codex_login_binary:
            codex_root["FACTORY_CODEX_AUTH_ROOT"] = str(output / "codex-account")
            server_env.update(
                codex_root,
                FACTORY_CODEX_WORKSPACE_ID=workspace,
                FACTORY_CODEX_LOGIN_BINARY=str(Path(args.codex_login_binary).resolve()),
                FACTORY_CODEX_AUTH_FILE=str(engine.auth_file),
            )
        server = start(stack, output, "server", "software_factory.cli", server_env, "serve")
        wait_ready(url + "/healthz", server)
        pricing = json.loads((ROOT / "config/codex_pricing.json").read_text())
        policy = {
            "agent_adapter": "codex_cli",
            "allow_external_model": True,
            "agent_configuration": {"version": "1", "engine_manifest_hash": engine.manifest_hash},
            "pricing_manifest": pricing,
            "allowed_read_paths": ["."],
            "allowed_write_paths": ["."],
            "protected_paths": [],
            "delivery_mode": "local_candidate",
            "verification": {
                "profile_id": "self-host-project",
                "required_checks": [CHECK, "review"],
            },
            "verification_commands": [
                {
                    "check_id": CHECK,
                    "argv": CHECK_ARGV,
                    "expected_test_ids": ["test_project::test_project"],
                    "junit_path": ".factory/project.xml",
                    "timeout_seconds": 30,
                }
            ],
            "execution_image_digest": image,
            "run_budget_microusd": 10_000_000,
            "job_reservation_microusd": 1_000_000,
        }
        with httpx.Client(
            base_url=url,
            timeout=30,
            trust_env=False,
            headers={"Authorization": f"Bearer {account['user_token']}"},
        ) as client:
            project = post(
                client,
                f"/v1/workspaces/{workspace}/projects",
                {"name": "Software Factory 改善", "policy": policy},
            )
            repository = post(
                client,
                f"/v1/projects/{project['project_id']}/repositories",
                {
                    "name": "iwata-n/software-factory",
                    "base_ref": args.base_ref,
                    "base_sha": base["sha"],
                    "remote_url": REMOTE,
                },
            )
            worker_specs = [
                (
                    "pipeline",
                    ["ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"],
                    ["--reviewer", "codex_cli"],
                ),
            ]
            generations = [0] * len(worker_specs)

            def provision(index):
                adapter, kinds, extra = worker_specs[index]
                generations[index] += 1
                slot = f"{adapter}-{index + 1}"
                name = f"{slot}-{generations[index]}"
                env = (
                    common
                    | codex_root
                    | {
                        "FACTORY_RUNNER_IMAGE_DIGEST": image,
                        "FACTORY_RUNNER_STATE_ROOT": str(output / slot),
                        "FACTORY_CODEX_AUTH_FILE": str(engine.auth_file),
                    }
                )
                arguments = [
                    "--api-url",
                    url,
                    "--adapter",
                    adapter,
                    "--codex-binary",
                    str(args.codex_binary.resolve()),
                    "--codex-manifest",
                    str(args.codex_manifest.resolve()),
                    *extra,
                ]
                return start_managed_worker(
                    stack, output, name, env, server_env, workspace, url, kinds, arguments
                )

            workers = [provision(index) for index in range(len(worker_specs))]
            pool = start_codex_pool(
                stack,
                output,
                common
                | codex_root
                | {
                    "FACTORY_RUNNER_IMAGE_DIGEST": image,
                    "FACTORY_CODEX_AUTH_FILE": str(engine.auth_file),
                },
                server_env,
                workspace,
                url,
                args.codex_binary.resolve(),
                args.codex_manifest.resolve(),
                task_chat=bool(codex_root),
            )
        if args.admin_username:
            password = secrets.token_urlsafe(24)
            with httpx.Client(
                base_url=url,
                timeout=30,
                trust_env=False,
                headers={"Authorization": f"Bearer {account['user_token']}", "Origin": url},
            ) as client:
                post(
                    client,
                    "/v1/auth/accounts",
                    {
                        "username": args.admin_username,
                        "password": password,
                        "access": "ADMIN",
                    },
                )
            credential_path = output / "admin-password.txt"
            credential_path.write_text(password + "\n")
        else:
            operator = admin("issue-user", "--workspace-id", workspace, "--access", "OPERATOR")
            credential_path = output / "operator-token.txt"
            credential_path.write_text(operator["user_token"] + "\n")
        (output / "workspace.json").write_text(
            json.dumps(
                {
                    **project,
                    **repository,
                    "base_ref": args.base_ref,
                    "base_sha": base["sha"],
                }
            )
        )
        print(f"{url}/operations\n{credential_path}\n{base['sha']}", flush=True)

        def restart(index):
            if index == 0:
                return start(
                    stack, output, "source-restarted", "software_factory.source_runner", source_env
                )
            return provision(index - 1)

        monitor_services(server, [source_process, *workers], restart, tick=pool.tick)
        raise RuntimeError(f"Self-host service stopped. See logs in {output}")


if __name__ == "__main__":
    main()
