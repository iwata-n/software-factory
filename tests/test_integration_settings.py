import hmac
import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from software_factory import integration_settings as settings
from software_factory.api import create_app
from software_factory.ci_poll import CIPoller
from software_factory.ci_webhook import GitHubWebhook
from software_factory.models import IntegrationSettings, Principal, Repository
from software_factory.service import issue_token, token_hash
from tests.acceptance.test_sf22_boundaries import outbox
from tests.conftest import example_spec

ROOT = "/v1/operations/integrations"
SITE = "https://jira-fixture.atlassian.net"
ORIGIN = "https://factory.example"
SLACK_TEAM = "T123456789"
SLACK_CHANNEL = "C123456789"
CLOUD = str(uuid4())


@pytest.fixture
def configured(world, monkeypatch):
    monkeypatch.setenv("FACTORY_INTEGRATION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FACTORY_OAUTH_ORIGIN", ORIGIN)
    app = create_app(world.database)
    client = TestClient(app, base_url=ORIGIN)
    client.headers["Authorization"] = f"Bearer {world.user_token}"
    return client


def jira_settings(world, **extra):
    return {
        "site": SITE,
        "project_key": "SWF",
        "field_id": "customfield_10076",
        "project_id": world.project_id,
        **extra,
    }


def provider_transport(monkeypatch, respond):
    monkeypatch.setattr(
        settings, "provider_client", lambda: httpx.Client(transport=httpx.MockTransport(respond))
    )


def test_settings_are_admin_only_encrypted_and_workspace_scoped(world, configured):
    viewer = issue_token()
    with world.database.transaction() as session:
        session.add(
            Principal(
                workspace_id=world.workspace_id,
                role="USER",
                user_access="VIEWER",
                token_hash=token_hash(viewer),
            )
        )
    headers = {"Authorization": f"Bearer {viewer}"}
    body = {"token": "secret-github-fixture"}
    assert configured.get(ROOT, headers=headers).status_code == 403
    assert configured.put(ROOT + "/github", json=body, headers=headers).status_code == 403
    assert configured.put("http://factory.example" + ROOT + "/github", json=body).status_code == 403
    assert configured.put(ROOT + "/github", json=body).json() == {"connected": True}
    current = configured.get(ROOT)
    assert current.headers["cache-control"] == "no-store"
    assert current.json()["providers"]["github"] == {
        "mode": "token",
        "connected": True,
        "webhooks": [],
    }
    assert body["token"] not in current.text
    other = configured.get(ROOT, headers={"Authorization": f"Bearer {world.other_user_token}"})
    assert other.json()["providers"]["github"] == {"connected": False, "webhooks": []}
    assert other.json()["projects"] == []
    with world.database.transaction() as session:
        row = session.get(IntegrationSettings, (world.workspace_id, "github"))
        assert body["token"].encode() not in row.ciphertext
    assert configured.delete(ROOT + "/github").status_code == 204
    assert configured.get(ROOT).json()["providers"]["github"] == {
        "connected": False,
        "webhooks": [],
    }


