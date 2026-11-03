"""Evaluate real Codex PLAN/IMPLEMENT/REVIEW through HTTP Workers and Linux verification.

Uses only the existing CLI ChatGPT login and a disposable PostgreSQL instance. Public GitHub
source is read. An optional fixed evaluation profile publishes a Draft PR and verifies GitHub CI.
"""

import argparse
import ast
import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from acceptance_support import source_manifest
from alembic import command
from alembic.config import Config
from evaluate_publication import EVALUATION_WORKFLOW, REPOSITORY, github, verify_live_ci
from sqlalchemy.engine import make_url

from software_factory.codex_adapter import CodexAdapter, archive_files
from software_factory.codex_engine import CodexCLI
from software_factory.evidence import classify_junit
from software_factory.sandbox import DockerRunner
from software_factory.worker import Supervisor

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/iwata-n/software-factory.git"
TARGET = "examples/evaluation/app.py"


PROFILES = {
    "empty": {
        "id": "EVAL-01",
        "function": "describe_items",
        "title": "空の一覧に説明文を追加",
        "objective": "Return データがありません for an empty list and preserve nonempty behavior.",
        "criteria": [
            "describe_items([]) returns exactly データがありません",
            "One and multiple items and stable_label retain existing behavior",
        ],
        "tests": ("test_empty", "test_one_item", "test_many_items"),
        "failing": ("test_empty",),
    },
    "count": {
        "id": "EVAL-02",
        "function": "parse_count",
        "title": "件数入力の形式を厳密に検証",
        "objective": "Parse exactly one to nine ASCII digits as an integer.",
        "criteria": [
            "parse_count accepts only one to nine ASCII digits and returns an integer including 0",
            (
                "Invalid lengths, non-ASCII digits, spaces and other invalid strings "
                "raise ValueError; stable_label is unchanged"
            ),
        ],
        "tests": (
            "test_valid[3-3]",
            "test_valid[0-0]",
            "test_valid[999999999-999999999]",
            "test_invalid[]",
            "test_invalid[-1]",
            "test_invalid[a]",
            "test_invalid[1.5]",
            "test_invalid[1000000000]",
            "test_invalid[ 3]",
            "test_invalid[3 ]",
            "test_invalid[\\uff11\\uff12]",
        ),
        "failing": (
            "test_invalid[-1]",
            "test_invalid[1000000000]",
            "test_invalid[ 3]",
            "test_invalid[3 ]",
            "test_invalid[\\uff11\\uff12]",
        ),
    },
    "spaces": {
        "id": "EVAL-03",
        "function": "normalize_spaces",
        "title": "半角スペースだけを整形",
        "objective": "Trim and collapse only U+0020 spaces, preserving all other characters.",
        "criteria": [
            "Leading and trailing ASCII spaces are removed and internal runs collapse to one",
            "Preserve tabs, newlines, ideographic spaces, Japanese characters and stable_label",
        ],
        "tests": (
            "test_ascii_space_only[ a  b -a b]",
            "test_ascii_space_only[-]",
            "test_ascii_space_only[   -]",
            "test_ascii_space_only[ \\u3042  \\u3044 -\\u3042 \\u3044]",
            "test_ascii_space_only[\\ta\\tb\\n-\\ta\\tb\\n]",
            "test_ascii_space_only[\\u3000a\\u3000-\\u3000a\\u3000]",
            "test_ascii_space_only[a    b-a b]",
        ),
        "failing": (
            "test_ascii_space_only[ a  b -a b]",
            "test_ascii_space_only[   -]",
            "test_ascii_space_only[ \\u3042  \\u3044 -\\u3042 \\u3044]",
            "test_ascii_space_only[a    b-a b]",
        ),
    },
}


def available_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def wait_ready(url, process):
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("evaluation service terminated")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError("evaluation service startup timeout")


def post(client, path, body):
    reply = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    if not reply.is_success:
        # Only the structured code is safe to report, never arbitrary provider bodies.
        raise RuntimeError(f"{path}: HTTP {reply.status_code}, {reply.json().get('code')}")
    return reply.json()


