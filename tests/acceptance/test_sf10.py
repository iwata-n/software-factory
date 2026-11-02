import base64
import hashlib
import io
import tarfile
import time
from dataclasses import replace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from software_factory.adapters import AgentResult, FakeAgent, ProducedArtifact
from software_factory.db import db_now
from software_factory.models import Enrollment, Job, Project, Repository
from software_factory.pipeline_worker import PipelineAdapter
from software_factory.service import issue_token, token_hash
from software_factory.storage import LocalArtifactStorage
from software_factory.worker import Supervisor
from tests.test_assembler import PATCH
from tests.test_assembler import runner as runner
from tests.test_assembler import source_bundle as source_bundle

from .helpers import worker_client


@pytest.fixture
def pipeline(world, source_bundle, runner, tmp_path):
    source = source_bundle
    service = world.client.app.state.service
    service.artifacts._storage = LocalArtifactStorage(tmp_path / "pipeline-artifacts")
    world.post(
        f"/v1/runs/{world.run_id}/cancel",
        {"expected_run_revision": world.run()["state_revision"], "reason": "candidate fixture"},
    )

    class Source:
        def resolve_details(self, reference):
            return source

        def fetch(self, remote, sha):
            assert sha == source["sha"]
            return source

    service.resolver = Source()
    enrollment = issue_token()
    with world.database.transaction() as s:
        project = s.get(Project, world.project_id)
        project.policy = project.policy | {
            "execution_image_digest": runner.image_digest,
            "protected_paths": [".github", "migrations", "checks"],
            "protected_manifest": {
                "checks/golden.py": hashlib.sha256(b"# immutable required check\n").hexdigest()
            }
            | source.get("protected_manifest", {}),
            "verification_commands": source.get("verification_commands", []),
        }
        s.get(Repository, world.repository_id).remote_url = "https://fixture.example/owner/repo.git"
        s.add(
            Enrollment(
                workspace_id=world.workspace_id,
                token_hash=token_hash(enrollment),
                capabilities=["ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW", "adapter:fake"],
                managed=True,
                expires_at=db_now(s) + 600,
            )
        )
    managed = world.client.post(
        "/v1/worker/enroll", json={"token": enrollment, "name": "managed assembler"}
    ).json()
    world.run_id = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}).json()[
        "run_id"
    ]

    class Pipeline:
        def __init__(self):
            self.world = world
            self.source = source
            self.runner = runner
            self.managed = replace(
                world, worker_token=managed["worker_token"], worker_id=managed["worker_id"]
            )
            self.client, self.trace = worker_client(self.managed)

        def prepare(self, patch=PATCH):
            client, _ = worker_client(world)
            assert Supervisor(client, FakeAgent()).run_once()["accepted"]

            class Implementer(FakeAgent):
                def start(self, job):
                    self._result = AgentResult(
                        "EXECUTED",
                        {"summary": "fixture change"},
                        artifacts=(ProducedArtifact("patch", "PATCH", patch),),
                    )

            assert Supervisor(client, Implementer()).run_once()["accepted"]
            assert world.run()["phase"] == "ASSEMBLE_CANDIDATE"

        def assemble(self, lose_response=False):
            self.client, self.trace = worker_client(self.managed, lose_completion=lose_response)
            return Supervisor(
                self.client, PipelineAdapter(runner), heartbeat_interval=0.1
            ).run_once()

        def report(self):
            assigned = self.managed.claim(token=self.managed.worker_token).json()
            adapter = PipelineAdapter(runner)
            path = f"/v1/worker/leases/{assigned['lease']['id']}"
            adapter.bind_transport(self.client, path, assigned["lease"]["fence"])
            adapter.start(assigned["job"] | {"source": source})
            deadline = time.monotonic() + 15
            while adapter.result() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            result = adapter.result()
            assert result and result.category is None, result
            output = result.artifacts[0]
            key = "report-source:" + assigned["job"]["attempt_id"]
            reserved = self.client.post(
                path + "/artifacts",
                json={
                    "fence": assigned["lease"]["fence"],
                    "kind": "SOURCE",
                    "size_bytes": len(output.body),
                    "sha256": hashlib.sha256(output.body).hexdigest(),
                },
                headers={"Idempotency-Key": key + ":reserve"},
            )
            assert reserved.status_code == 201, reserved.text
            aid = reserved.json()["id"]
            uploaded = self.client.put(
                path + f"/artifacts/{aid}/content",
                params={"fence": assigned["lease"]["fence"]},
                content=output.body,
                headers={"Idempotency-Key": key + ":upload"},
            )
            assert uploaded.status_code == 200, uploaded.text
            finalized = self.client.post(
                path + f"/artifacts/{aid}/finalize",
                json={"fence": assigned["lease"]["fence"]},
                headers={"Idempotency-Key": key + ":finalize"},
            )
            assert finalized.status_code == 200, finalized.text
            return path, {
                "fence": assigned["lease"]["fence"],
                "outcome": "EXECUTED",
                "artifact_ids": [aid],
                "result": result.result | {"source_artifact_id": aid},
            }

    return Pipeline()


