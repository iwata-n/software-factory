import json
import time

import pytest
from sqlalchemy import update
from starlette.testclient import TestClient

from software_factory.api import create_app
from software_factory.models import Principal
from software_factory.service import issue_token, token_hash
from software_factory.tailscale_access import TailscaleAccess
from tests.conftest import example_spec

ORIGIN = "https://factory.example-tailnet.ts.net"
FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "factory.example-tailnet.ts.net"}


def config(monkeypatch, tmp_path, token, **changes):
    path = tmp_path / "tailscale-access.json"
    path.write_text(json.dumps({"origin": ORIGIN, "user_token": token} | changes))
    path.chmod(0o600)
    monkeypatch.setenv("FACTORY_TAILSCALE_ACCESS_FILE", str(path))
    return path


@pytest.fixture
def tailscale(world, monkeypatch, tmp_path):
    token = issue_token()
    with world.database.transaction() as session:
        actor = Principal(
            workspace_id=world.workspace_id,
            role="USER",
            user_access="OPERATOR",
            token_hash=token_hash(token),
        )
        session.add(actor)
        session.flush()
        identifier = actor.id
    config(monkeypatch, tmp_path, token)
    return create_app(world.database), token, identifier


def client(app, address="100.107.27.109", origin=ORIGIN):
    return TestClient(app, base_url=origin, client=(address, 50000), headers=FORWARDED)


@pytest.mark.parametrize("address", ["100.107.27.109", "fd7a:115c:a1e0::1234"])
def test_tailscale_login_is_unnecessary_even_after_two_days(world, tailscale, monkeypatch, address):
    app, token, _ = tailscale
    assert app.state.tailscale_access.expires_at is None
    assert token not in repr(app.state.tailscale_access)
    now = time.time()
    for offset in (0, 172800):
        with monkeypatch.context() as patch:
            patch.setattr(
                "software_factory.temporary_web_access.time.time",
                lambda offset=offset: now + offset,
            )
            with client(app, address=address) as http:
                response = http.get("/v1/auth/session")
                assert response.status_code == 200
                account = response.json()
                assert account["authentication"] == "tailscale"
                assert account["username"] == "Tailscale 共有"
                assert account["access"] == "OPERATOR"
                assert "authentication_expires_at" not in account
                assert "set-cookie" not in response.headers and token not in response.text
                assert http.get("/v1/operations/tasks").status_code == 200
                assert http.get("/v1/auth/accounts").status_code == 403
                assert http.get("/v1/operations/context").json()["execution"]["workspace_id"] == (
                    world.workspace_id
                )


def test_tailscale_user_can_create_task_without_cookie_or_password(world, tailscale):
    app, token, _ = tailscale
    with client(app) as http:
        body = example_spec(world.repository_id)
        body["task"]["acceptance_criteria"] = []
        response = http.post(
            f"/v1/projects/{world.project_id}/tasks",
            json=body,
            headers={"Origin": ORIGIN, "Idempotency-Key": "tailscale-create"},
        )
        assert response.status_code == 201, response.text
        assert http.get(f"/v1/tasks/{response.json()['task_id']}").status_code == 200
        assert token not in response.text and not http.cookies
        assert (
            http.post(
                "/v1/worker/claims", json={"protocol_version": 1}, headers={"Origin": ORIGIN}
            ).status_code
            == 403
        )


@pytest.mark.parametrize(
    "address",
    ["192.168.1.50", "203.0.113.9", "127.0.0.1", "::1", "100.63.255.255", "fd7a:115c:a1e1::1"],
)
def test_other_networks_do_not_gain_tailscale_access(tailscale, address):
    app, _, _ = tailscale
    with client(app, address=address) as http:
        assert http.get("/v1/auth/session").status_code == 204
        assert (
            http.get("/v1/runs", headers={"X-Forwarded-For": "100.107.27.109"}).status_code == 401
        )


@pytest.mark.parametrize(
    "headers,origin",
    [
        ({"Tailscale-Funnel-Request": "?1"}, ORIGIN),
        ({"X-Forwarded-Host": "attacker.ts.net"}, ORIGIN),
        ({"X-Forwarded-Proto": "http"}, ORIGIN),
        ({}, "https://another.example-tailnet.ts.net"),
        ({}, "http://factory.example-tailnet.ts.net"),
    ],
)
def test_proxy_and_exact_origin_are_required(tailscale, headers, origin):
    app, _, _ = tailscale
    with client(app, origin=origin) as http:
        assert http.get("/v1/runs", headers=headers).status_code == 401


def test_cross_origin_and_explicit_credentials_keep_existing_rules(world, tailscale):
    app, _, _ = tailscale
    with client(app) as http:
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://attacker.example"}):
            assert (
                http.post(
                    f"/v1/tasks/{world.task_id}/runs", json={"spec_version": 1}, headers=headers
                ).json()["code"]
                == "ORIGIN_DENIED"
            )
        assert http.get("/v1/runs", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert http.get("/v1/runs", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert (
            http.get(
                "/v1/runs", headers={"Authorization": "Bearer " + world.worker_token}
            ).status_code
            == 403
        )


def test_disabling_shared_principal_revokes_access_immediately(world, tailscale):
    app, _, identifier = tailscale
    with client(app) as http:
        assert http.get("/v1/runs").status_code == 200
        with world.database.transaction() as session:
            session.execute(
                update(Principal).where(Principal.id == identifier).values(active=False)
            )
        assert http.get("/v1/auth/session").status_code == 401
        assert http.get("/v1/runs").status_code == 401


@pytest.mark.parametrize("access", ["ADMIN", "VIEWER"])
def test_shared_identity_must_be_operator(world, monkeypatch, tmp_path, access):
    token = issue_token()
    with world.database.transaction() as session:
        session.add(
            Principal(
                workspace_id=world.workspace_id,
                role="USER",
                user_access=access,
                token_hash=token_hash(token),
            )
        )
    config(monkeypatch, tmp_path, token)
    with client(create_app(world.database)) as http:
        assert http.get("/v1/auth/session").json()["code"] == "TAILSCALE_OPERATOR_REQUIRED"
        assert http.get("/v1/runs").status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "http://factory.example-tailnet.ts.net"},
        {"origin": "https://factory.example"},
        {"origin": ORIGIN + "/path"},
        {"origin": ORIGIN + ":444"},
        {"origin": "https://operator@factory.example-tailnet.ts.net"},
        {"user_token": "short"},
        {"expires_at": 123},
    ],
)
def test_invalid_configuration_is_rejected(monkeypatch, tmp_path, changes):
    config(monkeypatch, tmp_path, issue_token(), **changes)
    with pytest.raises(ValueError, match="FACTORY_TAILSCALE_ACCESS_FILE"):
        TailscaleAccess.configured()


def test_config_is_opt_in_and_owner_only(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_TAILSCALE_ACCESS_FILE", raising=False)
    assert TailscaleAccess.configured() is None
    path = config(monkeypatch, tmp_path, issue_token())
    path.chmod(0o644)
    with pytest.raises(ValueError, match="FACTORY_TAILSCALE_ACCESS_FILE"):
        TailscaleAccess.configured()
