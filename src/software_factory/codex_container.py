"""Disposable Docker network and restricted egress for a managed Codex process."""

import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import model_egress
from .sandbox import DockerRunner, RunnerError


@contextmanager
def model_network(image_digest, allowed_hosts):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ValueError("a pinned local image content digest is required")
    allowed_hosts = tuple(sorted(set(allowed_hosts)))
    model_egress.validate_hosts(allowed_hosts)

    suffix = uuid4().hex
    internal = "factory-model-internal-" + suffix
    external = "factory-model-external-" + suffix
    proxy = "factory-model-gateway-" + suffix
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
