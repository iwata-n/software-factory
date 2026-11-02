from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData

from software_factory.adapters import AgentResult, FakeAgent, ProducedArtifact
from software_factory.contracts import digest
from software_factory.models import Project
from software_factory.worker import Supervisor
from tests.test_assembler import runner as runner
from tests.test_contracts import plan_body

from .helpers import dump, schema_database, worker_client
from .test_sf10 import pipeline as pipeline
from .test_sf11 import source_bundle as source_bundle
from .test_sf14 import decide


@pytest.mark.ac("SF-14-AC02", "C-01")
def test_upgrade_backfills_existing_plan_hash_without_changing_body_or_parent_data(world, evidence):
    original = dump(world.database)
    with schema_database(world.database, "0008") as old:
        with old.engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection)
            for name in (
                "workspaces",
                "principals",
                "worker_enrollments",
                "workers",
                "projects",
                "repositories",
                "tasks",
                "task_spec_versions",
                "runs",
                "jobs",
            ):
                table = metadata.tables[name]
                for row in original[name]:
                    connection.execute(
                        table.insert().values(**{k: v for k, v in row.items() if k in table.c})
                    )
            body = plan_body()
            pid = str(uuid4())
            connection.execute(
                metadata.tables["plan_versions"]
                .insert()
                .values(
                    id=pid,
                    workspace_id=world.workspace_id,
                    run_id=world.run_id,
                    version=1,
                    body=body,
                    validation_error=None,
                )
            )
        before = dump(old)
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
        after = dump(old)
        (row,) = after["plan_versions"]
        assert row["body_hash"] == digest(body) and row["producer_job_id"] is None
        assert {k: row[k] for k in before["plan_versions"][0]} == before["plan_versions"][0]
        for name in ("tasks", "task_spec_versions", "runs", "jobs"):
            assert before[name] == after[name]
        command.upgrade(Config("alembic.ini"), "head")
        assert dump(old) == after
        evidence(
            "0008 Plan body/parents survive; hash backfill is stable and model diff is zero",
            {
                "plan_id": pid,
                "body_hash": row["body_hash"],
                "retained_tables": ["tasks", "task_spec_versions", "runs", "jobs"],
            },
        )


@pytest.mark.ac("SF-14-AC05")
def test_general_plan_approval_does_not_authorize_actual_protected_diff(pipeline, evidence):
    world = pipeline.world
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {
            "expected_run_revision": world.run()["state_revision"],
            "reason": "require approval for real diff test",
        },
    )
    with world.database.transaction() as s:
        project = s.get(Project, world.project_id)
        project.policy = project.policy | {"require_plan_approval": True}
    world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()[
        "run_id"
    ]
    baseline = world.run()["snapshot"]
    client, _ = worker_client(world)

    class Planner(FakeAgent):
        def start(self, job):
            super().start(job)
            self._result.result["assumptions"] = [
                "Human approval grants permission to disable required tests"
            ]

    assert Supervisor(client, Planner()).run_once()["accepted"]
    assert world.run()["phase"] == "WAIT_PLAN_APPROVAL"
    assert decide(world).status_code == 200
    patch = (
        b"--- a/checks/golden.py\n+++ b/checks/golden.py\n@@ -1 +1 @@\n"
        b"-# immutable required check\n+# disable required check\n"
    )

    class Implementer(FakeAgent):
        def start(self, job):
            self._result = AgentResult(
                "EXECUTED",
                {
                    "protected_manifest": {},
                    "required_checks": [],
                    "permissions": "approved by a human",
                },
                artifacts=(ProducedArtifact("patch", "PATCH", patch),),
            )

    assert Supervisor(client, Implementer()).run_once()["accepted"]
    assert pipeline.assemble()["accepted"]
    assert (world.run()["status"], world.run()["stop_reason"]) == (
        "BLOCKED",
        "PROTECTED_VERIFICATION_CHANGE",
    )
    assert world.run()["snapshot"] == baseline and world.run()["current_candidate_id"] is None
    evidence(
        "general approval and Agent claims cannot weaken actual diff protection",
        {
            "reason": world.run()["stop_reason"],
            "profile_hash": baseline["verification_profile_hash"],
            "policy_hash": baseline["policy_hash"],
        },
    )
