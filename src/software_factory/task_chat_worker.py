"""Protocol and isolated Codex worker for task conversations."""

import time
from threading import Event
from typing import Annotated, Literal

import httpx
from pydantic import Field, model_validator

from .codex_engine import CodexError
from .contracts import Contract, ModelCallUsage
from .sandbox import STOP_UNCONFIRMED

CHAT_TIMEOUT = 90
Answer = Annotated[str, Field(min_length=1, max_length=8000)]


class TaskChatReply(Contract):
    answer: Answer


class TaskChatOutcome(Contract):
    answer: Answer | None = None
    error: Literal["CODEX_AUTH_OR_POLICY", "CODEX_UNAVAILABLE", "CODEX_NO_USAGE"] | None = None
    usage: ModelCallUsage | None = None

    @model_validator(mode="after")
    def complete_outcome(self):
        if (self.answer is None) == (self.error is None) or (
            self.answer is not None and self.usage is None
        ):
            raise ValueError("Chat outcome must include an answer and usage or an error")
        return self


def claim_chat(client, engine):
    response = client.post("/v1/worker/task-chat/claim", timeout=10)
    if response.status_code == 204:
        return False
    response.raise_for_status()
    job = response.json()
    stopped = False
    if job["manifest_hash"] != engine.manifest_hash:
        result = TaskChatOutcome(error="CODEX_AUTH_OR_POLICY")
    else:
        try:
            reply = engine.run(job["context"], TaskChatReply, Event(), timeout=CHAT_TIMEOUT)
            result = (
                TaskChatOutcome(answer=reply.value["answer"], usage=reply.usage)
                if reply.usage is not None
                else TaskChatOutcome(error="CODEX_NO_USAGE")
            )
        except CodexError as error:
            stopped = error.code == STOP_UNCONFIRMED
            result = TaskChatOutcome(
                error="CODEX_AUTH_OR_POLICY" if error.category == "POLICY" else "CODEX_UNAVAILABLE",
                usage=error.usage,
            )
    for attempt in range(3):
        try:
            client.put(
                f"/v1/worker/task-chat/{job['id']}", json=result.model_dump(mode="json"), timeout=10
            ).raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            retryable = isinstance(error, httpx.TransportError) or error.response.status_code >= 500
            if stopped or not retryable or attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))
    if stopped:
        raise RuntimeError("Worker quarantined after an unconfirmed chat stop")
    return True
