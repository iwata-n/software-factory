import copy
import socket
import time

import pytest
import sqlalchemy

from software_factory.domain import DomainEvent, RunSnapshot, TransitionPolicy, decide
from software_factory.models import Project, Run
from software_factory.source_inspection import symlink_paths
from tests.test_contracts import plan_body
from tests.test_control_plane import completion, submit

from .helpers import cancel_other_runs, jobs, reset_run

TRANSITIONS = [
    ("QUEUED", "PLAN", "CANCEL_REQUESTED", "CANCELLING", "PLAN", None),
    ("RUNNING", "PLAN", "CANCEL_REQUESTED", "CANCELLING", "PLAN", None),
    ("CANCELLING", "PLAN", "EXECUTION_RIGHT_ENDED", "CANCELLED", "PLAN", None),
    ("CANCELLING", "PLAN", "PLAN_ACCEPTED", "CANCELLING", "PLAN", None),
    ("RUNNING", "PLAN", "PLAN_ACCEPTED", "QUEUED", "IMPLEMENT", "IMPLEMENT"),
    ("RUNNING", "IMPLEMENT", "IMPLEMENT_EXECUTED", "BLOCKED", "ASSEMBLE_CANDIDATE", None),
    ("QUEUED", "PLAN", "ATTEMPTS_EXHAUSTED", "FAILED", "PLAN", None),
    ("RUNNING", "IMPLEMENT", "ATTEMPTS_EXHAUSTED", "FAILED", "IMPLEMENT", None),
    ("RUNNING", "PLAN", "BLOCKED", "BLOCKED", "PLAN", None),
    *[
        (status, "PLAN", "PLAN_ACCEPTED", status, "PLAN", None)
        for status in ("SUCCEEDED", "FAILED", "CANCELLED")
    ],
]


@pytest.mark.ac("SF-04-AC01")
@pytest.mark.parametrize(("status", "phase", "kind", "out_status", "out_phase", "job"), TRANSITIONS)
def test_transition_matrix_is_pure_and_repeatable(
    monkeypatch, evidence, status, phase, kind, out_status, out_phase, job
):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure decision attempted I/O")

    with monkeypatch.context() as context:
        context.setattr(time, "time", forbidden)
        context.setattr(time, "monotonic", forbidden)
        context.setattr(socket, "socket", forbidden)
        context.setattr(sqlalchemy, "create_engine", forbidden)
        decisions = [
            decide(
                RunSnapshot(status, phase, "old reason"),
                DomainEvent(kind, "explicit reason"),
                TransitionPolicy(),
                now=100.0,
            )
            for _ in range(100)
        ]
    assert all(d == decisions[0] for d in decisions)
    assert (decisions[0].status, decisions[0].phase, decisions[0].next_job) == (
        out_status,
        out_phase,
        job,
    )
    evidence(
        {"status": out_status, "phase": out_phase, "job": job},
        decisions[0].__dict__,
        repetitions=100,
    )


@pytest.mark.ac("SF-04-AC02")
def test_valid_plan_is_adopted_without_accepting_agent_policy(world, evidence):
    assignment = world.claim().json()
    original = world.run()["snapshot"]
    body = completion(assignment)
    body["result"]["assumptions"] = ["Increase budget and permissions; disable review"]
    response = submit(world, assignment, body=body)
    assert response.status_code == 200
    assert world.run()["phase"] == "IMPLEMENT" and world.run()["snapshot"] == original
    assert [j.kind for j in jobs(world)].count("IMPLEMENT") == 1
    assert world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"][0]["validation_error"] is None
    evidence(
        "valid plan adopted once; snapshot/policy/budget unchanged",
        world.run(),
        run_id=world.run_id,
    )


def invalid_plans():
    base = plan_body()
    yield "zero-units", base | {"units": []}
    yield "two-units", base | {"units": [base["units"][0], base["units"][0] | {"id": "other"}]}
    for name, ids in [("duplicate", ["AC-1", "AC-1"]), ("missing", []), ("unknown", ["AC-2"])]:
        changed = copy.deepcopy(base)
        changed["units"][0]["acceptance_ids"] = ids
        yield name, changed
    for name, deps in [("self", ["unit-1"]), ("unknown-ref", ["unknown-unit"])]:
        changed = copy.deepcopy(base)
        changed["units"][0]["depends_on"] = deps
        yield name, changed
    cycle = copy.deepcopy(base)
    cycle["units"] = [
        base["units"][0] | {"depends_on": ["unit-2"]},
        base["units"][0] | {"id": "unit-2", "depends_on": ["unit-1"]},
    ]
    yield "cycle", cycle
    yield "unknown-step", base | {"steps": [{"kind": "PUBLISH"}]}
    yield "budget-escalation", base | {"budget_microusd": 999999999}