@pytest.mark.ac("SF-10-AC01", "SF-10-AC07")
def test_real_pipeline_pins_candidate_and_independent_jobs(pipeline, evidence):
    pipeline.prepare()
    result = pipeline.assemble()
    assert result["accepted"]
    run = pipeline.world.run()
    assert (run["status"], run["phase"]) == ("QUEUED", "VERIFY_REVIEW")
    item = pipeline.world.get(f"/v1/candidates/{run['current_candidate_id']}").json()
    snapshot = run["snapshot"]
    assert item["base_sha"] == pipeline.source["sha"]
    assert item["spec_hash"] == snapshot["task_spec_hash"]
    assert item["profile_hash"] == snapshot["verification_profile_hash"]
    assert (
        item["image_digest"] == snapshot["execution_image_digest"] == pipeline.runner.image_digest
    )
    commit = base64.b64decode(item["metadata_body"]["commit_object"])
    assert (
        hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
        == item["head_sha"]
    )
    with pipeline.world.database.transaction() as s:
        jobs = list(
            s.scalars(
                select(Job).where(Job.run_id == run["run_id"], Job.kind.in_(["VERIFY", "REVIEW"]))
            )
        )
        assert len(jobs) == 2 and all(
            j.payload["candidate_id"] == item["candidate_id"] for j in jobs
        )
    raw = pipeline.world.get(f"/v1/artifacts/{item['source_artifact_id']}/content").content
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as bundle:
        assert bundle.extractfile("src/example.py").read() == b"value = 2\n"
        fingerprint = hashlib.sha256()
        for member in sorted(bundle.getmembers(), key=lambda m: m.name):
            body = bundle.extractfile(member).read()
            mode = "100755" if member.mode & 0o111 else "100644"
            fingerprint.update(
                member.name.encode() + b"\0" + mode.encode() + b"\0" + hashlib.sha256(body).digest()
            )
        assert fingerprint.hexdigest() == item["source_digest"] == item["code_digest"]
    assert item["metadata_body"]["isolation"]["network"] == "none"
    evidence(
        "real managed assembler fixes Candidate tuple and creates two independent inputs",
        item,
        run_id=run["run_id"],
        candidate_id=item["candidate_id"],
        attempt_id=item["producer_attempt_id"],
    )


@pytest.mark.ac("SF-10-AC02", "C-03", "C-04")
def test_lost_accepted_report_and_immutable_candidate(pipeline, evidence):
    pipeline.prepare()
    pipeline.assemble(lose_response=True)
    assert pipeline.trace["lost"]
    reports = pipeline.trace["completion_requests"]
    assert len(reports) == 2 and reports[0] == reports[1]
    candidate_id = pipeline.world.run()["current_candidate_id"]
    before = pipeline.world.get(f"/v1/candidates/{candidate_id}").json()
    for method in ("PUT", "PATCH", "DELETE"):
        reply = pipeline.world.client.request(
            method,
            f"/v1/candidates/{candidate_id}",
            json={"head_sha": "0" * 40},
            headers={"Authorization": "Bearer " + pipeline.world.user_token},
        )
        assert reply.status_code == 409
    with pytest.raises(IntegrityError), pipeline.world.database.transaction() as s:
        s.execute(
            text("UPDATE candidates SET head_sha=:sha WHERE id=:id"),
            {"sha": "0" * 40, "id": candidate_id},
        )
    assert pipeline.world.get(f"/v1/candidates/{candidate_id}").json() == before
    with (
        pytest.raises(IntegrityError, match="CANDIDATE_IMMUTABLE"),
        pipeline.world.database.transaction() as s,
    ):
        s.execute(text("DELETE FROM candidates WHERE id=:id"), {"id": candidate_id})
    evidence(
        "lost response exact replay creates one immutable Candidate; API and SQL reject mutation",
        before,
        candidate_id=candidate_id,
    )
