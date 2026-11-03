"""Reclaim a managed Codex invocation if its Worker process disappears."""

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .sandbox import DockerRunner

AUTH_PREFIX = "factory-codex-auth-"


def names(invocation):
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ValueError("invalid Codex invocation identifier")
    return tuple(
        f"factory-{kind}-{invocation}"
        for kind in ("isolated-codex", "model-gateway", "model-external", "model-internal")
    )


def docker(*args, timeout):
    try:
        return DockerRunner.docker(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def absent_or_removed(result):
    if result is None:
        return False
    error = result.stderr.lower()
    return result.returncode == 0 or b"no such" in error or b"not found" in error


def reclaim(invocation, scratch):
    container, gateway, external, internal = names(invocation)
    docker("stop", "--signal", "SIGTERM", "--timeout", "5", container, timeout=7)
    # Remove the host credential copy even if the Docker daemon is unavailable.
    shutil.rmtree(scratch, ignore_errors=True)
    # A daemon request launched just before owner death may still create a resource.
    # Sweep unique names throughout the grace window for in-flight setup requests.
    deadline = time.monotonic() + 10
    while True:
        removed = [
            docker("rm", "--force", "--volumes", container, timeout=2),
            docker("rm", "--force", "--volumes", gateway, timeout=2),
            docker("network", "rm", external, timeout=2),
            docker("network", "rm", internal, timeout=2),
        ]
        if time.monotonic() >= deadline:
            return all(map(absent_or_removed, removed)) and not scratch.exists()
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("invocation")
    parser.add_argument("scratch", type=Path)
    args = parser.parse_args()
    names(args.invocation)
    if not args.scratch.is_absolute() or not args.scratch.name.startswith(
        AUTH_PREFIX + args.invocation + "-"
    ):
        parser.error("unexpected credential directory")
    try:
        sys.stdout.buffer.write(b"1")
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass
    if sys.stdin.buffer.read(1) == b"1":
        return 0
    return 0 if reclaim(args.invocation, args.scratch) else 1


if __name__ == "__main__":
    raise SystemExit(main())
