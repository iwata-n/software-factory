import pytest
from pydantic import ValidationError
from sqlalchemy import select

from software_factory.contracts import ProjectPolicy, digest
from software_factory.models import Job, Run
from tests.acceptance.test_sf10 import pipeline as pipeline
from tests.acceptance.test_sf11 import source_bundle as source_bundle
from tests.acceptance.test_sf12_integration import pass_local
from tests.conftest import example_policy
from tests.test_assembler import runner as runner


def test_explicit_local_delivery_finishes_with_verified_candidate_and_no_publication(pipeline):
    world = pipeline.world
    with world.database.transaction() as s:
        run = s.get(Run, world.run_id)
        policy = run.snapshot["policy"] | {"delivery_mode": "local_candidate"}
        run.snapshot = run.snapshot | {"policy": policy, "policy_hash": digest(policy)}
    gate = pass_local(pipeline)
    assert gate.status == "PASS"
    result = world.run()
    assert result["status"] == "SUCCEEDED" and result["phase"] == "LOCAL_COMPLETE"
    assert result["current_candidate_id"] and result["finished_at"]
    with world.database.transaction() as s:
        assert not s.scalars(
            select(Job).where(Job.run_id == world.run_id, Job.kind == "PUBLISH_DRAFT")
        ).all()


def test_local_delivery_does_not_silently_skip_required_ci():
    with pytest.raises(ValidationError, match="external CI"):
        ProjectPolicy.model_validate(
            example_policy()
            | {
                "delivery_mode": "local_candidate",
                "required_ci_checks": [
                    {"provider": "github", "app_id": 123, "check_name": "required"}
                ],
            }
        )
