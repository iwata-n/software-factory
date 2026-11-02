"""Managed Linux runner. Only this supervisor can access the container runtime."""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from threading import Event, Lock
from uuid import uuid4


class RunnerError(Exception):
    def __init__(self, code: str, category: str = "INFRASTRUCTURE"):
        self.code, self.category = code, category
        super().__init__(code)


@dataclass(frozen=True)
class RunnerResult:
    body: dict
    container_id: str
    image_digest: str
    elapsed_seconds: float
    isolation: dict


class DockerRunner:
    def __init__(
        self, image_digest: str, *, source_network: str = "bridge", source_ca_pem: str | None = None
    ):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise ValueError("a pinned local image content digest is required")
        self.image_digest = image_digest
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", source_network)
            or source_network == "host"
        ):
            raise ValueError("invalid source network")
        self.source_network = source_network
        self.source_ca_pem = source_ca_pem
        self._active: set[str] = set()
        self._lock = Lock()

    @staticmethod
    def docker(*args, **kwargs):
        # Never inherit credential-bearing application environment variables.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
        }
        return subprocess.run(["docker", *args], env=env, capture_output=True, **kwargs)

    def verify_image(self):
        result = self.docker(
            "image", "inspect", self.image_digest, "--format", "{{.Id}}", timeout=10
        )
        if result.returncode or result.stdout.decode().strip() != self.image_digest:
            raise RunnerError("IMAGE_DIGEST_MISMATCH")

    def cancel(self):
        with self._lock:
            names = list(self._active)
        for name in names:
            self.docker("stop", "--timeout", "5", name, timeout=7)

    def execute(
        self,
        request: dict,
        *,
        timeout: float = 120,
        network: bool = False,
        cancelled: Event | None = None,
    ) -> RunnerResult:
        self.verify_image()
        if timeout <= 10 or (cancelled and cancelled.is_set()):
            raise RunnerError("INSUFFICIENT_EXECUTION_TIME")
        name = "factory-" + uuid4().hex
        args = [
            "docker",
            "run",
            "--name",
            name,
            "--init",
            "-i",
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
            "--network",
            self.source_network if network else "none",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=256m,uid=65532,gid=65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m,uid=65532,gid=65532",
            "--workdir",
            "/work",
            self.image_digest,
        ]
        started = time.monotonic()
        with self._lock:
            self._active.add(name)
        try:
            # communicate uses a bounded host wait; the container is always removed on exit.
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    k: v
                    for k, v in os.environ.items()
                    if k in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
                },
            )
            trusted_request = dict(request)
            if network and self.source_ca_pem:
                trusted_request["source_ca_pem"] = self.source_ca_pem
            encoded = json.dumps(trusted_request).encode()
            while True:
                remaining = timeout - 10 - (time.monotonic() - started)
                if remaining <= 0 or (cancelled and cancelled.is_set()):
                    self.docker("stop", "--timeout", "5", name, timeout=7)
                    raise RunnerError(
                        "RUNNER_CANCELLED" if cancelled and cancelled.is_set() else "RUNNER_TIMEOUT"
                    )
                try:
                    stdout, _ = process.communicate(input=encoded, timeout=min(remaining, 0.25))
                    break
                except subprocess.TimeoutExpired:
                    encoded = None
            try:
                body = json.loads(stdout)
            except (ValueError, UnicodeDecodeError):
                raise RunnerError("RUNNER_OUTPUT_INVALID", "INCONCLUSIVE") from None
            if not body.get("ok"):
                raise RunnerError(
                    body.get("code", "RUNNER_FAILED"), body.get("category", "INFRASTRUCTURE")
                )
            if process.returncode:
                raise RunnerError("RUNNER_FAILED")
            inspected = self.docker("inspect", name, timeout=5)
            if inspected.returncode:
                raise RunnerError("RUNNER_INSPECTION_FAILED")
            actual = json.loads(inspected.stdout)[0]
            if actual["Image"] != self.image_digest:
                raise RunnerError("IMAGE_DIGEST_MISMATCH")
            isolation = {
                "supervisor_pid": os.getpid(),
                "runtime_client_pid": process.pid,
                "environment_keys": sorted(
                    item.split("=", 1)[0] for item in actual["Config"]["Env"]
                ),
                "user": actual["Config"]["User"],
                "image": actual["Image"],
                "read_only_root": actual["HostConfig"]["ReadonlyRootfs"],
                "network": actual["HostConfig"]["NetworkMode"],
                "cap_drop": actual["HostConfig"]["CapDrop"],
                "security_opt": actual["HostConfig"]["SecurityOpt"],
                "mounts": actual["Mounts"],
                "pids_limit": actual["HostConfig"]["PidsLimit"],
                "memory": actual["HostConfig"]["Memory"],
                "nano_cpus": actual["HostConfig"]["NanoCpus"],
                "tmpfs": actual["HostConfig"]["Tmpfs"],
            }
            return RunnerResult(
                body, name, self.image_digest, time.monotonic() - started, isolation
            )
        finally:
            self.docker("rm", "--force", name, timeout=7)
            if "process" in locals():
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
            with self._lock:
                self._active.discard(name)
