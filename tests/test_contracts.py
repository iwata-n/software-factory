import copy

import pytest
from pydantic import ValidationError

from software_factory.contracts import Plan, ProjectPolicy, TaskSpec, digest, validate_plan
from software_factory.domain import DomainEvent, RunSnapshot, decide

from .conftest import example_policy, example_spec


def plan_body():
    return {
        "units": [
            {
                "id": "unit-1",
                "objective": "Implement empty state",
                "acceptance_ids": ["AC-1"],
                "proposed_write_paths": ["src/view.py"],
            }
        ]
    }


@pytest.mark.parametrize(
    "paths",
    [
        ["../src/file"],
        ["/src/file"],
        ["src/../secret"],
        ["src\\file"],
        ["src2/file"],
        [".github/workflows/ci.yml"],
        ["tests/required/test.py"],
    ],
)
def test_untrusted_plan_cannot_expand_paths(paths):
    data = plan_body()
    data["units"][0]["proposed_write_paths"] = paths
    error = validate_plan(
        Plan.model_validate(data),
        TaskSpec.model_validate(example_spec("repo")),
        ProjectPolicy.model_validate(example_policy()),
    )
    assert error in {"PLAN_WRITE_PATH_DENIED", "PLAN_PROTECTED_PATH"}


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("acceptance_ids", ["AC-other"], "PLAN_ACCEPTANCE_COVERAGE"),
        ("depends_on", ["unit-1"], "PLAN_INVALID_DEPENDENCIES"),
    ],
)
def test_plan_coverage_and_dependencies(field, value, error):
    data = plan_body()
    data["units"][0][field] = value
    assert (
        validate_plan(
            Plan.model_validate(data),
            TaskSpec.model_validate(example_spec("repo")),
            ProjectPolicy.model_validate(example_policy()),
        )
        == error
    )


def test_open_questions_block_execution():
    data = plan_body() | {"open_questions": ["Which behavior should change?"]}
    assert (
        validate_plan(
            Plan.model_validate(data),
            TaskSpec.model_validate(example_spec("repo")),
            ProjectPolicy.model_validate(example_policy()),
        )
        == "PLAN_HAS_OPEN_QUESTIONS"
    )


def test_task_contract_rejects_unknown_fields_and_permission_escalation():
    body = example_spec("repo")
    for mutation in (
        {"permissions": ["merge"]},
        {"schema_version": 2},
        {"execution": {"max_parallel_jobs": 2}},
        {"policy_request": {"merge": "allowed"}},
    ):
        with pytest.raises(ValidationError):
            TaskSpec.model_validate(body | mutation)
    duplicate = copy.deepcopy(body)
    duplicate["task"]["acceptance_criteria"] *= 2
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(duplicate)


def test_hash_is_canonical_and_sensitive_to_semantic_change():
    assert digest({"a": 1, "b": "日本語"}) == digest({"b": "日本語", "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_fake_execution_cannot_produce_success():
    snapshot = RunSnapshot("RUNNING", "IMPLEMENT")
    event = DomainEvent("IMPLEMENT_EXECUTED")
    result = decide(snapshot, event)
    assert result == decide(snapshot, event)
    assert result.status == "BLOCKED"
    assert result.reason == "CHANGE_ARTIFACT_MISSING"


def test_cancellation_ignores_normal_progress_until_execution_right_ends():
    snapshot = RunSnapshot("CANCELLING", "PLAN")
    assert decide(snapshot, DomainEvent("PLAN_ACCEPTED")).status == "CANCELLING"
    assert decide(snapshot, DomainEvent("EXECUTION_RIGHT_ENDED")).status == "CANCELLED"


def test_invalid_events_cannot_skip_workflow():
    with pytest.raises(ValueError):
        decide(RunSnapshot("QUEUED", "PLAN"), DomainEvent("IMPLEMENT_EXECUTED"))