def probe_disconnect(cp_url, secret, engine):
    """Inject a Worker-to-CP transport failure only after the real CLI has started."""
    trace = {}
    process = None
    adapter = CodexAdapter(engine)
    started_at = time.monotonic()
    with httpx.HTTPTransport() as network:

        def route(request):
            nonlocal process
            path = request.url.path
            if path.endswith("/heartbeat") and engine._process is not None:
                process = engine._process
                trace["disconnect_seconds"] = time.monotonic() - started_at
                raise httpx.ConnectError("evaluation transport disconnected", request=request)
            if path.endswith(("/complete", "/fail", "/artifacts", "/events")):
                raise AssertionError("disconnected attempt tried to submit output")
            sent_at = time.monotonic()
            response = network.handle_request(request)
            if path.endswith("/heartbeat") and response.is_success:
                response.read()
                allowance = response.json()["lease_expires_in_seconds"]
                trace["confirmed_deadline_seconds"] = (
                    sent_at - started_at + allowance - min(10, allowance / 4)
                )
            return response

        with httpx.Client(
            base_url=cp_url,
            headers={"Authorization": "Bearer " + secret},
            transport=httpx.MockTransport(route),
        ) as client:
            try:
                Supervisor(client, adapter, heartbeat_interval=0.1).run_once()
            except httpx.ConnectError:
                pass
            else:
                raise RuntimeError("disconnect did not reach a running Codex process")
    result = adapter.result()
    if process is None or process.poll() is None or result is None or result.outcome != "CANCELLED":
        raise RuntimeError("disconnect did not confirm that the Codex CLI stopped")
    trace.update(
        process_exit_code=process.returncode,
        outcome=result.outcome,
        returned_seconds=time.monotonic() - started_at,
        mode="injected_worker_transport_error",
        usage_observed=bool(result.usage_deliveries),
    )
    if trace["returned_seconds"] >= trace["confirmed_deadline_seconds"]:
        raise RuntimeError("disconnect returned control after the confirmed lease deadline")
    return trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default="codex/sf17-publisher")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--task", choices=PROFILES, default="empty")
    parser.add_argument("--probe-disconnect", action="store_true")
    parser.add_argument("--actions-workflow-id", type=int)
    parser.add_argument("--fixture-base-sha")
    parser.add_argument("--codex-manifest", default="config/codex_cli.json")
    parser.add_argument(
        "--codex-binary",
        default=os.environ.get("FACTORY_CODEX_BINARY")
        or (
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        ),
    )
    args = parser.parse_args()
    profile = PROFILES[args.task]
    check_file = f"check_{args.task}"
    publish_live = args.actions_workflow_id is not None
    if publish_live and (
        args.actions_workflow_id <= 0
        or not args.fixture_base_sha
        or not args.base_ref.startswith("codex/factory-evaluation/")
        or args.probe_disconnect
    ):
        parser.error(
            "live publication requires a fixed evaluation base, workflow ID and no fault probe"
        )
    if args.fixture_base_sha and not publish_live:
        parser.error("--fixture-base-sha requires --actions-workflow-id")
    url = make_url(os.environ["FACTORY_DATABASE_URL"])
    if not str(url.query.get("host", "")).startswith("/private/tmp/factory-pg-"):
        raise SystemExit("use scripts/with_postgres.py; existing databases are not supported")
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    identity = source_manifest()
    engine = CodexCLI.from_file(args.codex_binary, ROOT / args.codex_manifest)
    image = os.environ["FACTORY_RUNNER_IMAGE_DIGEST"]
    report = {
        "result": "IN_PROGRESS",
        "evaluation_task": profile["id"],
        "started_at": datetime.now(UTC).isoformat(),
        "implementation": identity,
        "engine_manifest": engine.manifest.model_dump(),
        "engine_manifest_hash": engine.manifest_hash,
        "runner_image_digest": image,
        "scope": "real Codex CLI, live HTTP Workers, isolated Linux assembly and verification",
        "github_writes": 0,
        "notifications_sent": 0,
    }
    processes, known_secrets = [], []
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "TMPDIR",
            "CODEX_HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
        }
    }
    source_port, cp_port = available_port(), available_port()
    while cp_port == source_port:
        cp_port = available_port()
    cp_url, source_url = f"http://127.0.0.1:{cp_port}", f"http://127.0.0.1:{source_port}"
    source_secret = uuid4().hex + uuid4().hex
    known_secrets.append(source_secret)
    source_env = base_env | {
        "FACTORY_ALLOWED_REMOTES": REMOTE,
        "FACTORY_SOURCE_RUNNER_TOKEN": source_secret,
        "FACTORY_SOURCE_RUNNER_PORT": str(source_port),
        "FACTORY_RUNNER_IMAGE_DIGEST": image,
        "FACTORY_RUNNER_STATE_ROOT": str(output / "source-runner"),
    }
    cp_env = base_env | {
        "FACTORY_DATABASE_URL": os.environ["FACTORY_DATABASE_URL"],
        "FACTORY_PORT": str(cp_port),
        "FACTORY_SOURCE_RUNNER_URL": source_url,
        "FACTORY_SOURCE_RUNNER_TOKEN": source_secret,
        "FACTORY_ALLOWED_REMOTES": REMOTE,
        "FACTORY_ARTIFACT_ROOT": str(output / "artifacts"),
    }
    with ExitStack() as stack:
        try:
            for module, environment, name in [
                ("software_factory.source_runner", source_env, "source"),
                ("software_factory.cli", cp_env, "control-plane"),
            ]:
                log = stack.enter_context((output / (name + ".log")).open("wb"))
                argv = [sys.executable, "-m", module] + (
                    ["serve"] if name == "control-plane" else []
                )
                processes.append(
                    subprocess.Popen(argv, cwd=ROOT, env=environment, stdout=log, stderr=log)
                )
            wait_ready(source_url + "/openapi.json", processes[0])
            wait_ready(cp_url + "/healthz", processes[1])

            def admin(*arguments):
                result = subprocess.run(
                    [sys.executable, "-m", "software_factory.cli", *arguments],
                    cwd=ROOT,
                    env=cp_env,
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode:
                    raise RuntimeError("local administrative command failed")
                return json.loads(result.stdout)

            account = admin("bootstrap", "--name", "Real Codex evaluation")
            known_secrets.append(account["user_token"])
            user = stack.enter_context(
                httpx.Client(
                    base_url=cp_url,
                    timeout=180,
                    trust_env=False,
                    headers={"Authorization": "Bearer " + account["user_token"]},
                )
            )
            source = stack.enter_context(
                httpx.Client(
                    base_url=source_url,
                    timeout=180,
                    trust_env=False,
                    headers={"Authorization": "Bearer " + source_secret},
                )
            )
            fixed = post(
                source, "/v1/sources/resolve", {"remote_url": REMOTE, "base_ref": args.base_ref}
            )
            if publish_live and fixed["sha"] != args.fixture_base_sha:
                raise RuntimeError("Evaluation base does not match the fixed SHA")
            fetched = post(source, "/v1/sources/fetch", {"remote_url": REMOTE, "sha": fixed["sha"]})
            report["base_sha"] = fixed["sha"]
            files = archive_files(base64.b64decode(fetched["archive"]), fetched["source_digest"])
            protected = {
                "examples/evaluation/checks/" + name: hashlib.sha256(
                    files["examples/evaluation/checks/" + name][1]
                ).hexdigest()
                for name in [f"{check_file}.py", "check_compat.py", "load_target.py"]
            }
            check_prefix = f"examples.evaluation.checks.{check_file}::"
            test_ids = [check_prefix + name for name in profile["tests"]]
            test_ids += ["examples.evaluation.checks.check_compat::test_existing_label_api"]
            expected_failures = {check_prefix + name for name in profile["failing"]}
            verification = {
                "check_id": "unit_tests",
                "argv": [
                    "python",
                    "-m",
                    "pytest",
                    f"/candidate/examples/evaluation/checks/{check_file}.py",
                    "/candidate/examples/evaluation/checks/check_compat.py",
                    "--rootdir=/candidate",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--junitxml=.factory/unit.xml",
                ],
                "junit_path": ".factory/unit.xml",
                "timeout_seconds": 60,
                "expected_test_ids": test_ids,
            }
            policy = {
                "agent_adapter": "codex_cli",
                "allow_external_model": True,
                "agent_configuration": {
                    "version": "1",
                    "engine_manifest_hash": engine.manifest_hash,
                },
                "pricing_manifest": json.loads((ROOT / "config/codex_pricing.json").read_text()),
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
                    "profile_id": f"codex-{args.task}",
                    "required_checks": ["unit_tests", "review"],
                },
                "verification_commands": [verification],
                "execution_image_digest": image,
                "run_budget_microusd": 1_000_000,
                "job_reservation_microusd": 100_000,
            }
            if publish_live:
                workflow = files[EVALUATION_WORKFLOW][1]
                if workflow != (ROOT / EVALUATION_WORKFLOW).read_bytes():
                    raise RuntimeError("Remote evaluation workflow differs from reviewed fixture")
                protected[EVALUATION_WORKFLOW] = hashlib.sha256(workflow).hexdigest()
                policy["required_ci_checks"] = [
                    {
                        "provider": "github",
                        "app_id": 15368,
                        "check_name": f"factory-eval-{args.task}",
                        "github_workflow_id": args.actions_workflow_id,
                        "github_workflow_path": EVALUATION_WORKFLOW,
                    }
                ]
            os.environ["FACTORY_RUNNER_STATE_ROOT"] = str(output / "baseline-runner")
            runner = DockerRunner(image)
            baseline = runner.execute_candidate(
                {
                    "operation": "verify",
                    "candidate": {
                        "head_sha": fetched["sha"],
                        "tree_sha": fetched["tree"],
                        "source_digest": fetched["source_digest"],
                        "metadata_body": {
                            "file_manifest": {
                                name: hashlib.sha256(body).hexdigest()
                                for name, (_, body) in files.items()
                            },
                            "commit_object": fetched["commit_object"],
                        },
                    },
                    "profile": {"commands": [verification], "protected_manifest": protected},
                },
                base64.b64decode(fetched["archive"]),
                timeout=120,
            )
            observation = baseline.body["checks"][0]
            base_junit = base64.b64decode(observation["junit"])
            (output / "baseline-junit.xml").write_bytes(base_junit)
            baseline_verdict = classify_junit(
                base_junit, test_ids, observation["execution_state"], observation["exit_code"]
            )
            if (
                baseline_verdict["status"] != "FAIL"
                or set(baseline_verdict["failed_test_ids"]) != expected_failures
                or baseline_verdict["counts"]["errors"]
                or baseline_verdict["counts"]["skipped"]
            ):
                raise RuntimeError("base does not match the fixed expected test failures")
            report["baseline"] = baseline_verdict
            print(
                json.dumps(
                    {"stage": "baseline", "result": "expected FAIL", "base_sha": fetched["sha"]}
                ),
                flush=True,
            )
            project = post(
                user,
                f"/v1/workspaces/{account['workspace_id']}/projects",
                {"name": f"Real Codex {profile['id']} evaluation", "policy": policy},
            )
            repository = post(
                user,
                f"/v1/projects/{project['project_id']}/repositories",
                {
                    "name": "Public fixed evaluation source",
                    "base_ref": args.base_ref,
                    "base_sha": fixed["sha"],
                    "remote_url": REMOTE,
                },
            )
            spec = {
                "schema_version": 1,
                "task": {
                    "title": f"Codex: {profile['title']}",
                    "repository_id": repository["repository_id"],
                    "base_ref": args.base_ref,
                    "objective": profile["objective"],
                    "acceptance_criteria": [
                        {"id": f"AC-{i}", "description": criterion}
                        for i, criterion in enumerate(profile["criteria"], start=1)
                    ],
                    "constraints": [
                        "Only examples/evaluation/app.py may change; golden checks are immutable"
                    ],
                },
                "verification": policy["verification"],
            }
            (output / "task-spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False))
            (output / "policy.json").write_text(json.dumps(policy, indent=2, ensure_ascii=False))
            task = post(user, f"/v1/projects/{project['project_id']}/tasks", spec)
            run_id = post(user, f"/v1/tasks/{task['task_id']}/runs", {"spec_version": 1})["run_id"]
            report["run_id"] = run_id

            def enroll(kinds):
                enrolled = admin(
                    "issue-enrollment",
                    "--workspace-id",
                    account["workspace_id"],
                    "--adapter",
                    "codex_cli",
                    "--managed",
                    "--usage-authority",
                    "--kinds",
                    *kinds,
                )
                known_secrets.append(enrolled["enrollment_token"])
                result = post(
                    user,
                    "/v1/worker/enroll",
                    {"token": enrolled["enrollment_token"], "name": "Real Codex " + kinds[0]},
                )
                known_secrets.append(result["worker_token"])
                return result["worker_token"]

            model_secret = enroll(["PLAN", "IMPLEMENT", "REPAIR"])
            stage_secret = enroll(["ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"])

            def state():
                reply = user.get(f"/v1/runs/{run_id}")
                reply.raise_for_status()
                return reply.json()

            def worker(adapter, secret, cycle):
                log = stack.enter_context((output / f"{adapter}-{cycle}.jsonl").open("wb"))
                argv = [
                    sys.executable,
                    "-m",
                    "software_factory.worker",
                    "--api-url",
                    cp_url,
                    "--adapter",
                    adapter,
                    "--drain",
                    "--codex-binary",
                    args.codex_binary,
                    "--codex-manifest",
                    str(ROOT / args.codex_manifest),
                ]
                if adapter == "pipeline":
                    argv += ["--reviewer", "codex_cli"]
                environment = base_env | {
                    "FACTORY_WORKER_TOKEN": secret,
                    "FACTORY_RUNNER_IMAGE_DIGEST": image,
                    "FACTORY_RUNNER_STATE_ROOT": str(output / "stage-runner"),
                }
                assert "FACTORY_DATABASE_URL" not in environment
                process = subprocess.Popen(argv, cwd=ROOT, env=environment, stdout=log, stderr=log)
                processes.append(process)
                if process.wait(timeout=600):
                    raise RuntimeError("HTTP Worker process failed; see its private log")
                current = state()
                print(
                    json.dumps(
                        {
                            "stage": adapter,
                            "cycle": cycle,
                            "status": current["status"],
                            "phase": current["phase"],
                            "stop_reason": current["stop_reason"],
                        }
                    ),
                    flush=True,
                )

            for cycle in range(3):
                worker("codex_cli", model_secret, cycle)
                if state()["status"] == "BLOCKED":
                    break
                worker("pipeline", stage_secret, cycle)
                if state()["phase"] != "REPAIR":
                    break
            current = state()
            for name, suffix in [
                ("run", ""),
                ("graph", "/graph"),
                ("plan", "/plan"),
                ("costs", "/costs"),
                ("events", "/event-history"),
                ("artifacts", "/artifacts"),
            ]:
                reply = user.get(f"/v1/runs/{run_id}" + suffix)
                reply.raise_for_status()
                (output / (name + ".json")).write_text(
                    json.dumps(reply.json(), ensure_ascii=False, indent=2)
                )
            if not current["current_candidate_id"]:
                raise RuntimeError("real Codex did not produce an accepted Candidate")
            candidate_id = current["current_candidate_id"]
            checks = user.get(f"/v1/candidates/{candidate_id}/verification").json()
            (output / "verification.json").write_text(
                json.dumps(checks, ensure_ascii=False, indent=2)
            )
            candidate = user.get(f"/v1/candidates/{candidate_id}").json()
            (output / "candidate.json").write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2)
            )
            artifact_rows = user.get(f"/v1/runs/{run_id}/artifacts").json()["artifacts"]
            for artifact in artifact_rows:
                if artifact["status"] != "AVAILABLE":
                    continue
                content = user.get(f"/v1/artifacts/{artifact['id']}/content")
                content.raise_for_status()
                if hashlib.sha256(content.content).hexdigest() != artifact["stored_sha256"]:
                    raise RuntimeError("downloaded Artifact digest mismatch")
                if artifact["kind"] == "PATCH":
                    (output / (artifact["id"] + ".patch")).write_bytes(content.content)
                if artifact["kind"] == "JUNIT":
                    (output / (artifact["id"] + "-junit.xml")).write_bytes(content.content)
                if artifact["id"] == candidate["source_artifact_id"]:
                    final_files = archive_files(content.content, candidate["source_digest"])
                    before_ast = ast.parse(files[TARGET][1].decode())
                    after_ast = ast.parse(final_files[TARGET][1].decode())

                    def unrelated(tree):
                        return {
                            node.name: ast.dump(node)
                            for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name != profile["function"]
                        }

                    if unrelated(before_ast) != unrelated(after_ast):
                        raise RuntimeError("Codex changed a non-target function")
                    (output / "generated-app.py").write_bytes(final_files[TARGET][1])
            costs = user.get(f"/v1/runs/{run_id}/costs").json()
            if current["phase"] != "PUBLISH_DRAFT" or current["status"] == "SUCCEEDED":
                raise RuntimeError(
                    "real Codex Candidate did not pass Local Gate before publication"
                )
            if current["cost_state"] != "KNOWN" or current["reserved_microusd"] != 0:
                raise RuntimeError("model usage was not settled")
            if len(costs["receipts"]) < 3 or not all(r["final"] for r in costs["receipts"]):
                raise RuntimeError("PLAN/IMPLEMENT/REVIEW require separate measured usage receipts")
            if candidate["head_sha"] == fixed["sha"]:
                raise RuntimeError("candidate did not change source")
            if args.probe_disconnect:
                fault_spec = spec | {"task": spec["task"] | {"title": "Codex disconnect probe"}}
                fault_task = post(user, f"/v1/projects/{project['project_id']}/tasks", fault_spec)
                fault_run = post(
                    user, f"/v1/tasks/{fault_task['task_id']}/runs", {"spec_version": 1}
                )
                report["disconnect"] = probe_disconnect(cp_url, model_secret, engine)
                report["disconnect"]["run_id"] = fault_run["run_id"]
            if publish_live:
                published = admin(
                    "issue-publisher",
                    "--workspace-id",
                    account["workspace_id"],
                    "--repository-id",
                    repository["repository_id"],
                    "--name",
                    "Codex evaluation Publisher",
                )
                publisher_token = published["publisher_token"]
                known_secrets.append(publisher_token)
                credential = output / "publisher.token"
                credential.write_text(publisher_token)
                try:
                    dispatched = json.loads(
                        subprocess.check_output(
                            [
                                sys.executable,
                                "-m",
                                "software_factory.publisher",
                                "--control-plane-url",
                                cp_url,
                                "--publisher-token-file",
                                str(credential),
                                "--github-use-gh",
                                "--allowed-remote",
                                REMOTE,
                                "--state-dir",
                                str(output / "publisher"),
                                "--once",
                            ],
                            cwd=ROOT,
                            env=base_env,
                            timeout=180,
                        )
                    )
                finally:
                    credential.unlink(missing_ok=True)
                assert dispatched["state"] == "SUCCEEDED"
                publication = user.get(f"/v1/runs/{run_id}/publication").json()
                assert publication["pull_request"]["head_sha"] == candidate["head_sha"]
                integration = uuid4().hex
                github_token = subprocess.check_output(
                    ["gh", "auth", "token", "--hostname", "github.com"], text=True
                ).strip()
                webhook_secret = uuid4().hex + uuid4().hex
                known_secrets.extend([github_token, webhook_secret])
                cp_env["FACTORY_GITHUB_WEBHOOKS"] = json.dumps(
                    {
                        integration: {
                            "workspace_id": account["workspace_id"],
                            "repository_id": repository["repository_id"],
                            "github_repository_id": github("")["id"],
                            "github_repository": REPOSITORY,
                            "secret": webhook_secret,
                        }
                    }
                )
                cp_env["FACTORY_GITHUB_CI_READ_TOKENS"] = json.dumps({integration: github_token})

                def restart_control_plane():
                    processes[1].terminate()
                    processes[1].wait(timeout=10)
                    log = stack.enter_context((output / "control-plane-live-ci.log").open("ab"))
                    processes[1] = subprocess.Popen(
                        [sys.executable, "-m", "software_factory.cli", "serve"],
                        env=cp_env,
                        cwd=ROOT,
                        stdout=log,
                        stderr=log,
                    )
                    wait_ready(cp_url + "/healthz", processes[1])

                restart_control_plane()
                verify_live_ci(
                    user, run_id, publication, state, output, identity, restart_control_plane
                )
                report["publication"] = json.loads((output / "evaluation.json").read_text())
                report["publication"]["local_agent_and_reviewer"] = "codex_cli"
                report["github_write_requests"] = [
                    request
                    for observation in publication["observations"]
                    for request in observation["body"].get("provider_requests", [])
                    if request["method"] != "GET"
                ]
                report["github_writes"] = len(report["github_write_requests"])
                current = state()
            report.update(
                result="PASS",
                candidate_id=candidate_id,
                candidate_head_sha=candidate["head_sha"],
                final_phase=current["phase"],
                usage_receipts=len(costs["receipts"]),
                spent_microusd=current["spent_microusd"],
                currency_basis="budget_estimate",
                human_code_edits=0,
            )
        except Exception as error:
            message = str(error)
            for secret in known_secrets:
                message = message.replace(secret, "[REDACTED]")
            report.update(result="FAIL", error={"type": type(error).__name__, "message": message})
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for path in output.rglob("*"):
                if path.is_file() and any(
                    secret.encode() in path.read_bytes() for secret in known_secrets
                ):
                    raise RuntimeError("credential found in evaluation evidence")
            report["finished_at"] = datetime.now(UTC).isoformat()
            report["implementation_unchanged"] = (
                source_manifest()["code_digest"] == identity["code_digest"]
            )
            if not report["implementation_unchanged"]:
                report["result"] = "FAIL"
                report["error"] = {
                    "message": "implementation inputs changed during real evaluation"
                }
            (output / "evaluation.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in [
                    "result",
                    "run_id",
                    "candidate_id",
                    "final_phase",
                    "spent_microusd",
                    "error",
                ]
            },
            ensure_ascii=False,
        )
    )
    if report["result"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
