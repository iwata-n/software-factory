import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from software_factory.adapters import FakeAgent
from software_factory.models import Artifact, Attempt, Enrollment, Principal, RunEvent
from software_factory.service import issue_token, token_hash
from software_factory.storage import LocalArtifactStorage, content_hash
from tests.test_processes import start_control_plane

from .helpers import dump


@pytest.fixture
def artifacts(world, tmp_path):
    world.client.app.state.service.artifacts._storage = LocalArtifactStorage(tmp_path / "storage")
    assignment = world.claim().json()

    class Client:
        lease = assignment["lease"]
        base = f"/v1/worker/leases/{lease['id']}/artifacts"
        headers = {"Authorization": "Bearer " + world.worker_token}

        def reserve(self, body=b"hello", kind="RESULT", **overrides):
            return world.post(
                self.base,
                {
                    "fence": self.lease["fence"],
                    "kind": kind,
                    "size_bytes": len(body),
                    "sha256": content_hash(body),
                    **overrides,
                },
                token=world.worker_token,
            )

        def upload(self, artifact_id, body=b"hello", *, key=None, fence=None):
            return world.client.put(
                self.base + f"/{artifact_id}/content",
                params={"fence": fence or self.lease["fence"]},
                content=body,
                headers=self.headers | {"Idempotency-Key": key or str(uuid4())},
            )

        def finalize(self, artifact_id, *, key=None, fence=None):
            return world.post(
                self.base + f"/{artifact_id}/finalize",
                {"fence": fence or self.lease["fence"]},
                token=world.worker_token,
                key=key,
            )

        def ready(self, body=b"hello", kind="RESULT"):
            response = self.reserve(body, kind)
            assert response.status_code == 201, response.text
            artifact_id = response.json()["id"]
            assert self.upload(artifact_id, body).status_code == 200
            assert self.finalize(artifact_id).status_code == 200
            return artifact_id

    return Client()


@pytest.mark.ac("SF-09-AC01", "C-04")
def test_full_lifecycle_and_completion(artifacts, world, evidence):
    reserved = artifacts.reserve().json()
    aid = reserved["id"]
    assert reserved["status"] == "RESERVED"
    assert world.get(f"/v1/artifacts/{aid}/content").status_code == 404
    assert artifacts.upload(aid).json()["status"] == "UPLOADED"
    assert world.get(f"/v1/artifacts/{aid}/content").status_code == 404
    finalized = artifacts.finalize(aid).json()
    assert finalized["status"] == "AVAILABLE"
    assert finalized["workspace_id"] == world.workspace_id
    assert finalized["run_id"] == world.run_id
    assert world.get(f"/v1/artifacts/{aid}/content").content == b"hello"
    agent = FakeAgent()
    agent.start(
        world.get(f"/v1/runs/{world.run_id}").json()
        | {"kind": "PLAN", "snapshot": world.run()["snapshot"]}
    )
    reply = world.post(
        f"/v1/worker/leases/{artifacts.lease['id']}/complete",
        {
            "fence": artifacts.lease["fence"],
            "outcome": "EXECUTED",
            "artifact_ids": [aid],
            "result": agent.result().result,
        },
        token=world.worker_token,
    )
    assert reply.status_code == 200
    assert reply.json()["run"]["phase"] == "IMPLEMENT"
    evidence(
        "server-issued, lease-bound artifact becomes readable only after finalize",
        finalized,
        run_id=world.run_id,
        attempt_id=finalized["attempt_id"],
        artifact_id=aid,
    )


@pytest.mark.ac("SF-09-AC02", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_finalize_concurrent_and_immutable(artifacts, world, evidence, trial):
    aid = artifacts.reserve().json()["id"]
    assert artifacts.upload(aid).status_code == 200
    gate = Barrier(2)

    def send():
        gate.wait(timeout=5)
        return artifacts.finalize(aid)

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: send(), range(2)))
    assert [r.status_code for r in replies] == [200, 200]
    assert replies[0].json() == replies[1].json()
    with world.database.transaction() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.run_id == world.run_id, RunEvent.kind == "ARTIFACT_AVAILABLE")
            )
            == 1
        )
    assert artifacts.upload(aid, b"other").status_code == 409
    assert world.get(f"/v1/artifacts/{aid}/content").content == b"hello"
    evidence(
        "20 races finalize once and preserve exact bytes", [r.json() for r in replies], trial=trial
    )


