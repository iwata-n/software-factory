import json
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from software_factory.codex_engine import CodexError, CodexResponse
from software_factory.contracts import Contract, ModelCallUsage
from software_factory.project_reader import run_project_model


class Reply(Contract):
    answer: str


class Engine:
    manifest = SimpleNamespace(max_input_bytes=18000)

    def __init__(self, responses):
        self.responses, self.contexts = iter(responses), []

    def run(self, context, schema, cancelled, *, timeout):
        assert timeout > 0
        assert len(json.dumps(context, ensure_ascii=False).encode()) <= 14000
        self.contexts.append(context)
        output = next(self.responses)
        if isinstance(output, Exception):
            raise output
        # Exercise the actual model output contract, including extra-property rejection.
        schema.model_validate(output)
        usage = ModelCallUsage(
            call_id=f"call-{len(self.contexts)}", input_tokens=10, output_tokens=3
        )
        return CodexResponse(output, usage, usage.call_id)


def read(path, start=1, count=20):
    return {"path": path, "start_line": start, "line_count": count}


def request(*reads):
    return {"read_requests": list(reads), "result": None}


FINISH = {"read_requests": [], "result": {"answer": "Source inspected"}}


def execute(engine, files, roots=None, cancelled=None):
    return run_project_model(
        engine,
        {"role": "PLAN"},
        Reply,
        cancelled or Event(),
        files=files,
        read_paths=roots or ["."],
        write_paths=["."],
        timeout=10,
    )


def test_project_reads_files_outside_write_scope_without_modifying_source():
    files = {
        "src/api.py": ("100644", b"first\nsecond\nthird\n"),
        "tests/test_api.py": ("100644", b"assert True\n"),
        "auth/session.py": ("100644", b"enabled = True\n"),
    }
    before = files.copy()
    engine = Engine([request(read("src/api.py", 2, 1), read("tests/test_api.py")), FINISH])
    reply = execute(engine, files)
    assert reply.value == {"answer": "Source inspected"}
    assert {item["path"] for item in engine.contexts[0]["project_file_index"]} == set(files)
    assert engine.contexts[0]["project_access"]["mode"] == "read_only"
    assert engine.contexts[0]["source_files"] == {}
    assert engine.contexts[1]["project_file_excerpts"] == [
        {"path": "src/api.py", "start_line": 2, "content": "second\n", "error": None},
        {"path": "tests/test_api.py", "start_line": 1, "content": "assert True\n", "error": None},
    ]
    assert files == before
    assert [call.call_id for call in reply.calls] == ["call-1", "call-2"]
    assert reply.usage.input_tokens == 20 and reply.usage.output_tokens == 6


@pytest.mark.parametrize(
    "path", ["../outside", "/etc/passwd", ".git/config", "src/.GiT/config", "docs/a"]
)
def test_cannot_request_files_outside_read_scope(path):
    engine = Engine([request(read(path))])
    with pytest.raises(CodexError, match="PROJECT_READ_PATH_DENIED") as failure:
        execute(engine, {"src/api.py": ("100644", b"value = 1\n")}, roots=["src"])
    assert failure.value.usage.input_tokens == 10
    assert len(failure.value.calls) == 1


def test_symlink_binary_missing_and_secrets_are_not_exposed():
    files = {
        "link": ("120000", b"/etc/passwd"),
        "blob": ("100644", b"\xff\x00"),
        "config.py": ("100644", b'api_key = "secret-canary"\npublic = True\n'),
    }
    engine = Engine(
        [request(read("link"), read("blob"), read("missing"), read("config.py")), FINISH]
    )
    execute(engine, files)
    excerpts = engine.contexts[1]["project_file_excerpts"]
    assert all(item["content"] is None and item["error"] for item in excerpts[:3])
    assert "secret-canary" not in json.dumps(engine.contexts)
    assert "[REDACTED]" in excerpts[3]["content"]
    assert excerpts[3]["content"].splitlines()[1] == "public = True"


def test_large_excerpt_returns_bounded_error_and_can_retry_fewer_lines():
    engine = Engine([request(read("large", count=2)), request(read("large", 2, 1)), FINISH])
    execute(engine, {"large": ("100644", b"x" * 15000 + b"\nsmall\n")})
    assert engine.contexts[1]["project_file_excerpts"][0]["error"]
    assert engine.contexts[2]["project_file_excerpts"][-1]["content"] == "small\n"


def test_read_budget_requires_final_result_and_records_all_measured_calls():
    engine = Engine([request(read("src/a"))] * 15 + [{"answer": "Source inspected"}])
    result = execute(engine, {"src/a": ("100644", b"content")})
    assert result.value == {"answer": "Source inspected"}
    assert len(result.calls) == 16 and result.usage.input_tokens == 160


