"""Run a loopback Software Factory workspace against its GitHub repository."""

import argparse
import json
import os
import secrets
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

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/iwata-n/software-factory.git"
TARGET = "docs/Operations_Recovery_Runbook.md"
CHECK = "runbook_smoke"
# The verifier is supplied by the operator policy, outside the candidate tree.
TEST = (
    "from pathlib import Path\n"
    "def test_runbook():\n"
    "    text=Path('/candidate/docs/Operations_Recovery_Runbook.md').read_text()\n"
    "    assert all(x in text for x in "
    "('## 1. 停止と失効', '## 2. 復元と公開照合', '## 演習記録'))\n"
)
PYTEST = (
    "import sys,pathlib,pytest; p=pathlib.Path('test_runbook.py'); "
    "p.write_text(sys.argv[1]); raise SystemExit(pytest.main([str(p), "
    "'--junitxml=.factory/runbook.xml','-q','-p','no:cacheprovider']))"
)


def start(stack, output, name, module, env, *args):
    log = stack.enter_context((output / f"{name}.log").open("wb"))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--codex-binary", required=True, type=Path)
    parser.add_argument("--codex-manifest", required=True, type=Path)
    parser.add_argument("--port", type=int)
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
        server = start(stack, output, "server", "software_factory.cli", server_env, "serve")
        wait_ready(url + "/healthz", server)
        account = admin("bootstrap", "--name", "Software Factory")
        workspace = account["workspace_id"]
        pricing = json.loads((ROOT / "config/codex_pricing.json").read_text())
        policy = {
            "agent_adapter": "codex_cli",
            "allow_external_model": True,
            "agent_configuration": {"version": "1", "engine_manifest_hash": engine.manifest_hash},
            "pricing_manifest": pricing,
            "allowed_write_paths": [TARGET],
            "verification": {
                "profile_id": "self-host-runbook",
                "required_checks": [CHECK, "review"],
            },
            "verification_commands": [
                {
                    "check_id": CHECK,
                    "argv": ["python", "-c", PYTEST, TEST],
                    "expected_test_ids": ["test_runbook::test_runbook"],
                    "junit_path": ".factory/runbook.xml",
                    "timeout_seconds": 30,
                }
            ],
            "execution_image_digest": image,
            "run_budget_microusd": 1_000_000,
            "job_reservation_microusd": 100_000,
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
            workers = []
            for adapter, kinds, extra in (
                ("codex_cli", ["PLAN", "IMPLEMENT", "REPAIR"], []),
                (
                    "pipeline",
                    ["ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"],
                    ["--reviewer", "codex_cli"],
                ),
            ):
                enrollment = admin(
                    "issue-enrollment",
                    "--workspace-id",
                    workspace,
                    "--managed",
                    "--usage-authority",
                    "--adapter",
                    "codex_cli",
                    "--kinds",
                    *kinds,
                )
                credential = post(
                    client,
                    "/v1/worker/enroll",
                    {
                        "token": enrollment["enrollment_token"],
                        "name": f"Self-host {adapter}",
                    },
                )
                workers.append(
                    start(
                        stack,
                        output,
                        adapter,
                        "software_factory.worker",
                        common
                        | {
                            "FACTORY_WORKER_TOKEN": credential["worker_token"],
                            "FACTORY_RUNNER_IMAGE_DIGEST": image,
                            "FACTORY_RUNNER_STATE_ROOT": str(output / adapter),
                            "FACTORY_CODEX_AUTH_FILE": str(engine.auth_file),
                        },
                        "--api-url",
                        url,
                        "--adapter",
                        adapter,
                        "--codex-binary",
                        str(args.codex_binary.resolve()),
                        "--codex-manifest",
                        str(args.codex_manifest.resolve()),
                        *extra,
                    )
                )
        operator = admin("issue-user", "--workspace-id", workspace, "--access", "OPERATOR")
        token_path = output / "operator-token.txt"
        token_path.write_text(operator["user_token"] + "\n")
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
        print(f"{url}/operations\n{token_path}\n{base['sha']}", flush=True)
        while all(process.poll() is None for process in [source_process, server, *workers]):
            time.sleep(1)
        raise RuntimeError(f"Self-host service stopped. See logs in {output}")


if __name__ == "__main__":
    main()
