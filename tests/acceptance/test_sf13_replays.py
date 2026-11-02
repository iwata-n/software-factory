from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from software_factory.models import Candidate, Job
from tests.test_assembler import runner as runner

from .helpers import dump
from .test_sf10 import pipeline as pipeline
from .test_sf11 import source_bundle as source_bundle
from .test_sf11_boundaries import deliver, prepared
from .test_sf12_integration import rows
from .test_sf13 import initial_failure, repair_adapter, value_patch


@pytest.mark.ac("SF-13-AC01", "SF-13-AC02", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_concurrent_repair_patch_completion_creates_one_next_assembler(pipeline, trial, evidence):
    cid, _ = initial_failure(pipeline)
    report = prepared(pipeline, "REPAIR", adapter=repair_adapter(value_patch(3, 2)))
    barrier = Barrier(2)

    def complete():
        barrier.wait(5)
        return deliver(pipeline, report)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: complete(), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assemblers = [j for j in rows(pipeline, Job) if j.kind == "ASSEMBLE_CANDIDATE"]
    assert len(assemblers) == 2 and len([j for j in rows(pipeline, Job) if j.kind == "REPAIR"]) == 1
    (next_job,) = [j for j in assemblers if j.state == "READY"]
    assert next_job.payload["source_candidate_id"] == cid and next_job.payload["repair_round"] == 1
    assert next_job.payload["parent_sha"] == next_job.payload["candidate"]["head_sha"]
    assert len(rows(pipeline, Candidate)) == 1
    evidence(
        "20 concurrent exact Repair replays create one new assembler with immutable parent/input",
        {
            "trial": trial,
            "parent_candidate": cid,
            "assembler_id": next_job.id,
            "parent_sha": next_job.payload["parent_sha"],
            "round": 1,
        },
    )


@pytest.mark.ac("SF-13-AC02", "C-04")
def test_repair_transition_rolls_back_patch_reference_fees_and_next_job(pipeline, evidence):
    initial_failure(pipeline)
    report = prepared(pipeline, "REPAIR", adapter=repair_adapter(value_patch(3, 2)))
    before = dump(pipeline.world.database)
    service = pipeline.world.client.app.state.service

    def fail(point):
        if point == "write.before_commit":
            raise RuntimeError("injected Repair commit failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            deliver(pipeline, report)
    finally:
        service.checkpoint = lambda name: None
    assert dump(pipeline.world.database) == before
    assert deliver(pipeline, report).status_code == 200
    assert len([j for j in rows(pipeline, Job) if j.kind == "ASSEMBLE_CANDIDATE"]) == 2
    evidence(
        "Repair completion, patch, fees, next assembler, events and replay record are atomic",
        {"rollback_equal": True, "assemblers_after_retry": 2},
    )
