import base64
import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from software_factory.models import Candidate, Job, Worker
from tests.test_assembler import runner as runner
from tests.test_assembler import source_bundle as source_bundle

from .helpers import dump
from .test_sf10 import pipeline as pipeline


@pytest.mark.ac("SF-10-AC02", "C-03")
@pytest.mark.parametrize("trial", range(20))
def test_concurrent_assembly_report_commits_once(pipeline, trial, evidence):
    pipeline.prepare()
    path, body = pipeline.report()
    key = str(uuid4())
    barrier = Barrier(2)

    def complete():
        barrier.wait(5)
        return pipeline.client.post(path + "/complete", json=body, headers={"Idempotency-Key": key})

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: complete(), range(2)))
    assert [reply.status_code for reply in replies] == [200, 200]
    assert replies[0].json() == replies[1].json()
    with pipeline.world.database.transaction() as s:
        assert s.scalar(select(func.count()).select_from(Candidate)) == 1
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "VERIFY")) == 1
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "REVIEW")) == 1
    evidence(
        "20 concurrent exact assembly replays create one Candidate and one of each next Job",
        {
            "trial": trial,
            "candidate_id": pipeline.world.run()["current_candidate_id"],
            "replies": [r.status_code for r in replies],
        },
    )


@pytest.mark.ac("SF-10-AC01", "SF-10-AC07", "C-04")
@pytest.mark.parametrize(
    "field",
    [
        "base_sha",
        "head_sha",
        "tree_sha",
        "spec_hash",
        "profile_hash",
        "image_digest",
        "patch_artifact_id",
        "source_artifact_id",
        "origin",
        "network",
    ],
)
def test_wrong_candidate_tuple_or_claimed_origin_never_commits(pipeline, field, evidence):
    pipeline.prepare()
    path, body = pipeline.report()
    report = json.loads(json.dumps(body["result"]))
    if field in {"base_sha", "head_sha", "tree_sha"}:
        report[field] = "0" * 40
    elif field in {"spec_hash", "profile_hash"}:
        report[field] = "0" * 64
    elif field == "image_digest":
        report[field] = "sha256:" + "0" * 64
    elif field == "patch_artifact_id":
        report[field] = report["source_artifact_id"]
    elif field == "source_artifact_id":
        report[field] = report["patch_artifact_id"]
    elif field == "network":
        report["isolation"]["network"] = "bridge"
    else:
        report["origin"] = "managed"
    before = dump(pipeline.world.database)
    reply = pipeline.client.post(
        path + "/complete",
        json=body | {"result": report},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert reply.status_code == (422 if field == "origin" else 409), reply.text
    assert dump(pipeline.world.database) == before
    assert pipeline.world.run()["current_candidate_id"] is None
    evidence(
        "wrong target/image/isolation or body-claimed trust cannot create a Candidate",
        reply.json(),
        field=field,
    )


@pytest.mark.ac("SF-10-AC03", "SF-10-AC04", "SF-10-AC05")
@pytest.mark.parametrize(
    "attack",
    [
        "outside",
        "protected_edit",
        "protected_delete",
        "protected_rename",
        "protected_mode",
        "conftest_skip",
        "traversal",
        "git_hook",
        "symlink",
        "ci",
        "migration",
        "authentication",
        "billing",
    ],
)
def test_actual_diff_denies_bypass_after_plan_adoption(pipeline, attack, tmp_path, evidence):
    canary = tmp_path / "host-canary"
    canary.write_text("unchanged")

    def new(path, body):
        return (f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{body}\n").encode()

    if attack == "outside":
        patch = new("outside.py", "not allowed")
    elif attack == "protected_edit":
        patch = (
            b"--- a/checks/golden.py\n+++ b/checks/golden.py\n@@ -1 +1 @@\n"
            b"-# immutable required check\n+# skipped\n"
        )
    elif attack == "protected_delete":
        patch = (
            b"--- a/checks/golden.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-# immutable required check\n"
        )
    elif attack == "protected_rename":
        patch = (
            b"diff --git a/checks/golden.py b/src/moved.py\nsimilarity index 100%\n"
            b"rename from checks/golden.py\nrename to src/moved.py\n"
        )
    elif attack == "protected_mode":
        patch = (
            b"diff --git a/checks/golden.py b/checks/golden.py\nold mode 100644\nnew mode 100755\n"
        )
    elif attack == "conftest_skip":
        patch = new("tests/conftest.py", "def pytest_collection_modifyitems(items): items.clear()")
    elif attack == "traversal":
        patch = new("../../host-canary", "escaped")
    elif attack == "git_hook":
        patch = new(".git/hooks/post-checkout", "touch /host-canary")
    elif attack in {"ci", "migration", "authentication", "billing"}:
        path = {
            "ci": ".github/workflows/ci.yml",
            "migration": "migrations/001.py",
            "authentication": "src/auth/session.py",
            "billing": "src/billing/payment.py",
        }[attack]
        patch = new(path, "unexpected critical change")
    else:
        patch = (
            "diff --git a/src/link b/src/link\nnew file mode 120000\n"
            "--- /dev/null\n+++ b/src/link\n"
            "@@ -0,0 +1 @@\n+" + str(canary) + "\n"
        ).encode()
    pipeline.prepare(patch)
    response = pipeline.assemble()
    assert response["accepted"]
    run = pipeline.world.run()
    protected = attack.startswith("protected") or attack in {
        "conftest_skip",
        "ci",
        "migration",
        "authentication",
        "billing",
    }
    assert run["status"] == "BLOCKED"
    assert run["stop_reason"] == ("PROTECTED_VERIFICATION_CHANGE" if protected else "POLICY_DENIED")
    assert run["current_candidate_id"] is None
    assert canary.read_text() == "unchanged"
    evidence(
        "real diff blocks protected or escaping changes even after normal Plan approval",
        run,
        attack=attack,
    )


@pytest.mark.ac("SF-10-AC04")
@pytest.mark.parametrize("attack", ["hardlink", "altered_base"])
def test_untrusted_source_archive_or_base_is_never_adopted(pipeline, attack, tmp_path, evidence):
    pipeline.prepare()
    canary = tmp_path / "external"
    canary.write_text("safe")
    if attack == "hardlink":
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            info = tarfile.TarInfo("src/hardlink")
            info.type = tarfile.LNKTYPE
            info.linkname = str(canary)
            bundle.addfile(info)
        pipeline.source["archive"] = base64.b64encode(archive.getvalue()).decode()
    else:
        pipeline.source["commit_object"] = base64.b64encode(b"tree " + b"0" * 40 + b"\n").decode()
    pipeline.assemble()
    assert pipeline.world.run()["current_candidate_id"] is None
    assert pipeline.world.run()["stop_reason"] == "POLICY_DENIED"
    assert canary.read_text() == "safe"
    evidence(
        "hardlink or altered base refused inside isolated assembler",
        pipeline.world.run(),
        attack=attack,
    )


@pytest.mark.ac("SF-10-AC02", "C-02")
def test_candidate_tenant_and_managed_origin_boundary(pipeline, evidence):
    pipeline.prepare()
    assert pipeline.world.claim().status_code == 204
    path, body = pipeline.report()
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).managed = False
    reply = pipeline.client.post(
        path + "/complete", json=body, headers={"Idempotency-Key": str(uuid4())}
    )
    assert reply.status_code == 403 and pipeline.world.run()["current_candidate_id"] is None
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).managed = True
    assert (
        pipeline.client.post(
            path + "/complete", json=body, headers={"Idempotency-Key": str(uuid4())}
        ).status_code
        == 200
    )
    candidate_id = pipeline.world.run()["current_candidate_id"]
    for target in (
        f"/v1/candidates/{candidate_id}",
        f"/v1/runs/{pipeline.world.run_id}/candidates",
    ):
        assert pipeline.world.client.get(target).status_code == 401
        assert pipeline.world.get(target, token=pipeline.managed.worker_token).status_code == 403
        assert pipeline.world.get(target, token=pipeline.world.other_user_token).status_code == 404
    evidence(
        "unmanaged claims/reports fail; Candidate API is User and Workspace scoped",
        {"candidate_id": candidate_id, "unmanaged_report": 403},
    )


