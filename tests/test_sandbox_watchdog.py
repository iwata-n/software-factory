"""DB-free, opt-in Docker integration probes for the first SF-21 implementation."""

import json
import os
import subprocess
import sys
import time

import pytest

from software_factory.sandbox import DockerRunner

IMAGE = os.environ.get("SF21_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set SF21_TEST_IMAGE to a pinned runner image")


def argv(script, lease, job, image=IMAGE):
    return [
        sys.executable,
        "-m",
        "software_factory.sandbox_watchdog",
        image,
        str(lease),
        str(job),
        "python",
        "-u",
        "-c",
        script,
    ]


def launch(script, trace_file, lease, job):
    with trace_file.open("w") as output:
        return subprocess.Popen(argv(script, lease, job), stdin=subprocess.PIPE, stdout=output)


def records(trace_file):
    return [
        json.loads(line)
        for line in trace_file.read_text().splitlines(keepends=True)
        if line.endswith("\n")
    ]


def until(callback, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.025)
    pytest.fail("probe did not become ready")


def event(trace_file, name):
    return next((row for row in records(trace_file) if row["event"] == name), None)


def event_actions(path):
    return [row["Action"] for row in records(path)]


def logs(container):
    return DockerRunner.docker("logs", container, timeout=2).stdout.decode().strip()


def removed(container):
    result = DockerRunner.docker("inspect", container, timeout=2)
    return result.returncode and b"no such" in result.stderr.lower()


@pytest.mark.parametrize("trigger", ["cancel", "owner_death", "lease", "job"])
def test_stops_term_resistant_tree(tmp_path, trigger):
    script = """
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
parent = os.getpid()
if os.fork() == 0:
    os.setsid()
    child = os.getpid()
    if os.fork() == 0:
        print(parent, child, os.getpid(), flush=True)
while True:
    time.sleep(1)
"""
    trace_file = tmp_path / "stop.jsonl"
    now = time.monotonic()
    lease = now + (14 if trigger == "lease" else 50)
    job = now + (14 if trigger == "job" else 50)
    if trigger == "owner_death":
        # This intermediary is the sole holder of the watchdog control pipe.
        launcher = """
import subprocess, sys
subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE)
sys.stdin.read()
"""
        with trace_file.open("w") as output:
            proc = subprocess.Popen(
                [sys.executable, "-c", launcher, *argv(script, lease, job)],
                stdin=subprocess.PIPE,
                stdout=output,
            )
    else:
        proc = launch(script, trace_file, lease, job)
    try:
        started = until(lambda: event(trace_file, "started"))
        container = started["container_id"]
        pids = until(lambda: logs(container))
        assert len(set(pids.split())) == 3
        top = DockerRunner.docker("top", container, "-eo", "pid,ppid", timeout=2)
        assert top.returncode == 0
        host_pids = [row.split()[0] for row in top.stdout.decode().splitlines()[1:]]
        assert len(host_pids) == 4
        if trigger == "cancel":
            proc.stdin.write(b"x")
            proc.stdin.flush()
        elif trigger == "owner_death":
            proc.kill()
        cleanup = until(lambda: event(trace_file, "cleanup"), timeout=13)
        assert proc.wait(timeout=2) == (-9 if trigger == "owner_death" else 1)
        stopping = event(trace_file, "stopping")
        stopped = event(trace_file, "stopped")
        assert stopped is not None, records(trace_file)
        assert 5 <= stopped["monotonic"] - stopping["monotonic"] <= 7
        if trigger in {"lease", "job"}:
            assert stopping["reason"] == "DEADLINE"
            assert 0 <= (min(lease, job) - 10) - stopping["monotonic"] < 0.2
        else:
            assert stopping["reason"] == "CANCELLED_OR_OWNER_LOST"
        assert cleanup["monotonic"] - stopped["monotonic"] < 60
        assert removed(container)
        # Subscribe with replay rather than bounding the server stream using the
        # client's wall clock. Docker Desktop can skew the two clocks.
        audit_file = tmp_path / "runtime-events.jsonl"
        with (
            audit_file.open("w") as output,
            subprocess.Popen(
                [
                    "docker",
                    "events",
                    "--since",
                    "1m",
                    "--filter",
                    f"container={container}",
                    "--format",
                    "{{json .}}",
                ],
                stdout=output,
            ) as audit,
        ):
            try:
                until(lambda: event_actions(audit_file).count("kill") == 2, timeout=5)
            finally:
                audit.terminate()
        events = records(audit_file)
        term, kill = (row for row in events if row["Action"] == "kill")
        assert [row["Actor"]["Attributes"]["signal"] for row in (term, kill)] == ["15", "9"]
        assert 5 <= (kill["timeNano"] - term["timeNano"]) / 1e9 < 6
        # Test-only observer in the Linux VM's host PID namespace.
        probe = DockerRunner.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--pid",
            "host",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            "import os,sys; assert not any(os.path.exists('/proc/'+p) for p in sys.argv[1:])",
            *host_pids,
            timeout=5,
        )
        assert probe.returncode == 0, probe.stderr.decode()
        (tmp_path / "pids.json").write_text(json.dumps({"container": pids, "host": host_pids}))
    finally:
        proc.stdin.close()
        if proc.poll() is None:
            proc.wait(timeout=12)


