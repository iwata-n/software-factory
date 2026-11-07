"""A2UI v0.9.1 specification interviews for blocked plans."""

from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, Field, ValidationError
from sqlalchemy import select

from .codex_engine import sensitive_input
from .contracts import Contract, TaskSpec
from .db import db_now
from .models import Run, Task, TaskChatTurn, Workspace
from .service import DomainError, owned
from .task_chat import (
    ACTIVE,
    chat_limit_reached,
    eligible,
    enqueue_turn,
    interview_spec,
    interview_turns,
    latest_plan,
    plan_questions,
    turn_view,
)
from .task_chat_worker import InterviewReply, TaskAnswerOptions
from .web_accounts import browser_write

VERSION = "v0.9.1"
CATALOG = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
CONTINUE = "continue_interview"
ADOPT = "adopt_specification"
REFINE = "refine_specification"
OTHER = "other"


class AnswerValue(Contract):
    choice: list[Literal["1", "2", "3", "other"]] = Field(max_length=1)
    text: Annotated[str, Field(max_length=1200)]


class ActionContext(Contract):
    revision: Annotated[int, Field(strict=True, ge=1)]
    spec_version: Annotated[int, Field(strict=True, ge=1)]
    turn_id: str | None = None
    proposal_id: str | None = None
    answers: dict[Literal["1"], AnswerValue] = Field(default_factory=dict, max_length=1)


class SurfaceAction(Contract):
    name: Literal["continue_interview", "adopt_specification", "refine_specification"]
    surface_id: Annotated[str, Field(alias="surfaceId", max_length=64)]
    component_id: Annotated[str, Field(alias="sourceComponentId", max_length=64)]
    timestamp: AwareDatetime
    context: ActionContext


class ActionEnvelope(Contract):
    version: Literal["v0.9.1"]
    action: SurfaceAction


def proposal(session, run, plan):
    return session.scalar(
        select(TaskChatTurn)
        .where(
            TaskChatTurn.workspace_id == run.workspace_id,
            TaskChatTurn.run_id == run.id,
            TaskChatTurn.plan_id == plan.id,
            TaskChatTurn.interview.is_(None),
        )
        .order_by(TaskChatTurn.created_at.desc(), TaskChatTurn.id.desc())
        .limit(1)
    )


def options_for_first_question(turn):
    if not turn or turn.status != "ANSWERED":
        return []
    reply = TaskAnswerOptions.model_validate_json(turn.answer)
    return next((row.options for row in reply.questions if row.question_number == 1), [])


def interview_state(session, run, plan, task, questions):
    version = task.latest_spec_version
    turns = interview_turns(session, run, plan, version)
    if not turns and version > 1:
        previous = interview_turns(session, run, plan, version - 1)
        if previous and previous[-1].interview.get("adopted_version") == version:
            turns = previous
            version -= 1
    latest = turns[-1] if turns else None
    completed = next((turn for turn in reversed(turns) if turn.status == "ANSWERED"), None)
    reply = InterviewReply.model_validate_json(completed.answer) if completed else None
    initial_proposal = proposal(session, run, plan) if not completed else None
    follow_up = reply.follow_up if reply else None
    return {
        "version": version,
        "turns": turns,
        "latest": latest,
        "surface": f"attention-{completed.id if completed else plan.id}",
        "proposal": initial_proposal,
        "question": follow_up.question if follow_up else questions[0],
        "options": follow_up.options if follow_up else options_for_first_question(initial_proposal),
        "specification": reply.specification if reply else None,
    }


