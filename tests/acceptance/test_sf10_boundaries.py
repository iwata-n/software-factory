import io
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from software_factory.models import Attempt, Project
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.sandbox import DockerRunner, RunnerError
from software_factory.worker import Supervisor
from tests.conftest import example_policy, example_spec
from tests.test_assembler import PATCH, request
from tests.test_assembler import runner as runner
from tests.test_assembler import source_bundle as source_bundle

from .helpers import dump, worker_client
from .test_sf10 import make_pipeline
from .test_sf10 import pipeline as pipeline


@pytest.mark.ac("SF-10-AC02", "SF-10-AC06", "C-04")
def test_candidate_transaction_failure_rolls_back_every_row(pipeline, evidence):
    pipeline.prepare()
    path, body = pipeline.report()
    service = pipeline.world.client.app.state.service
    before = dump(pipeline.world.database)
    key = str(uuid4())

    def fail(point):
        if point == "candidate.created":
            raise RuntimeError("injected candidate commit failure")

    service.checkpoint = fail
    try:
        with pytest.raises(RuntimeError, match="injected"):
            pipeline.client.post(path + "/complete", json=body, headers={"Idempotency-Key": key})
    finally:
        service.checkpoint = lambda name: None
    assert dump(pipeline.world.database) == before
    reply = pipeline.client.post(path + "/complete", json=body, headers={"Idempotency-Key": key})
    assert reply.status_code == 200 and reply.json()["run"]["current_candidate_id"]
    evidence(
        "flushed Candidate, settlement, events and jobs all roll back before commit",
        {"rollback_equal": True, "retry_status": reply.status_code},
        candidate_id=reply.json()["run"]["current_candidate_id"],
    )


@pytest.mark.ac("SF-10-AC02")
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
def test_candidate_never_accepts_expired_attempt(pipeline, clock, offset, evidence):
    pipeline.prepare()
    path, body = pipeline.report()
    with pipeline.world.database.transaction() as s:
        expires = s.scalar(
            select(Attempt.expires_at).where(Attempt.lease_id == path.rsplit("/", 1)[-1])
        )
    clock.value = expires + offset
    reply = pipeline.client.post(
        path + "/complete", json=body, headers={"Idempotency-Key": str(uuid4())}
    )
    assert reply.status_code == (200 if offset < 0 else 409)
    evidence(
        "Candidate acceptance uses the same strict lease boundary as all worker results",
        {"offset": offset, "status": reply.status_code},
    )


@pytest.mark.ac("SF-10-AC07")
def test_nonexistent_real_image_cannot_start_container(tmp_path, monkeypatch, evidence):
    monkeypatch.setenv("FACTORY_RUNNER_STATE_ROOT", str(tmp_path / "runner-state"))
    runner = DockerRunner("sha256:" + "0" * 64)
    with pytest.raises(RunnerError) as denied:
        runner.execute({"operation": "assemble"})
    assert denied.value.code == "IMAGE_DIGEST_MISMATCH"
    assert not runner._active
    evidence(
        "actual Docker inspection fails before start when the pinned image does not exist",
        {"reason": denied.value.code, "containers_started": 0},
    )


@pytest.mark.ac("SF-10-AC03")
def test_new_test_inside_allowed_paths_is_adopted(pipeline, evidence):
    patch = b"--- /dev/null\n+++ b/tests/new_test.py\n@@ -0,0 +1 @@\n+assert 1 + 1 == 2\n"
    pipeline.prepare(patch)
    pipeline.assemble()
    candidate_id = pipeline.world.run()["current_candidate_id"]
    assert candidate_id
    item = pipeline.world.get(f"/v1/candidates/{candidate_id}").json()
    assert item["metadata_body"]["changed_paths"] == ["tests/new_test.py"]
    evidence(
        "new optional tests inside allowed roots do not weaken protected verification",
        item,
        candidate_id=candidate_id,
    )


@pytest.mark.ac("SF-10-AC04")
@pytest.mark.parametrize(
    "patch", [b"GIT binary patch\nliteral 0\n", b"\x1f\x8barchive", b"--- a/a\n+++ b/a\n\0"]
)
def test_unsupported_change_format_returns_422(pipeline, patch, evidence):
    with pytest.raises(httpx.HTTPStatusError) as failure:
        pipeline.prepare(patch)
    assert failure.value.response.status_code == 422
    assert pipeline.world.run()["current_candidate_id"] is None
    evidence(
        "binary/archive/NUL changes rejected as 422 before any Candidate is assembled",
        failure.value.response.json(),
    )


