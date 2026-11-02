import sys
import time
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import func, select

from scripts.acceptance_support import source_manifest
from software_factory.adapters import AgentResult, Capabilities, FakeAgent
from software_factory.models import Attempt, IdempotencyRecord, Job, Run, WorkerEvent
from software_factory.worker import Supervisor, main, validate_api_url
from tests.test_worker import assignment

from .helpers import worker_client


@pytest.mark.ac("SF-06-AC01")
def test_worker_protocol_talks_only_to_scoped_http_endpoints(world, evidence):
    client, trace = worker_client(world)
    with client:
        result = Supervisor(client, FakeAgent()).run_once()
    assert result["accepted"] is True
    assert trace["paths"] == [
        "/v1/worker/claims",
        next(path for path in trace["paths"] if path.endswith("/heartbeat")),
        next(path for path in trace["paths"] if path.endswith("/complete")),
    ]
    evidence("claim/heartbeat/complete via CP HTTP only", trace["paths"])


@pytest.mark.ac("SF-06-AC02")
@pytest.mark.parametrize("scenario", ["success", "infra-fail", "inconclusive"])
def test_fake_scenarios_never_create_code_or_verified_success(world, clock, evidence, scenario):
    before = source_manifest()["code_digest"]
    client, trace = worker_client(world)
    with client:
        supervisor = Supervisor(client, FakeAgent(scenario))
        assert supervisor.run_once()["accepted"]
        if scenario == "success":
            assert supervisor.run_once()["accepted"]
    run = world.run()
    expected = {"success": "BLOCKED", "infra-fail": "QUEUED", "inconclusive": "BLOCKED"}[scenario]
    assert run["status"] == expected and run["status"] != "SUCCEEDED"
    if scenario == "success":
        assert run["stop_reason"] == "CANDIDATE_PIPELINE_NOT_IMPLEMENTED"
    if scenario == "infra-fail":
        with world.database.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == 1
            assert session.scalar(select(Job.state)) == "RETRY_WAIT"
    assert run["spent_microusd"] == 0 and source_manifest()["code_digest"] == before
    evidence(
        "fake-only scenario follows deterministic branch without edits or provider cost",
        {
            "scenario": scenario,
            "status": run["status"],
            "reason": run["stop_reason"],
            "paths": trace["paths"],
            "spent": 0,
        },
        run_id=world.run_id,
    )


@pytest.mark.ac("SF-06-AC03", "C-03", "C-04")
def test_real_completion_commit_then_response_loss_replays_once(world, evidence):
    client, trace = worker_client(world, lose_completion=True)
    with client:
        result = Supervisor(client, FakeAgent()).run_once()
    assert result["accepted"] and trace["lost"]
    requests = trace["completion_requests"]
    assert len(requests) == 2 and requests[0] == requests[1]
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.operation.like("complete:%"))
            )
            == 1
        )
        assert session.get(Run, world.run_id).reserved_microusd == 0
    evidence(
        "same key/body after lost accepted response; one completion and one next job",
        requests,
        run_id=world.run_id,
    )


@pytest.mark.ac("SF-06-AC04")
def test_cancellation_between_claim_and_start_prevents_agent_start(world, evidence):
    agent = FakeAgent()
    agent.start = Mock(side_effect=AssertionError("must not start"))

    def cancel(assignment):
        run = world.run()
        assert (
            world.post(
                f"/v1/runs/{world.run_id}/cancel",
                {"expected_run_revision": run["state_revision"], "reason": "before worker start"},
            ).status_code
            == 202
        )

    client, _ = worker_client(world, after_claim=cancel)
    with client:
        result = Supervisor(client, agent).run_once()
    agent.start.assert_not_called()
    assert result["run"]["status"] == "CANCELLED"
    evidence(
        "no start call; cancellation acknowledged", result["run"]["status"], run_id=world.run_id
    )


class HangingAgent:
    capabilities = Capabilities()

    def __init__(self):
        self.started_at = None
        self.cancelled_at = None

    def start(self, job):
        self.started_at = time.monotonic()

    def events(self):
        return []

    def result(self):
        return AgentResult("CANCELLED", {}) if self.cancelled_at is not None else None

    def cancel(self):
        self.cancelled_at = time.monotonic()


