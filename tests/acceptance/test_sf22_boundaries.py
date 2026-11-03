import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from software_factory.api import create_app
from software_factory.jira_input import JiraMapping
from software_factory.jira_relay import import_issue
from software_factory.models import (
    JiraIssueRevision,
    OutboxEvent,
    Principal,
    Run,
    SpecVersion,
    Task,
)
from software_factory.service import token_hash, transition
from software_factory.slack_delivery import (
    CLAIM_SECONDS,
    SlackClient,
    SlackDispatcher,
    enqueue_result,
)
from tests.conftest import example_spec

CHANNEL = "C0123456789"
SITE = "https://fixture.atlassian.net"
FIELD = "customfield_12345"
JIRA_PATH = "/v1/integrations/jira/jira-eval/issues"
JIRA_TOKEN = "fixture-relay-token-01234567890123456789"


@pytest.fixture
def jira(world, monkeypatch):
    with world.database.transaction() as session:
        principal = session.scalar(
            select(Principal).where(Principal.token_hash == token_hash(world.user_token))
        )
        mapping = {
            "integration_id": "jira-eval",
            "site": SITE,
            "project_key": "SWF",
            "workspace_id": world.workspace_id,
            "project_id": world.project_id,
            "principal_id": principal.id,
            "field_id": FIELD,
        }
    monkeypatch.setenv("FACTORY_JIRA_MAPPING", json.dumps(mapping))
    monkeypatch.setenv("FACTORY_JIRA_RELAY_TOKEN", JIRA_TOKEN)
    with TestClient(create_app(world.database, reconcile_interval=3600)) as client:
        yield client, mapping


def input_for(world, version, spec=None):
    return {
        "site": SITE,
        "project_key": "SWF",
        "issue_key": "SWF-123",
        "issue_id": "10099",
        "source_version": version,
        "fields": {FIELD: spec if spec is not None else example_spec(world.repository_id)},
    }


