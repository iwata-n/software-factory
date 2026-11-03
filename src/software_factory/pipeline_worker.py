"""Trusted managed stages. Transport credentials stay outside the runner."""

import base64
import json
import time
from threading import Event, Lock, Thread

import httpx

from .adapters import AgentExecutionError, AgentResult, Capabilities, ProducedArtifact
from .sandbox import DockerRunner, RunnerError


class PipelineAdapter:
    capabilities = Capabilities(
        kinds=("ASSEMBLE_CANDIDATE", "VERIFY", "REVIEW"), adapters=("fake", "codex_cli")
    )

    def __init__(self, runner: DockerRunner, *, reviewer=None):
        self.runner = runner
        self.reviewer = reviewer
        self._result = None
        self._events = []
        self._cancel = Event()
        self._thread = None
        self._lock = Lock()
        self._generation = 0

    def bind_transport(self, client, lease_path, fence):
        self.client, self.lease_path, self.fence = client, lease_path, fence

    def input_bytes(self, artifact_id, transport):
        client, lease_path, fence = transport
        result = client.get(
            lease_path + f"/inputs/{artifact_id}/content",
            params={"fence": fence},
            timeout=10,
        )
        result.raise_for_status()
        return result.content

    def start(self, job):
        with self._lock:
            self._result = None
            self._events = []
            self._cancel = Event()
            self._generation += 1
            arguments = (
                job,
                self._cancel,
                self._generation,
                (self.client, self.lease_path, self.fence),
            )
            self._thread = Thread(
                target=self.execute, args=arguments, daemon=True, name="factory-managed-stage"
            )
        self._thread.start()

    def execute(self, job, cancelled, generation, transport):
        events = []
        result_value = AgentResult(
            "FAILED", {}, category="INFRASTRUCTURE", reason="MANAGED_STAGE_CRASHED"
        )
        try:
            snapshot = job["snapshot"]
            if self.runner.image_digest != snapshot["execution_image_digest"]:
                raise RunnerError("IMAGE_DIGEST_MISMATCH")
            if job["kind"] in {"VERIFY", "REVIEW"}:
                result_value, events = self.execute_evidence(job, cancelled, transport)
            elif job["kind"] == "ASSEMBLE_CANDIDATE":
                patch_id = job["input"]["patch_artifact_id"]
                patch = self.input_bytes(patch_id, transport)
                source = job.get("source")
                parent_sha = job["base_sha"]
                if job["input"].get("source_candidate_id"):
                    parent = job["input"]["candidate"]
                    source = {
                        "sha": parent["head_sha"],
                        "tree": parent["tree_sha"],
                        "source_digest": parent["source_digest"],
                        "commit_object": parent["metadata_body"]["commit_object"],
                        "archive": base64.b64encode(
                            self.input_bytes(parent["source_artifact_id"], transport)
                        ).decode(),
                    }
                    parent_sha = job["input"]["parent_sha"]
                if not source or source["sha"] != parent_sha:
                    raise RunnerError("FIXED_SOURCE_REQUIRED", "POLICY")
                result = self.runner.execute(
                    {
                        "operation": "assemble",
                        "source": source,
                        "patch": base64.b64encode(patch).decode(),
                        "allowed_write_paths": snapshot["policy"]["allowed_write_paths"],
                        "protected_paths": snapshot["policy"]["protected_paths"],
                        "protected_manifest": snapshot["policy"].get("protected_manifest", {}),
                    },
                    timeout=job["execution"]["timeout_seconds"],
                    cancelled=cancelled,
                )
                body = result.body
                report = {
                    "base_sha": job["base_sha"],
                    "parent_sha": body["base_sha"],
                    "head_sha": body["head_sha"],
                    "tree_sha": body["tree"],
                    "source_digest": body["source_digest"],
                    "spec_hash": snapshot["task_spec_hash"],
                    "profile_hash": snapshot["verification_profile_hash"],
                    "image_digest": result.image_digest,
                    "source_artifact_id": "$artifact/source",
                    "patch_artifact_id": patch_id,
                    "file_manifest": body["file_manifest"],
                    "changed_paths": body["changed_paths"],
                    "commit_object": body["commit_object"],
                    "isolation": result.isolation,
                }
                output = ProducedArtifact(
                    "source", "SOURCE", base64.b64decode(body["archive"], validate=True)
                )
                events.append(
                    {
                        "kind": "PROGRESS",
                        "body": {"stage": job["kind"], "container_id": result.container_id},
                    }
                )
                result_value = AgentResult("EXECUTED", report, artifacts=(output,))
            else:
                raise RunnerError("MANAGED_STAGE_UNSUPPORTED", "POLICY")
        except AgentExecutionError as error:
            result_value = AgentResult(
                "FAILED",
                {},
                category=error.category,
                reason=error.reason,
                usage_microusd=None,
                usage_deliveries=error.usage_deliveries,
            )
        except RunnerError as error:
            result_value = AgentResult("FAILED", {}, category=error.category, reason=error.code)
        except httpx.HTTPStatusError as error:
            result_value = AgentResult(
                "FAILED",
                {},
                category="INFRASTRUCTURE" if error.response.status_code >= 500 else "POLICY",
                reason="INPUT_FETCH_REJECTED",
            )
        except (httpx.TransportError, ValueError, KeyError):
            result_value = AgentResult(
                "FAILED", {}, category="INFRASTRUCTURE", reason="MANAGED_INPUT_UNAVAILABLE"
            )
        except Exception:
            result_value = AgentResult(
                "FAILED", {}, category="INFRASTRUCTURE", reason="MANAGED_STAGE_CRASHED"
            )
        finally:
            if cancelled.is_set():
                result_value = AgentResult(
                    "CANCELLED",
                    {},
                    usage_microusd=result_value.usage_microusd,
                    usage_deliveries=result_value.usage_deliveries,
                )
            with self._lock:
                if generation == self._generation:
                    self._events.extend(events)
                    self._result = result_value

    def execute_evidence(self, job, cancelled, transport):
        candidate = job["input"]["candidate"]
        profile = job["snapshot"]["verification_profile"]
        archive = self.input_bytes(job["input"]["source_artifact_id"], transport)
        target = {
            key: candidate[key]
            for key in (
                "candidate_id",
                "head_sha",
                "source_digest",
                "spec_hash",
                "profile_hash",
                "image_digest",
            )
        }
        started = time.time()
        try:
            outcome = self.runner.execute_candidate(
                {
                    "operation": "verify" if job["kind"] == "VERIFY" else "inspect_candidate",
                    "candidate": candidate,
                    "profile": profile,
                },
                archive,
                timeout=job["execution"]["timeout_seconds"],
                cancelled=cancelled,
            )
            observations, isolation = outcome.body["checks"], outcome.isolation
            events = [
                {
                    "kind": "PROGRESS",
                    "body": {"stage": job["kind"], "container_id": outcome.container_id},
                }
            ]
        except RunnerError as error:
            if job["kind"] != "VERIFY" or error.category == "POLICY" or cancelled.is_set():
                raise
            observations = [
                {
                    "check_id": command["check_id"],
                    "execution_state": "START_FAILED",
                    "started_at": started,
                    "finished_at": time.time(),
                    "exit_code": None,
                    "log": base64.b64encode(error.code.encode()).decode(),
                    "junit": None,
                }
                for command in profile.get("commands", [])
            ]
            isolation, events = {"start_failed": True}, []
        report = {
            "target": target,
            "isolation": isolation,
            "checks": [],
            "criteria": [],
            "findings": [],
        }
        outputs = []
        usage_deliveries = ()
        if job["kind"] == "VERIFY":
            for index, observation in enumerate(observations):
                item = {
                    key: observation[key]
                    for key in (
                        "check_id",
                        "execution_state",
                        "exit_code",
                        "started_at",
                        "finished_at",
                    )
                }
                log_key = f"check-{index}-log"
                log = (
                    base64.b64decode(observation["log"], validate=True)
                    .decode("utf-8", errors="replace")
                    .encode()
                )
                outputs.append(ProducedArtifact(log_key, "LOG", log))
                item["log_artifact_id"] = "$artifact/" + log_key
                if observation["junit"] is not None:
                    key = f"check-{index}-junit"
                    outputs.append(
                        ProducedArtifact(
                            key, "JUNIT", base64.b64decode(observation["junit"], validate=True)
                        )
                    )
                    item["junit_artifact_id"] = "$artifact/" + key
                report["checks"].append(item)
        else:
            # The backend receives immutable bytes and a data context, never the HTTP client/token.
            reviewer = self.reviewer
            if reviewer and hasattr(reviewer, "for_job"):
                reviewer = reviewer.for_job(job)
            reviewed = (
                reviewer.review(
                    {
                        "target": target,
                        "task_spec": job["snapshot"]["task_spec"],
                        "profile": profile,
                        "source_artifact_id": job["input"]["source_artifact_id"],
                        "source_archive": archive,
                    },
                    cancelled,
                )
                if reviewer
                else {}
            )
            if not isinstance(reviewed, dict) or set(reviewed) - {"criteria", "findings"}:
                raise RunnerError("REVIEW_SCHEMA_INVALID", "POLICY")
            report["criteria"] = reviewed.get("criteria", [])
            report["findings"] = reviewed.get("findings", [])
            usage_deliveries = getattr(reviewed, "usage_deliveries", ())
            outputs.append(
                ProducedArtifact(
                    "review-log",
                    "LOG",
                    json.dumps(
                        {
                            "backend_configured": self.reviewer is not None,
                            "criteria_reported": len(report["criteria"]),
                            "engine": getattr(reviewed, "audit", {}),
                        }
                    ).encode(),
                )
            )
        return AgentResult(
            "EXECUTED",
            report,
            artifacts=tuple(outputs),
            usage_microusd=None if usage_deliveries else 0,
            usage_deliveries=usage_deliveries,
        ), events

    def events(self):
        with self._lock:
            events, self._events = self._events, []
            return events

    def result(self):
        with self._lock:
            return self._result

    def cancel(self):
        with self._lock:
            cancelled, generation, thread = self._cancel, self._generation, self._thread
        cancelled.set()
        self.runner.cancel()
        if self.reviewer and hasattr(self.reviewer, "cancel"):
            self.reviewer.cancel()
        if thread:
            thread.join(timeout=2)
        with self._lock:
            if generation == self._generation:
                result = self._result
                self._result = AgentResult(
                    "CANCELLED",
                    {},
                    usage_microusd=result.usage_microusd if result else None,
                    usage_deliveries=result.usage_deliveries if result else (),
                )
