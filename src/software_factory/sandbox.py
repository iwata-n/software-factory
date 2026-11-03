"""Managed Linux runner. Only this supervisor can access the container runtime."""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

STOP_UNCONFIRMED = "STOP_UNCONFIRMED"


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
        self._quarantined = Event()
        self.state_root = Path(
            os.environ.get("FACTORY_RUNNER_STATE_ROOT", ".local/runner/owners")
        ).absolute()
        if any(path.is_symlink() for path in (self.state_root, *self.state_root.parents)):
            raise ValueError("runner state directory cannot contain symlinks")
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.namespace = hashlib.sha256(str(self.state_root).encode()).hexdigest()[:24]
        self.owner = uuid4().hex
        self._owner_fd = os.open(
            self.state_root / (self.owner + ".lock"),
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.cleanup_orphans()

    def __del__(self):
        if hasattr(self, "_owner_fd"):
            os.close(self._owner_fd)

    def cleanup_orphans(self):
        listing = self.docker(
            "ps", "-aq", "--filter", "label=factory.namespace=" + self.namespace, timeout=10
        )
        if listing.returncode:
            raise RunnerError("CONTAINER_RUNTIME_UNAVAILABLE")
        removed = []
        for container in listing.stdout.decode().split():
            inspected = self.docker("inspect", container, timeout=5)
            if inspected.returncode:
                self.require_orphan_absent(container)
                continue
            labels = json.loads(inspected.stdout)[0]["Config"].get("Labels") or {}
            owner = labels.get("factory.owner", "")
            if owner == self.owner:
                continue
            if not re.fullmatch(r"[0-9a-f]{32}", owner):
                self.require_orphan_absent(container)
                continue
            try:
                fd = os.open(self.state_root / (owner + ".lock"), os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                self.require_orphan_absent(container)
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                result = self.docker("rm", "--force", container, timeout=7)
                if result.returncode:
                    self.require_orphan_absent(container)
                removed.append(container)
            finally:
                os.close(fd)
        volumes = self.docker(
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=factory.namespace=" + self.namespace,
            timeout=10,
        )
        if volumes.returncode:
            raise RunnerError("CONTAINER_RUNTIME_UNAVAILABLE")
        for volume in volumes.stdout.decode().split():
            inspected = self.docker("volume", "inspect", volume, timeout=5)
            if inspected.returncode:
                continue
            labels = json.loads(inspected.stdout)[0].get("Labels") or {}
            owner = labels.get("factory.owner", "")
            if owner == self.owner or not re.fullmatch(r"[0-9a-f]{32}", owner):
                continue
            try:
                fd = os.open(self.state_root / (owner + ".lock"), os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                result = self.docker("volume", "rm", volume, timeout=7)
                if result.returncode:
                    raise RunnerError("ORPHAN_VOLUME_CLEANUP_UNCONFIRMED")
                removed.append(volume)
            finally:
                os.close(fd)
        return removed

    def require_orphan_absent(self, container):
        # A concurrent owner may have removed it since listing. Confirm absence
        # from the daemon rather than inferring it from a failed inspect/remove.
        listing = self.docker("ps", "-aq", "--filter", f"id={container}", timeout=5)
        if listing.returncode or listing.stdout.strip():
            self._quarantined.set()
            raise RunnerError("ORPHAN_STOP_UNCONFIRMED")

    def execute_candidate(self, request, archive, *, timeout=120, cancelled=None):
        from .candidate_mount import readonly_tar

        self.verify_image()
        started = time.monotonic()
        volume = "factory-candidate-" + uuid4().hex
        helper = "factory-volume-" + uuid4().hex
        packed = readonly_tar(archive)
        created = self.docker(
            "volume",
            "create",
            "--label",
            "factory.namespace=" + self.namespace,
            "--label",
            "factory.owner=" + self.owner,
            volume,
            timeout=10,
        )
        if created.returncode:
            raise RunnerError("CANDIDATE_VOLUME_CREATE_FAILED")
        try:
            prepared = self.docker(
                "create",
                "--name",
                helper,
                "--read-only",
                "--network",
                "none",
                "--user",
                "65532:65532",
                "--label",
                "factory.namespace=" + self.namespace,
                "--label",
                "factory.owner=" + self.owner,
                "--mount",
                "type=volume,source=" + volume + ",target=/candidate",
                "--entrypoint",
                "true",
                self.image_digest,
                timeout=10,
            )
            if prepared.returncode:
                raise RunnerError("CANDIDATE_VOLUME_PREPARE_FAILED")
            copied = self.docker(
                "cp",
                "-",
                helper + ":/candidate",
                input=packed,
                timeout=max(1, min(30, timeout - 10 - (time.monotonic() - started))),
            )
            if copied.returncode:
                raise RunnerError("CANDIDATE_VOLUME_COPY_FAILED")
            return self.execute(
                request,
                timeout=timeout - (time.monotonic() - started),
                cancelled=cancelled,
                candidate_volume=volume,
            )
        finally:
            self.docker("rm", "--force", helper, timeout=7)
            removed = self.docker("volume", "rm", volume, timeout=7)
            if removed.returncode:
                raise RunnerError("CANDIDATE_VOLUME_CLEANUP_FAILED")

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
        if self._quarantined.is_set():
            raise RunnerError(STOP_UNCONFIRMED)
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
        pause_at: str | None = None,
        candidate_volume: str | None = None,
    ) -> RunnerResult:
        self.verify_image()
        if timeout <= 10 or (cancelled and cancelled.is_set()):
            raise RunnerError("INSUFFICIENT_EXECUTION_TIME")
        name = "factory-" + uuid4().hex
        if pause_at not in {None, "patch_applied", "before_commit"}:
            raise ValueError("unsupported trusted test checkpoint")
        args = [
            "docker",
            "run",
            "--name",
            name,
            "--label",
            "factory.namespace=" + self.namespace,
            "--label",
            "factory.owner=" + self.owner,
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
        ]
        if pause_at:
            args += ["--env", "FACTORY_TEST_PAUSE_AT=" + pause_at]
        if candidate_volume:
            if not re.fullmatch(r"factory-candidate-[0-9a-f]{32}", candidate_volume):
                raise ValueError("only supervisor-owned candidate volumes are allowed")
            args += [
                "--mount",
                "type=volume,source=" + candidate_volume + ",target=/candidate,readonly",
            ]
        args += [self.image_digest]
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
                    try:
                        stopped = self.docker("stop", "--timeout", "5", name, timeout=7)
                        if stopped.returncode:
                            raise RunnerError(STOP_UNCONFIRMED)
                    except (OSError, subprocess.SubprocessError):
                        raise RunnerError(STOP_UNCONFIRMED) from None
                    raise RunnerError(
                        "RUNNER_CANCELLED" if cancelled and cancelled.is_set() else "RUNNER_TIMEOUT"
                    )
                try:
                    stdout, _ = process.communicate(input=encoded, timeout=min(remaining, 0.25))
                    break
                except subprocess.TimeoutExpired:
                    encoded = None
            if cancelled and cancelled.is_set():
                raise RunnerError("RUNNER_CANCELLED")
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
        except RunnerError as error:
            if error.code == STOP_UNCONFIRMED:
                self._quarantined.set()
            raise
        finally:
            try:
                if "process" in locals():
                    try:
                        process.communicate(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=2)
                    removed = self.docker("rm", "--force", name, timeout=7)
                    if removed.returncode:
                        remaining = self.docker(
                            "ps", "-aq", "--filter", f"name=^/{name}$", timeout=2
                        )
                        if remaining.returncode or remaining.stdout.strip():
                            raise RunnerError(STOP_UNCONFIRMED)
            except (OSError, subprocess.SubprocessError, RunnerError):
                self._quarantined.set()
                raise RunnerError(STOP_UNCONFIRMED) from None
            finally:
                with self._lock:
                    self._active.discard(name)