@pytest.mark.ac("SF-09-AC03")
def test_exact_size_limit_and_oversize(artifacts, world, evidence):
    body = b"x" * (16 * 1024 * 1024)
    aid = artifacts.ready(body)
    assert content_hash(world.get(f"/v1/artifacts/{aid}/content").content) == content_hash(body)
    assert artifacts.reserve(size_bytes=len(body) + 1).status_code == 413
    other = artifacts.reserve(body).json()["id"]
    assert artifacts.upload(other, body + b"x").status_code == 413
    assert world.get(f"/v1/artifacts/{other}/content").status_code == 404
    evidence(
        "16 MiB accepted; reserve/upload +1 byte returns 413",
        {"accepted": len(body), "rejected": len(body) + 1},
    )


@pytest.mark.ac("SF-09-AC03", "SF-09-AC04")
@pytest.mark.parametrize("mode", ["size", "digest", "not_uploaded", "unfinalized_complete"])
def test_mismatch_never_available(artifacts, world, evidence, mode):
    aid = artifacts.reserve().json()["id"]
    if mode in {"size", "digest"}:
        response = artifacts.upload(aid, b"wrong" if mode == "digest" else b"too long")
    elif mode == "not_uploaded":
        response = artifacts.finalize(aid)
    else:
        response = world.post(
            f"/v1/worker/leases/{artifacts.lease['id']}/complete",
            {"fence": artifacts.lease["fence"], "outcome": "EXECUTED", "artifact_ids": [aid]},
            token=world.worker_token,
        )
    assert response.status_code == 409
    assert world.get(f"/v1/artifacts/{aid}/content").status_code == 404
    assert world.run()["status"] == "RUNNING"
    evidence("invalid/incomplete bytes never become available or complete a job", response.json())


@pytest.mark.ac("SF-09-AC04")
@pytest.mark.parametrize("attack", ["uri", "path", "workspace_id", "attempt_id"])
def test_client_cannot_choose_storage(artifacts, world, evidence, attack):
    response = artifacts.reserve(**{attack: "../../outside"})
    assert response.status_code == 422
    with world.database.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Artifact)) == 0
    evidence("unrecognized storage/ownership fields rejected", response.status_code, field=attack)


@pytest.mark.ac("SF-09-AC04")
@pytest.mark.parametrize(
    "attack", ["workspace_symlink", "file_symlink", "hardlink", "root_symlink"]
)
def test_storage_link_escape_denied(artifacts, world, tmp_path, evidence, attack):
    aid = artifacts.reserve().json()["id"]
    store = world.client.app.state.service.artifacts.storage
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary"
    canary.write_bytes(b"outside")
    tenant = store.root / world.workspace_id
    if attack == "root_symlink":
        store.root.rmdir()
        store.root.symlink_to(outside, target_is_directory=True)
    elif attack == "workspace_symlink":
        tenant.symlink_to(outside, target_is_directory=True)
    else:
        tenant.mkdir()
        if attack == "hardlink":
            os.link(canary, tenant / aid)
        else:
            (tenant / aid).symlink_to(canary)
    response = artifacts.upload(aid)
    assert response.status_code == 409
    assert canary.read_bytes() == b"outside"
    assert sorted(x.name for x in outside.iterdir()) == ["canary"]
    evidence("no storage link escape or outside write", response.json(), attack=attack)


@pytest.mark.ac("SF-09-AC05")
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
@pytest.mark.parametrize("operation", ["reserve", "upload", "finalize"])
def test_lease_expiry_boundary(artifacts, world, clock, evidence, offset, operation):
    aid = artifacts.reserve().json()["id"]
    assert artifacts.upload(aid).status_code == 200
    with world.database.transaction() as s:
        expires = s.scalar(select(Attempt.expires_at))
    clock.value = expires + offset
    response = {
        "reserve": lambda: artifacts.reserve(),
        "upload": lambda: artifacts.upload(aid),
        "finalize": lambda: artifacts.finalize(aid),
    }[operation]()
    expected = (201 if operation == "reserve" else 200) if offset < 0 else 409
    assert response.status_code == expected
    evidence(
        "now >= expires_at refuses new submissions",
        response.status_code,
        operation=operation,
        offset=offset,
    )


@pytest.mark.ac("SF-09-AC05", "C-03")
def test_accepted_replay_after_expiry_and_fence_rejection(artifacts, world, clock, evidence):
    aid = artifacts.reserve().json()["id"]
    key_upload, key_finalize = str(uuid4()), str(uuid4())
    uploaded = artifacts.upload(aid, key=key_upload)
    finalized = artifacts.finalize(aid, key=key_finalize)
    assert uploaded.status_code == finalized.status_code == 200
    clock.advance(91)
    assert artifacts.upload(aid, key=key_upload).json() == uploaded.json()
    assert artifacts.finalize(aid, key=key_finalize).json() == finalized.json()
    assert artifacts.finalize(aid).status_code == 409
    assert artifacts.finalize(aid, fence=artifacts.lease["fence"] + 1).status_code == 409
    evidence(
        "accepted exact replay survives expiry; new requests and old fences rejected",
        finalized.json(),
    )


