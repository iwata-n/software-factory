import base64
import io
import os
import socket
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from runner import entry
from software_factory.adapters import FakeAgent
from software_factory.models import Attempt, Job, Repository, Run
from software_factory.resolver import (
    HttpSourceResolver,
    ReferenceNotFound,
    ResolutionUnavailable,
    SourceReference,
    SourceRejected,
)
from software_factory.sandbox import DockerRunner, RunnerError
from software_factory.source_runner import SourcePolicy, create_source_app
from software_factory.worker import Supervisor
from tests.conftest import example_spec

from .helpers import worker_client
from .https_fixture import docker, endpoints

REMOTE = "https://github.com/iwata-n/software-factory.git"
PINNED = "a4b0266ea8a6bd4d714b3c6b56e7b2320766ec3c"


@pytest.mark.ac("SF-08-AC03", "SF-08-AC05")
@pytest.mark.parametrize("mode", ["redirect", "auth", "interrupted", "slow"])
def test_real_tls_redirect_auth_interruption_and_timeout(docker_runner, tmp_path, mode, evidence):
    with endpoints(tmp_path, docker_runner.image_digest, mode) as (runner, servers):
        with pytest.raises(RunnerError) as failed:
            runner.execute(
                {
                    "operation": "fetch",
                    "url": "https://first.fixture.test/owner/repo.git",
                    "ref": "main",
                },
                network=True,
                timeout=11 if mode == "slow" else 30,
            )
        first, second = [docker("logs", server) for server in servers]
        assert "REQUEST" in first
        assert "REQUEST" not in second
        expected = {
            "redirect": "SOURCE_REDIRECT_DENIED",
            "auth": "SOURCE_ACCESS_DENIED",
            "interrupted": "SOURCE_FETCH_FAILED",
            "slow": "RUNNER_TIMEOUT",
        }[mode]
        assert failed.value.code == expected
        evidence(
            "real TLS failures classified; redirect target contacted zero times",
            {
                "failure": failed.value.code,
                "category": failed.value.category,
                "first_requests": first.count("REQUEST"),
                "second_requests": second.count("REQUEST"),
            },
            image_digest=runner.image_digest,
        )


