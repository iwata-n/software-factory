import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from pydantic import BaseModel, ConfigDict

from software_factory.codex_engine import (
    CodexCLI,
    CodexError,
    file_hash,
    parse_events,
    strict_schema,
)

ROOT = Path(__file__).resolve().parents[1]


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def events(value=None):
    return [
        {"type": "thread.started", "thread_id": "test-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "1",
                "type": "agent_message",
                "text": json.dumps(value or {"value": "ok"}),
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
        },
    ]


def encoded(items):
    return "\n".join(json.dumps(item) for item in items).encode()


@pytest.fixture
def cli(tmp_path):
    def make(mode="ok"):
        binary = tmp_path / ("fake-codex-" + mode)
        script = f"""#!{sys.executable}
import json,os,signal,sys,time
if '--version' in sys.argv:
 print('codex-cli test-version');raise SystemExit(0)
payload=sys.stdin.read()
mode={mode!r}
if mode=='hang':
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 time.sleep(60)
if mode=='badexit':
 print('Bearer secret-canary',file=sys.stderr);raise SystemExit(7)
if mode=='badjson':
 print('invalid');raise SystemExit(0)
if mode=='large':
 print('x'*10000);raise SystemExit(0)
items={events()!r}
if mode=='env':
 items[2]['item']['text']=json.dumps({{'value':','.join(sorted(os.environ))}})
for item in items: print(json.dumps(item),flush=True)
if mode=='metered-exit':
 raise SystemExit(7)
"""
        binary.write_text(script)
        binary.chmod(0o700)
        manifest = json.loads((ROOT / "config/codex_cli.json").read_text())
        manifest.update(
            version="test-version",
            binary_sha256=file_hash(binary),
            timeout_seconds=10,
            max_output_bytes=2048,
        )
        return CodexCLI(binary, manifest)

    return make


def test_cli_runs_in_empty_directory_with_pinned_safe_flags(cli):
    engine = cli()
    result = engine.run({"task": "Return ok"}, Reply, Event())
    assert result.value == {"value": "ok"}
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 3
    argv = engine.argv(Path("schema.json"))
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert "--ephemeral" in argv and "read-only" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert engine._process is None


def test_control_plane_github_and_api_credentials_are_not_in_cli_environment(cli, monkeypatch):
    for key in [
        "FACTORY_DATABASE_URL",
        "FACTORY_USER_TOKEN",
        "FACTORY_WORKER_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    ]:
        monkeypatch.setenv(key, "credential-canary")
    names = cli("env").run({"task": "inspect env"}, Reply, Event()).value["value"].split(",")
    assert "CODEX_HOME" in names and "FACTORY_WORKER_TOKEN" not in names
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"} & set(names)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("badexit", "CODEX_PROCESS_FAILED"),
        ("badjson", "CODEX_EVENT_SCHEMA_INVALID"),
        ("large", "CODEX_OUTPUT_TOO_LARGE"),
    ],
)
def test_process_failures_never_become_success_or_echo_stderr(cli, mode, code):
    engine = cli(mode)
    with pytest.raises(CodexError) as caught:
        engine.run({"task": "safe"}, Reply, Event())
    assert caught.value.code == code
    assert "secret-canary" not in str(caught.value) and engine._process is None


def test_timeout_stops_term_ignoring_cli(cli):
    engine = cli("hang")
    started = time.monotonic()
    with pytest.raises(CodexError, match="CODEX_TIMEOUT"):
        engine.run({"task": "safe"}, Reply, Event(), timeout=0.1)
    assert time.monotonic() - started < 8 and engine._process is None


def test_cancel_stops_running_cli_and_next_call_is_not_cancelled(cli):
    engine, stopped = cli("hang"), Event()
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(engine.run, {"task": "safe"}, Reply, stopped)
        deadline = time.monotonic() + 5
        while engine._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped.set()
        with pytest.raises(CodexError, match="CODEX_CANCELLED"):
            pending.result(timeout=8)
    assert engine._process is None


