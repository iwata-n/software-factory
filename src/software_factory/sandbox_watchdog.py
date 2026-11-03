"""Independent supervisor for a single managed, offline Linux container.

The trusted worker launches this module with a dedicated stdin pipe. Closing
that pipe (including on worker death) or writing a byte cancels the run.
Deadlines are absolute readings from the same host monotonic clock.
"""

import argparse
import json
import math
import os
import re
import select
import subprocess
import time
from uuid import uuid4

from .sandbox import DockerRunner

STOP_MARGIN = 10
DISPATCH_MARGIN = 0.1
TERM_GRACE = 5
KILL_CONFIRMATION = 2


def trace(event, **details):
    try:
        print(json.dumps({"event": event, "monotonic": time.monotonic(), **details}), flush=True)
    except BrokenPipeError:
        # A vanished trace consumer must not interrupt enforcement.
        pass


def cancellation_pending(timeout=0):
    readable, _, _ = select.select([0], [], [], timeout)
    return bool(readable)


def run(image, lease_deadline, job_deadline, command):
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
        or not all(map(math.isfinite, (lease_deadline, job_deadline)))
        or not command
    ):
        raise ValueError("a pinned image, finite deadlines and a trusted command are required")
    cutoff = min(lease_deadline, job_deadline) - STOP_MARGIN - DISPATCH_MARGIN

    def admission():
        if cancellation_pending():
            return "CANCELLED"
        if time.monotonic() >= cutoff:
            return "INSUFFICIENT_EXECUTION_TIME"
        return None

    reason = admission()
    if reason:
        trace("rejected", code=reason)
        return 1
    name = "factory-watchdog-" + uuid4().hex
    docker = DockerRunner.docker
    container = None
    inspected = docker("image", "inspect", "--format", "{{.Id}}", image, timeout=5)
    if inspected.returncode or inspected.stdout.decode().strip() != image:
        trace("rejected", code="IMAGE_DIGEST_MISMATCH")
        return 1
    reason = admission()
    if reason:
        trace("rejected", code=reason)
        return 1
    try:
        created = docker(
            "create",
            "--name",
            name,
            "--pull",
            "never",
            "--restart",
            "no",
            "--init",
            "--no-healthcheck",
            "--ipc",
            "private",
            "--cgroupns",
            "private",
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
            "none",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=256m,uid=65532,gid=65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16m,uid=65532,gid=65532",
            "--workdir",
            "/work",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--entrypoint",
            command[0],
            image,
            *command[1:],
            timeout=5,
        )
        container = created.stdout.decode().strip()
        if created.returncode or not re.fullmatch(r"[0-9a-f]{64}", container):
            trace("error", code="CONTAINER_CREATE_FAILED")
            return 1
        trace("created", container_id=container)
        reason = admission()
        if reason:
            trace("rejected", code=reason)
            return 1
        started = docker("start", container, timeout=min(2, max(0.001, cutoff - time.monotonic())))
        if started.returncode:
            trace("error", code="CONTAINER_START_FAILED")
            return 2
        trace("started", container_id=container, stop_cutoff=cutoff)
        return monitor(container, cutoff)
    finally:
        # Address the unique name even when the create response is lost.
        removed = docker("rm", "--force", "--volumes", name, timeout=7)
        if removed.returncode and (container or b"no such container" not in removed.stderr.lower()):
            trace("error", code="CLEANUP_UNCONFIRMED", container_id=container)
            raise RuntimeError("managed sandbox cleanup unconfirmed")
        trace("cleanup", container_id=container)


def monitor(container, cutoff):
    # docker wait blocks in the daemon, so no inspect RPC can delay the stop decision.
    with subprocess.Popen(
        ["docker", "wait", container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=docker_environment(),
    ) as waiter:
        try:
            while True:
                remaining = cutoff - time.monotonic()
                if remaining <= 0:
                    return stop(container, "DEADLINE")
                if cancellation_pending(min(remaining, 0.025)):
                    return stop(container, "CANCELLED_OR_OWNER_LOST")
                if waiter.poll() is not None:
                    break
            if waiter.returncode:
                return stop(container, "RUNTIME_WAIT_FAILED")
            state = inspect_state(container, timeout=2)
            if not exited(state):
                return stop(container, "RUNTIME_STATE_UNCONFIRMED")
            trace("exited", exit_code=state["ExitCode"], oom_killed=state["OOMKilled"])
            return int(state["ExitCode"] != 0 or state["OOMKilled"])
        finally:
            if waiter.poll() is None:
                waiter.kill()


def inspect_state(container, *, timeout):
    state = DockerRunner.docker(
        "inspect", "--format", "{{json .State}}", container, timeout=timeout
    )
    if state.returncode:
        raise RuntimeError("container state unavailable")
    return json.loads(state.stdout)


def exited(state):
    # The container has a private PID namespace. Linux tears down its
    # remaining members when its init exits, including session leaders.
    return state["Status"] == "exited" and state["Pid"] == 0


def stop(container, reason):
    trace("stopping", reason=reason, container_id=container)
    confirmation_deadline = time.monotonic() + TERM_GRACE + KILL_CONFIRMATION
    try:
        # Let the daemon own the TERM-to-KILL timer, even if its client dies.
        DockerRunner.docker(
            "stop",
            "--signal",
            "SIGTERM",
            "--timeout",
            str(TERM_GRACE),
            container,
            timeout=TERM_GRACE + KILL_CONFIRMATION,
        )
        remaining = confirmation_deadline - time.monotonic()
        if remaining > 0 and exited(inspect_state(container, timeout=remaining)):
            if time.monotonic() <= confirmation_deadline:
                trace("stopped", reason=reason, container_id=container)
                return 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        pass
    trace("error", code="STOP_UNCONFIRMED", container_id=container)
    return 2


def docker_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("lease_deadline", type=float)
    parser.add_argument("job_deadline", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return run(args.image, args.lease_deadline, args.job_deadline, args.command)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        trace("error", code="WATCHDOG_FAILURE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