@pytest.fixture(scope="module")
def docker_runner():
    image = os.environ.get("FACTORY_RUNNER_IMAGE_DIGEST")
    if not image:
        result = subprocess.run(
            ["docker", "image", "inspect", "software-factory-runner:dev", "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, "Build runner/Dockerfile or set FACTORY_RUNNER_IMAGE_DIGEST"
        image = result.stdout.strip()
    return DockerRunner(image)


@pytest.fixture(scope="module")
def real_source(docker_runner):
    token = "acceptance-source-runner-credential"
    app = create_source_app(SourcePolicy((REMOTE,)), docker_runner, token)
    client = HttpSourceResolver("http://127.0.0.1", token, client=TestClient(app))
    return client


@pytest.mark.ac("SF-08-AC01")
def test_real_https_branch_and_pinned_fetch(real_source, evidence):
    reference = SourceReference("fixture", "codex/sf01-07-acceptance", "unused", PINNED, REMOTE)
    branch_sha = real_source.resolve(reference)
    fetched = real_source.fetch(REMOTE, PINNED)
    assert fetched["sha"] == PINNED
    archive = base64.b64decode(fetched["archive"], validate=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        assert "src/software_factory/service.py" in bundle.getnames()
        assert bundle.extractfile(".python-version").read().strip() == b"3.11"
    assert len(fetched["source_digest"]) == 64 and len(fetched["tree"]) == 40
    evidence(
        "real HTTPS branch resolves; fetch uses exact pinned SHA and immutable tree",
        {k: v for k, v in fetched.items() if k != "archive"},
        remote=REMOTE,
        branch_sha=branch_sha,
        source_sha=PINNED,
    )


@pytest.mark.ac("SF-08-AC01")
def test_real_https_tag_resolution(docker_runner, evidence):
    remote = "https://github.com/pallets/itsdangerous.git"
    token = "acceptance-source-runner-credential"
    resolver = HttpSourceResolver(
        "http://127.0.0.1",
        token,
        client=TestClient(create_source_app(SourcePolicy((remote,)), docker_runner, token)),
    )
    tag_sha = resolver.resolve(SourceReference("tag-fixture", "2.2.0", "unused", PINNED, remote))
    fetched = resolver.fetch(remote, tag_sha)
    assert fetched["sha"] == tag_sha
    evidence(
        "real HTTPS tag dereferenced to commit and fetched by full SHA",
        {k: v for k, v in fetched.items() if k != "archive"},
        remote=remote,
        tag="2.2.0",
    )


@pytest.mark.ac("SF-08-AC02")
def test_moving_bare_ref_never_substitutes_new_commit(tmp_path, monkeypatch, evidence):
    # A file transport override exists only in this trusted fixture, never in the runner API.
    origin = tmp_path / "origin.git"
    working = tmp_path / "author"
    working.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(working)], check=True)

    def author(*args):
        return (
            subprocess.check_output(
                ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", *args],
                cwd=working,
            )
            .decode()
            .strip()
        )

    (working / "file").write_text("A")
    author("add", ".")
    author("commit", "-qm", "A")
    a = author("rev-parse", "HEAD")
    author("push", "-q", str(origin), "HEAD:refs/heads/main")
    author("checkout", "--orphan", "new-root")
    (working / "file").write_text("B")
    author("add", ".")
    author("commit", "-qm", "B")
    b = author("rev-parse", "HEAD")
    author("push", "-q", "--force", str(origin), "HEAD:refs/heads/main")
    original_git = entry.git
    monkeypatch.setattr(entry, "GIT", entry.GIT + ["-c", "protocol.file.allow=always"])

    def fixture_transport(*args, **kwargs):
        return original_git(
            *(str(origin) if arg == "https://fixture.test/owner/repo.git" else arg for arg in args),
            **kwargs,
        )

    monkeypatch.setattr(entry, "git", fixture_transport)
    outputs = []
    for ref in (a, "main"):
        directory = tmp_path / ("fetch-" + ref)
        directory.mkdir()
        monkeypatch.chdir(directory)
        outputs.append(
            entry.fetch(
                {
                    "url": "https://fixture.test/owner/repo.git",
                    "ref": ref,
                    **({"sha": a} if ref == a else {}),
                }
            )
        )
    assert [item["sha"] for item in outputs] == [a, b]
    assert outputs[0]["source_digest"] != outputs[1]["source_digest"]
    subprocess.run(
        ["git", "--git-dir=" + str(origin), "reflog", "expire", "--expire=now", "--all"], check=True
    )
    subprocess.run(
        ["git", "--git-dir=" + str(origin), "gc", "--prune=now"], check=True, capture_output=True
    )
    missing = tmp_path / "missing"
    missing.mkdir()
    monkeypatch.chdir(missing)
    with pytest.raises(entry.Refused) as refused:
        entry.fetch({"url": "https://fixture.test/owner/repo.git", "ref": a, "sha": a})
    assert refused.value.code in {"SOURCE_REF_NOT_FOUND", "SOURCE_FETCH_FAILED"}
    evidence(
        "moving refs affect new runs only; missing pinned SHA never falls forward",
        {"pinned": a, "new": b, "missing_result": refused.value.code},
    )


@pytest.mark.ac("SF-08-AC03")
@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/repo",
        "ssh://git@github.com/iwata-n/software-factory",
        "git://github.com/a/b",
        "ext::touch /tmp/canary",
        "https://user:pass@github.com/iwata-n/software-factory",
        "https://@github.com/iwata-n/software-factory",
        "https://github.com.evil.test/iwata-n/software-factory",
        "https://github.com/another/repo",
        "https://github.com/iwata-n/software-factory/../other",
        "https://github.com/iwata-n/software-factory?token=canary",
        "https://github.com:8443/iwata-n/software-factory",
        "https://github.com/iwata-n/software-factory#fragment",
        "https://github.com/iwata-n%2fother/repo",
    ],
)
def test_forbidden_url_never_reaches_runner(url, evidence):
    class NeverRunner:
        def execute(self, *args, **kwargs):
            raise AssertionError("forbidden destination contacted")

    token = "acceptance-source-runner-credential"
    app = create_source_app(SourcePolicy((REMOTE,)), NeverRunner(), token)
    reply = TestClient(app).post(
        "/v1/sources/resolve",
        json={"remote_url": url, "base_ref": "main"},
        headers={"Authorization": "Bearer " + token},
    )
    assert reply.status_code == 422
    evidence("forbidden URL rejected before any runtime/network operation", reply.status_code)


