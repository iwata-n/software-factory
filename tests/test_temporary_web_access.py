import json
import time

import pytest
from sqlalchemy import update
from starlette.testclient import TestClient

from software_factory.api import create_app
from software_factory.models import Principal
from software_factory.service import issue_token, token_hash
from software_factory.temporary_web_access import TemporaryWebAccess

ORIGIN = "https://factory.example-tailnet.ts.net"
FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "factory.example-tailnet.ts.net"}


def config(monkeypatch, tmp_path, token, **changes):
    value = {"origin": ORIGIN, "user_token": token, "expires_at": time.time() + 3600} | changes
    path = tmp_path / "temporary-web-access.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    monkeypatch.setenv("FACTORY_TEMPORARY_WEB_ACCESS_FILE", str(path))
    return path


@pytest.fixture
def temporary(world, monkeypatch, tmp_path):
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
        actor_id = actor.id
    config(monkeypatch, tmp_path, token)
    return create_app(world.database), token, actor_id


def client(app, address="100.107.27.109", origin=ORIGIN):
    return TestClient(app, base_url=origin, client=(address, 50000), headers=FORWARDED)


def test_temporary_access_uses_operator_without_browser_credentials(world, temporary):
    app, token, _ = temporary
    with client(app) as http:
        session = http.get("/v1/auth/session")
        assert session.status_code == 200
        assert session.json()["authentication"] == "temporary"
        assert session.json()["access"] == "OPERATOR"
        assert "set-cookie" not in session.headers and token not in session.text
        assert http.get("/v1/operations/tasks").status_code == 200
        assert http.get("/v1/auth/accounts").status_code == 403
        response = http.post(
            f"/v1/tasks/{world.task_id}/runs",
            json={"spec_version": 1, "reuse_existing": True},
            headers={"Origin": ORIGIN, "Idempotency-Key": "temporary-execution"},
        )
        assert response.status_code == 202
        assert response.json()["run_id"] == world.run_id
        assert (
            http.post(
                "/v1/worker/claims", json={"protocol_version": 1}, headers={"Origin": ORIGIN}
            ).status_code
            == 403
        )
        assert http.get("/v1/runs", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert (
            http.get(
                "/v1/runs",
                headers={
                    "Authorization": f"Bearer {world.worker_token}",
                },
            ).status_code
            == 403
        )


@pytest.mark.parametrize("address", ["192.168.1.50", "203.0.113.9", "127.0.0.1", "testclient"])
def test_other_networks_cannot_use_temporary_access(temporary, address):
    app, _, _ = temporary
    with client(app, address=address) as http:
        assert http.get("/v1/auth/session").status_code == 204
        assert (
            http.get("/v1/runs", headers={"X-Forwarded-For": "100.107.27.109"}).status_code == 401
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"Tailscale-Funnel-Request": "?1"},
        {"X-Forwarded-Host": "attacker.ts.net"},
        {"X-Forwarded-Proto": "http"},
    ],
)
def test_wrong_proxy_or_public_funnel_cannot_use_temporary_access(temporary, headers):
    app, _, _ = temporary
    with client(app) as http:
        assert http.get("/v1/runs", headers=headers).status_code == 401


def test_expiry_revocation_and_origin_checks(world, temporary, monkeypatch):
    app, _, actor_id = temporary
    with client(app) as http:
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://attacker.example"}):
            assert (
                http.post(
                    f"/v1/tasks/{world.task_id}/runs", json={"spec_version": 1}, headers=headers
                ).json()["code"]
                == "ORIGIN_DENIED"
            )
        assert http.get("/v1/runs", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        with monkeypatch.context() as patch:
            patch.setattr(
                "software_factory.temporary_web_access.time.time",
                lambda: app.state.temporary_web_access.expires_at,
            )
            assert http.get("/v1/auth/session").status_code == 204
            assert http.get("/v1/runs").status_code == 401
        with world.database.transaction() as session:
            session.execute(update(Principal).where(Principal.id == actor_id).values(active=False))
        assert http.get("/v1/runs").status_code == 401


def test_mode_is_opt_in_and_cannot_be_configured_with_an_admin(world, monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_TEMPORARY_WEB_ACCESS_FILE", raising=False)
    assert TemporaryWebAccess.configured() is None
    config(monkeypatch, tmp_path, world.user_token)
    with client(create_app(world.database)) as http:
        assert http.get("/v1/auth/session").json()["code"] == "TEMPORARY_OPERATOR_REQUIRED"
        assert http.get("/v1/runs").status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "http://factory.example-tailnet.ts.net"},
        {"origin": "https://factory.example"},
        {"origin": ORIGIN + "/path"},
        {"expires_at": float("inf")},
        {"expires_at": time.time() + 172800},
    ],
)
def test_invalid_config_is_rejected(monkeypatch, tmp_path, changes):
    config(monkeypatch, tmp_path, "fixture-token-with-more-than-32-characters", **changes)
    with pytest.raises(ValueError, match="FACTORY_TEMPORARY_WEB_ACCESS_FILE"):
        TemporaryWebAccess.configured()


def test_config_is_owner_only(monkeypatch, tmp_path):
    path = config(monkeypatch, tmp_path, "fixture-token-with-more-than-32-characters")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="FACTORY_TEMPORARY_WEB_ACCESS_FILE"):
        TemporaryWebAccess.configured()