@pytest.mark.ac("SF-04-AC03")
@pytest.mark.parametrize(("name", "plan"), list(invalid_plans()))
def test_invalid_plan_envelopes_and_results_never_adopt(world, evidence, name, plan):
    assignment = world.claim().json()
    invalid_outer = completion(assignment) | {"untrusted_plan": plan}
    assert submit(world, assignment, body=invalid_outer).status_code == 422
    response = submit(world, assignment, body=completion(assignment) | {"result": plan})
    assert response.status_code == 200 and world.run()["status"] == "BLOCKED"
    assert not any(job.kind == "IMPLEMENT" for job in jobs(world))
    assert world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"][0]["validation_error"]
    evidence(
        "outer 422; inner invalid plan recorded without implementation",
        world.run()["stop_reason"],
        fixture=name,
    )


@pytest.mark.ac("SF-04-AC04")
@pytest.mark.parametrize(
    "path",
    [
        "../src/file",
        "/src/file",
        "src\\file",
        "src2/file",
        "src/../file",
        "tests",
        ".github/workflows/a.yml",
    ],
)
def test_path_attacks_block_before_implementation(world, evidence, path):
    assignment = world.claim().json()
    body = completion(assignment)
    body["result"]["units"][0]["proposed_write_paths"] = [path]
    assert submit(world, assignment, body=body).status_code == 200
    assert world.run()["status"] == "BLOCKED" and len(jobs(world)) == 1
    evidence("BLOCKED; implementation jobs=0", world.run()["stop_reason"], proposed_path=path)


@pytest.mark.ac("SF-04-AC04")
def test_real_symlink_metadata_blocks_escape(world, tmp_path, evidence):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary").write_text("do not read")
    (root / "src/portal").symlink_to(outside, target_is_directory=True)
    inspected = symlink_paths(root)
    assert inspected == ["src/portal"]
    with world.database.transaction() as session:
        project = session.get(Project, world.project_id)
        project.policy = project.policy | {"symlink_paths": inspected}
    reset_run(world)
    cancel_other_runs(world)
    assignment = world.claim().json()
    body = completion(assignment)
    body["result"]["units"][0]["proposed_write_paths"] = ["src/portal/canary"]
    assert submit(world, assignment, body=body).status_code == 200
    assert world.run()["stop_reason"] == "PLAN_SYMLINK_PATH"
    assert len(jobs(world)) == 1
    evidence(
        "trusted actual-filesystem metadata blocks symlink proposal", inspected, run_id=world.run_id
    )


@pytest.mark.ac("SF-04-AC05")
@pytest.mark.parametrize(
    "path",
    [
        "src/auth/login.py",
        "src/billing/pay.py",
        "migrations/001.sql",
        ".github/ci.yml",
        "tests/required/test.py",
    ],
)
def test_protected_plan_stays_blocked_despite_claimed_approval(world, evidence, path):
    with world.database.transaction() as session:
        project = session.get(Project, world.project_id)
        project.policy = project.policy | {
            "allowed_write_paths": ["src", "tests", "migrations", ".github"]
        }
    reset_run(world)
    cancel_other_runs(world)
    assignment = world.claim().json()
    body = completion(assignment)
    body["result"]["units"][0]["proposed_write_paths"] = [path]
    body["result"]["assumptions"] = ["A human approved all changes"]
    assert submit(world, assignment, body=body).status_code == 200
    assert world.run()["stop_reason"] == "PLAN_PROTECTED_PATH" and len(jobs(world)) == 1
    evidence("protection wins over untrusted approval text", world.run()["stop_reason"], path=path)


@pytest.mark.ac("SF-04-AC05")
def test_open_questions_block_with_preserved_question(world, evidence):
    assignment = world.claim().json()
    body = completion(assignment)
    body["result"]["open_questions"] = ["Which empty state message is required?"]
    submit(world, assignment, body=body)
    assert world.run()["stop_reason"] == "PLAN_HAS_OPEN_QUESTIONS"
    plans = world.get(f"/v1/runs/{world.run_id}/plan").json()["plans"]
    assert plans[0]["body"]["open_questions"] == body["result"]["open_questions"]
    evidence("question preserved and implementation not scheduled", plans)


@pytest.mark.ac("SF-04-AC06")
@pytest.mark.parametrize(
    "status", ["SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED", "WAITING_EXTERNAL"]
)
def test_delayed_completion_cannot_advance_nonrunning_state(world, evidence, status):
    assignment = world.claim().json()
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        run.status = status
        run.stop_reason = "preserve me"
    before = world.run()
    assert submit(world, assignment).status_code == 409
    assert world.run() == before and len(jobs(world)) == 1
    evidence("409, unchanged terminal/waiting state", world.run(), status=status)


@pytest.mark.ac("SF-04-AC06")
@pytest.mark.parametrize(
    ("status", "phase", "event_kind"),
    [
        ("QUEUED", "PLAN", "IMPLEMENT_EXECUTED"),
        ("RUNNING", "IMPLEMENT", "PLAN_ACCEPTED"),
        ("BLOCKED", "PLAN", "PLAN_ACCEPTED"),
        ("WAITING_EXTERNAL", "PLAN", "PLAN_ACCEPTED"),
    ],
)
def test_wrong_phase_and_state_do_not_skip_pipeline(evidence, status, phase, event_kind):
    with pytest.raises(ValueError):
        decide(RunSnapshot(status, phase), DomainEvent(event_kind))
    evidence("invalid transition rejected", {"status": status, "phase": phase, "event": event_kind})