@pytest.mark.ac("SF-08-AC03", "C-02")
def test_registration_is_allowlisted_and_workspace_scoped(world, monkeypatch, evidence):
    monkeypatch.setenv("FACTORY_ALLOWED_REMOTES", REMOTE)
    body = {"name": "real source", "base_sha": PINNED, "base_ref": "main", "remote_url": REMOTE}
    path = f"/v1/projects/{world.project_id}/repositories"
    accepted = world.post(path, body)
    assert accepted.status_code == 201
    assert world.post(path, body, token=world.other_user_token).status_code == 404
    assert world.post(path, body | {"remote_url": "https://evil.test/a/b"}).status_code == 422
    evidence("repository allowlist and project ownership checked before save", accepted.json())


@pytest.mark.ac("SF-08-AC04")
@pytest.mark.parametrize("malicious", ["submodule", "lfs", "hook_and_filters", "global_config"])
def test_repository_metadata_cannot_execute_commands(tmp_path, monkeypatch, evidence, malicious):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q", "."], check=True)
    canary = tmp_path / "executed-canary"
    if malicious == "submodule":
        (tmp_path / ".gitmodules").write_text("[submodule 'x']\npath=x\nurl=https://evil.test/x/y")
    elif malicious == "lfs":
        (tmp_path / "file").write_text(
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 1\n"
        )
    else:
        (tmp_path / "file").write_text("ordinary source")
        if malicious == "hook_and_filters":
            hook = tmp_path / ".git/hooks/post-checkout"
            hook.write_text("#!/bin/sh\ntouch '" + str(canary) + "'\n")
            hook.chmod(0o755)
            subprocess.run(
                ["git", "config", "filter.evil.smudge", "touch '" + str(canary) + "'"], check=True
            )
            (tmp_path / ".gitattributes").write_text("file filter=evil export-subst\n")
        else:
            config = tmp_path / "outside-config"
            config.write_text("[core]\nfsmonitor = touch '" + str(canary) + "'\n")
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "add", "."],
        check=True,
        env={k: v for k, v in os.environ.items() if k != "GIT_CONFIG_GLOBAL"},
    )
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@localhost",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        env={k: v for k, v in os.environ.items() if k != "GIT_CONFIG_GLOBAL"},
    )
    if malicious in {"submodule", "lfs"}:
        with pytest.raises(entry.Refused) as refused:
            entry.records("HEAD")
        result = refused.value.code
    else:
        result = entry.pack(entry.records("HEAD"))[0]
    assert not canary.exists()
    evidence(
        "submodule/LFS blocked; hooks, filters and inherited config never execute",
        result,
        malicious=malicious,
    )


@pytest.mark.ac("SF-08-AC04")
@pytest.mark.parametrize(
    "path", ["../outside", "/outside", "a/../../outside", ".git/config", "a\\b"]
)
def test_source_tree_path_escape_rejected(monkeypatch, evidence, path):
    monkeypatch.setattr(
        entry, "git", lambda *args: b"100644 blob " + b"a" * 40 + b"\t" + path.encode() + b"\0"
    )
    with pytest.raises(entry.Refused):
        entry.records("sha")
    evidence("invalid source path rejected before blob extraction", "POLICY_DENIED", path=path)