def attention_messages(run, state, *, review=False):
    surface = state["surface"]
    latest = state["latest"]
    scope = {
        "revision": run.state_revision,
        "spec_version": state["version"],
        "turn_id": latest.id if latest else None,
    }
    data = {"answers": {}}
    if review:
        name, label = ADOPT, "この仕様を採用"
        data["answers"]["1"] = {"choice": [OTHER], "text": ""}
        components = [
            {
                "id": "feedback",
                "component": "TextField",
                "label": "仕様案の修正・追加で確認したいこと",
                "variant": "longText",
                "value": {"path": "/answers/1/text"},
            },
            {"id": "refine-label", "component": "Text", "text": "仕様案の修正を依頼"},
            {
                "id": REFINE,
                "component": "Button",
                "child": "refine-label",
                "variant": "borderless",
                "action": {
                    "event": {
                        "name": REFINE,
                        "context": scope | {"answers": {"path": "/answers"}},
                    },
                },
            },
        ]
    else:
        name, label = CONTINUE, "回答を送る"
        data["answers"]["1"] = {"choice": [], "text": ""}
        scope["answers"] = {"path": "/answers"}
        if state["proposal"] and state["options"]:
            scope["proposal_id"] = state["proposal"].id
        components = [
            {"id": "question", "component": "Text", "variant": "h4", "text": state["question"]},
            {
                "id": "choices",
                "component": "ChoicePicker",
                "variant": "mutuallyExclusive",
                "label": "回答候補",
                "options": [
                    {
                        "label": text
                        + (
                            "（推奨）"
                            if number == 1 and not text.endswith(("（推奨）", "(推奨)"))
                            else ""
                        ),
                        "value": str(number),
                    }
                    for number, text in enumerate(state["options"], 1)
                ]
                + [{"label": "その他", "value": OTHER}],
                "value": {"path": "/answers/1/choice"},
            },
            {
                "id": "answer",
                "component": "TextField",
                "label": "回答・補足",
                "variant": "longText",
                "value": {"path": "/answers/1/text"},
            },
        ]
    children = [
        component["id"] for component in components if component["id"] != "refine-label"
    ] + [name]
    return [
        {"version": VERSION, "createSurface": {"surfaceId": surface, "catalogId": CATALOG}},
        {
            "version": VERSION,
            "updateComponents": {
                "surfaceId": surface,
                "components": [
                    {"id": "root", "component": "Column", "children": children},
                    *components,
                    {"id": f"{name}-label", "component": "Text", "text": label},
                    {
                        "id": name,
                        "component": "Button",
                        "child": f"{name}-label",
                        "variant": "primary",
                        "action": {"event": {"name": name, "context": scope}},
                    },
                ],
            },
        },
        {"version": VERSION, "updateDataModel": {"surfaceId": surface, "value": data}},
    ]


def answer_text(value, options):
    if value is None:
        raise DomainError(422, "ATTENTION_ANSWER_REQUIRED")
    choice = value.choice[0] if value.choice else OTHER
    if choice == OTHER:
        answer = value.text
    else:
        if int(choice) > len(options):
            raise DomainError(409, "ATTENTION_SURFACE_STALE")
        answer = "\n".join(filter(None, (options[int(choice) - 1], value.text)))
    if not answer:
        raise DomainError(422, "ATTENTION_ANSWER_REQUIRED")
    if sensitive_input(answer):
        raise DomainError(422, "SENSITIVE_INPUT")
    return answer


