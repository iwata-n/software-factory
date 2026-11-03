import pytest
from pydantic import BaseModel

from software_factory.codex_app_server import TOOL_NAME, structured_turn
from software_factory.codex_engine import CodexError


class Result(BaseModel):
    answer: str


def drive(calls):
    scope = {"threadId": "thread", "turnId": "turn"}
    request = scope | {"tool": TOOL_NAME, "callId": "tool-1", "arguments": {"id": "AC-1"}}
    events = iter(
        [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread"}}},
            {"id": 3, "result": {"turn": {"id": "turn"}}},
            *[
                {"id": n, "method": "item/tool/call", "params": request | change}
                for n, change in enumerate(calls, start=10)
            ],
            {
                "method": "thread/tokenUsage/updated",
                "params": scope
                | {
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 2,
                            "cachedInputTokens": 0,
                            "outputTokens": 3,
                        }
                    }
                },
            },
            {
                "method": "item/completed",
                "params": scope | {"item": {"type": "agentMessage", "text": '{"answer":"ok"}'}},
            },
            {
                "method": "turn/completed",
                "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
            },
        ]
    )
    outgoing = []
    context = {"task": {"acceptance_criteria": [{"id": "AC-1", "description": "example"}]}}
    return structured_turn(
        outgoing.append, lambda: next(events), "model", "low", context, Result
    ), outgoing


def test_tool_call_is_counted_by_provider_id_even_when_request_repeats():
    response, outgoing = drive([{}, {}])
    assert response.value == {"answer": "ok"}
    assert response.usage.model_dump()["tool_calls"] == [{"call_id": "tool-1", "name": TOOL_NAME}]
    assert response.usage.input_tokens == 2 and response.usage.output_tokens == 3
    assert [message["result"]["contentItems"] for message in outgoing if "result" in message] == [
        [{"type": "inputText", "text": "example"}],
        [{"type": "inputText", "text": "example"}],
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "shell"},
        {"threadId": "foreign"},
        {"turnId": "foreign"},
        {"namespace": "external"},
        {"arguments": {"id": "AC-2"}},
    ],
)
def test_untrusted_tool_requests_are_denied(change):
    with pytest.raises(CodexError, match="CODEX_TOOL_ACTIVITY_DENIED"):
        drive([change])