@pytest.mark.ac("SF-06-AC04", "SF-06-AC06")
@pytest.mark.parametrize("failure", ["disconnect", "delayed-response"])
def test_inflight_heartbeat_never_blocks_stop_deadline(evidence, failure):
    agent = HangingAgent()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("claims"):
            return httpx.Response(200, json=assignment())
        if request.url.path.endswith("heartbeat"):
            if calls.count(request.url.path) > 1:
                if failure == "disconnect":
                    raise httpx.ReadError("disconnected", request=request)
                time.sleep(1)  # Deliberately violate the transport timeout.
            return httpx.Response(
                200, json={"lease_expires_in_seconds": 0.4, "cancel_requested": False}
            )
        raise AssertionError("expired or disconnected work cannot be newly submitted")

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises((httpx.TransportError, TimeoutError)):
            Supervisor(client, agent, heartbeat_interval=0.02).run_once()
    assert agent.cancelled_at is not None and agent.cancelled_at - agent.started_at < 0.4
    assert not any(path.endswith("/complete") for path in calls)
    evidence(
        "cancel before 0.4-second confirmed lease; no completion",
        {"failure": failure, "stop_seconds": agent.cancelled_at - agent.started_at, "paths": calls},
    )


@pytest.mark.ac("SF-06-AC05")
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://127.0.0.1.evil",
        "https://user:pass@example.com",
        "https://example.com?token=canary",
        "file:///tmp/api",
    ],
)
def test_disallowed_api_urls_fail_before_network(evidence, url):
    with pytest.raises(ValueError):
        validate_api_url(url)
    evidence(
        "URL rejected before any claim/start", {"rejected": True, "url_kind": url.split(":")[0]}
    )


@pytest.mark.ac("SF-06-AC05")
@pytest.mark.parametrize("variant", ["protocol", "job", "adapter"])
def test_unsupported_assignment_does_not_start(evidence, variant):
    body = assignment()
    if variant == "protocol":
        body["protocol_version"] = 2
    elif variant == "job":
        body["job"]["kind"] = "PUBLISH"
    else:
        body["job"]["snapshot"]["agent_adapter"] = "unknown"
    agent = Mock(capabilities=Capabilities())
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    ) as client:
        with pytest.raises(RuntimeError):
            Supervisor(client, agent).run_once()
    agent.start.assert_not_called()
    evidence("unsupported assignment rejected; agent starts=0", variant)


@pytest.mark.ac("SF-06-AC05", "C-06")
@pytest.mark.parametrize("status", [401, 403])
def test_cli_permanent_auth_failure_exits_without_retry(monkeypatch, capsys, evidence, status):
    requests = []
    agent = Mock(capabilities=Capabilities())

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(status, json={"code": "DENIED"})

    client = httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler))
    monkeypatch.setenv("FACTORY_WORKER_TOKEN", "worker-token-canary-must-not-log")
    monkeypatch.setenv("FACTORY_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(sys, "argv", ["factory-worker"])
    monkeypatch.setattr("software_factory.worker.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("software_factory.worker.FakeAgent", lambda scenario: agent)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1 and len(requests) == 1
    assert "worker-token-canary" not in capsys.readouterr().out
    agent.start.assert_not_called()
    evidence(
        "one rejected request, exit 1, no agent start",
        {"status": status, "requests": len(requests), "exit": result.value.code},
    )


@pytest.mark.ac("SF-06-AC05")
def test_server_rejects_unknown_protocol_without_job_claim(world, evidence):
    response = world.post("/v1/worker/claims", {"protocol_version": 2}, token=world.worker_token)
    assert response.status_code == 422
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0
    evidence("422 before claim", response.json())


@pytest.mark.ac("SF-06-AC06")
def test_transient_completion_retries_and_adapter_events_without_resume(world, evidence):
    class TracedAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.trace = []
            self.sent = False

        def start(self, job):
            self.trace.append("start")
            super().start(job)

        def events(self):
            self.trace.append("events")
            if self.sent:
                return []
            self.sent = True
            return [{"kind": "LOG", "body": {"message": "synthetic progress"}}]

        def result(self):
            self.trace.append("result")
            return super().result()

        def resume(self, *args):
            raise AssertionError("resume is unsupported")

    agent = TracedAgent()
    client, trace = worker_client(world, completion_statuses=[429, 503])
    with client:
        assert Supervisor(client, agent).run_once()["accepted"]
    assert len(trace["completion_requests"]) == 3
    assert (
        trace["completion_requests"][0]
        == trace["completion_requests"][1]
        == trace["completion_requests"][2]
    )
    assert {"start", "events", "result"} <= set(
        agent.trace
    ) and not agent.capabilities.supports_resume
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(WorkerEvent)) == 1
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1
    evidence(
        "429/503 same input retries, adapter events persisted, resume not attempted",
        {"trace": agent.trace, "requests": trace["paths"]},
    )