@pytest.mark.ac("SF-09-AC05")
def test_cancel_stops_all_new_submissions(artifacts, world, evidence):
    aid = artifacts.reserve().json()["id"]
    assert artifacts.upload(aid).status_code == 200
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "cancel artifact work"},
    )
    replies = [artifacts.reserve(), artifacts.upload(aid), artifacts.finalize(aid)]
    assert [r.status_code for r in replies] == [409, 409, 409]
    evidence("CANCELLING prevents reserve/upload/finalize", [r.json() for r in replies])


@pytest.mark.ac("SF-09-AC06")
def test_expired_pending_gc_preserves_available_and_is_repeatable(
    artifacts, world, clock, evidence
):
    kept = artifacts.ready()
    pending = artifacts.reserve().json()["id"]
    assert artifacts.upload(pending).status_code == 200
    store = world.client.app.state.service.artifacts.storage
    partial = store.root / world.workspace_id / (pending + ".dead.part")
    partial.write_bytes(b"interrupted")
    clock.advance(3599.999)
    service = world.client.app.state.service
    service.reconcile()
    assert (store.root / world.workspace_id / pending).exists()
    clock.advance(0.0011)
    service.reconcile()
    service.reconcile()
    assert not (store.root / world.workspace_id / pending).exists()
    assert not partial.exists()
    assert world.get(f"/v1/artifacts/{pending}").status_code == 404
    assert world.get(f"/v1/artifacts/{kept}/content").content == b"hello"
    evidence(
        "1h pending bytes and interrupted parts collected once; available retained",
        {"deleted": pending, "kept": kept},
    )


@pytest.mark.ac("SF-09-AC07", "C-02", "C-06")
def test_artifact_authentication_tenancy_and_masked_logs(artifacts, world, evidence):
    secret = "ghp_artifact_acceptance_92ac8"
    body = ("authorization=Bearer " + secret + "\n" + world.worker_token).encode()
    aid = artifacts.ready(body, "LOG")
    downloaded = world.get(f"/v1/artifacts/{aid}/content").content
    assert secret.encode() not in downloaded and world.worker_token.encode() not in downloaded
    assert b"[REDACTED]" in downloaded
    for path in (
        f"/v1/artifacts/{aid}",
        f"/v1/artifacts/{aid}/content",
        f"/v1/runs/{world.run_id}/artifacts",
    ):
        assert world.client.get(path).status_code == 401
        assert world.get(path, token=world.other_user_token).status_code == 404
        assert world.get(path, token=world.worker_token).status_code == 403
    other_worker = world.make_worker()["worker_token"]
    path = artifacts.base + f"/{aid}/content?fence={artifacts.lease['fence']}"
    assert world.get(path, token=other_worker).status_code == 404
    assert world.get(path, token=world.worker_token).content == downloaded
    evidence(
        "A/B and User/Worker separation; only redacted log bytes persisted",
        {"stored_digest": content_hash(downloaded), "secret_occurrences": 0},
    )


