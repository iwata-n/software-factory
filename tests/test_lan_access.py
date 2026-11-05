import json

import pytest
from sqlalchemy import update
from starlette.testclient import TestClient

from software_factory.api import create_app
from software_factory.lan_access import LANAccess
from software_factory.models import Principal
from software_factory.service import issue_token, token_hash

ORIGIN = "https://192.168.1.34:18770"
PROXY_SECRET = "fixture-proxy-secret-for-private-network"


def configure(
    monkeypatch, tmp_path, token="fixture-user-token-with-at-least-32-characters", **changes
):
    data = {
        "origin": ORIGIN,
        "networks": ["192.168.1.0/24"],
        "proxy_secret": PROXY_SECRET,
        "user_token": token,
    } | changes
    path = tmp_path / "lan-access.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setenv("FACTORY_LAN_ACCESS_FILE", str(path))
    return path


def browser(world, *, address="192.168.1.90", origin=ORIGIN):
    return TestClient(create_app(world.database), base_url=origin, client=(address, 50000))


def test_shared_lan_access_has_no_login_or_browser_credential(world, monkeypatch, tmp_path):
    token = issue_token()
    with world.database.transaction() as session:
        session.add(
            Principal(
                workspace_id=world.workspace_id,
                role="USER",
                user_access="OPERATOR",
                token_hash=token_hash(token),
            )
        )
    configure(monkeypatch, tmp_path, token)
    with browser(world) as http:
        http.headers["X-Factory-LAN-Key"] = PROXY_SECRET
        session = http.get("/v1/auth/session")
        assert session.json()["authentication"] == "lan"
        assert session.json()["username"] == "LAN 共有"
        assert session.json()["access"] == "OPERATOR"
        assert token not in session.text
        assert PROXY_SECRET not in session.text
        assert "set-cookie" not in session.headers
        assert http.get("/v1/runs").status_code == 200
        assert http.get("/v1/auth/accounts").status_code == 403
        assert http.get("/v1/operations/context").json()["execution"]["workspace_id"] == (
            world.workspace_id
        )
        response = http.post(
            f"/v1/tasks/{world.task_id}/runs",
            json={"spec_version": 1, "reuse_existing": True},
            headers={"Origin": ORIGIN, "Idempotency-Key": "lan-shared-start"},
        )
        assert response.status_code == 202
        assert response.json()["run_id"] == world.run_id
        assert (
            http.post(
                "/v1/worker/claims",
                json={"protocol_version": 1},
                headers={
                    "Origin": ORIGIN,
                },
            ).status_code
            == 403
        )


@pytest.mark.parametrize(
    "address,origin,key",
    [
        ("203.0.113.10", ORIGIN, PROXY_SECRET),
        ("127.0.0.1", ORIGIN, PROXY_SECRET),
        ("192.168.1.90", "https://attacker.example", PROXY_SECRET),
        ("192.168.1.90", ORIGIN, "forged-key"),
        ("192.168.1.90", "http://192.168.1.34:18770", PROXY_SECRET),
    ],
)
def test_shared_lan_access_requires_all_boundary_checks(
    world,
    monkeypatch,
    tmp_path,
    address,
    origin,
    key,
):
    configure(monkeypatch, tmp_path, world.user_token)
    with browser(world, address=address, origin=origin) as http:
        http.headers["X-Factory-LAN-Key"] = key
        assert http.get("/v1/auth/session").status_code == 204
        assert http.get("/v1/runs").status_code == 401


def test_lan_writes_require_same_origin_and_explicit_credentials_do_not_fall_back(
    world,
    monkeypatch,
    tmp_path,
):
    configure(monkeypatch, tmp_path, world.user_token)
    with browser(world) as http:
        http.headers["X-Factory-LAN-Key"] = PROXY_SECRET
        path = f"/v1/tasks/{world.task_id}/runs"
        for headers in ({}, {"Origin": "https://attacker.example"}, {"Origin": "null"}):
            assert http.post(path, json={"spec_version": 1}, headers=headers).json()["code"] == (
                "ORIGIN_DENIED"
            )
        assert http.get("/v1/runs", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert http.get("/v1/runs", headers={"Authorization": "Bearer invalid"}).status_code == 401
        # Explicit Worker credentials retain their role and do not become the LAN user.
        response = http.post(
            "/v1/worker/claims",
            json={"protocol_version": 1},
            headers={
                "Authorization": f"Bearer {world.worker_token}",
            },
        )
        assert response.status_code == 200


def test_shared_identity_respects_revocation(world, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, world.user_token)
    with browser(world) as http:
        http.headers["X-Factory-LAN-Key"] = PROXY_SECRET
        principal_id = http.get("/v1/auth/session").json()["id"]
        with world.database.transaction() as session:
            session.execute(
                update(Principal).where(Principal.id == principal_id).values(active=False)
            )
        assert http.get("/v1/auth/session").status_code == 401
        assert http.get("/v1/runs").status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"networks": ["0.0.0.0/0"]},
        {"networks": ["203.0.113.0/24"]},
        {"origin": "http://192.168.1.34:18770"},
        {"origin": ORIGIN + "/path"},
        {"proxy_secret": "short"},
        {"networks": []},
    ],
)
def test_invalid_lan_configuration_fails_closed(monkeypatch, tmp_path, changes):
    configure(monkeypatch, tmp_path, **changes)
    with pytest.raises(ValueError, match="FACTORY_LAN_ACCESS_FILE"):
        LANAccess.configured()


def test_lan_access_is_opt_in_and_configuration_is_private(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_LAN_ACCESS_FILE", raising=False)
    assert LANAccess.configured() is None
    path = configure(monkeypatch, tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="FACTORY_LAN_ACCESS_FILE"):
        LANAccess.configured()
