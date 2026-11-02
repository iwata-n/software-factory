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
