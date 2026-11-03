"""Outbound HTTP-only worker. No DB module, user token, git token, or Agent secrets."""

import argparse
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import Future
from threading import Thread
from urllib.parse import urlparse

import httpx

from .adapters import AgentAdapter, AgentResult, FakeAgent


def validate_api_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API URL must not contain credentials, a query, or a fragment")
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("API URL must use HTTP(S)")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("HTTP is permitted only on loopback; use HTTPS for remote Control Planes")


class Supervisor:
    def __init__(
        self, client: httpx.Client, adapter: AgentAdapter, *, heartbeat_interval: float = 15
    ):
        self.client = client
        self.adapter = adapter
        self.heartbeat_interval = heartbeat_interval
        self._pending_usage = {}

    def _queue_usage(self, attempt_id, result):
        if result is None:
            return
        for index, delivery in enumerate(result.usage_deliveries):
            self._pending_usage[(attempt_id, index)] = delivery

    def _flush_usage(self, deadline=None):
        for (attempt_id, index), delivery in list(self._pending_usage.items()):
            for retry in range(3):
                remaining = 10 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("confirmed lease deadline reached before usage submission")
                try:
                    reply = self.client.post(
                        f"/v1/worker/attempts/{attempt_id}/usage",
                        json=delivery,
                        headers={"Idempotency-Key": f"usage:{attempt_id}:{index}"},
                        timeout=min(10, remaining),
                    )
                    reply.raise_for_status()
                except (httpx.TransportError, httpx.HTTPStatusError) as error:
                    transient = not isinstance(error, httpx.HTTPStatusError) or (
                        error.response.status_code == 429 or error.response.status_code >= 500
                    )
                    if not transient or retry == 2:
                        raise
                    time.sleep(0.2 * (retry + 1))
                else:
                    del self._pending_usage[(attempt_id, index)]
                    break

    def run_once(self, *, wait_seconds: int = 0) -> dict | None:
        # Late financial receipts have their own authority and do not renew execution rights.
        self._flush_usage()
        response = self.client.post(
            "/v1/worker/claims",
            json={"protocol_version": 1, "wait_seconds": wait_seconds},
            timeout=wait_seconds + 10,
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        assignment = response.json()
        if assignment.get("protocol_version", 1) != 1:
            raise RuntimeError("Worker received an unsupported protocol")
        job, lease = assignment["job"], assignment["lease"]
        if (
            job["snapshot"]["agent_adapter"] not in self.adapter.capabilities.adapters
            or job["kind"] not in self.adapter.capabilities.kinds
        ):
            raise RuntimeError("Worker received an unsupported adapter or job kind")
        lease_path = f"/v1/worker/leases/{lease['id']}"
        if hasattr(self.adapter, "bind_transport"):
            self.adapter.bind_transport(self.client, lease_path, lease["fence"])
        # Confirm the lease before starting. Round-trip time consumes the returned allowance.
        deadline = 0.0

        def heartbeat() -> tuple[float, bool]:
            sent_at = time.monotonic()
            remaining = max(0.1, deadline - sent_at) if deadline else 10
            reply = self.client.post(
                lease_path + "/heartbeat",
                json={"fence": lease["fence"]},
                timeout=min(10, remaining),
            )
            reply.raise_for_status()
            payload = reply.json()
            allowance = payload["lease_expires_in_seconds"]
            # A proportionate margin also works with short test/job deadlines.
            confirmed_until = sent_at + max(0, allowance - min(10, allowance / 4))
            return confirmed_until, payload["cancel_requested"]

        def background_heartbeat() -> Future:
            future = Future()

            def invoke():
                try:
                    future.set_result(heartbeat())
                except BaseException as error:
                    future.set_exception(error)

            # A stalled HTTP request must never block the supervisor's stop deadline.
            Thread(target=invoke, daemon=True, name="factory-heartbeat").start()
            return future

        started = False
        usage_queued = False
        try:
            deadline, cancelled = heartbeat()
            preparation = None
            preparation_error = None
            if not cancelled and time.monotonic() < deadline:
                if (
                    job["snapshot"].get("source")
                    and job["kind"] in {"PLAN", "IMPLEMENT", "ASSEMBLE_CANDIDATE"}
                    and not job.get("input", {}).get("source_candidate_id")
                ):
                    preparation = Future()

                    def fetch_source():
                        try:
                            reply = self.client.get(
                                lease_path + "/source",
                                params={"fence": lease["fence"]},
                                timeout=max(0.1, min(60, deadline - time.monotonic())),
                            )
                            reply.raise_for_status()
                            preparation.set_result(reply.json())
                        except BaseException as error:
                            preparation.set_exception(error)

                    Thread(target=fetch_source, daemon=True, name="factory-source-fetch").start()
                else:
                    started = True
                    self.adapter.start(job)
            else:
                self.adapter.cancel()
            next_heartbeat = time.monotonic() + self.heartbeat_interval
            pending_heartbeat = None
            buffered_events = []
            while preparation_error is None and (
                (preparation is not None and not cancelled) or self.adapter.result() is None
            ):
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError("confirmed lease deadline reached; Agent cancelled")
                if pending_heartbeat is not None and pending_heartbeat.done():
                    deadline, cancelled = pending_heartbeat.result()
                    pending_heartbeat = None
                    if cancelled:
                        self.adapter.cancel()
                    next_heartbeat = time.monotonic() + self.heartbeat_interval
                if (
                    preparation is not None
                    and preparation.done()
                    and not cancelled
                    and time.monotonic() < deadline
                ):
                    try:
                        source = preparation.result()
                        if not isinstance(source, dict) or source.get("sha") != job["base_sha"]:
                            preparation_error = AgentResult(
                                "FAILED", {}, category="POLICY", reason="SOURCE_SHA_MISMATCH"
                            )
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code in {401, 403}:
                            raise
                        category = (
                            "INFRASTRUCTURE" if error.response.status_code >= 500 else "POLICY"
                        )
                        preparation_error = AgentResult(
                            "FAILED", {}, category=category, reason="SOURCE_FETCH_REJECTED"
                        )
                    except (httpx.TransportError, ValueError):
                        preparation_error = AgentResult(
                            "FAILED",
                            {},
                            category="INFRASTRUCTURE",
                            reason="SOURCE_FETCH_UNAVAILABLE",
                        )
                    preparation = None
                    if preparation_error is None:
                        started = True
                        self.adapter.start(job | {"source": source})
                buffered_events.extend(self.adapter.events())
                if now >= next_heartbeat and pending_heartbeat is None:
                    pending_heartbeat = background_heartbeat()
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            result = preparation_error or self.adapter.result()
            if time.monotonic() >= deadline:
                raise TimeoutError("confirmed lease deadline reached before submission")
            buffered_events.extend(self.adapter.events())
            # Metering precedes output upload so a later failure cannot erase usage.
            self._queue_usage(job["attempt_id"], result)
            usage_queued = True
            self._flush_usage(deadline)
            artifact_ids = []
            references = {}
            for artifact in result.artifacts:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "confirmed lease deadline reached before artifact submission"
                    )
                if "$artifact/" + artifact.key in references:
                    raise ValueError("duplicate artifact output key")
                key = f"artifact:{job['attempt_id']}:{artifact.key}"
                reserved = self.client.post(
                    lease_path + "/artifacts",
                    json={
                        "fence": lease["fence"],
                        "kind": artifact.kind,
                        "size_bytes": len(artifact.body),
                        "sha256": hashlib.sha256(artifact.body).hexdigest(),
                    },
                    headers={"Idempotency-Key": key + ":reserve"},
                    timeout=10,
                )
                reserved.raise_for_status()
                aid = reserved.json()["id"]
                uploaded = self.client.put(
                    lease_path + f"/artifacts/{aid}/content",
                    params={"fence": lease["fence"]},
                    content=artifact.body,
                    headers={"Idempotency-Key": key + ":upload"},
                    timeout=10,
                )
                uploaded.raise_for_status()
                finalized = self.client.post(
                    lease_path + f"/artifacts/{aid}/finalize",
                    json={"fence": lease["fence"]},
                    headers={"Idempotency-Key": key + ":finalize"},
                    timeout=10,
                )
                finalized.raise_for_status()
                artifact_ids.append(aid)
                references["$artifact/" + artifact.key] = aid

            def resolve_references(value):
                if isinstance(value, str):
                    return references.get(value, value)
                if isinstance(value, list):
                    return [resolve_references(item) for item in value]
                if isinstance(value, dict):
                    return {key: resolve_references(item) for key, item in value.items()}
                return value

            for offset in range(0, len(buffered_events), 100):
                if time.monotonic() >= deadline:
                    raise TimeoutError("confirmed lease deadline reached before event submission")
                batch = [
                    {**entry, "sequence": offset + number + 1}
                    for number, entry in enumerate(buffered_events[offset : offset + 100])
                ]
                report = self.client.post(
                    lease_path + "/events",
                    json={"fence": lease["fence"], "events": batch},
                    timeout=min(10, max(0.1, deadline - time.monotonic())),
                )
                report.raise_for_status()
            if result.category:
                endpoint = "fail"
                body = {
                    "fence": lease["fence"],
                    "category": result.category,
                    "reason": result.reason,
                    "usage_microusd": result.usage_microusd,
                }
            else:
                endpoint = "complete"
                body = {
                    "fence": lease["fence"],
                    "outcome": result.outcome,
                    "artifact_ids": artifact_ids,
                    "result": resolve_references(result.result),
                    "usage_microusd": result.usage_microusd,
                }
            # Identical key and body across retries, including a lost completion response.
            for retry in range(3):
                try:
                    if retry == 0 and time.monotonic() >= deadline:
                        raise TimeoutError("confirmed lease deadline reached before completion")
                    reply = self.client.post(
                        lease_path + "/" + endpoint,
                        json=body,
                        headers={"Idempotency-Key": f"{endpoint}:{job['attempt_id']}"},
                        timeout=10,
                    )
                    if reply.status_code == 429 or reply.status_code >= 500:
                        reply.raise_for_status()
                    else:
                        reply.raise_for_status()
                        return reply.json()
                except (httpx.TransportError, httpx.HTTPStatusError) as error:
                    transient = not isinstance(error, httpx.HTTPStatusError) or (
                        error.response.status_code == 429 or error.response.status_code >= 500
                    )
                    if not transient or retry == 2:
                        raise
                    time.sleep(0.2 * (retry + 1))
        except BaseException:
            # Real adapters must make cancel() a bounded process-group stop (future sandbox task).
            if started:
                # Capture a completed result before cancel() can replace it.
                result = self.adapter.result()
                self.adapter.cancel()
                if not usage_queued:
                    self._queue_usage(job["attempt_id"], result or self.adapter.result())
            raise


