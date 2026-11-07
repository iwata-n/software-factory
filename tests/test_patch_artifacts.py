import hashlib
from uuid import uuid4

import pytest


def upload(world, body):
    lease = world.claim().json()["lease"]
    root = f"/v1/worker/leases/{lease['id']}/artifacts"
    reserved = world.post(
        root,
        {
            "fence": lease["fence"],
            "kind": "PATCH",
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
        token=world.worker_token,
    )
    assert reserved.status_code == 201, reserved.text
    identifier = reserved.json()["id"]
    reply = world.client.put(
        root + f"/{identifier}/content",
        params={"fence": lease["fence"]},
        content=body,
        headers={"Authorization": "Bearer " + world.worker_token, "Idempotency-Key": str(uuid4())},
    )
    return reply, root, identifier, lease


def test_code_patch_preserves_variable_bindings_and_authorization_interpolation(
    world, monkeypatch, tmp_path
):
    monkeypatch.setenv("FACTORY_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    body = (
        b"--- /dev/null\n+++ b/src/headers.py\n@@ -0,0 +1,2 @@\n"
        b"+def headers(token: Token):\n+    return {'Authorization': f'Bearer {token}'}\n"
    )
    reply, root, identifier, lease = upload(world, body)
    assert reply.status_code == 200, reply.text
    final = world.post(
        root + f"/{identifier}/finalize", {"fence": lease["fence"]}, token=world.worker_token
    )
    assert final.status_code == 200, final.text
    assert world.get(f"/v1/artifacts/{identifier}/content").content == body


@pytest.mark.parametrize(
    "content",
    [
        "value = 'Bearer secret-canary'",
        "value = 'ghp_RECOGNIZED_CREDENTIAL'",
        "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----",
    ],
)
def test_recognized_credentials_in_patch_remain_rejected(world, monkeypatch, tmp_path, content):
    monkeypatch.setenv("FACTORY_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    added = "".join("+" + line + "\n" for line in content.splitlines())
    body = (
        "--- /dev/null\n+++ b/src/value.py\n@@ -0,0 +1,"
        + str(len(content.splitlines()))
        + " @@\n"
        + added
    ).encode()
    reply, *_ = upload(world, body)
    assert reply.status_code == 409 and reply.json()["code"] == "SENSITIVE_ARTIFACT_REJECTED"


def test_worker_credential_is_rejected_even_in_code_without_a_known_prefix(
    world, monkeypatch, tmp_path
):
    monkeypatch.setenv("FACTORY_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    body = (
        "--- /dev/null\n+++ b/src/value.py\n@@ -0,0 +1 @@\n+value = '" + world.worker_token + "'\n"
    ).encode()
    reply, *_ = upload(world, body)
    assert reply.status_code == 409 and reply.json()["code"] == "SENSITIVE_ARTIFACT_REJECTED"


def test_known_worker_credential_in_removed_context_is_still_rejected(world, monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    body = (
        "--- a/src/value.py\n+++ b/src/value.py\n@@ -1 +1 @@\n-value = '"
        + world.worker_token
        + "'\n+value = None\n"
    ).encode()
    reply, *_ = upload(world, body)
    assert reply.status_code == 409 and reply.json()["code"] == "SENSITIVE_ARTIFACT_REJECTED"