def attention_router(service, token_type, codex_workspace):
    router = APIRouter(prefix="/v1/runs/{run_id}/attention")

    def subject(session, actor, run_id):
        run = owned(session, Run, run_id, actor.workspace_id)
        plan = latest_plan(session, run) if run.status == "BLOCKED" else None
        return run, plan, plan_questions(plan)

    def available(run):
        return run.workspace_id == codex_workspace and eligible(run)

    @router.get("")
    def get_surface(run_id: str, token: token_type, response: Response):
        response.headers["Cache-Control"] = "no-store"

        def query(session, actor):
            run, plan, questions = subject(session, actor, run_id)
            if not questions or actor.user_access == "VIEWER" or not available(run):
                return {"messages": [], "pending": False, "available": False}
            task = owned(session, Task, run.task_id, actor.workspace_id)
            state = interview_state(session, run, plan, task, questions)
            latest = state["latest"]
            now = db_now(session)
            status = turn_view(latest, now)["status"] if latest else None
            specification = state["specification"]
            adopted = latest.interview.get("adopted_version") if latest else None
            resumed = (
                session.scalar(
                    select(Run.id)
                    .where(
                        Run.workspace_id == run.workspace_id,
                        Run.task_id == run.task_id,
                        Run.spec_version == adopted,
                    )
                    .order_by(Run.created_at.desc(), Run.id.desc())
                    .limit(1)
                )
                if adopted
                else None
            )
            initial = state["proposal"]
            waiting_reason = None
            if not latest and not initial:
                if session.get(Workspace, run.workspace_id).execution_stopped:
                    waiting_reason = "WORKSPACE_EXECUTION_STOPPED"
                elif chat_limit_reached(session, run.workspace_id, now):
                    waiting_reason = "TASK_CHAT_DAILY_LIMIT"
            proposing = not latest and (
                turn_view(initial, now)["status"] in ACTIVE if initial else waiting_reason is None
            )
            messages = (
                []
                if adopted
                or status in ACTIVE
                or (not latest and not run.snapshot["task_spec"]["task"]["acceptance_criteria"])
                else attention_messages(run, state, review=specification is not None)
            )
            return {
                "available": True,
                "messages": messages,
                "pending": status in ACTIVE or proposing,
                "status": status,
                "proposing": proposing,
                "waiting_reason": waiting_reason,
                "surface_id": state["surface"],
                "history": [
                    {"question": turn.interview["question"], "answer": turn.question}
                    for turn in state["turns"]
                    if not turn.interview.get("bootstrap")
                    and (turn.status == "ANSWERED" or turn is latest)
                ],
                "specification": specification,
                "adopted_version": adopted,
                "resumed_run_id": resumed,
            }

        return service.read(token, query)

    @router.post("/actions")
    def answer(run_id: str, data: ActionEnvelope, request: Request, token: token_type):
        browser_write(request)
        action = data.action
        context = action.context

        def command(session, actor):
            run, plan, questions = subject(session, actor, run_id)
            if not available(run):
                raise DomainError(409, "TASK_CHAT_UNAVAILABLE")
            if not questions or context.revision != run.state_revision:
                raise DomainError(409, "ATTENTION_SURFACE_STALE")
            task = owned(session, Task, run.task_id, actor.workspace_id)
            state = interview_state(session, run, plan, task, questions)
            latest = state["latest"]
            if (
                action.surface_id != state["surface"]
                or action.component_id != action.name
                or context.spec_version != state["version"]
                or context.turn_id != (latest.id if latest else None)
            ):
                raise DomainError(409, "ATTENTION_SURFACE_STALE")
            specification = state["specification"]
            if latest and turn_view(latest, db_now(session))["status"] in ACTIVE:
                raise DomainError(409, "TASK_CHAT_BUSY")
            if action.name == ADOPT and specification:
                if adopted := latest.interview.get("adopted_version"):
                    return {"spec_version": adopted}
                base = interview_spec(session, run, state["version"])
                try:
                    spec = TaskSpec.model_validate(
                        base.body | {"task": base.body["task"] | specification.model_dump()}
                    )
                except ValidationError:
                    raise DomainError(422, "INTERVIEW_SPECIFICATION_INVALID") from None
                result = service.new_spec(session, actor, task.id, spec)
                task.clarified_spec_version = result["spec_version"]
                latest.interview = latest.interview | {"adopted_version": result["spec_version"]}
                return {"spec_version": result["spec_version"]}
            refining = action.name == REFINE and specification is not None
            if (
                latest
                and latest.interview.get("adopted_version")
                or not (refining or (action.name == CONTINUE and specification is None))
            ):
                raise DomainError(409, "ATTENTION_SURFACE_STALE")
            initial = state["proposal"]
            options = state["options"] if not refining else []
            if initial and initial.id != context.proposal_id:
                options = []
            answer = answer_text(context.answers.get("1"), options)
            turn = enqueue_turn(
                session,
                run,
                answer,
                plan_id=plan.id,
                interview={
                    "revision": run.state_revision,
                    "spec_version": state["version"],
                    "question": "仕様案への修正・確認事項" if refining else state["question"],
                },
            )
            return {"id": turn.id}

        return service.write(
            token,
            "USER",
            f"attention:{run_id}:{action.name}",
            request.headers.get("Idempotency-Key"),
            data.model_dump(mode="json", exclude={"action": {"timestamp"}}),
            command,
        )

    return router