def main():
    parser = argparse.ArgumentParser(description="Software Factory HTTP worker")
    parser.add_argument(
        "--api-url", default=os.environ.get("FACTORY_API_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--once", action="store_true", help="Execute at most one job")
    parser.add_argument(
        "--drain", action="store_true", help="Exit when no job is immediately ready"
    )
    parser.add_argument(
        "--scenario", choices=["success", "infra-fail", "inconclusive"], default="success"
    )
    parser.add_argument("--adapter", choices=["fake", "pipeline", "codex_cli"], default="fake")
    parser.add_argument("--reviewer", choices=["none", "codex_cli"], default="none")
    parser.add_argument(
        "--codex-manifest",
        default=os.environ.get("FACTORY_CODEX_MANIFEST", "config/codex_cli.json"),
    )
    parser.add_argument(
        "--codex-binary",
        default=os.environ.get("FACTORY_CODEX_BINARY")
        or shutil.which("codex")
        or (
            "/Applications/ChatGPT.app/Contents/Resources/"
            "codex-cli/CodexCLI.app/Contents/MacOS/codex"
        ),
    )
    args = parser.parse_args()
    validate_api_url(args.api_url)
    token = os.environ.get("FACTORY_WORKER_TOKEN")
    if not token:
        parser.error("FACTORY_WORKER_TOKEN is required")
    engine = None
    if args.adapter == "codex_cli" or args.reviewer == "codex_cli":
        from .codex_engine import CodexCLI

        engine = CodexCLI.from_file(args.codex_binary, args.codex_manifest)
    with httpx.Client(
        base_url=args.api_url, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        if args.adapter == "pipeline":
            from .pipeline_worker import PipelineAdapter
            from .sandbox import DockerRunner

            image = os.environ.get("FACTORY_RUNNER_IMAGE_DIGEST")
            if not image:
                parser.error("FACTORY_RUNNER_IMAGE_DIGEST is required for managed stages")
            reviewer = None
            if args.reviewer == "codex_cli":
                from .codex_adapter import CodexReviewer

                reviewer = CodexReviewer(engine)
            adapter = PipelineAdapter(DockerRunner(image), reviewer=reviewer)
        elif args.adapter == "codex_cli":
            from .codex_adapter import CodexAdapter

            adapter = CodexAdapter(engine)
        else:
            adapter = FakeAgent(args.scenario)
        supervisor = Supervisor(client, adapter)
        while True:
            try:
                result = supervisor.run_once(wait_seconds=0 if args.once or args.drain else 20)
                if result:
                    print(json.dumps(result, ensure_ascii=False))
                if args.once or (args.drain and result is None):
                    return
            except (httpx.HTTPError, TimeoutError) as error:
                # Never log tokens, request bodies, or arbitrary Agent output here.
                print(json.dumps({"worker_error": type(error).__name__}))
                permanent = isinstance(error, httpx.HTTPStatusError) and (
                    error.response.status_code < 500 and error.response.status_code != 429
                )
                if args.once or args.drain or permanent:
                    raise SystemExit(1) from None
                time.sleep(1)


if __name__ == "__main__":
    main()