@pytest.mark.ac("SF-10-AC06", "SF-10-AC07")
def test_orphan_collection_never_removes_a_live_supervisor(
    source_bundle, runner, tmp_path, monkeypatch, evidence
):
    monkeypatch.setenv("FACTORY_RUNNER_STATE_ROOT", str(tmp_path / "live-owners"))
    first = DockerRunner(runner.image_digest)
    cancel = Event()
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(
            first.execute,
            request(source_bundle, PATCH),
            timeout=30,
            cancelled=cancel,
            pause_at="patch_applied",
        )
        try:
            container = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with first._lock:
                    names = list(first._active)
                if names:
                    logs = first.docker("logs", names[0], timeout=3)
                    if b"FACTORY_STAGE:patch_applied" in logs.stdout + logs.stderr:
                        container = names[0]
                        break
                time.sleep(0.05)
            assert container
            second = DockerRunner(runner.image_digest)
            actual = second.docker(
                "inspect", container, "--format", "{{.State.Running}}", timeout=3
            )
            assert actual.returncode == 0 and actual.stdout.strip() == b"true"
            assert second.cleanup_orphans() == []
            evidence(
                "startup cleanup respects a live owner's kernel lock instead of killing its runner",
                {"owner_live": True, "running": True, "removed": 0},
                container_id=container,
            )
        finally:
            cancel.set()
            first.cancel()
            with pytest.raises(RunnerError):
                pending.result(timeout=10)


@pytest.mark.ac("SF-10-AC05", "C-06")
def test_protected_token_filename_preserves_its_hash_without_allowing_credentials(world, evidence):
    policy = example_policy() | {"protected_manifest": {"src/auth/token.py": "a" * 64}}
    reply = world.post(
        f"/v1/workspaces/{world.workspace_id}/projects",
        {"name": "protected metadata", "policy": policy},
    )
    assert reply.status_code == 201, reply.text
    with world.database.transaction() as s:
        stored = s.get(Project, reply.json()["project_id"]).policy["protected_manifest"]
    assert stored == policy["protected_manifest"]
    rejected = world.post(
        f"/v1/workspaces/{world.workspace_id}/projects",
        {
            "name": "credentials are still forbidden",
            "policy": policy | {"agent_configuration": {"api_key": "ghp_private_canary"}},
        },
    )
    assert rejected.status_code == 422
    evidence(
        "protected file hashes are preserved; credential fields remain forbidden",
        {
            "path": "src/auth/token.py",
            "stored_sha256": stored["src/auth/token.py"],
            "rejected_status": rejected.status_code,
        },
    )


@pytest.mark.ac("SF-21-AC05", "SF-23-AC02")
@pytest.mark.parametrize("trigger", ["cancel", "disconnect"])
def test_managed_worker_confirms_container_removed_on_stop(
    pipeline, tmp_path, monkeypatch, evidence, trigger
):
    class PausedRunner(DockerRunner):
        def execute(self, request, **kwargs):
            return super().execute(request, pause_at="patch_applied", **kwargs)

    pipeline.prepare()
    world = pipeline.world
    monkeypatch.setenv("FACTORY_RUNNER_STATE_ROOT", str(tmp_path / "cancel-owners"))
    runner = PausedRunner(pipeline.runner.image_digest)
    adapter = PipelineAdapter(runner)
    supervisor = Supervisor(pipeline.client, adapter, heartbeat_interval=0.1)
    container = None
    confirmed = []
    disconnected = Event()

    def check_before_report(request):
        if disconnected.is_set():
            raise httpx.ReadError("injected loss of Control Plane connection", request=request)
        if not request.url.path.endswith("/complete"):
            return
        assert container is not None
        listing = runner.docker("ps", "-aq", "--filter", f"name=^/{container}$", timeout=5)
        assert listing.returncode == 0 and not listing.stdout.strip()
        confirmed.append(container)

    pipeline.client.event_hooks["request"].append(check_before_report)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(supervisor.run_once)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with runner._lock:
                    names = list(runner._active)
                if names:
                    logs = runner.docker("logs", names[0], timeout=3)
                    if b"FACTORY_STAGE:patch_applied" in logs.stdout + logs.stderr:
                        container = names[0]
                        break
                time.sleep(0.05)
            assert container is not None
            if trigger == "cancel":
                response = world.post(
                    f"/v1/runs/{world.run_id}/cancel",
                    {"expected_run_revision": world.run()["state_revision"], "reason": "test stop"},
                )
                assert response.status_code == 202
                assert pending.result(timeout=12)["run"]["status"] == "CANCELLED"
                assert confirmed == [container]
            else:
                disconnected.set()
                with pytest.raises(httpx.ReadError, match="loss of Control Plane"):
                    pending.result(timeout=12)
                assert not confirmed
            assert adapter.result().outcome == "CANCELLED"
            listing = runner.docker("ps", "-aq", "--filter", f"name=^/{container}$", timeout=5)
            assert listing.returncode == 0 and not listing.stdout.strip()
            workers = world.get(f"/v1/workspaces/{world.workspace_id}/workers").json()["workers"]
            managed = next(
                worker for worker in workers if worker["worker_id"] == pipeline.managed.worker_id
            )
            expected = "WORKER_REPORTED_STOP" if trigger == "cancel" else None
            assert managed["stop_confirmation"] == expected
            evidence(
                "managed assembler stops and removes its container after the stop trigger",
                {"trigger": trigger, "container_removed": True, "worker": managed},
                container_id=container,
            )
        finally:
            if container and not pending.done():
                runner.docker("rm", "--force", container, timeout=7)