@pytest.mark.ac("SF-08-AC05")
@pytest.mark.parametrize(
    "failure, category, status",
    [
        ("SOURCE_ACCESS_DENIED", "POLICY", 422),
        ("SOURCE_REF_NOT_FOUND", "POLICY", 422),
        ("SOURCE_FETCH_FAILED", "INFRASTRUCTURE", 503),
        ("RUNNER_TIMEOUT", "INFRASTRUCTURE", 503),
    ],
)
def test_source_failures_classified_without_raw_secrets(failure, category, status, evidence):
    class FailedRunner:
        def execute(self, *args, **kwargs):
            raise RunnerError(failure, category)

    token = "acceptance-source-runner-credential"
    reply = TestClient(create_source_app(SourcePolicy((REMOTE,)), FailedRunner(), token)).post(
        "/v1/sources/resolve",
        json={"remote_url": REMOTE, "base_ref": "main"},
        headers={"Authorization": "Bearer " + token},
    )
    assert reply.status_code == status and reply.json()["code"] == failure
    evidence(
        "policy/ref faults 422; transient infrastructure 503; no raw command output", reply.json()
    )


@pytest.mark.ac("SF-08-AC06")
def test_fetch_outside_transaction_and_revocation_wins(world, evidence):
    service = world.client.app.state.service
    assignment = world.claim().json()
    started, release = Event(), Event()

    class SlowSource:
        def fetch(self, remote, sha):
            started.set()
            assert release.wait(5)
            return {"sha": sha, "archive": "", "source_digest": "a" * 64}

    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.snapshot = run.snapshot | {"source": {"remote_url": REMOTE, "base_sha": PINNED}}
    service.resolver = SlowSource()
    path = (
        f"/v1/worker/leases/{assignment['lease']['id']}/source?fence={assignment['lease']['fence']}"
    )
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(world.get, path, token=world.worker_token)
        assert started.wait(3)
        with world.database.transaction() as s:
            s.execute(text("SET LOCAL lock_timeout = '200ms'"))
            s.get(Repository, world.repository_id).active = False
        release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 409
    assert "archive" not in response.json()
    evidence("no DB lock during fetch and revoked source result is discarded", response.json())


@pytest.mark.ac("SF-08-AC06", "SF-08-AC05")
def test_remote_resolution_failure_creates_no_run_or_job(world, evidence):
    service = world.client.app.state.service

    class BrokenSource:
        def resolve(self, reference):
            raise ReferenceNotFound

    service.resolver = BrokenSource()
    with world.database.transaction() as s:
        counts = [s.scalar(select(func.count()).select_from(model)) for model in (Run, Job)]
    response = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
    assert response.status_code == 422
    with world.database.transaction() as s:
        assert [s.scalar(select(func.count()).select_from(model)) for model in (Run, Job)] == counts
    evidence(
        "resolution refusal records failure but creates no executable run/job", response.json()
    )


