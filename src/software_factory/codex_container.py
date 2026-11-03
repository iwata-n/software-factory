"""Disposable Docker network and restricted egress for a managed Codex process."""

import os
import re
import select
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from . import model_egress
from .codex_engine import DISABLED_FEATURES, CodexCLI, CodexError, file_hash
from .codex_watchdog import AUTH_PREFIX, names
from .sandbox import STOP_UNCONFIRMED, DockerRunner, RunnerError
from .sandbox_watchdog import docker_environment


@contextmanager
def owner_guard(invocation, scratch):
    environment = docker_environment() | {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    with subprocess.Popen(
        [sys.executable, "-m", "software_factory.codex_watchdog", invocation, scratch],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    ) as guard:
        try:
            ready, _, _ = select.select([guard.stdout], [], [], 5)
            if not ready or guard.stdout.read(1) != b"1":
                raise CodexError("CODEX_WATCHDOG_UNAVAILABLE", "INFRASTRUCTURE", before_launch=True)
            yield
        finally:
            try:
                guard.stdin.write(b"1")
                guard.stdin.close()
                guard.wait(timeout=2)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                guard.kill()
                guard.wait(timeout=2)


@contextmanager
def model_network(image_digest, allowed_hosts, invocation=None):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ValueError("a pinned local image content digest is required")
    allowed_hosts = tuple(sorted(set(allowed_hosts)))
    model_egress.validate_hosts(allowed_hosts)

    _, proxy, external, internal = names(invocation or uuid4().hex)
    alias = "model-gateway"
    proxy_url = f"http://{alias}:8080"
    resources = []

    def create(args, removal):
        # Names are unique. Schedule cleanup before the mutation in case its response is lost.
        resources.append(removal)
        result = DockerRunner.docker(*args, timeout=10)
        if result.returncode:
            raise RunnerError("MODEL_NETWORK_SETUP_FAILED")

    try:
        create(("network", "create", "--internal", internal), ("network", "rm", internal))
        create(("network", "create", external), ("network", "rm", external))
        arguments = [
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            internal,
            "--network-alias",
            alias,
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "128m",
            "--mount",
            f"type=bind,src={Path(model_egress.__file__).resolve()},dst=/opt/egress.py,readonly",
            "--entrypoint",
            "python",
            image_digest,
            "/opt/egress.py",
            "--listen",
            alias,
        ]
        for host in allowed_hosts:
            arguments.extend(("--allow-host", host))
        create(arguments, ("rm", "--force", proxy))
        if DockerRunner.docker("network", "connect", external, proxy, timeout=10).returncode:
            raise RunnerError("MODEL_NETWORK_SETUP_FAILED")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = DockerRunner.docker(
                "exec",
                proxy,
                "python",
                "-c",
                "import socket; socket.create_connection(('model-gateway',8080),.5).close()",
                timeout=2,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RunnerError("MODEL_GATEWAY_UNAVAILABLE")
        yield internal, proxy_url
    finally:
        cleanup_failed = False
        for args in reversed(resources):
            try:
                result = DockerRunner.docker(*args, timeout=10)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
            else:
                # A failed creation can legitimately leave a resource absent.
                cleanup_failed |= result.returncode != 0 and b"No such" not in result.stderr
        if cleanup_failed:
            raise RunnerError("MODEL_NETWORK_CLEANUP_UNCONFIRMED")


class DockerCodexCLI(CodexCLI):
    isolated = True

    def __init__(self, binary, manifest, auth_file):
        if not auth_file:
            raise CodexError("ISOLATED_CODEX_AUTH_FILE_REQUIRED", "POLICY", before_launch=True)
        self.auth_file = Path(auth_file).absolute()
        self._run_lock = Lock()
        self._stop_lock = Lock()
        self._container = None
        super().__init__(binary, manifest)

    def container_options(self):
        return [
            "--init",
            "--log-driver",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "2",
            "--memory",
            "2g",
            "--memory-swap",
            "2g",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=256m,uid=65532,gid=65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=16m,uid=65532,gid=65532",
            "--workdir",
            "/work",
            "--env",
            "HOME=/work",
            "--env",
            "CODEX_HOME=/work",
            "--mount",
            f"type=bind,src={self.binary},dst=/opt/codex,readonly",
        ]

    def verify_binary(self):
        if file_hash(self.binary) != self.manifest.binary_sha256:
            raise CodexError("CODEX_BINARY_DIGEST_MISMATCH", "POLICY", before_launch=True)
        try:
            result = DockerRunner.docker(
                "run",
                "--rm",
                "--network",
                "none",
                *self.container_options(),
                "--entrypoint",
                "/opt/codex",
                self.manifest.isolation.image_digest,
                "--version",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise CodexError("CODEX_UNAVAILABLE", "INFRASTRUCTURE", before_launch=True) from None
        if (
            result.returncode
            or result.stdout.strip() != f"codex-cli {self.manifest.version}".encode()
        ):
            raise CodexError("CODEX_VERSION_MISMATCH", "POLICY", before_launch=True)

    def copy_auth(self, destination):
        try:
            with os.fdopen(os.open(self.auth_file, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or not stat.S_ISREG(info.st_mode)
                    or not 0 < info.st_size <= 1024 * 1024
                ):
                    raise ValueError("invalid auth file")
                content = source.read(1024 * 1024 + 1)
                if len(content) != info.st_size:
                    raise ValueError("auth file changed while reading")
            destination.write_bytes(content)
            # The parent directory is owner-only. The non-root container can read this mount.
            destination.chmod(0o444)
        except (OSError, ValueError):
            raise CodexError(
                "ISOLATED_CODEX_AUTH_FILE_INVALID", "POLICY", before_launch=True
            ) from None

    def argv(self, schema_path):
        schema_path.chmod(0o444)
        name = self._container_name
        self._container = name
        manifest = self.manifest
        extra_mounts = []
        cli_args = super().argv("/opt/schema.json")[1:]
        if manifest.code_mode_host_sha256:
            helper = Path(self.binary).with_name("codex-code-mode-host")
            try:
                verified = file_hash(helper) == manifest.code_mode_host_sha256
            except OSError:
                verified = False
            if not verified:
                raise CodexError("CODEX_TOOL_HOST_DIGEST_MISMATCH", "POLICY", before_launch=True)
            extra_mounts = [
                "--mount",
                f"type=bind,src={helper},dst=/opt/codex-code-mode-host,readonly",
            ]
            cli_args = [
                "app-server",
                "--listen",
                "stdio://",
                "-c",
                'web_search="disabled"',
                "-c",
                "project_doc_max_bytes=0",
            ]
            for feature in DISABLED_FEATURES:
                cli_args.extend(("--disable", feature))
        created = DockerRunner.docker(
            "create",
            "-i",
            "--name",
            name,
            "--network",
            self._network,
            *self.container_options(),
            *extra_mounts,
            "--env",
            f"HTTPS_PROXY={self._proxy_url}",
            "--mount",
            f"type=bind,src={schema_path},dst=/opt/schema.json,readonly",
            "--mount",
            f"type=bind,src={self._auth_copy},dst=/opt/auth.json,readonly",
            "--entrypoint",
            "/bin/sh",
            self.manifest.isolation.image_digest,
            "-ec",
            'umask 077; cp /opt/auth.json /work/auth.json; exec /opt/codex "$@"',
            "--",
            *cli_args,
            timeout=10,
        )
        if created.returncode:
            raise CodexError("ISOLATED_CODEX_CREATE_FAILED", "INFRASTRUCTURE", before_launch=True)
        return ["docker", "start", "--attach", "--interactive", name]

    def cancel(self):
        with self._stop_lock:
            with self._lock:
                container = self._container
                process = self._process
            if container is not None:
                try:
                    DockerRunner.docker("stop", "--timeout", "5", container, timeout=7)
                    removed = DockerRunner.docker("rm", "--force", container, timeout=2)
                    if removed.returncode:
                        listing = DockerRunner.docker(
                            "ps", "-aq", "--filter", f"name=^/{container}$", timeout=2
                        )
                        if listing.returncode or listing.stdout.strip():
                            raise OSError("container removal unconfirmed")
                except (OSError, subprocess.SubprocessError):
                    raise CodexError(STOP_UNCONFIRMED, "INFRASTRUCTURE") from None
                self._container = None
            if process is not None:
                try:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                except (OSError, subprocess.SubprocessError):
                    raise CodexError(STOP_UNCONFIRMED, "INFRASTRUCTURE") from None

    def run(self, context, output_model, cancelled, *, timeout=None):
        if cancelled.is_set():
            raise CodexError("CODEX_CANCELLED", "POLICY", before_launch=True)
        if not self._run_lock.acquire(blocking=False):
            raise CodexError("CODEX_ALREADY_RUNNING", "POLICY", before_launch=True)
        try:
            invocation = uuid4().hex
            self._container_name = names(invocation)[0]
            scratch = TemporaryDirectory(prefix=AUTH_PREFIX + invocation + "-")
            with owner_guard(invocation, scratch.name), scratch as root:
                self._auth_copy = Path(root) / "auth.json"
                self.copy_auth(self._auth_copy)
                isolation = self.manifest.isolation
                try:
                    with model_network(
                        isolation.image_digest, isolation.allowed_hosts, invocation
                    ) as network:
                        self._network, self._proxy_url = network
                        try:
                            if self.manifest.code_mode_host_sha256:
                                from .codex_app_server import run_app_server

                                return run_app_server(
                                    self, Path(root), context, output_model, cancelled, timeout
                                )
                            return super().run(context, output_model, cancelled, timeout=timeout)
                        finally:
                            self.cancel()
                            with self._lock:
                                process, self._process = self._process, None
                            if process is not None:
                                for stream in (process.stdin, process.stdout):
                                    if stream is not None:
                                        stream.close()
                except RunnerError as error:
                    raise CodexError(error.code, error.category) from None
        finally:
            self._run_lock.release()