def receive(jira, body, token=JIRA_TOKEN):
    return jira[0].post(JIRA_PATH, json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.ac("SF-22-AC01", "SF-22-AC02")
def test_jira_uses_core_spec_and_keeps_active_run_fixed(world, jira):
    assert world.client.post(JIRA_PATH, json={}).status_code == 404
    manual_run = world.run()
    first = input_for(world, 2, json.dumps(example_spec(world.repository_id)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: receive(jira, first), range(4)))
    assert all(reply.status_code == 200 for reply in replies)
    accepted = replies[0].json()
    assert all(reply.json() == accepted for reply in replies)
    assert accepted["state"] == "IMPORTED"
    assert receive(jira, input_for(world, 1)).json()["state"] == "STALE"

    task_id = accepted["task_id"]
    result = world.post(f"/v1/tasks/{task_id}/runs", {"spec_version": 1})
    assert result.status_code == 202
    imported_run = result.json()
    assert imported_run["snapshot"] == manual_run["snapshot"]

    update = input_for(world, 3)
    update["fields"][FIELD]["task"]["objective"] = "Updated"
    assert receive(jira, update).json()["spec_version"] == 2
    conflicting = deepcopy(update)
    conflicting["fields"][FIELD]["task"]["objective"] = "Same version, different content"
    assert receive(jira, conflicting).status_code == 409
    assert world.get(f"/v1/runs/{imported_run['run_id']}").json() == imported_run

    invalid = input_for(world, 4)
    del invalid["fields"][FIELD]["task"]["acceptance_criteria"]
    assert receive(jira, invalid).json()["state"] == "NEEDS_INPUT"
    assert receive(jira, input_for(world, 4)).status_code == 409
    with world.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 2
        assert session.scalar(select(func.count()).select_from(Run)) == 2
        assert session.scalar(select(func.count()).select_from(SpecVersion)) == 3
        assert session.scalar(select(func.count()).select_from(JiraIssueRevision)) == 4


@pytest.mark.ac("SF-22-AC03")
def test_jira_mapping_revocation_and_quarantine(world, jira, caplog):
    body = input_for(world, 1)
    assert receive(jira, body, world.user_token).status_code == 401
    assert receive(jira, {**body, "project_key": "OTHER"}).status_code == 403
    assert receive(jira, {**body, "workspace_id": world.other_workspace_id}).status_code == 422
    assert receive(jira, {**body, "site": "https://another.atlassian.net"}).status_code == 403
    canary = "xoxb-9000-quietfixture"
    body["fields"][FIELD]["task"]["objective"] = f"Secret {canary}"
    result = receive(jira, body)
    assert result.json()["state"] == "QUARANTINED"
    assert canary not in result.text + caplog.text
    with world.database.transaction() as session:
        revision = session.scalar(select(JiraIssueRevision))
        assert revision.task_id is None and revision.fingerprint is None
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        for model, column in [(SpecVersion, SpecVersion.body), (OutboxEvent, OutboxEvent.body)]:
            assert canary not in str(session.scalars(select(column).select_from(model)).all())
        session.get(Principal, jira[1]["principal_id"]).active = False
    assert receive(jira, input_for(world, 2)).status_code == 403


def outbox(world, status):
    with world.database.transaction() as session:
        run = session.get(Run, world.run_id)
        if status == "SUCCEEDED":
            run.status = status
            enqueue_result(
                session,
                run,
                pr_url="https://github.com/iwata-n/software-factory/pull/22",
                ci_urls=[
                    "https://github.com/iwata-n/software-factory/actions/runs/29",
                    "https://github.com/iwata-n/software-factory/artifacts/1?token=fixture",
                ],
            )
        elif status == "BLOCKED":
            transition(session, run, "BLOCKED", "SPEC_CHANGE_REQUIRED")
        else:
            transition(session, run, "ATTEMPTS_EXHAUSTED")
    return world.run()


def dispatcher(world, transport):
    client = httpx.Client(transport=httpx.MockTransport(transport))
    return SlackDispatcher(world.database, SlackClient(client, "stub"), world.workspace_id, CHANNEL)


def notifications(world):
    path = f"/v1/runs/{world.run_id}/notifications"
    assert world.get(path, token=world.other_user_token).status_code == 404
    return world.get(path).json()["notifications"]


@pytest.mark.ac("SF-22-AC04")
@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "BLOCKED"])
@pytest.mark.parametrize("history_access", [True, False])
def test_recover_after_post_before_db_ack(world, clock, status, history_access, monkeypatch):
    snapshot = outbox(world, status)
    (pending,) = notifications(world)
    assert pending["state"] == "PENDING"
    posts = []

    def transport(request):
        if request.method == "POST":
            post = json.loads(request.content)
            posts.append(post)
            assert post["channel"] == CHANNEL
            assert status in post["text"] and world.run_id in post["text"]
            assert "artifacts/" not in post["text"] and "fixture" not in post["text"]
            if status == "SUCCEEDED":
                assert "/pull/22" in post["text"] and "/actions/runs/29" in post["text"]
            return httpx.Response(200, json={"ok": True, "channel": CHANNEL, "ts": "1.000001"})
        if not history_access:
            return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
        return httpx.Response(
            200,
            json={"ok": True, "messages": [{"metadata": posts[0]["metadata"], "ts": "1.000001"}]},
        )

    worker = dispatcher(world, transport)
    original = worker.slack.post

    def crash_after_post(*args):
        original(*args)
        raise SystemExit(9)

    with monkeypatch.context() as patch:
        patch.setattr(worker.slack, "post", crash_after_post)
        with pytest.raises(SystemExit):
            worker.deliver_one()
    assert worker.deliver_one() is None
    clock.advance(CLAIM_SECONDS)
    assert dispatcher(world, transport).deliver_one() == ("SENT" if history_access else "UNKNOWN")
    assert worker.deliver_one() is None
    (delivery,) = notifications(world)
    assert posts[0]["metadata"]["event_payload"]["key"] == delivery["delivery_key"]
    assert delivery["delivery_key"] == pending["delivery_key"]
    assert delivery["attempts"] == 1
    assert delivery["message_id"] == ("1.000001" if history_access else None)
    assert len(posts) == 1 and world.run() == snapshot


