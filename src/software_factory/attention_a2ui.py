"""A2UI v0.9.1 surfaces for answering a blocked plan's questions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, Field
from sqlalchemy import select

from .codex_engine import sensitive_input
from .contracts import Contract
from .db import db_now
from .models import Run, Task, TaskChatTurn, Workspace
from .service import DomainError, owned
from .task_chat import chat_limit_reached, eligible, latest_plan, plan_questions, turn_view
from .task_chat_worker import TaskAnswerOptions
from .web_accounts import browser_write

VERSION = "v0.9.1"
CATALOG = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
DRAFT = "draft_answers"
OTHER = "other"


class AnswerValue(Contract):
    choice: list[Literal["1", "2", "3", "other"]] = Field(max_length=1)
    text: Annotated[str, Field(max_length=1200)]


class ActionContext(Contract):
    revision: Annotated[int, Field(strict=True, ge=1)]
    spec_version: Annotated[int, Field(strict=True, ge=1)]
    proposal_id: str | None = None
    answers: dict[str, AnswerValue] = Field(default_factory=dict, max_length=100)


class SurfaceAction(Contract):
    name: Literal["draft_answers"]
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
        )
        .order_by(TaskChatTurn.created_at.desc(), TaskChatTurn.id.desc())
        .limit(1)
    )


def options_by_question(turn):
    if not turn or turn.status != "ANSWERED":
        return {}
    reply = TaskAnswerOptions.model_validate_json(turn.answer)
    return {row.question_number: row.options for row in reply.questions}


def surface_id(plan):
    return f"attention-{plan.id}"


def attention_messages(run, plan, questions, task, turn, state):
    surface = surface_id(plan)
    components = []
    children = []
    options = options_by_question(turn)
    data = {
        "questions": questions,
        "answers": {
            str(index): {"choice": [], "text": ""} for index in range(1, len(questions) + 1)
        },
    }
    scope = {
        "revision": run.state_revision,
        "spec_version": task.latest_spec_version,
    }

    def add(identifier, component, **properties):
        components.append({"id": identifier, "component": component, **properties})
        children.append(identifier)

    def button(name, label, context, variant):
        components.append({"id": f"{name}-label", "component": "Text", "text": label})
        add(
            name,
            "Button",
            child=f"{name}-label",
            variant=variant,
            action={"event": {"name": name, "context": context}},
        )

    for index in range(1, len(questions) + 1):
        add(f"question-{index}", "Text", variant="h4", text={"path": f"/questions/{index - 1}"})
        choices = options.get(index, [])
        add(
            f"choices-{index}",
            "ChoicePicker",
            variant="mutuallyExclusive",
            label=f"質問 {index} の回答候補",
            options=[
                {"label": text, "value": str(number)} for number, text in enumerate(choices, 1)
            ]
            + [{"label": "その他", "value": OTHER}],
            value={"path": f"/answers/{index}/choice"},
        )
        add(
            f"answer-{index}",
            "TextField",
            label=f"質問 {index} への回答・補足",
            variant="longText",
            value={"path": f"/answers/{index}/text"},
        )
    if state in {"WAITING", "QUEUED", "RUNNING"}:
        add("proposal-state", "Text", text="回答候補を準備中です。「その他」から先に回答できます。")
    elif state == "PAUSED":
        add(
            "proposal-state",
            "Text",
            text="回答候補の生成は一時停止中です。「その他」から回答できます。",
        )
    elif state == "FAILED":
        add(
            "proposal-state",
            "Text",
            text="回答候補を取得できませんでした。自由入力で回答できます。",
        )
    elif state == "ANSWERED" and not options:
        add(
            "proposal-state",
            "Text",
            text="Codex から回答候補はありません。自由入力で回答できます。",
        )
    button(
        DRAFT,
        "回答を仕様案に反映",
        scope
        | {
            **({"proposal_id": turn.id} if options else {}),
            "answers": {"path": "/answers"},
        },
        "primary",
    )
    return [
        {"version": VERSION, "createSurface": {"surfaceId": surface, "catalogId": CATALOG}},
        {
            "version": VERSION,
            "updateComponents": {
                "surfaceId": surface,
                "components": [
                    {"id": "root", "component": "Column", "children": children},
                    *components,
                ],
            },
        },
        {"version": VERSION, "updateDataModel": {"surfaceId": surface, "value": data}},
    ]


def attention_router(service, token_type, codex_workspace):
    router = APIRouter(prefix="/v1/runs/{run_id}/attention")

    def subject(session, actor, run_id):
        run = owned(session, Run, run_id, actor.workspace_id)
        plan = latest_plan(session, run) if run.status == "BLOCKED" else None
        questions = plan_questions(plan)
        return run, plan, questions

    def can_generate(run):
        return run.workspace_id == codex_workspace and eligible(run)

    @router.get("")
    def get_surface(run_id: str, token: token_type, response: Response):
        response.headers["Cache-Control"] = "no-store"

        def query(session, actor):
            run, plan, questions = subject(session, actor, run_id)
            if not questions or actor.user_access == "VIEWER":
                return {"messages": [], "pending": False}
            task = owned(session, Task, run.task_id, actor.workspace_id)
            turn = proposal(session, run, plan)
            now = db_now(session)
            state = turn_view(turn, now)["status"] if turn else None
            if not turn and can_generate(run):
                paused = session.get(Workspace, run.workspace_id).execution_stopped
                state = (
                    "PAUSED"
                    if paused or chat_limit_reached(session, run.workspace_id, now)
                    else "WAITING"
                )
            return {
                "messages": attention_messages(run, plan, questions, task, turn, state),
                "pending": state in {"WAITING", "QUEUED", "RUNNING"},
            }

        return service.read(token, query)

    @router.post("/actions")
    def answer(run_id: str, data: ActionEnvelope, request: Request, token: token_type):
        browser_write(request)
        action = data.action
        event = action.context

        def validate(session, actor):
            service.require_user_access(actor, "OPERATOR")
            run, plan, questions = subject(session, actor, run_id)
            if (
                not questions
                or action.surface_id != surface_id(plan)
                or action.component_id != action.name
                or event.revision != run.state_revision
            ):
                raise DomainError(409, "ATTENTION_SURFACE_STALE")
            task = owned(session, Task, run.task_id, actor.workspace_id)
            if event.spec_version != task.latest_spec_version:
                raise DomainError(409, "ATTENTION_SURFACE_STALE")
            return run, plan, questions

        def draft(session, actor):
            run, plan, questions = validate(session, actor)
            turn = proposal(session, run, plan)
            options = options_by_question(turn) if turn and turn.id == event.proposal_id else {}
            if set(event.answers) != {str(i) for i in range(1, len(questions) + 1)}:
                raise DomainError(422, "ATTENTION_ANSWER_REQUIRED")
            lines = []
            for index, question in enumerate(questions, 1):
                value = event.answers[str(index)]
                choices = options.get(index, [])
                choice = value.choice[0] if value.choice else None
                selected = ""
                if choice and choice != OTHER:
                    if int(choice) > len(choices):
                        raise DomainError(409, "ATTENTION_SURFACE_STALE")
                    selected = choices[int(choice) - 1]
                answer = "\n".join(text for text in (selected, value.text) if text)
                if not answer:
                    raise DomainError(422, "ATTENTION_ANSWER_REQUIRED")
                if sensitive_input(answer):
                    raise DomainError(422, "SENSITIVE_INPUT")
                lines.append(f"質問 {index}  {question}\n回答  {answer}")
            addition = "\n\n【Codex の質問への回答】\n" + "\n\n".join(lines)
            if len(addition) > 16000:
                raise DomainError(422, "ATTENTION_ANSWER_TOO_LONG")
            return {"addition": addition, "spec_version": event.spec_version}

        return service.read(token, draft)

    return router