def test_unknown_later_usage_does_not_become_final_known_usage():
    engine = Engine([request(read("src/a")), CodexError("CODEX_NO_USAGE")])
    with pytest.raises(CodexError) as failure:
        execute(engine, {"src/a": ("100644", b"content")})
    assert failure.value.usage is None and len(failure.value.calls) == 1
    assert failure.value.before_launch is False


def test_measured_failure_aggregates_earlier_calls():
    usage = ModelCallUsage(call_id="failed-call", input_tokens=7, output_tokens=1)
    engine = Engine([request(read("src/a")), CodexError("CODEX_INVALID_OUTPUT", usage=usage)])
    with pytest.raises(CodexError) as failure:
        execute(engine, {"src/a": ("100644", b"content")})
    assert failure.value.usage.input_tokens == 17
    assert len(failure.value.calls) == 2


def test_cancelled_reader_never_calls_engine():
    cancelled = Event()
    cancelled.set()
    engine = Engine([])
    with pytest.raises(CodexError, match="CODEX_CANCELLED"):
        execute(engine, {}, cancelled=cancelled)
    assert engine.contexts == []


def test_edit_output_is_not_accepted_during_read_only_interview():
    engine = Engine([FINISH | {"changes": [{"path": "src/a", "content": "bad"}]}])
    with pytest.raises(CodexError, match="PROJECT_READ_RESPONSE_INVALID"):
        execute(engine, {})
    with pytest.raises(ValidationError):
        # Reader requests cannot ask for hundreds of thousands of lines at once.
        from software_factory.project_reader import FileRead

        FileRead.model_validate(read("src/a", count=301))


def test_literal_search_finds_exact_anchors_without_loading_entire_files():
    engine = Engine(
        [
            {
                "read_requests": [],
                "search_requests": [{"query": "def owned", "path": "src", "max_matches": 1}],
                "result": None,
            },
            FINISH,
        ]
    )
    files = {
        "src/service.py": (
            "100644",
            b"# header\ndef owned():\n    pass\n\ndef owned_other():\n    pass\n",
        ),
        "docs/service.md": ("100644", b"def owned in docs\n"),
    }
    execute(engine, files)
    result = engine.contexts[1]["project_search_results"][0]
    assert result["truncated"] and len(result["matches"]) == 1
    assert result["matches"][0]["path"] == "src/service.py"
    assert "def owned():" in result["matches"][0]["content"]


def test_search_respects_read_scope_and_never_follows_links():
    engine = Engine(
        [
            {
                "read_requests": [],
                "search_requests": [{"query": "hidden", "path": None, "max_matches": 10}],
                "result": None,
            },
            FINISH,
        ]
    )
    execute(
        engine,
        {
            "src/hidden.py": ("100644", b"hidden\n"),
            "docs/link": ("120000", b"hidden"),
            "docs/spec": ("100644", b"public\n"),
        },
        roots=["docs"],
    )
    assert engine.contexts[1]["project_search_results"][0]["matches"] == []


def test_overlapping_reads_are_merged_without_duplicate_source_context():
    engine = Engine([request(read("src/a", 1, 3)), request(read("src/a", 2, 3)), FINISH])
    execute(engine, {"src/a": ("100644", b"one\ntwo\nthree\nfour\nfive\n")})
    excerpts = engine.contexts[2]["project_file_excerpts"]
    assert len(excerpts) == 1 and excerpts[0]["start_line"] == 1
    assert excerpts[0]["content"] == "one\ntwo\nthree\nfour\n"


def test_validation_retry_retains_source_and_records_usage_without_rereading():
    engine = Engine([request(read("src/a")), FINISH, {"answer": "corrected"}])
    checked = []

    def validate(value):
        checked.append(value["answer"])
        if value["answer"] != "corrected":
            raise CodexError("MODEL_REJECTED", details={"path": "src/a"})

    result = run_project_model(
        engine,
        {},
        Reply,
        Event(),
        files={"src/a": ("100644", b"source\n")},
        read_paths=["."],
        write_paths=["."],
        timeout=10,
        validate_result=validate,
    )
    assert result.value == {"answer": "corrected"} and len(result.calls) == 3
    assert result.usage.input_tokens == 30
    assert checked == ["Source inspected", "corrected"]
    assert (
        engine.contexts[2]["project_file_excerpts"] == engine.contexts[1]["project_file_excerpts"]
    )
    assert engine.contexts[2]["generation_validation_error"]["code"] == "MODEL_REJECTED"


def test_validation_self_correction_is_bounded_and_keeps_all_known_usage():
    engine = Engine([FINISH, {"answer": "bad"}, {"answer": "bad"}])

    def reject(value):
        raise CodexError("MODEL_REJECTED")

    with pytest.raises(CodexError, match="MODEL_REJECTED") as caught:
        run_project_model(
            engine,
            {},
            Reply,
            Event(),
            files={},
            read_paths=["."],
            write_paths=["."],
            timeout=10,
            validate_result=reject,
        )
    assert caught.value.usage.input_tokens == 30 and len(caught.value.calls) == 3
    assert len(engine.contexts) == 3
