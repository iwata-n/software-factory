"""Trusted managed stages. Transport credentials stay outside the runner."""

import base64
from threading import Event, Lock, Thread

import httpx

from .adapters import AgentResult, Capabilities, ProducedArtifact
from .sandbox import DockerRunner, RunnerError


class PipelineAdapter:
    capabilities = Capabilities(kinds=("ASSEMBLE_CANDIDATE",), adapters=("fake", "codex_cli"))

    def __init__(self, runner: DockerRunner):
        self.runner = runner
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
            if job["kind"] != "ASSEMBLE_CANDIDATE":
                raise RunnerError("MANAGED_STAGE_UNSUPPORTED", "POLICY")
            patch_id = job["input"]["patch_artifact_id"]
            patch = self.input_bytes(patch_id, transport)
            source = job.get("source")
            if not source or source["sha"] != job["base_sha"]:
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
                result_value = AgentResult("CANCELLED", {})
            with self._lock:
                if generation == self._generation:
                    self._events.extend(events)
                    self._result = result_value

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
        if thread:
            thread.join(timeout=2)
        with self._lock:
            if generation == self._generation:
                self._result = AgentResult("CANCELLED", {})