@pytest.mark.ac("SF-21-AC02")
def test_two_workspaces_cannot_share_managed_worker_inputs(
    pipeline, source_bundle, tmp_path, monkeypatch, evidence
):
    first = pipeline.world
    other = replace(first, workspace_id=first.other_workspace_id, user_token=first.other_user_token)
    project = other.post(
        f"/v1/workspaces/{other.workspace_id}/projects",
        {"name": "other", "policy": example_policy()},
    ).json()
    repository = other.post(
        f"/v1/projects/{project['project_id']}/repositories",
        {"name": "other", "base_sha": "a" * 40},
    ).json()
    task = other.post(
        f"/v1/projects/{project['project_id']}/tasks", example_spec(repository["repository_id"])
    ).json()
    initial = other.post(f"/v1/tasks/{task['task_id']}/runs", {"spec_version": 1}).json()
    worker = other.make_worker()
    other = replace(
        other,
        project_id=project["project_id"],
        repository_id=repository["repository_id"],
        task_id=task["task_id"],
        run_id=initial["run_id"],
        worker_id=worker["worker_id"],
        worker_token=worker["worker_token"],
    )
    second = make_pipeline(other, source_bundle, pipeline.runner, tmp_path)

    monkeypatch.setenv("FACTORY_RUNNER_STATE_ROOT", str(tmp_path / "tenant-owners"))
    results = []

    class InspectRunner(DockerRunner):
        def execute(self, request, **kwargs):
            result = super().execute(request, **kwargs)
            results.append(result.isolation)
            return result

    runner = InspectRunner(pipeline.runner.image_digest)
    stages = (pipeline, second)
    values = (b"tenant_a_canary", b"tenant_b_canary")
    for stage, canary in zip(stages, values, strict=True):
        stage.prepare(PATCH.replace(b"value = 2", b"value = '" + canary + b"'"))

    barrier = Barrier(2)
    leases = {}

    def assemble(index):
        stage = stages[index]

        def attack(assignment):
            leases[index] = assignment["lease"]
            barrier.wait(timeout=10)
            foreign = leases[1 - index]
            response = stage.world.get(
                f"/v1/worker/leases/{foreign['id']}/source?fence={foreign['fence']}",
                token=stage.managed.worker_token,
            )
            assert response.status_code == 404

        with worker_client(stage.managed, after_claim=attack)[0] as client:
            assert Supervisor(client, PipelineAdapter(runner), heartbeat_interval=0.1).run_once()[
                "accepted"
            ]

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(assemble, range(2)))
    assert len(results) == 2 and all(row["mounts"] == [] for row in results)
    assert all(row["network"] == "none" for row in results)
    assert all(set(row["tmpfs"]) == {"/tmp", "/work"} for row in results)
    for index, stage in enumerate(stages):
        world = stage.world
        candidate = world.get(f"/v1/candidates/{world.run()['current_candidate_id']}").json()
        path = f"/v1/artifacts/{candidate['source_artifact_id']}/content"
        assert stages[1 - index].world.get(path).status_code == 404
        with tarfile.open(fileobj=io.BytesIO(world.get(path).content), mode="r:gz") as archive:
            actual = archive.extractfile("src/example.py").read()
            assert values[index] in actual and values[1 - index] not in actual
    evidence(
        "two Workspace credentials cannot cross leases or artifacts; no shared cache is mounted",
        {"workspaces": 2, "cross_lease": 404, "cross_artifact": 404, "shared_mounts": 0},
    )
