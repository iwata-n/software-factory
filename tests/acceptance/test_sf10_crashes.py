import hashlib
import json
import os
import select as select_module
import socket
import subprocess
import sys
import time

import httpx
import pytest
from sqlalchemy import select

from scripts.acceptance_support import ROOT
from software_factory.db import db_now
from software_factory.models import Attempt, Candidate, Job, Worker
from tests.test_assembler import runner as runner
from tests.test_assembler import source_bundle as source_bundle

from .test_sf10 import pipeline as pipeline


@pytest.mark.ac("SF-10-AC06")
@pytest.mark.parametrize("stage", ["patch_reading", "patch_applied", "before_commit", "reported"])
def test_real_worker_kill_and_retry_never_publishes_partial_tree(
    pipeline, tmp_path, stage, evidence
):
    pipeline.prepare()
    source_file = tmp_path / "fixed-source.json"
    source_file.write_text(json.dumps(pipeline.source))
    state = tmp_path / "runner-state"
    namespace = hashlib.sha256(str(state.absolute()).encode()).hexdigest()[:24]
    with pipeline.world.database.transaction() as s:
        s.get(Worker, pipeline.managed.worker_id).capabilities = [
            "ASSEMBLE_CANDIDATE",
            "adapter:fake",
        ]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server_env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
    server_env.update(
        FACTORY_DATABASE_URL=pipeline.world.database.engine.url.render_as_string(
            hide_password=False
        ),
        FACTORY_TEST_SOURCE_FILE=str(source_file),
        FACTORY_ARTIFACT_ROOT=str(pipeline.world.client.app.state.service.artifacts.storage.root),
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.acceptance.pipeline_probe:app_factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        env=server_env,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    processes = []

    def docker(*args):
        return subprocess.check_output(
            ["docker", *args], text=True, stderr=subprocess.STDOUT
        ).strip()

    def launch(selected_stage):
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {"PATH", "HOME", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
        }
        env.update(
            FACTORY_API_URL=base,
            FACTORY_WORKER_TOKEN=pipeline.managed.worker_token,
            FACTORY_RUNNER_IMAGE_DIGEST=pipeline.runner.image_digest,
            FACTORY_RUNNER_STATE_ROOT=str(state),
            FACTORY_TEST_STAGE=selected_stage,
        )
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.acceptance.pipeline_probe"],
            env=env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes.append(child)
        return child

    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/healthz", timeout=0.1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("fixture control plane did not start")
        child = launch(stage)
        observed = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not observed:
            if child.poll() is not None:
                raise AssertionError(
                    "probe exited before checkpoint: " + child.stderr.read().decode()[-1500:]
                )
            if stage in {"patch_reading", "reported"}:
                ready, _, _ = select_module.select([child.stdout], [], [], 0.1)
                if ready:
                    observed = child.stdout.readline().decode().strip() == "CHECKPOINT:" + stage
            else:
                containers = docker(
                    "ps", "-aq", "--filter", "label=factory.namespace=" + namespace
                ).split()
                observed = any(
                    "FACTORY_STAGE:" + stage in docker("logs", cid) for cid in containers
                )
                time.sleep(0.05)
        assert observed, "requested crash boundary not reached"
        child.kill()
        child.communicate(timeout=10)
        with pipeline.world.database.transaction() as s:
            initial = list(
                s.scalars(select(Candidate).where(Candidate.run_id == pipeline.world.run_id))
            )
            assert len(initial) == (1 if stage == "reported" else 0)
            attempt = s.scalar(
                select(Attempt)
                .join(Job, Attempt.job_id == Job.id)
                .where(Job.run_id == pipeline.world.run_id, Job.kind == "ASSEMBLE_CANDIDATE")
            )
            if stage != "reported":
                attempt.expires_at = db_now(s) - 1
        if stage != "reported":
            pipeline.world.client.app.state.service.reconcile()
            time.sleep(2.1)
        retry = launch("")
        stdout, stderr = retry.communicate(timeout=25)
        assert retry.returncode == 0, stderr.decode()[-1500:]
        with pipeline.world.database.transaction() as s:
            candidates = list(
                s.scalars(select(Candidate).where(Candidate.run_id == pipeline.world.run_id))
            )
            assert len(candidates) == 1
            jobs = list(s.scalars(select(Job).where(Job.run_id == pipeline.world.run_id)))
            assert (
                sum(j.kind == "VERIFY" for j in jobs) == sum(j.kind == "REVIEW" for j in jobs) == 1
            )
            attempts = list(
                s.scalars(
                    select(Attempt)
                    .join(Job, Attempt.job_id == Job.id)
                    .where(Job.run_id == pipeline.world.run_id, Job.kind == "ASSEMBLE_CANDIDATE")
                )
            )
            assert len(attempts) == (1 if stage == "reported" else 2)
            assert len({a.input_digest for a in attempts}) == 1
        assert not docker("ps", "-aq", "--filter", "label=factory.namespace=" + namespace)
        evidence(
            "actual Worker SIGKILL; orphan cleanup and fenced retry yield one Candidate",
            {
                "stage": stage,
                "attempts": len(attempts),
                "candidates": 1,
                "verify_jobs": 1,
                "review_jobs": 1,
                "orphan_containers": 0,
            },
            candidate_id=candidates[0].id,
            run_id=pipeline.world.run_id,
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        for cid in docker("ps", "-aq", "--filter", "label=factory.namespace=" + namespace).split():
            docker("rm", "--force", cid)
        server.terminate()
        server.communicate(timeout=10)
