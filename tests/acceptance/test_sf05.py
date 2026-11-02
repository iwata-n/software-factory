import json
import os
import select as select_module
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import func, select, text

from software_factory.models import Attempt, Job, JobDependency, Run, Worker
from software_factory.service import Service, enqueue
from tests.test_processes import start_control_plane

from .helpers import current_attempt, dump


@pytest.mark.ac("SF-05-AC01")
def test_dependencies_are_pending_then_ready(world, clock, evidence):
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        original = session.scalar(select(Job).where(Job.run_id == run.id))
        blocker = enqueue(session, run, "PLAN", step_key="dependency:fixture")
        blocker.available_at = clock() + 1000
        original.state = "PENDING"
        session.add(
            JobDependency(
                workspace_id=world.workspace_id,
                run_id=run.id,
                job_id=original.id,
                depends_on_id=blocker.id,
            )
        )
        original_id, blocker_id = original.id, blocker.id
    assert world.claim().status_code == 204
    with world.database.transaction() as session:
        assert session.get(Job, original_id).state == "PENDING"
        session.get(Job, blocker_id).state = "SUCCEEDED"
    assigned = world.claim()
    assert assigned.status_code == 200 and assigned.json()["job"]["id"] == original_id
    graph = world.get(f"/v1/runs/{world.run_id}/graph").json()
    assert {"from": blocker_id, "to": original_id} in graph["edges"]
    evidence("incomplete dependency blocks; completed dependency releases job", graph)


@pytest.mark.ac("SF-05-AC01")
@pytest.mark.parametrize(
    "condition", ["future", "wrong-capability", "wrong-adapter", "cancelled-run"]
)
def test_ineligible_jobs_are_never_claimed(world, clock, evidence, condition):
    with world.database.transaction() as session:
        job = session.scalar(select(Job))
        if condition == "future":
            job.available_at = clock() + 0.001
        elif condition == "wrong-capability":
            session.get(Worker, world.worker_id).capabilities = ["IMPLEMENT", "adapter:fake"]
        elif condition == "wrong-adapter":
            session.get(Worker, world.worker_id).capabilities = ["PLAN", "adapter:other"]
        else:
            session.get(Run, world.run_id).status = "CANCELLED"
    assert world.claim().status_code == 204
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0
    evidence("204 and no attempt/reservation", {"condition": condition, "attempts": 0})


@pytest.mark.ac("SF-05-AC01")
def test_priority_and_stable_tie_breakers(world, clock, evidence):
    for _ in range(4):
        assert world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).status_code == 202
    with world.database.transaction() as session:
        queued = list(session.scalars(select(Job).order_by(Job.id)))
        for index, job in enumerate(queued):
            job.priority = 2 if index < 4 else 1
            job.available_at = clock() - 2 if index < 3 else clock() - 1
            job.created_at = 100 if index < 2 else 200
        expected = [
            j.id
            for j in sorted(queued, key=lambda j: (-j.priority, j.available_at, j.created_at, j.id))
        ]
    observed = []
    for _ in range(5):
        assignment = world.claim().json()
        observed.append(assignment["job"]["id"])
        response = world.post(
            f"/v1/worker/leases/{assignment['lease']['id']}/fail",
            {
                "fence": assignment["lease"]["fence"],
                "category": "INCONCLUSIVE",
                "reason": "stop ordering fixture",
            },
            token=world.worker_token,
        )
        assert response.status_code == 200
    assert observed == expected
    evidence(expected, observed)


@pytest.mark.ac("SF-05-AC02", "C-03")
@pytest.mark.parametrize("iteration", range(20))
@pytest.mark.parametrize("scenario", ["worker-slot", "same-job", "run-slot"])
def test_claim_slots_and_budget_are_atomic(world, evidence, iteration, scenario):
    tokens = [world.worker_token, world.worker_token]
    if scenario == "worker-slot":
        world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
    else:
        tokens[1] = world.make_worker()["worker_token"]
    if scenario == "run-slot":
        with world.database.transaction() as session:
            enqueue(session, session.get(Run, world.run_id), "PLAN", step_key="parallel:fixture")
    barrier = Barrier(2)

    def claim(token):
        barrier.wait(timeout=5)
        return Service(world.database).claim(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, tokens))
    assert sum(r is not None for r in results) == 1
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1
        assert session.scalar(select(func.sum(Run.reserved_microusd))) == 100000
        assert (
            session.scalar(select(func.count()).select_from(Job).where(Job.state == "LEASED")) == 1
        )
    evidence(
        "one attempt, one slot, reservation=100000",
        {"attempts": 1, "reserved": 100000, "scenario": scenario, "iteration": iteration},
    )


