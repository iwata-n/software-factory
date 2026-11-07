from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from software_factory.codex_adapter import ChangeSet, make_patch
from software_factory.codex_engine import CodexError
from software_factory.contracts import Plan, ProjectPolicy, TaskSpec, digest, validate_plan
from software_factory.models import AuditEvent, Principal, Project
from software_factory.service import issue_token, token_hash
from tests.conftest import example_policy, example_spec


def root_policy():
    policy = example_policy()
    del policy["allowed_write_paths"]
    del policy["protected_paths"]
    return ProjectPolicy.model_validate(policy)


def plan(path):
    return Plan.model_validate(
        {
            "units": [
                {
                    "id": "one",
                    "objective": "Improve project",
                    "acceptance_ids": ["AC-1"],
                    "proposed_write_paths": [path],
                    "depends_on": [],
                }
            ],
            "assumptions": [],
            "open_questions": [],
        }
    )


@pytest.mark.parametrize(
    "path",
    [
        "src/api.py",
        "tests/conftest.py",
        "pyproject.toml",
        "migrations/0031.py",
        "src/auth/session.py",
        ".github/workflows/ci.yml",
    ],
)
def test_new_project_defaults_allow_project_code_and_configuration(path):
    policy = root_policy()
    assert policy.allowed_read_paths == policy.allowed_write_paths == ["."]
    assert policy.protected_paths == []
    assert validate_plan(plan(path), TaskSpec.model_validate(example_spec("repo")), policy) is None
    changes = ChangeSet(
        summary="Improve",
        acceptance_ids=["AC-1"],
        changes=[{"path": path, "content": "value = 2\n"}],
    )
    patch = make_patch(changes, {}, policy.model_dump(), plan(path).model_dump(), ["AC-1"])
    assert ("+++ b/" + path).encode() in patch


@pytest.mark.parametrize(
    "path", ["../escape", "/etc/passwd", ".git/config", "src/.GiT/hooks/post-checkout"]
)
def test_whole_project_does_not_allow_git_metadata_or_path_escape(path):
    policy = root_policy()
    assert (
        validate_plan(plan(path), TaskSpec.model_validate(example_spec("repo")), policy)
        == "PLAN_WRITE_PATH_DENIED"
    )
    changes = ChangeSet(
        summary="Denied", acceptance_ids=["AC-1"], changes=[{"path": path, "content": "bad\n"}]
    )
    with pytest.raises(CodexError, match="MODEL_CHANGE_PATH_DENIED"):
        make_patch(changes, {}, policy.model_dump(), plan(path).model_dump(), ["AC-1"])


def test_explicit_protections_override_whole_project_scope():
    policy = root_policy().model_copy(
        update={"protected_paths": ["checks"], "protected_manifest": {"trusted.py": "a" * 64}}
    )
    for path in ["checks/test.py", "trusted.py"]:
        assert (
            validate_plan(plan(path), TaskSpec.model_validate(example_spec("repo")), policy)
            == "PLAN_PROTECTED_PATH"
        )
        changes = ChangeSet(
            summary="Denied", acceptance_ids=["AC-1"], changes=[{"path": path, "content": "bad\n"}]
        )
        with pytest.raises(CodexError, match="MODEL_CHANGE_PATH_DENIED"):
            make_patch(changes, {}, policy.model_dump(), plan(path).model_dump(), ["AC-1"])


def test_admin_updates_policy_with_compare_and_swap_and_preserves_run_snapshot(world):
    with world.database.transaction() as db:
        original = deepcopy(db.get(Project, world.project_id).policy)
        viewer, operator = issue_token(), issue_token()
        db.add_all(
            [
                Principal(
                    workspace_id=world.workspace_id,
                    role="USER",
                    user_access=role,
                    token_hash=token_hash(token),
                )
                for role, token in [("VIEWER", viewer), ("OPERATOR", operator)]
            ]
        )
    snapshot = world.run()["snapshot"]
    path = f"/v1/projects/{world.project_id}/policy"
    body = {
        "expected_policy_hash": digest(original),
        "policy": original | {"allowed_write_paths": ["."], "protected_paths": []},
    }

    def patch(token, payload=body, key=None):
        return world.client.patch(
            path,
            json=payload,
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": key or str(uuid4())},
        )

    assert patch(viewer).status_code == 403
    assert patch(operator).status_code == 403
    assert patch(world.other_user_token).status_code == 404
    key = str(uuid4())
    first = patch(world.user_token, key=key)
    assert first.status_code == 200, first.text
    assert patch(world.user_token, key=key).json() == first.json()
    assert patch(world.user_token).json()["code"] == "PROJECT_POLICY_STALE"
    assert world.run()["snapshot"] == snapshot
    # Reusing a task cannot silently pick a Run pinned to the old docs-only policy.
    fresh = world.post(
        f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1, "reuse_existing": True}
    )
    assert fresh.status_code == 202, fresh.text
    assert fresh.json()["run_id"] != world.run_id
    assert fresh.json()["snapshot"]["policy"]["allowed_write_paths"] == ["."]
    assert (
        world.post(
            f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1, "reuse_existing": True}
        ).json()["run_id"]
        == fresh.json()["run_id"]
    )
    with world.database.transaction() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.operation == "project.policy.update")
        ).all()
        assert len(events) == 1
        assert events[0].body["policy_hash"] == first.json()["policy_hash"]
