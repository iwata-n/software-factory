import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from software_factory.models import Attempt, Project
from software_factory.sandbox import DockerRunner, RunnerError
from tests.conftest import example_policy
from tests.test_assembler import PATCH, request
from tests.test_assembler import runner as runner
from tests.test_assembler import source_bundle as source_bundle

from .helpers import dump
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
