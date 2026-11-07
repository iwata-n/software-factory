from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.contracts import RunStart
from software_factory.models import Run, Task, TaskChatTurn
from software_factory.service import DomainError
from software_factory.task_chat import enqueue_turn
from software_factory.task_lifecycle import lock_run_task
from tests.test_task_deletion import deletion_events, stop


@pytest.mark.ac("AC-08", "AC-10")
@pytest.mark.parametrize("first", ["delete", "start", "chat", "resume"])
def test_task_mutations_serialize_across_independent_transactions(world, first):
    stop(world)
    service = world.client.app.state.service
    entered, release, second_entered = Event(), Event(), Event()

    def pause(point):
        if point == "task.deleted":
            entered.set()
            assert release.wait(10)

    def mutate(kind, *, hold=False):
        if kind == "delete":
            try:
                return service.delete_task_request(world.user_token, world.task_id)
            except DomainError as error:
                return error.code
        with world.database.transaction() as session:
            actor = service.locked_principal(session, world.user_token, "USER")
            run = session.get(Run, world.run_id)
            try:
                lock_run_task(session, run)
                if kind == "start":
                    result = service.start_run(
                        session, actor, world.task_id, RunStart(spec_version=1)
                    )
                elif kind == "chat":
                    result = {"id": enqueue_turn(session, run, "Clarify the task").id}
                else:
                    # Exercise the shared lock used before blocked-run and approval resume.
                    run.status = "QUEUED"
                    result = {"run_id": run.id}
                session.flush()
                if hold:
                    entered.set()
                    assert release.wait(10)
                return result
            except DomainError as error:
                return error.code

    def second(kind):
        second_entered.set()
        return mutate(kind)

    service.checkpoint = pause if first == "delete" else lambda point: None
    try:
        with ThreadPoolExecutor(2) as pool:
            leading = pool.submit(mutate, first, hold=first != "delete")
            try:
                assert entered.wait(10)
                trailing = pool.submit(second, "start" if first == "delete" else "delete")
                assert second_entered.wait(10)
                # The held DB transaction must prevent the other mutation from completing.
                assert not trailing.done()
            finally:
                release.set()
            assert isinstance(leading.result(timeout=10), dict)
            result = trailing.result(timeout=10)
            assert result == (
                "NOT_FOUND"
                if first == "delete"
                else "TASK_DELETE_ACTIVE_CHAT"
                if first == "chat"
                else "TASK_DELETE_ACTIVE_RUN"
            )
    finally:
        service.checkpoint = lambda point: None
    with world.database.transaction() as session:
        assert (session.get(Task, world.task_id).deleted_at is not None) == (first == "delete")
        assert deletion_events(session, world.task_id) == (first == "delete")
        if first == "chat":
            assert session.scalar(select(TaskChatTurn)).status == "QUEUED"


@pytest.mark.ac("AC-07", "AC-10")
def test_old_approval_and_attention_receipts_do_not_replay_after_deletion(world):
    from software_factory.models import ApprovalRequest, IdempotencyRecord

    stop(world)
    service = world.client.app.state.service
    approval_id, key = str(uuid4()), str(uuid4())
    with world.database.transaction() as session:
        actor = service.principal(session, world.user_token, "USER")
        session.add(
            ApprovalRequest(
                id=approval_id,
                workspace_id=world.workspace_id,
                run_id=world.run_id,
                action="approve_budget",
                target={},
                target_hash="a" * 64,
                status="INVALIDATED",
                created_at=0,
                expires_at=1,
            )
        )
        session.add(
            IdempotencyRecord(
                workspace_id=world.workspace_id,
                principal_id=actor.id,
                operation=f"attention:{world.run_id}:continue_interview",
                key=key,
                request_hash="a" * 64,
                response={"id": "retained-response"},
            )
        )
    service.delete_task_request(world.user_token, world.task_id)
    with pytest.raises(DomainError) as denied:
        service.write(
            world.user_token,
            "USER",
            f"attention:{world.run_id}:continue_interview",
            key,
            {},
            lambda session, actor: pytest.fail("deleted attention command ran"),
        )
    assert denied.value.code == "NOT_FOUND"
    with pytest.raises(DomainError) as denied:
        service.write(
            world.user_token,
            "USER",
            f"approval:{approval_id}:decision",
            str(uuid4()),
            {},
            lambda session, actor: pytest.fail("deleted approval command ran"),
        )
    assert denied.value.code == "NOT_FOUND"