@pytest.mark.ac("SF-09-AC07", "C-02")
@pytest.mark.parametrize(
    "route",
    ["reserve", "upload", "finalize", "worker_content", "source", "metadata", "content", "list"],
)
@pytest.mark.parametrize(
    "role",
    [
        "unauthenticated",
        "user",
        "other_user",
        "other_worker",
        "other_workspace_worker",
        "revoked_worker",
    ],
)
def test_all_added_api_authorization_matrix(artifacts, world, route, role, evidence):
    aid = artifacts.ready()
    second = world.make_worker()
    revoked = world.make_worker()
    enrollment = issue_token()
    with world.database.transaction() as s:
        s.get(Principal, revoked["worker_id"]).active = False
        s.add(
            Enrollment(
                workspace_id=world.other_workspace_id,
                token_hash=token_hash(enrollment),
                capabilities=["PLAN", "IMPLEMENT", "adapter:fake"],
                expires_at=10**12,
            )
        )
    cross = world.client.post(
        "/v1/worker/enroll", json={"token": enrollment, "name": "workspace B"}
    ).json()
    tokens = {
        "user": world.user_token,
        "other_user": world.other_user_token,
        "other_worker": second["worker_token"],
        "other_workspace_worker": cross["worker_token"],
        "revoked_worker": revoked["worker_token"],
    }
    lease_id, fence = artifacts.lease["id"], artifacts.lease["fence"]
    endpoints = {
        "reserve": (
            "POST",
            artifacts.base,
            {"fence": fence, "kind": "RESULT", "size_bytes": 5, "sha256": content_hash(b"hello")},
        ),
        "upload": ("PUT", artifacts.base + f"/{aid}/content?fence={fence}", b"hello"),
        "finalize": ("POST", artifacts.base + f"/{aid}/finalize", {"fence": fence}),
        "worker_content": ("GET", artifacts.base + f"/{aid}/content?fence={fence}", None),
        "source": ("GET", f"/v1/worker/leases/{lease_id}/source?fence={fence}", None),
        "metadata": ("GET", f"/v1/artifacts/{aid}", None),
        "content": ("GET", f"/v1/artifacts/{aid}/content", None),
        "list": ("GET", f"/v1/runs/{world.run_id}/artifacts", None),
    }
    method, path, body = endpoints[route]
    public = route in {"metadata", "content", "list"}
    expected = (
        401
        if role in {"unauthenticated", "revoked_worker"}
        else 200
        if public and role == "user"
        else 404
        if public and role == "other_user"
        else 403
        if public or role in {"user", "other_user"}
        else 404
    )
    headers = {"Idempotency-Key": str(uuid4())}
    if role != "unauthenticated":
        headers["Authorization"] = "Bearer " + tokens[role]
    before = dump(world.database)
    reply = world.client.request(
        method,
        path,
        headers=headers,
        **(
            {"content": body}
            if isinstance(body, bytes)
            else {"json": body}
            if body is not None
            else {}
        ),
    )
    assert reply.status_code == expected, reply.text
    assert dump(world.database) == before
    evidence(
        "all added APIs enforce role/workspace/lease ownership without rejected side effects",
        {"route": route, "role": role, "status": reply.status_code},
    )


@pytest.mark.ac("SF-09-AC03")
def test_real_partial_http_upload_is_never_downloadable(artifacts, world, evidence):
    aid = artifacts.reserve().json()["id"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = start_control_plane(port)
    try:
        path = artifacts.base + f"/{aid}/content?fence={artifacts.lease['fence']}"
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            headers = (
                f"PUT {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {world.worker_token}\r\n"
                f"Idempotency-Key: {uuid4()}\r\nContent-Length: 5\r\nConnection: close\r\n\r\n"
            )
            connection.sendall(headers.encode() + b"he")
            connection.shutdown(socket.SHUT_WR)
        time.sleep(0.2)
        assert world.get(f"/v1/artifacts/{aid}/content").status_code == 404
        assert world.get(f"/v1/artifacts/{aid}").json()["status"] == "RESERVED"
        assert artifacts.finalize(aid).status_code == 409
        evidence(
            "real TCP disconnect after 2 of 5 bytes leaves reservation unreadable",
            {"sent_bytes": 2, "declared_bytes": 5, "status": "RESERVED"},
            artifact_id=aid,
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)


@pytest.mark.ac("SF-09-AC03")
def test_finalize_checks_actual_stored_bytes(artifacts, world, evidence):
    aid = artifacts.reserve().json()["id"]
    assert artifacts.upload(aid).status_code == 200
    path = world.client.app.state.service.artifacts.storage.root / world.workspace_id / aid
    path.chmod(0o600)
    path.write_bytes(b"corrupted disk contents")
    reply = artifacts.finalize(aid)
    assert reply.status_code == 409
    assert world.get(f"/v1/artifacts/{aid}/content").status_code == 404
    evidence("finalize hashes actual bytes instead of trusting uploaded metadata", reply.json())


@pytest.mark.ac("SF-09-AC04", "C-02")
def test_other_attempt_artifact_cannot_complete_job(artifacts, world, evidence):
    aid = artifacts.ready()
    another = world.make_worker()
    new_run = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()
    assigned = world.claim(token=another["worker_token"]).json()
    assert assigned["job"]["run_id"] == new_run["run_id"]
    reply = world.post(
        f"/v1/worker/leases/{assigned['lease']['id']}/complete",
        {"fence": assigned["lease"]["fence"], "outcome": "EXECUTED", "artifact_ids": [aid]},
        token=another["worker_token"],
    )
    assert reply.status_code == 409
    assert world.get(f"/v1/runs/{new_run['run_id']}").json()["status"] == "RUNNING"
    evidence("available artifact from another attempt never adopts or completes job", reply.json())