@pytest.mark.ac("SF-05-AC03")
@pytest.mark.parametrize(("offset", "status"), [(-0.001, 200), (0, 409), (0.001, 409)])
def test_lease_expiry_and_identity_boundaries(world, clock, evidence, offset, status):
    assignment = world.claim().json()
    lease = assignment["lease"]
    path = f"/v1/worker/leases/{lease['id']}/heartbeat"
    other = world.make_worker()
    assert (
        world.post(path, {"fence": lease["fence"]}, token=other["worker_token"]).status_code == 404
    )
    assert (
        world.post(path, {"fence": lease["fence"] + 1}, token=world.worker_token).status_code == 409
    )
    attempt = current_attempt(world)
    clock.value = attempt.expires_at + offset
    response = world.post(path, {"fence": lease["fence"]}, token=world.worker_token)
    assert response.status_code == status
    if status == 200:
        assert current_attempt(world).expires_at == min(clock() + 90, attempt.deadline_at)
    else:
        assert current_attempt(world).expires_at == attempt.expires_at
    evidence(
        status,
        response.status_code,
        offset=offset,
        db_now=clock(),
        expires_at=attempt.expires_at,
        attempt_id=attempt.id,
    )


@pytest.mark.ac("SF-05-AC03")
def test_heartbeat_is_clamped_to_execution_deadline(world, clock, evidence):
    assignment = world.claim().json()
    lease = assignment["lease"]
    with world.database.transaction() as session:
        session.scalar(select(Attempt)).deadline_at = clock() + 10
    response = world.post(
        f"/v1/worker/leases/{lease['id']}/heartbeat",
        {"fence": lease["fence"]},
        token=world.worker_token,
    )
    assert response.json()["lease_expires_in_seconds"] == 10
    evidence("lease cannot exceed job deadline", response.json())


