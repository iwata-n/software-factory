import base64
import hashlib
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from software_factory.source_runner import (
    SourcePolicy,
    SourceSnapshot,
    configured_snapshot,
    create_source_app,
)
from tests.test_assembler import runner as runner
from tests.test_assembler import source_bundle as source_bundle

REMOTE = "https://github.com/iwata-n/software-factory.git"
TOKEN = "source-snapshot-test-credential"


def snapshot(source_bundle):
    source = deepcopy(source_bundle)
    source["file_manifest"] = {
        "src/example.py": hashlib.sha256(b"value = 1\n").hexdigest(),
        "checks/golden.py": hashlib.sha256(b"# immutable required check\n").hexdigest(),
    }
    return SourceSnapshot(remote_url=REMOTE, base_ref="local/self-host", source=source)


def test_operator_snapshot_is_verified_by_real_isolated_runner(source_bundle, runner):
    frozen = snapshot(source_bundle)
    app = create_source_app(SourcePolicy((REMOTE,)), runner, TOKEN, snapshot=frozen)
    with TestClient(app) as http:
        body = {"remote_url": REMOTE, "base_ref": frozen.base_ref}
        assert http.post("/v1/sources/resolve", json=body).status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        resolved = http.post("/v1/sources/resolve", json=body, headers=headers)
        assert resolved.status_code == 200, resolved.text
        data = resolved.json()
        assert data["sha"] == source_bundle["sha"] and "archive" not in data
        assert data["source_origin"] == "operator_snapshot"
        assert data["isolation"]["network"] == "none"
        fetched = http.post(
            "/v1/sources/fetch", json={"remote_url": REMOTE, "sha": data["sha"]}, headers=headers
        )
        assert fetched.status_code == 200, fetched.text
        assert base64.b64decode(fetched.json()["archive"]) == base64.b64decode(
            source_bundle["archive"]
        )
        denied = http.post(
            "/v1/sources/resolve", json=body | {"base_ref": "local/unregistered"}, headers=headers
        )
        assert denied.json()["code"] == "LOCAL_SOURCE_SNAPSHOT_NOT_FOUND"
        assert (
            http.post(
                "/v1/sources/resolve",
                json=body | {"remote_url": "https://github.com/other/repo.git"},
                headers=headers,
            ).status_code
            == 422
        )


@pytest.mark.parametrize("field", ["sha", "tree", "source_digest", "file_manifest"])
def test_snapshot_content_identity_mismatch_is_denied(source_bundle, runner, field):
    frozen = snapshot(source_bundle)
    source = deepcopy(frozen.source)
    if field == "file_manifest":
        source[field]["src/example.py"] = "0" * 64
    else:
        source[field] = "0" * (64 if field == "source_digest" else 40)
    app = create_source_app(
        SourcePolicy((REMOTE,)),
        runner,
        TOKEN,
        snapshot=frozen.model_copy(update={"source": source}),
    )
    with TestClient(app) as http:
        reply = http.post(
            "/v1/sources/resolve",
            json={"remote_url": REMOTE, "base_ref": frozen.base_ref},
            headers={"Authorization": "Bearer " + TOKEN},
        )
        assert reply.status_code == 422 and reply.json()["category"] == "POLICY"


def test_snapshot_configuration_requires_private_operator_file(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"remote_url": REMOTE, "base_ref": "local/self-host", "source": {}}))
    path.chmod(0o644)
    monkeypatch.setenv("FACTORY_SOURCE_SNAPSHOT_FILE", str(path))
    with pytest.raises(ValueError, match="owner-only"):
        configured_snapshot()
    path.chmod(0o600)
    assert configured_snapshot().base_ref == "local/self-host"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    monkeypatch.setenv("FACTORY_SOURCE_SNAPSHOT_FILE", str(link))
    with pytest.raises(ValueError, match="owner-only"):
        configured_snapshot()