@pytest.mark.ac("SF-08-AC01", "SF-08-AC06", "C-02")
def test_real_source_process_keeps_git_out_of_control_plane(
    world, docker_runner, monkeypatch, evidence
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = "acceptance-source-process-credential"
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    }
    env.update(
        FACTORY_ALLOWED_REMOTES=REMOTE,
        FACTORY_RUNNER_IMAGE_DIGEST=docker_runner.image_digest,
        FACTORY_SOURCE_RUNNER_TOKEN=token,
        FACTORY_SOURCE_RUNNER_PORT=str(port),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "software_factory.source_runner"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(base + "/openapi.json", timeout=0.1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("source process did not start")
        service = world.client.app.state.service
        service.resolver = HttpSourceResolver(base, token)
        world.post(
            f"/v1/runs/{world.run_id}/cancel",
            {"expected_run_revision": world.run()["state_revision"], "reason": "use remote source"},
        )
        with world.database.transaction() as s:
            s.get(Repository, world.repository_id).remote_url = REMOTE
        spec = example_spec(world.repository_id)
        spec["task"]["base_ref"] = "codex/sf01-07-acceptance"
        assert world.post(f"/v1/tasks/{world.task_id}/spec-versions", spec).status_code == 201

        def forbidden_spawn(*args, **kwargs):
            raise AssertionError("Control Plane tried to launch a process")

        with monkeypatch.context() as scoped:
            scoped.setattr(subprocess, "Popen", forbidden_spawn)
            run = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 2})
            assert run.status_code == 202, run.text
            world.run_id = run.json()["run_id"]
            assigned = world.claim().json()
            path = (
                f"/v1/worker/leases/{assigned['lease']['id']}/source"
                f"?fence={assigned['lease']['fence']}"
            )
            fetched = world.get(path, token=world.worker_token)
            assert fetched.status_code == 200, fetched.text
            assert world.client.get(path).status_code == 401
            assert world.get(path).status_code == 403
        body = fetched.json()
        assert body["sha"] == run.json()["base_sha"] == assigned["job"]["base_sha"]
        assert body["isolation"]["supervisor_pid"] == process.pid != os.getpid()
        assert body["isolation"]["mounts"] == []
        assert not {
            "FACTORY_DATABASE_URL",
            "FACTORY_USER_TOKEN",
            "FACTORY_WORKER_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        } & set(body["isolation"]["environment_keys"])
        evidence(
            "real source RPC uses separate PID/container and CP cannot spawn Git",
            {k: v for k, v in body.items() if k not in {"archive", "commit_object"}},
            cp_pid=os.getpid(),
            source_pid=process.pid,
            run_id=world.run_id,
            attempt_id=assigned["job"]["attempt_id"],
            environment_keys=sorted(env),
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)


@pytest.mark.ac("SF-08-AC05")
@pytest.mark.parametrize("category", ["INFRASTRUCTURE", "POLICY"])
def test_fetch_failure_uses_same_job_bounded_retries(world, clock, category, evidence):
    class FailedRunner:
        def execute(self, *args, **kwargs):
            raise RunnerError(
                "SOURCE_FETCH_FAILED" if category == "INFRASTRUCTURE" else "SOURCE_ACCESS_DENIED",
                category,
            )

    token = "acceptance-source-failure-credential"
    service = world.client.app.state.service
    service.resolver = HttpSourceResolver(
        "http://127.0.0.1",
        token,
        client=TestClient(create_source_app(SourcePolicy((REMOTE,)), FailedRunner(), token)),
    )
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.snapshot = run.snapshot | {"source": {"remote_url": REMOTE, "base_sha": run.base_sha}}
    jobs = []
    for number in range(3 if category == "INFRASTRUCTURE" else 1):
        assigned = world.claim().json()
        jobs.append(assigned["job"]["id"])
        lease = assigned["lease"]
        response = world.get(
            f"/v1/worker/leases/{lease['id']}/source?fence={lease['fence']}",
            token=world.worker_token,
        )
        assert response.status_code == (503 if category == "INFRASTRUCTURE" else 409)
        failed = world.post(
            f"/v1/worker/leases/{lease['id']}/fail",
            {"fence": lease["fence"], "category": category, "reason": response.json()["code"]},
            token=world.worker_token,
        )
        assert failed.status_code == 200
        if category == "INFRASTRUCTURE" and number < 2:
            assert world.claim().status_code == 204
            clock.advance(2 ** (number + 1))
    assert len(set(jobs)) == 1
    assert world.claim().status_code == 204
    with world.database.transaction() as s:
        attempts = list(s.scalars(select(Attempt)))
        assert len(attempts) == len(jobs)
        assert len({a.input_digest for a in attempts}) == 1
        assert all(j.kind != "REPAIR" for j in s.scalars(select(Job)))
    assert world.run()["status"] == ("FAILED" if category == "INFRASTRUCTURE" else "BLOCKED")
    evidence(
        "source infrastructure retries same input 3 times with 2/4s waits; policy denial blocks",
        {"job_ids": jobs, "attempts": len(attempts), "status": world.run()["status"]},
    )


@pytest.mark.ac("SF-08-AC01", "SF-08-AC05", "SF-08-AC06")
@pytest.mark.parametrize("scenario", ["success", "infrastructure", "permission"])
def test_supervisor_prepares_pinned_source_before_agent(world, real_source, scenario, evidence):
    class SpyAgent(FakeAgent):
        starts = 0

        def start(self, job):
            self.starts += 1
            assert job["source"]["sha"] == PINNED
            super().start(job)

    class Source:
        def fetch(self, remote, sha):
            if scenario == "infrastructure":
                raise ResolutionUnavailable
            if scenario == "permission":
                raise SourceRejected("SOURCE_ACCESS_DENIED")
            return real_source.fetch(remote, sha)

    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        run.base_sha = PINNED
        run.snapshot = run.snapshot | {
            "base_sha": PINNED,
            "source": {"remote_url": REMOTE, "base_sha": PINNED},
        }
    world.client.app.state.service.resolver = Source()
    client, trace = worker_client(world)
    agent = SpyAgent()
    result = Supervisor(client, agent, heartbeat_interval=0.1).run_once()
    assert result["accepted"] is True
    assert agent.starts == (1 if scenario == "success" else 0)
    expected = {
        "success": ("QUEUED", "IMPLEMENT"),
        "infrastructure": ("QUEUED", "PLAN"),
        "permission": ("BLOCKED", "PLAN"),
    }[scenario]
    run = world.run()
    assert (run["status"], run["phase"]) == expected
    assert any(path.endswith("/source") for path in trace["paths"])
    if scenario == "success":
        Supervisor(client, agent, heartbeat_interval=0.1).run_once()
        assert agent.starts == 2
        assert world.run()["phase"] == "ASSEMBLE_CANDIDATE"
    evidence(
        "real Supervisor gets fixed source before start; fetch failure never starts Agent",
        {
            "scenario": scenario,
            "starts": agent.starts,
            "status": run["status"],
            "phase": run["phase"],
            "paths": trace["paths"],
        },
        run_id=world.run_id,
    )


@pytest.mark.ac("SF-08-AC06", "SF-06-AC04")
def test_hanging_source_transport_never_starts_agent_past_deadline(evidence):
    from tests.test_worker import assignment

    release, finished = Event(), Event()
    starts = []
    agent = FakeAgent()
    agent.start = lambda job: starts.append(job)
    reports = []

    def handler(request):
        if request.url.path.endswith("/claims"):
            body = assignment()
            body["job"]["base_sha"] = PINNED
            body["job"]["snapshot"]["source"] = {"remote_url": REMOTE, "base_sha": PINNED}
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200, json={"lease_expires_in_seconds": 0.4, "cancel_requested": False}
            )
        if request.url.path.endswith("/source"):
            assert release.wait(3)
            finished.set()
            return httpx.Response(200, json={"sha": PINNED})
        reports.append(request.url.path)
        return httpx.Response(200, json={"accepted": True})

    client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler))
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            Supervisor(client, agent).run_once()
        assert not starts and not reports
        assert agent.result().outcome == "CANCELLED"
    finally:
        release.set()
        assert finished.wait(2)
    assert not starts
    evidence(
        "a source request that ignores timeout cannot start an Agent after confirmed expiry",
        {"starts": 0, "completion_submissions": 0, "elapsed": time.monotonic() - started},
    )


