"""Protocol and isolated Codex worker for task conversations."""

import base64
import json
import time
from threading import Event
from typing import Annotated, Literal

import httpx
from pydantic import Field, model_validator

from .codex_adapter import archive_files
from .codex_engine import CodexError
from .contracts import Contract, Criterion, ModelCallUsage
from .project_reader import aggregate_usage, run_project_model
from .sandbox import STOP_UNCONFIRMED

CHAT_TIMEOUT = 90
Answer = Annotated[str, Field(min_length=1, max_length=8000)]


class TaskChatReply(Contract):
    answer: Answer


class QuestionOptions(Contract):
    question_number: Annotated[int, Field(ge=1, le=8, strict=True)]
    options: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=3
    )


class TaskAnswerOptions(Contract):
    questions: list[QuestionOptions] = Field(max_length=8)

    @model_validator(mode="after")
    def distinct_questions(self):
        numbers = [question.question_number for question in self.questions]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Question numbers must be unique")
        return self


class InterviewQuestion(Contract):
    question: Annotated[str, Field(min_length=1, max_length=600)]
    options: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=2, max_length=3
    )


class InterviewSpec(Contract):
    objective: Annotated[str, Field(min_length=1, max_length=3000)]
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_criteria(self):
        identifiers = [item.id for item in self.acceptance_criteria]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Acceptance criterion IDs must be unique")
        return self


class InterviewReply(Contract):
    follow_up: InterviewQuestion | None
    specification: InterviewSpec | None

    @model_validator(mode="after")
    def question_or_specification(self):
        if (self.follow_up is None) == (self.specification is None):
            raise ValueError("Return either a follow-up or a specification")
        return self


class TaskChatOutcome(Contract):
    answer: Annotated[str, Field(min_length=1, max_length=500000)] | None = None
    error: Literal["CODEX_AUTH_OR_POLICY", "CODEX_UNAVAILABLE", "CODEX_NO_USAGE"] | None = None
    usage: ModelCallUsage | None = None
    calls: list[ModelCallUsage] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def complete_outcome(self):
        if (self.answer is None) == (self.error is None) or (
            self.answer is not None and self.usage is None
        ):
            raise ValueError("Chat outcome must include an answer and usage or an error")
        if len({call.call_id for call in self.calls}) != len(self.calls):
            raise ValueError("Model call IDs must be unique")
        if self.calls and self.usage:
            measured = aggregate_usage(self.calls)
            if any(
                getattr(measured, key) != getattr(self.usage, key)
                for key in ("input_tokens", "cached_input_tokens", "output_tokens")
            ):
                raise ValueError("Combined usage must match the recorded model calls")
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
            if job.get("interview"):
                schema = InterviewReply
            elif job.get("answer_options"):
                schema = TaskAnswerOptions
            else:
                schema = TaskChatReply
            if job.get("project_source"):
                source = (
                    client.get(f"/v1/worker/task-chat/{job['id']}/source", timeout=120)
                    .raise_for_status()
                    .json()
                )
                files = archive_files(
                    base64.b64decode(source["archive"], validate=True), source["source_digest"]
                )
                access = job["project_source"]
                reply = run_project_model(
                    engine,
                    job["context"],
                    schema,
                    Event(),
                    files=files,
                    read_paths=access["read_paths"],
                    write_paths=access["write_paths"],
                    timeout=CHAT_TIMEOUT,
                )
            else:
                reply = engine.run(job["context"], schema, Event(), timeout=CHAT_TIMEOUT)
            answer = (
                reply.value["answer"]
                if schema is TaskChatReply
                else json.dumps(reply.value, ensure_ascii=False)
            )
            result = (
                TaskChatOutcome(
                    answer=answer, usage=reply.usage, calls=list(getattr(reply, "calls", ()))
                )
                if reply.usage is not None
                else TaskChatOutcome(error="CODEX_NO_USAGE")
            )
        except CodexError as error:
            stopped = error.code == STOP_UNCONFIRMED
            result = TaskChatOutcome(
                error="CODEX_AUTH_OR_POLICY" if error.category == "POLICY" else "CODEX_UNAVAILABLE",
                usage=error.usage,
                calls=list(error.calls),
            )
        except (httpx.HTTPError, ValueError, KeyError):
            result = TaskChatOutcome(error="CODEX_UNAVAILABLE")
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