def test_concurrent_call_is_rejected_without_cancelling_owner(cli):
    engine, stop = cli("hang"), Event()
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(engine.run, {"task": "safe"}, Reply, stop)
        deadline = time.monotonic() + 5
        while engine._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        process = engine._process
        with pytest.raises(CodexError, match="CODEX_ALREADY_RUNNING"):
            engine.run({"task": "safe"}, Reply, Event())
        assert process.poll() is None
        stop.set()
        with pytest.raises(CodexError, match="CODEX_CANCELLED"):
            owner.result(timeout=8)


def test_binary_replacement_is_rejected_before_execution(cli):
    engine = cli()
    Path(engine.binary).write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(CodexError, match="CODEX_BINARY_DIGEST_MISMATCH"):
        engine.run({"task": "safe"}, Reply, Event())


@pytest.mark.parametrize(
    "attack",
    [
        "usage_missing",
        "usage_negative",
        "unknown_event",
        "tool",
        "duplicate_turn",
        "non_json_message",
    ],
)
def test_event_stream_rejects_missing_or_forged_success(attack):
    items = events()
    if attack == "usage_missing":
        items.pop()
    elif attack == "usage_negative":
        items[-1]["usage"]["input_tokens"] = -1
    elif attack == "unknown_event":
        items.insert(2, {"type": "future.mandatory.event"})
    elif attack == "tool":
        items[2]["item"]["type"] = "command_execution"
    elif attack == "duplicate_turn":
        items.append(items[-1])
    else:
        items[2]["item"]["text"] = "not json"
    with pytest.raises(CodexError):
        parse_events(encoded(items))


def test_invalid_final_json_keeps_real_usage_for_failure_settlement():
    items = events()
    items[2]["item"]["text"] = "broken"
    with pytest.raises(CodexError) as caught:
        parse_events(encoded(items))
    assert caught.value.usage.output_tokens == 3


def test_sensitive_context_and_pre_cancel_never_start_provider(cli):
    engine = cli()
    with pytest.raises(CodexError, match="SENSITIVE_MODEL_INPUT"):
        engine.run({"content": "Bearer secret-canary"}, Reply, Event())
    stopped = Event()
    stopped.set()
    with pytest.raises(CodexError, match="CODEX_CANCELLED"):
        engine.run({"task": "safe"}, Reply, stopped)
    assert engine._process is None


def test_json_schema_requires_nullable_and_defaulted_properties():
    result = strict_schema(
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "default": "a"},
                "b": {"anyOf": [{"type": "null"}]},
            },
        }
    )
    assert result["required"] == ["a", "b"] and result["additionalProperties"] is False
    assert "default" not in result["properties"]["a"]


@pytest.mark.parametrize(
    "secret",
    [
        'API_KEY = "quoted-secret-canary"',
        '{"password": "quoted-secret-canary"}',
        "access_token = 'quoted-secret-canary'",
    ],
)
def test_quoted_source_credentials_are_rejected_before_serializing_context(cli, secret):
    engine = cli()
    with pytest.raises(CodexError, match="SENSITIVE_MODEL_INPUT"):
        engine.run({"source_files": {"src/config.py": secret}}, Reply, Event())
    assert engine._process is None


def test_nonzero_exit_preserves_validated_usage(cli):
    engine = cli("metered-exit")
    with pytest.raises(CodexError, match="CODEX_PROCESS_FAILED") as caught:
        engine.run({"task": "safe"}, Reply, Event())
    assert caught.value.category == "INFRASTRUCTURE"
    assert caught.value.usage.input_tokens == 10
    assert caught.value.thread_id == "test-thread"


def test_cancel_during_manifest_check_does_not_launch_provider(cli, monkeypatch):
    engine = cli()
    cancelled = Event()

    def cancel_during_verification():
        cancelled.set()

    def reject_launch(*args, **kwargs):
        pytest.fail("cancelled engine attempted to launch the provider")

    monkeypatch.setattr(engine, "verify_binary", cancel_during_verification)
    monkeypatch.setattr(engine, "argv", reject_launch)
    with pytest.raises(CodexError, match="CODEX_CANCELLED") as caught:
        engine.run({"task": "safe"}, Reply, cancelled)
    assert caught.value.before_launch and caught.value.usage is None
    assert engine._process is None