@pytest.mark.parametrize("oauth", [False, True])
def test_jira_web_credentials_drive_real_import_boundary(world, configured, monkeypatch, oauth):
    calls = []
    spec = example_spec(world.repository_id)
    issue = {
        "id": "124",
        "key": "SWF-1",
        "fields": {
            "project": {"key": "SWF"},
            "updated": "2026-11-04T10:00:00+00:00",
            "customfield_10076": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": json.dumps(spec)}]}
                ],
            },
        },
    }

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url == settings.ATLASSIAN_TOKEN_URL:
            body = json.loads(request.content)
            if body["grant_type"] == "authorization_code":
                assert body["code"] == "fixture-code"
                return httpx.Response(
                    200,
                    json={"access_token": "first", "refresh_token": "refresh-1", "expires_in": 1},
                )
            assert body["refresh_token"] == "refresh-1"
            return httpx.Response(
                200,
                json={"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 3600},
            )
        if request.url.path == "/oauth/token/accessible-resources":
            assert request.headers["Authorization"] == "Bearer first"
            return httpx.Response(
                200, json=[{"url": SITE, "id": CLOUD, "scopes": ["read:jira-work"]}]
            )
        if oauth:
            assert request.url.host == "api.atlassian.com"
            assert request.url.path == f"/ex/jira/{CLOUD}/rest/api/3/issue/SWF-1"
            assert request.headers["Authorization"] == "Bearer fresh"
        else:
            assert str(request.url).startswith(SITE + "/rest/api/3/issue/SWF-1")
            assert request.headers["Authorization"].startswith("Basic ")
        assert request.url.params["fields"] == "project,updated,customfield_10076"
        return httpx.Response(200, json=issue)

    provider_transport(monkeypatch, respond)
    credentials = (
        {"mode": "oauth", "client_id": "client", "client_secret": "secret"}
        if oauth
        else {
            "mode": "token",
            "email": "operator@example.test",
            "token": "manual-jira-token",
        }
    )
    saved = configured.put(ROOT + "/jira", json=jira_settings(world, **credentials))
    assert saved.status_code == 200
    if oauth:
        started = configured.post(ROOT + "/jira/oauth/start")
        assert "HttpOnly" in started.headers["set-cookie"]
        params = parse_qs(urlsplit(started.json()["url"]).query)
        assert params["scope"] == ["read:jira-work offline_access"]
        cb = ROOT + "/jira/oauth/callback"
        bad = configured.get(
            cb, params={"state": "wrong", "code": "fixture-code"}, follow_redirects=False
        )
        assert "failed" in bad.headers["location"] and calls == []
        started = configured.post(ROOT + "/jira/oauth/start")
        state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
        callback = configured.get(
            cb, params={"state": state, "code": "fixture-code"}, follow_redirects=False
        )
        assert callback.status_code == 303 and "connected" in callback.headers["location"]
        assert "fixture-code" not in callback.headers["location"]
        count = len(calls)
        configured.cookies.set("factory_oauth_jira", state)
        replay = configured.get(
            cb, params={"state": state, "code": "fixture-code"}, follow_redirects=False
        )
        assert "failed" in replay.headers["location"] and len(calls) == count
    imported = configured.post(ROOT + "/jira/import", json={"issue_key": "SWF-1"})
    assert imported.status_code == 200
    assert imported.json()["state"] == "IMPORTED"
    again = configured.post(ROOT + "/jira/import", json={"issue_key": "SWF-1"})
    assert again.json() == imported.json()
    assert [row for row in calls if row[0] == "POST"] == (
        [("POST", "/oauth/token")] * 2 if oauth else []
    )
    assert all(
        secret not in configured.get(ROOT).text
        for secret in ("refresh-2", "fresh", "manual-jira-token")
    )


def test_slack_oauth_connects_expected_team_and_drains_notification_once(
    world, configured, monkeypatch
):
    posts = []

    def respond(request):
        if request.url == settings.SLACK_TOKEN_URL:
            assert parse_qs(request.content.decode())["code"] == ["fixture-code"]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token_type": "bot",
                    "team": {"id": SLACK_TEAM},
                    "access_token": "fixture-slack-bot",
                },
            )
        assert str(request.url) == "https://slack.com/api/chat.postMessage"
        assert request.headers["Authorization"] == "Bearer fixture-slack-bot"
        payload = json.loads(request.content)
        assert payload["channel"] == SLACK_CHANNEL
        posts.append(payload)
        return httpx.Response(
            200, json={"ok": True, "channel": SLACK_CHANNEL, "ts": "12345.123456"}
        )

    provider_transport(monkeypatch, respond)
    assert configured.put(
        ROOT + "/slack",
        json={
            "mode": "oauth",
            "client_id": "client",
            "client_secret": "secret",
            "team_id": SLACK_TEAM,
            "channel": SLACK_CHANNEL,
        },
    ).is_success
    started = configured.post(ROOT + "/slack/oauth/start")
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
    callback = configured.get(
        ROOT + "/slack/oauth/callback",
        params={"state": state, "code": "fixture-code"},
        follow_redirects=False,
    )
    assert "connected" in callback.headers["location"]
    outbox(world, "SUCCEEDED")
    assert configured.post(ROOT + "/slack/deliver").json() == {"state": "SENT"}
    assert configured.post(ROOT + "/slack/deliver").json() == {"state": None}
    assert len(posts) == 1
    assert "fixture-slack-bot" not in configured.get(ROOT).text


def test_github_poller_uses_saved_workspace_credential(configured, world, monkeypatch):
    key = uuid4()
    config = GitHubWebhook(
        workspace_id=world.workspace_id,
        repository_id=world.repository_id,
        github_repository_id=123,
        github_repository="owner/repo",
        secret="s" * 32,
    )
    vault = settings.Vault(world.database, settings.os.getenv("FACTORY_INTEGRATION_KEY"))
    poller = CIPoller(configured.app.state.service, {key: config}, {}, vault)
    assert configured.put(ROOT + "/github", json={"token": "github-vault-fixture"}).is_success
    assert poller.enabled
    with httpx.Client() as client:
        binding = poller.binding(client, world.workspace_id, world.repository_id)
        assert binding[0] == key and binding[2].get_secret_value() == "github-vault-fixture"
        assert poller.binding(client, world.other_workspace_id, world.repository_id) is None


def test_webhook_uses_workspace_signing_secret(world, monkeypatch):
    monkeypatch.setenv("FACTORY_INTEGRATION_KEY", Fernet.generate_key().decode())
    identifier = str(uuid4())
    repository_name = "fixture/ci"
    monkeypatch.setenv(
        "FACTORY_GITHUB_WEBHOOKS",
        json.dumps(
            {
                identifier: {
                    "workspace_id": world.workspace_id,
                    "repository_id": world.repository_id,
                    "github_repository_id": 9001,
                    "github_repository": repository_name,
                }
            }
        ),
    )
    with world.database.transaction() as session:
        session.get(
            Repository, world.repository_id
        ).remote_url = f"https://github.com/{repository_name}.git"

    client = TestClient(create_app(world.database), base_url=ORIGIN)
    client.headers["Authorization"] = f"Bearer {world.user_token}"
    path = f"{ROOT}/github/webhooks/{identifier}"
    secret = "local-test-webhook-signing-secret"
    body = json.dumps({"repository": {"id": 9001, "full_name": repository_name}}).encode()

    def deliver(signing_key):
        return client.post(
            f"/v1/webhooks/github/{identifier}",
            content=body,
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": str(uuid4()),
                "X-Hub-Signature-256": "sha256="
                + hmac.new(signing_key.encode(), body, "sha256").hexdigest(),
            },
        )

    assert deliver(secret).status_code == 401
    assert (
        client.put(
            path,
            json={"secret": secret},
            headers={"Authorization": f"Bearer {world.other_user_token}"},
        ).status_code
        == 404
    )
    assert client.put(path, json={"secret": secret}).is_success
    assert client.put(ROOT + "/github", json={"token": "test-pat"}).is_success
    assert client.delete(ROOT + "/github").is_success
    listed = client.get(ROOT)
    assert secret not in listed.text
    assert listed.json()["providers"]["github"]["webhooks"][0]["configured"]
    assert deliver("wrong").status_code == 401
    assert deliver(secret).status_code == 202
    assert client.delete(path).status_code == 204
    assert deliver(secret).status_code == 401
