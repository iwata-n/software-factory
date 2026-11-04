import json
import sys
import time

from fastapi.testclient import TestClient

from software_factory.api import create_app
from software_factory.codex_container import DockerCodexCLI

ORIGIN = "https://factory.example"
ROOT = "/v1/operations/integrations/codex"
AUTH = {
    "auth_mode": "chatgpt",
    "tokens": {"access_token": "fixture-access", "refresh_token": "fixture-refresh"},
}


def test_workspace_admin_can_choose_chatgpt_and_worker_reads_same_selection(
    world, monkeypatch, tmp_path
):
    current = tmp_path / "existing.json"
    current.write_text(json.dumps(AUTH))
    current.chmod(0o600)
    home = tmp_path / "auth"
    host = tmp_path / "codex"
    host.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "assert json.loads(input())['method'] == 'initialize'\n"
        "print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "assert json.loads(input())['method'] == 'initialized'\n"
        "assert json.loads(input())['params']['type'] == 'chatgptDeviceCode'\n"
        "print(json.dumps({'id': 2, 'result': {'loginId': 'fixture', "
        "'verificationUrl': 'https://auth.openai.com/codex/device', "
        "'userCode': 'ABCD-1234'}}), flush=True)\n"
        "while not pathlib.Path(os.environ['CODEX_HOME']).parent.joinpath('approved').exists():\n"
        "    time.sleep(0.01)\n"
        "path = pathlib.Path(os.environ['CODEX_HOME']) / 'auth.json'\n"
        f"path.write_text({json.dumps(AUTH | {'source': 'device'})!r})\n"
        "path.chmod(0o600)\n"
        "print(json.dumps({'method':'account/login/completed',"
        "'params': {'loginId':'fixture', 'success':True}}), flush=True)\n"
    )
    host.chmod(0o700)
    for key, value in {
        "FACTORY_CODEX_WORKSPACE_ID": world.workspace_id,
        "FACTORY_CODEX_AUTH_ROOT": str(home),
        "FACTORY_CODEX_AUTH_FILE": str(current),
        "FACTORY_CODEX_LOGIN_BINARY": str(host),
    }.items():
        monkeypatch.setenv(key, value)

    app = create_app(world.database)
    client = TestClient(app, base_url=ORIGIN)
    client.headers["Authorization"] = f"Bearer {world.user_token}"
    other = {"Authorization": f"Bearer {world.other_user_token}", "Origin": ORIGIN}
    assert client.get(ROOT, headers=other).json() == {"available": False}
    assert client.post(ROOT + "/login", headers=other).status_code == 404
    assert (
        client.post(ROOT + "/login", headers={"Origin": "https://wrong.example"}).status_code == 403
    )
    assert client.get(ROOT).json()["connected"]
    response = client.post(ROOT + "/login", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    for _ in range(200):
        response = client.get(ROOT)
        if response.json()["user_code"]:
            break
        time.sleep(0.01)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["user_code"] == "ABCD-1234"
    assert "fixture-access" not in response.text
    (home / "approved").touch()
    for _ in range(200):
        status = client.get(ROOT).json()
        if status["login"] == "complete":
            break
        time.sleep(0.01)
    assert status["mode"] == "device"
    assert status["connected"]
    assert status["user_code"] is None

    worker = object.__new__(DockerCodexCLI)
    worker.auth_file = current
    output = tmp_path / "job-auth.json"
    worker.copy_auth(output)
    assert json.loads(output.read_bytes())["source"] == "device"
    assert client.post(ROOT + "/existing", headers={"Origin": ORIGIN}).json()["mode"] == "existing"
    output.unlink()
    worker.copy_auth(output)
    assert json.loads(output.read_bytes()) == AUTH