@pytest.mark.ac("SF-22-AC05")
@pytest.mark.parametrize("auth_error", [True, False])
@pytest.mark.parametrize("retry_after", [0, 7])
def test_slack_delivery_backoff_and_terminal_run_are_independent(
    world, clock, auth_error, retry_after
):
    snapshot = outbox(world, "SUCCEEDED")
    keys = []

    def transport(request):
        keys.append(json.loads(request.content)["metadata"]["event_payload"]["key"])
        if auth_error:
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        if len(keys) == 1:
            return httpx.Response(429, headers={"Retry-After": str(retry_after)})
        return httpx.Response(503, json={"ok": False, "error": "service_unavailable"})

    worker = dispatcher(world, transport)
    if auth_error:
        assert worker.deliver_one() == "FAILED"
    else:
        assert worker.deliver_one() == "RETRY"
        clock.advance(max(2, retry_after) - 1)
        assert worker.deliver_one() is None
        clock.advance(1)
        assert worker.deliver_one() == "RETRY"
        clock.advance(3)
        assert worker.deliver_one() is None
        clock.advance(1)
        assert worker.deliver_one() == "FAILED"
    assert worker.deliver_one() is None
    (delivery,) = notifications(world)
    assert delivery["reason"] == ("SLACK_REJECTED" if auth_error else "RETRIES_EXHAUSTED")
    assert delivery["attempts"] == len(keys) == (1 if auth_error else 3)
    assert len(set(keys)) == 1 and world.run() == snapshot


@pytest.mark.ac("SF-22-AC04", "SF-22-AC05")
def test_ambiguous_5xx_is_reconciled_and_never_reposted(world):
    outbox(world, "SUCCEEDED")
    methods = []

    def transport(request):
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={"ok": True, "messages": []})

    worker = dispatcher(world, transport)
    assert worker.deliver_one() == "UNKNOWN"
    assert worker.deliver_one() is None
    assert methods == ["POST", "GET"]
    (delivery,) = notifications(world)
    assert delivery["reason"] == "AMBIGUOUS_RESPONSE"
    assert delivery["attempts"] == 1


@pytest.mark.ac("SF-22-AC01", "SF-22-AC02")
def test_jira_rest_relay_imports_adf_and_rejects_wrong_project(world, jira):
    relay, settings = jira
    mapping = JiraMapping.model_validate(settings)
    spec = example_spec(world.repository_id)
    document = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "codeBlock", "content": [{"type": "text", "text": json.dumps(spec)}]}],
    }
    calls = []
    provider = {
        "id": "10099",
        "key": "SWF-123",
        "fields": {
            "project": {"key": "SWF"},
            "updated": "2026-11-03T10:00:00.001+0900",
            FIELD: document,
        },
    }

    def respond(request):
        calls.append(request.method)
        if request.method == "GET":
            assert str(request.url).startswith(SITE + "/rest/api/3/issue/SWF-123?")
            assert request.url.params["fields"] == f"project,updated,{FIELD}"
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json=provider)
        assert request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == f"Bearer {JIRA_TOKEN}"
        result = relay.post(request.url.path, content=request.content, headers=request.headers)
        return httpx.Response(result.status_code, content=result.content)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:

        def run(key):
            return import_issue(
                client,
                mapping,
                key,
                "http://127.0.0.1:8000",
                "jira@example.test",
                "fixture-jira-token",
                JIRA_TOKEN,
            )

        with pytest.raises(ValueError, match="configured Jira project"):
            run("OTHER-123")
        assert not calls
        first = run("SWF-123")
        assert first["state"] == "IMPORTED" and first["spec_version"] == 1
        assert first["issue_url"] == SITE + "/browse/SWF-123"
        assert run("SWF-123") == first
        provider["fields"]["project"]["key"] = "OTHER"
        with pytest.raises(ValueError, match="Invalid Jira issue response"):
            run("SWF-123")
        assert calls == ["GET", "POST", "GET", "POST", "GET"]
    with world.database.transaction() as session:
        rows = session.scalars(select(JiraIssueRevision)).all()
        assert len(rows) == 1 and rows[0].task_id == first["task_id"]


@pytest.mark.ac("SF-22-AC03")
def test_jira_mapping_cannot_elevate_viewer(world, jira):
    _, mapping = jira
    with world.database.transaction() as session:
        session.get(Principal, mapping["principal_id"]).user_access = "VIEWER"
    result = receive(jira, input_for(world, 1))
    assert result.status_code == 403 and result.json()["code"] == "JIRA_INTEGRATION_REVOKED"
