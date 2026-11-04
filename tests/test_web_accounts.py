from sqlalchemy import select, update
from starlette.testclient import TestClient

from software_factory.api import create_app
from software_factory.models import WebAccount, WebSession

ORIGIN = "https://factory.example"
AUTH = "/v1/auth"
PASSWORD = "A fixture password 42"
REPLACEMENT = "A different fixture password 82"
ACCOUNT = {"username": "Example.User", "password": PASSWORD, "access": "OPERATOR"}


def client(world):
    return TestClient(create_app(world.database), base_url=ORIGIN)


def headers(token=None, origin=ORIGIN):
    return {"Origin": origin} | ({"Authorization": f"Bearer {token}"} if token else {})


def login(http, password=PASSWORD):
    return http.post(
        AUTH + "/login",
        json={"username": "example.user", "password": password},
        headers=headers(),
    )


def test_browser_session_uses_workspace_permissions_and_revokes_on_logout(world):
    admin = client(world)
    assert (
        admin.post(AUTH + "/accounts", json=ACCOUNT, headers=headers(world.user_token)).status_code
        == 201
    )
    browser = client(world)
    response = login(browser)
    assert response.status_code == 200
    assert response.json()["username"] == "example.user"
    assert PASSWORD not in response.text
    cookie = response.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "max-age=604800"):
        assert flag in cookie
    assert browser.get(AUTH + "/session").json()["access"] == "OPERATOR"
    assert (
        browser.get("/v1/operations/context").json()["execution"]["workspace_id"]
        == world.workspace_id
    )
    assert browser.get(AUTH + "/accounts").status_code == 403

    path = f"/v1/tasks/{world.task_id}/runs"
    assert browser.post(path, json={"spec_version": 1}).json()["code"] == "ORIGIN_DENIED"
    denied = browser.post(
        path, json={"spec_version": 1}, headers=headers(origin="https://untrusted.example")
    )
    assert denied.status_code == 403
    raw = browser.cookies.get("factory_session")
    assert admin.get("/v1/runs", headers=headers(raw)).status_code == 401
    assert browser.post(AUTH + "/logout", headers=headers()).status_code == 204
    browser.cookies.set("factory_session", raw)
    assert browser.get("/v1/runs").status_code == 401
    assert browser.get(AUTH + "/session").status_code == 204
    with world.database.transaction() as session:
        account = session.scalar(select(WebAccount))
        assert account.password_hash != PASSWORD.encode()
        assert session.scalar(select(WebSession)) is None


def test_admin_changes_are_scoped_and_invalidate_sessions(world):
    admin = client(world)
    admin_headers = headers(world.user_token)
    created = admin.post(AUTH + "/accounts", json=ACCOUNT, headers=admin_headers)
    path = AUTH + "/accounts/" + created.json()["id"]
    duplicate = admin.post(AUTH + "/accounts", json=ACCOUNT, headers=admin_headers)
    assert duplicate.status_code == 409
    assert PASSWORD not in duplicate.text
    other_headers = headers(world.other_user_token)
    assert admin.get(AUTH + "/accounts", headers=other_headers).json()["accounts"] == []
    assert admin.patch(path, json={"active": False}, headers=other_headers).status_code == 404
    browser = client(world)
    assert login(browser).status_code == 200
    assert admin.patch(path, json={"active": False}, headers=admin_headers).status_code == 200
    assert browser.get("/v1/runs").status_code == 401
    assert login(browser).status_code == 401
    assert admin.patch(path, json={"active": True}, headers=admin_headers).status_code == 200
    assert login(browser).status_code == 200
    assert (
        admin.put(
            path + "/password", json={"password": REPLACEMENT}, headers=admin_headers
        ).status_code
        == 204
    )
    assert browser.get("/v1/runs").status_code == 401
    assert login(browser).status_code == 401
    assert login(browser, REPLACEMENT).status_code == 200
    with world.database.transaction() as session:
        session.execute(update(WebSession).values(expires_at=0))
    assert browser.get("/v1/runs").status_code == 401


def test_password_forms_require_https_or_loopback_and_exact_origin(world):
    insecure = TestClient(create_app(world.database), base_url="http://192.168.1.34")
    response = insecure.post(
        AUTH + "/accounts", json=ACCOUNT, headers=headers(world.user_token, "http://192.168.1.34")
    )
    assert response.json()["code"] == "HTTPS_REQUIRED"
    response = client(world).post(
        AUTH + "/accounts", json=ACCOUNT, headers=headers(world.user_token, "null")
    )
    assert response.json()["code"] == "ORIGIN_DENIED"
