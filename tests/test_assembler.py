import base64
import hashlib
import io
import os
import subprocess
import tarfile

import pytest

from runner import entry
from software_factory.sandbox import DockerRunner, RunnerError


@pytest.fixture
def source_bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "checks").mkdir()
    (tmp_path / "src/example.py").write_bytes(b"value = 1\n")
    (tmp_path / "checks/golden.py").write_bytes(b"# immutable required check\n")
    entry.git("init", "-q", ".")
    entry.git("add", ".")
    entry.git("commit", "-qm", "base")
    sha = entry.git("rev-parse", "HEAD").decode().strip()
    code, archive, _ = entry.pack(entry.records(sha))
    return {
        "sha": sha,
        "tree": entry.git("rev-parse", sha + "^{tree}").decode().strip(),
        "source_digest": code,
        "archive": base64.b64encode(archive).decode(),
        "commit_object": base64.b64encode(entry.git("cat-file", "commit", sha)).decode(),
    }


@pytest.fixture(scope="module")
def runner():
    image = (
        os.environ.get("FACTORY_RUNNER_IMAGE_DIGEST")
        or subprocess.check_output(
            ["docker", "image", "inspect", "software-factory-runner:dev", "--format", "{{.Id}}"],
            text=True,
        ).strip()
    )
    return DockerRunner(image)


def request(source, patch):
    return {
        "operation": "assemble",
        "source": source,
        "patch": base64.b64encode(patch).decode(),
        "allowed_write_paths": ["src", "checks"],
        "protected_paths": ["checks"],
        "protected_manifest": {
            "checks/golden.py": hashlib.sha256(b"# immutable required check\n").hexdigest()
        },
    }


PATCH = b"--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"


def test_real_assembler_exact_content_parent_reproducibility_and_isolation(source_bundle, runner):
    first = runner.execute(request(source_bundle, PATCH))
    second = runner.execute(request(source_bundle, PATCH))
    body = first.body
    assert body["head_sha"] == second.body["head_sha"]
    assert body["code_digest"] == second.body["code_digest"]
    assert body["base_sha"] == source_bundle["sha"]
    assert body["base_source_digest"] == source_bundle["source_digest"]
    assert body["changed_paths"] == ["src/example.py"]
    with tarfile.open(
        fileobj=io.BytesIO(base64.b64decode(body["archive"])), mode="r:gz"
    ) as archive:
        assert archive.extractfile("src/example.py").read() == b"value = 2\n"
        assert archive.extractfile("checks/golden.py").read() == b"# immutable required check\n"
    commit = base64.b64decode(body["commit_object"])
    assert b"parent " + source_bundle["sha"].encode() + b"\n" in commit
    assert (
        hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
        == body["head_sha"]
    )
    actual = first.isolation
    assert actual["network"] == "none" and actual["read_only_root"] is True
    assert actual["user"] == "65532:65532" and actual["mounts"] == []
    assert actual["cap_drop"] == ["ALL"] and "no-new-privileges" in actual["security_opt"]
    assert actual["image"] == runner.image_digest


@pytest.mark.parametrize(
    "attack",
    ["outside", "protected", "binary", "traversal", "git_config", "base_digest", "base_sha"],
)
def test_real_assembler_refuses_untrusted_changes(source_bundle, runner, attack):
    patch = PATCH
    if attack == "outside":
        patch = b"--- /dev/null\n+++ b/outside.py\n@@ -0,0 +1 @@\n+bad\n"
    elif attack == "protected":
        patch = (
            b"--- a/checks/golden.py\n+++ b/checks/golden.py\n@@ -1 +1 @@\n"
            b"-# immutable required check\n+# disabled\n"
        )
    elif attack == "binary":
        patch = b"GIT binary patch\nliteral 0\n"
    elif attack == "traversal":
        patch = b"--- /dev/null\n+++ b/../../outside\n@@ -0,0 +1 @@\n+bad\n"
    elif attack == "git_config":
        patch = b"--- /dev/null\n+++ b/.git/config\n@@ -0,0 +1 @@\n+bad\n"
    elif attack == "base_digest":
        source_bundle = source_bundle | {"source_digest": "0" * 64}
    elif attack == "base_sha":
        source_bundle = source_bundle | {"sha": "0" * 40}
    with pytest.raises(RunnerError) as failed:
        runner.execute(request(source_bundle, patch))
    assert failed.value.category == "POLICY"


def test_unconfirmed_cleanup_quarantines_real_managed_runner(source_bundle, runner, monkeypatch):
    isolated = DockerRunner(runner.image_digest)
    docker = isolated.docker
    pending = []

    def lose_remove(*args, **kwargs):
        if args[:2] == ("rm", "--force"):
            name = args[2]
            inspect = docker("inspect", name, "--format", "{{.State.Status}}", timeout=5)
            assert inspect.returncode == 0 and inspect.stdout.strip() == b"exited"
            pending.append(name)
            return subprocess.CompletedProcess(args, 1, b"", b"injected Docker remove failure")
        return docker(*args, **kwargs)

    monkeypatch.setattr(isolated, "docker", lose_remove)
    try:
        with pytest.raises(RunnerError, match="^STOP_UNCONFIRMED$"):
            isolated.execute(request(source_bundle, PATCH))
        assert len(pending) == 1
        with pytest.raises(RunnerError, match="^STOP_UNCONFIRMED$"):
            isolated.execute(request(source_bundle, PATCH))
        assert len(pending) == 1
    finally:
        for name in pending:
            assert docker("rm", "--force", name, timeout=7).returncode == 0


def test_restart_refuses_uninspectable_managed_container(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_RUNNER_STATE_ROOT", str(tmp_path / "state"))
    previous = DockerRunner(runner.image_digest)
    docker = DockerRunner.docker
    started = docker(
        "run",
        "--detach",
        "--network",
        "none",
        "--label",
        f"factory.namespace={previous.namespace}",
        "--label",
        f"factory.owner={previous.owner}",
        "--entrypoint",
        "sleep",
        runner.image_digest,
        "60",
        timeout=10,
    )
    assert started.returncode == 0, started.stderr.decode()
    container = started.stdout.decode().strip()
    del previous

    def lose_inspect(*args, **kwargs):
        if args[0] == "inspect" and container.startswith(args[1]):
            return subprocess.CompletedProcess(args, 1, b"", b"injected inspect failure")
        return docker(*args, **kwargs)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(DockerRunner, "docker", staticmethod(lose_inspect))
            with pytest.raises(RunnerError, match="^ORPHAN_STOP_UNCONFIRMED$"):
                DockerRunner(runner.image_digest)
        DockerRunner(runner.image_digest)
        listing = docker("ps", "-aq", "--filter", f"id={container}", timeout=5)
        assert listing.returncode == 0 and not listing.stdout.strip()
    finally:
        docker("rm", "--force", container, timeout=7)