@pytest.mark.ac("SF-08-AC04", "SF-04-AC04")
def test_inspected_remote_symlink_metadata_blocks_plan(world, evidence):
    class InspectedSource:
        def resolve_details(self, reference):
            return {
                "sha": PINNED,
                "tree": "b" * 40,
                "source_digest": "c" * 64,
                "symlink_paths": ["src/linked"],
                "image_digest": "sha256:" + "0" * 64,
            }

    service = world.client.app.state.service
    service.resolver = InspectedSource()
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "inspect remote links"},
    )
    with world.database.transaction() as s:
        s.get(Repository, world.repository_id).remote_url = REMOTE
    run = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()
    world.run_id = run["run_id"]
    assigned = world.claim().json()
    plan = {
        "units": [
            {
                "id": "u1",
                "objective": "write through link",
                "acceptance_ids": ["AC-1"],
                "proposed_write_paths": ["src/linked/new.py"],
            }
        ]
    }
    reply = world.post(
        f"/v1/worker/leases/{assigned['lease']['id']}/complete",
        {"fence": assigned["lease"]["fence"], "outcome": "EXECUTED", "result": plan},
        token=world.worker_token,
    )
    assert reply.status_code == 200
    assert reply.json()["run"]["stop_reason"] == "PLAN_SYMLINK_PATH"
    assert world.claim().status_code == 204
    evidence(
        "trusted source inspection is pinned and checked instead of Agent symlink claims",
        reply.json()["run"],
        run_id=world.run_id,
    )