@pytest.mark.ac("SF-05-AC04", "SF-06-AC01")
def test_actual_supervisor_kill_then_reassignment(world, clock, evidence):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = start_control_plane(port)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "FACTORY_DATABASE_URL",
            "TEST_DATABASE_URL",
            "FACTORY_USER_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        }
    }
    env["FACTORY_WORKER_TOKEN"] = world.worker_token
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.acceptance.worker_probe",
            "--api-url",
            f"http://127.0.0.1:{port}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select_module.select([process.stdout], [], [], 10)[0], "worker did not start"
        trace = json.loads(process.stdout.readline())
        assert trace["started"] and trace["pid"] != server.pid
        assert trace["database_modules"] == []
        assert not {"FACTORY_DATABASE_URL", "FACTORY_USER_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"} & set(
            trace["environment_keys"]
        )
        old = current_attempt(world)
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode < 0
        clock.value = old.expires_at
        service = Service(world.database)
        assert service.reconcile() == 1 and service.reconcile() == 0
        with world.database.transaction() as session:
            assert session.get(Attempt, old.id).state == "EXPIRED"
            assert session.get(Attempt, old.id).stop_confirmation == "EXECUTION_RIGHT_EXPIRED"
        clock.advance(2)
        new = world.claim().json()
        assert new["job"]["id"] == old.job_id and new["lease"]["fence"] == old.fence + 1
        stale = world.post(
            f"/v1/worker/leases/{old.lease_id}/complete",
            {"fence": old.fence, "outcome": "EXECUTED", "result": {}},
            token=world.worker_token,
        )
        assert stale.status_code == 409
        evidence(
            "killed HTTP-only supervisor, expired once, fence increased, old report 409",
            {"worker": trace, "old_fence": old.fence, "new_fence": new["lease"]["fence"]},
            attempt_id=old.id,
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        server.terminate()
        server.communicate(timeout=10)


@pytest.mark.ac("SF-05-AC05")
@pytest.mark.parametrize("mode", ["reported", "expired"])
def test_retry_timing_input_identity_and_three_attempt_limit(world, clock, evidence, mode):
    digests = []
    assignments = []
    for number in range(1, 4):
        assigned = world.claim().json()
        assignments.append(assigned)
        attempt = current_attempt(world)
        digests.append(attempt.input_digest)
        if mode == "reported":
            response = world.post(
                f"/v1/worker/leases/{attempt.lease_id}/fail",
                {"fence": attempt.fence, "category": "INFRASTRUCTURE", "reason": "fixture failure"},
                token=world.worker_token,
            )
            assert response.status_code == 200
        else:
            clock.value = attempt.expires_at
            assert Service(world.database).reconcile() == 1
        if number < 3:
            with world.database.transaction() as session:
                available = session.get(Job, attempt.job_id).available_at
            assert available - clock() == 2**number
            clock.value = available - 0.001
            assert world.claim().status_code == 204
            clock.value = available
    assert len(set(digests)) == 1 and digests[0]
    assert (
        world.run()["status"] == "FAILED"
        and world.run()["stop_reason"] == "INFRA_ATTEMPTS_EXHAUSTED"
    )
    assert world.claim().status_code == 204
    graph = world.get(f"/v1/runs/{world.run_id}/graph").json()
    assert len(graph["jobs"]) == 1 and len(graph["jobs"][0]["attempts"]) == 3
    evidence(
        "2s/4s waits; 3 infrastructure attempts; immutable inputs; no repair",
        graph,
        input_digests=digests,
    )


@pytest.mark.ac("SF-05-AC06")
def test_twenty_second_poll_holds_no_db_transaction_or_row_lock(world, evidence):
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "empty queue"},
    )
    entered = Event()
    release = Event()

    def checkpoint(name):
        if name == "poll.wait" and not entered.is_set():
            entered.set()
            assert release.wait(5)

    world.client.app.state.service.checkpoint = checkpoint
    with ThreadPoolExecutor(max_workers=1) as pool:
        start = time.monotonic()
        request = pool.submit(
            lambda: world.post(
                "/v1/worker/claims",
                {"protocol_version": 1, "wait_seconds": 20},
                token=world.worker_token,
            )
        )
        try:
            assert entered.wait(5)
            with world.database.engine.connect() as connection:
                active = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND xact_start IS NOT NULL"
                    )
                )
                assert active == 0
            created = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
            assert created.status_code == 202
            run = created.json()
            assert (
                world.post(
                    f"/v1/runs/{run['run_id']}/cancel",
                    {"expected_run_revision": run["state_revision"], "reason": "concurrent cancel"},
                ).status_code
                == 202
            )
        finally:
            release.set()
        response = request.result(timeout=23)
    elapsed = time.monotonic() - start
    assert response.status_code == 204 and 19 <= elapsed <= 21
    evidence(
        "204 after 20±1 sec; no active transaction while waiting; concurrent mutations succeed",
        {"elapsed": elapsed, "active_transactions": active},
    )


@pytest.mark.ac("SF-05-AC07", "C-04")
@pytest.mark.parametrize("point", ["claim.attempt", "claim.reservation", "claim.event"])
def test_all_claim_mutations_rollback_at_every_boundary(world, evidence, point):
    before = dump(world.database)

    def fail(name):
        if name == point:
            raise RuntimeError("injected commit boundary failure")

    with pytest.raises(RuntimeError):
        Service(world.database, checkpoint=fail).claim(world.worker_token)
    assert dump(world.database) == before
    assert Service(world.database).claim(world.worker_token)
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1
        assert session.get(Run, world.run_id).reserved_microusd == 100000
    evidence(
        "rollback leaves exact prior state; retry claims once",
        {
            "point": point,
            "before_hash": __import__("software_factory.contracts", fromlist=["digest"]).digest(
                before
            ),
        },
    )
