import json

import httpx
import pytest

from software_factory.adapters import AgentResult, Capabilities, FakeAgent
from software_factory.worker import Supervisor, validate_api_url


def assignment():
    return {
        "job": {
            "id": "job",
            "attempt_id": "attempt",
            "kind": "IMPLEMENT",
            "snapshot": {"agent_adapter": "fake"},
        },
        "lease": {"id": "lease", "fence": 1, "expires_in_seconds": 90},
    }


def test_worker_retries_lost_response_with_identical_key_and_body():
    reports = []

    def handler(request):
        if request.url.path.endswith("claims"):
            return httpx.Response(200, json=assignment())
        if request.url.path.endswith("heartbeat"):
            return httpx.Response(
                200,
                json={
                    "cancel_requested": False,
                    "lease_expires_in_seconds": 90,
                },
            )
        reports.append((request.headers["idempotency-key"], request.content))
        if len(reports) == 1:
            raise httpx.ReadError("response lost after acceptance", request=request)
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        assert Supervisor(client, FakeAgent()).run_once() == {"accepted": True}
    assert len(reports) == 2
    assert reports[0] == reports[1]


def test_measured_usage_is_replayed_identically_before_completion():
    calls, deliveries = [], []

    class MeteredAgent(FakeAgent):
        def start(self, job):
            self._result = AgentResult(
                "EXECUTED",
                {"summary": "metered fixture"},
                usage_microusd=None,
                usage_deliveries=(
                    {
                        "provider": "fixture",
                        "delivery_id": "delivery-one",
                        "sequence": 1,
                        "total_microusd": 47,
                        "final": True,
                        "currency": "USD",
                        "calls": [],
                    },
                ),
            )

    def receive(request):
        calls.append(request.url.path)
        if request.url.path.endswith("claims"):
            return httpx.Response(200, json=assignment())
        if request.url.path.endswith("heartbeat"):
            return httpx.Response(
                200, json={"cancel_requested": False, "lease_expires_in_seconds": 90}
            )
        if request.url.path.endswith("usage"):
            deliveries.append((request.headers["idempotency-key"], request.content))
            if len(deliveries) == 1:
                raise httpx.ReadError("lost accepted response", request=request)
            return httpx.Response(200, json={"accepted": True})
        assert request.url.path.endswith("complete")
        assert json.loads(request.content)["usage_microusd"] is None
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(receive)
    ) as client:
        assert Supervisor(client, MeteredAgent()).run_once()["accepted"]
    assert deliveries[0] == deliveries[1]
    assert calls.index("/v1/worker/attempts/attempt/usage") < calls.index(
        "/v1/worker/leases/lease/complete"
    )


def test_cancel_requested_before_start_does_not_start_agent():
    class RecordingAgent(FakeAgent):
        def start(self, job):
            raise AssertionError("cancelled assignment must never start")

    def handler(request):
        if request.url.path.endswith("claims"):
            return httpx.Response(200, json=assignment())
        if request.url.path.endswith("heartbeat"):
            return httpx.Response(
                200,
                json={
                    "cancel_requested": True,
                    "lease_expires_in_seconds": 90,
                },
            )
        assert json.loads(request.content)["outcome"] == "CANCELLED"
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        assert Supervisor(client, RecordingAgent()).run_once()["accepted"]


def test_agent_is_cancelled_when_lease_expires(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("software_factory.worker.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "software_factory.worker.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    class HangingAgent:
        capabilities = Capabilities()
        cancelled = False

        def start(self, job):
            pass

        def events(self):
            return []

        def result(self):
            return AgentResult("CANCELLED", {}) if self.cancelled else None

        def cancel(self):
            self.cancelled = True

    agent = HangingAgent()

    def handler(request):
        if request.url.path.endswith("claims"):
            return httpx.Response(200, json=assignment())
        if request.url.path.endswith("heartbeat"):
            return httpx.Response(
                200,
                json={
                    "cancel_requested": False,
                    "lease_expires_in_seconds": 1,
                },
            )
        raise AssertionError("expired work must never be submitted")

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(TimeoutError):
            Supervisor(client, agent).run_once()
    assert agent.cancelled
    assert clock[0] < 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///tmp/api",
        "https://user:password@example.com",
        "https://example.com?token=secret",
    ],
)
def test_worker_refuses_remote_plaintext_and_credentials_in_url(url):
    with pytest.raises(ValueError):
        validate_api_url(url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000", "http://[::1]:8000", "https://example.com"]
)
def test_worker_accepts_loopback_http_and_remote_https(url):
    validate_api_url(url)


def test_disconnect_replays_receipt_before_claim_without_submitting_expired_output():
    class MeteredAgent(FakeAgent):
        def start(self, job):
            self._result = AgentResult(
                "EXECUTED", {}, usage_deliveries=({"delivery_id": "metered-one"},)
            )

    deliveries = []

    def receive(request):
        path = request.url.path
        if path.endswith("claims"):
            if len(deliveries) == 4:
                return httpx.Response(204)
            assert not deliveries
            return httpx.Response(200, json=assignment())
        if path.endswith("heartbeat"):
            return httpx.Response(
                200, json={"cancel_requested": False, "lease_expires_in_seconds": 90}
            )
        assert path.endswith("usage")
        deliveries.append((request.headers["idempotency-key"], request.content))
        if len(deliveries) <= 3:
            raise httpx.ConnectError("control plane unavailable", request=request)
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(receive)
    ) as client:
        supervisor = Supervisor(client, MeteredAgent())
        with pytest.raises(httpx.ConnectError):
            supervisor.run_once()
        assert supervisor.run_once() is None

    assert len(deliveries) == 4
    assert all(delivery == deliveries[0] for delivery in deliveries)


def test_failed_heartbeat_cancels_running_agent_before_replaying_usage():
    class MeteredAgent(FakeAgent):
        def start(self, job):
            assert self._result is None

        def cancel(self):
            self._result = AgentResult(
                "CANCELLED", {}, usage_deliveries=({"delivery_id": "cancelled-metered"},)
            )

    agent = MeteredAgent()
    paths = []

    def receive(request):
        path = request.url.path.rsplit("/", 1)[-1]
        paths.append(path)
        if paths == ["claims"]:
            return httpx.Response(200, json=assignment())
        if paths == ["claims", "heartbeat"]:
            return httpx.Response(
                200, json={"cancel_requested": False, "lease_expires_in_seconds": 90}
            )
        if paths == ["claims", "heartbeat", "heartbeat"]:
            raise httpx.ConnectError("control plane unavailable", request=request)
        if path == "usage":
            assert agent.result().outcome == "CANCELLED"
            return httpx.Response(200, json={"accepted": True})
        assert path == "claims"
        return httpx.Response(204)

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(receive)
    ) as client:
        supervisor = Supervisor(client, agent, heartbeat_interval=0)
        with pytest.raises(httpx.ConnectError):
            supervisor.run_once()
        assert agent.result().outcome == "CANCELLED"
        assert supervisor.run_once() is None
    assert paths == ["claims", "heartbeat", "heartbeat", "usage", "claims"]