@pytest.mark.parametrize(
    ("remaining", "bad_image", "code"),
    [
        (10, False, "INSUFFICIENT_EXECUTION_TIME"),
        (50, True, "IMAGE_DIGEST_MISMATCH"),
    ],
)
def test_admission_rejects_before_create(remaining, bad_image, code):
    now = time.monotonic()
    image = "sha256:" + "0" * 64 if bad_image else IMAGE
    with subprocess.Popen(
        argv("raise AssertionError", now + remaining, now + 100, image),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as proc:
        # Keep the owner pipe open until the watchdog exits.
        assert proc.wait(timeout=7) == 1
        assert [row["code"] for row in map(json.loads, proc.stdout)] == [code]


def test_live_isolation_and_limits(tmp_path, monkeypatch):
    host_canary = tmp_path / "secret"
    host_canary.write_text("host-only")
    monkeypatch.setenv("CP_TOKEN", "cp-canary")
    monkeypatch.setenv("PUBLISHER_TOKEN", "publisher-canary")
    script = """
import errno, json, os, socket, time
from pathlib import Path

cg = Path('/sys/fs/cgroup')
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert os.getuid() == 65532
assert int(status['CapEff'], 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert 'CP_TOKEN' not in os.environ and 'PUBLISHER_TOKEN' not in os.environ
for p in ['/var/run/docker.sock', '/root/.ssh', HOST_CANARY]:
    try:
        Path(p).read_bytes()
    except (FileNotFoundError, PermissionError):
        pass
    else:
        raise AssertionError(p)
Path('/work/escape').symlink_to(HOST_CANARY)
assert not Path('/work/escape').exists()
try:
    Path('/etc/sf21').touch()
except OSError as exc:
    assert exc.errno == errno.EROFS
else:
    raise AssertionError('root writable')
for address in ['1.1.1.1', '10.0.0.1', '169.254.169.254']:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex((address, 443)) == errno.ENETUNREACH
limits = {key: (cg / key).read_text().strip() for key in
          ['cpu.max', 'memory.max', 'memory.swap.max', 'pids.max']}
quota, period = map(int, limits['cpu.max'].split())
assert quota == 2 * period
assert limits['memory.max'] == str(2**31)
assert limits['memory.swap.max'] == '0'
assert limits['pids.max'] == '128'

try:
    with open('/work/fill', 'wb', buffering=0) as f:
        for _ in range(257):
            f.write(b'x' * (2**20))
except OSError as exc:
    assert exc.errno == errno.ENOSPC
else:
    raise AssertionError('tmpfs limit bypassed')
assert Path('/work/fill').stat().st_size == 256 * 2**20
Path('/work/fill').unlink()

children = []
try:
    for _ in range(128):
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        children.append(pid)
except OSError as exc:
    assert exc.errno == errno.EAGAIN
else:
    raise AssertionError('pids limit bypassed')
finally:
    peak_pids = int((cg / 'pids.current').read_text())
    for pid in children:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
assert peak_pids == 128
print(json.dumps({'limits': limits, 'peak_pids': peak_pids, 'work_bytes': 256 * 2**20,
                  'uid': os.getuid(), 'cap_eff': status['CapEff'].strip(),
                  'no_new_privs': status['NoNewPrivs'].strip(),
                  'pid_namespace': os.readlink('/proc/self/ns/pid'),
                  'mounts': Path('/proc/self/mountinfo').read_text().splitlines(),
                  'network': 'ENETUNREACH'}))
time.sleep(2)
raise SystemExit(72)
"""
    script = f"HOST_CANARY = {str(host_canary)!r}\n" + script
    trace_file = tmp_path / "limits.jsonl"
    with launch(script, trace_file, time.monotonic() + 40, time.monotonic() + 40) as proc:
        container = until(lambda: event(trace_file, "started"))["container_id"]
        metrics = json.loads(until(lambda: logs(container)))
        (tmp_path / "metrics.json").write_text(json.dumps(metrics, indent=2))
        assert proc.wait(timeout=12) == 1
        assert event(trace_file, "exited")["exit_code"] == 72
        assert removed(container)


def test_memory_pressure_is_oom_failure(tmp_path):
    script = """
import time
from pathlib import Path
print(Path('/sys/fs/cgroup/memory.max').read_text().strip())
time.sleep(1)
pages = []
while True:
    pages.append(bytearray(16 * 2**20))
"""
    trace_file = tmp_path / "oom.jsonl"
    now = time.monotonic()
    with launch(script, trace_file, now + 40, now + 40) as proc:
        container = until(lambda: event(trace_file, "started"))["container_id"]
        assert until(lambda: logs(container)) == str(2**31)
        assert proc.wait(timeout=15) == 1
        exit_event = event(trace_file, "exited")
        assert exit_event["oom_killed"] and exit_event["exit_code"] == 137
        assert removed(container)


def test_cpu_pressure_is_throttled(tmp_path):
    script = """
import json, os, time
from pathlib import Path

def stats():
    return {key: int(value) for key, value in
            (line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())}

start = stats()
wall = time.monotonic()
children = []
for _ in range(8):
    pid = os.fork()
    if pid == 0:
        while time.monotonic() - wall < 2:
            pass
        os._exit(0)
    children.append(pid)
for pid in children:
    os.waitpid(pid, 0)
elapsed = time.monotonic() - wall
end = stats()
print(json.dumps({'wall_seconds': elapsed, 'cpu_seconds':
                  (end['usage_usec'] - start['usage_usec']) / 1e6,
                  'throttled_periods': end['nr_throttled'] - start['nr_throttled']}))
time.sleep(2)
"""
    trace_file = tmp_path / "cpu.jsonl"
    now = time.monotonic()
    with launch(script, trace_file, now + 40, now + 40) as proc:
        container = until(lambda: event(trace_file, "started"))["container_id"]
        metrics = json.loads(until(lambda: logs(container)))
        assert metrics["throttled_periods"] > 0
        assert metrics["cpu_seconds"] <= 2 * metrics["wall_seconds"] + 0.2
        (tmp_path / "cpu-metrics.json").write_text(json.dumps(metrics, indent=2))
        assert proc.wait(timeout=8) == 0
        assert removed(container)


def test_concurrent_sandboxes_have_separate_work_and_tmp(tmp_path):
    script = """
from pathlib import Path
import time
for directory in ['/work', '/tmp']:
    marker = Path(directory) / 'workspace'
    with marker.open('x') as stream:
        stream.write(WORKSPACE)
print(WORKSPACE)
while True:
    for directory in ['/work', '/tmp']:
        assert (Path(directory) / 'workspace').read_text() == WORKSPACE
    time.sleep(0.025)
"""
    now = time.monotonic()
    trace_a, trace_b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    with (
        launch("WORKSPACE = 'A'\n" + script, trace_a, now + 40, now + 40) as first,
        launch("WORKSPACE = 'B'\n" + script, trace_b, now + 40, now + 40) as second,
    ):
        try:
            a = until(lambda: event(trace_a, "started"))["container_id"]
            b = until(lambda: event(trace_b, "started"))["container_id"]
            assert until(lambda: logs(a)) == "A"
            assert until(lambda: logs(b)) == "B"
            first.stdin.close()
            assert first.wait(timeout=8) == 1
            assert removed(a)
            assert not removed(b)
            assert logs(b) == "B"
        finally:
            second.stdin.close()
            first.stdin.close()
        assert second.wait(timeout=8) == 1
        assert removed(b)


def test_codex_owner_death_reclaims_network_and_auth_copy(tmp_path):
    from uuid import uuid4

    from software_factory.codex_watchdog import AUTH_PREFIX, names

    invocation = uuid4().hex
    container, gateway, external, internal = names(invocation)
    scratch = tmp_path / (AUTH_PREFIX + invocation + "-test")
    scratch.mkdir(mode=0o700)
    ready = tmp_path / "ready"
    launcher = """
import sys, time
from pathlib import Path
from software_factory.codex_container import model_network, owner_guard
from software_factory.codex_watchdog import names
from software_factory.sandbox import DockerRunner

image, invocation, scratch, ready = sys.argv[1:]
container = names(invocation)[0]
with owner_guard(invocation, scratch):
    Path(scratch, "auth.json").write_text("synthetic-credential")
    with model_network(image, ["chatgpt.com"], invocation) as (network, _):
        result = DockerRunner.docker(
            "run", "-d", "--name", container, "--network", network, "--entrypoint", "python",
            image, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)", timeout=10,
        )
        assert result.returncode == 0
        Path(ready).touch()
        time.sleep(60)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", launcher, IMAGE, invocation, str(scratch), str(ready)]
    )
    try:
        until(ready.exists, timeout=20)
        until(lambda: logs(container) == "ready")
        started = time.monotonic()
        owner.kill()
        assert owner.wait(timeout=2) == -9
        until(lambda: removed(container), timeout=8)
        assert 5 <= time.monotonic() - started < 8

        def reclaimed():
            networks = DockerRunner.docker("network", "ls", "--format", "{{.Name}}", timeout=2)
            assert networks.returncode == 0
            return (
                removed(gateway)
                and not ({external, internal} & set(networks.stdout.decode().splitlines()))
                and not scratch.exists()
            )

        until(reclaimed, timeout=10)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=2)