@pytest.mark.ac("SF-10-AC02", "C-02")
def test_candidate_and_input_routes_enforce_each_authority(pipeline, evidence):
    pipeline.prepare()
    path, body = pipeline.report()
    patch = body["result"]["patch_artifact_id"]
    target = path + f"/inputs/{patch}/content"
    fence = body["fence"]
    before = dump(pipeline.world.database)
    cases = [
        (None, 401),
        (pipeline.world.user_token, 403),
        (pipeline.world.other_user_token, 403),
        (pipeline.world.worker_token, 404),
    ]
    for token, status in cases:
        headers = {"Authorization": "Bearer " + token} if token else {}
        reply = pipeline.world.client.get(target, params={"fence": fence}, headers=headers)
        assert reply.status_code == status
    assert pipeline.client.get(target, params={"fence": fence}).status_code == 200
    assert pipeline.client.get(target, params={"fence": fence + 1}).status_code == 409
    for aid in (body["result"]["source_artifact_id"], str(uuid4())):
        assert (
            pipeline.client.get(
                path + f"/inputs/{aid}/content", params={"fence": fence}
            ).status_code
            == 404
        )
    assert dump(pipeline.world.database) == before
    reply = pipeline.client.post(
        path + "/complete", json=body, headers={"Idempotency-Key": str(uuid4())}
    )
    candidate = reply.json()["run"]["current_candidate_id"]
    before = dump(pipeline.world.database)
    targets = [
        ("GET", f"/v1/candidates/{candidate}"),
        ("GET", f"/v1/runs/{pipeline.world.run_id}/candidates"),
    ]
    targets += [(method, f"/v1/candidates/{candidate}") for method in ("PUT", "PATCH", "DELETE")]
    for method, route in targets:
        for token, status in (
            (None, 401),
            (pipeline.managed.worker_token, 403),
            (pipeline.world.other_user_token, 404),
        ):
            headers = {"Authorization": "Bearer " + token} if token else {}
            assert (
                pipeline.world.client.request(method, route, headers=headers).status_code == status
            )
    assert dump(pipeline.world.database) == before
    evidence(
        "all new Candidate routes and explicit input grants reject wrong authority without writes",
        {"candidate_routes": len(targets), "input_denials": 7, "database_unchanged": True},
        candidate_id=candidate,
    )
