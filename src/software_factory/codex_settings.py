"""Workspace-scoped web setup for a co-located Codex Worker."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Lock, Thread, Timer

from fastapi import APIRouter, Request, Response

from .codex_credentials import chatgpt_credentials, selected_auth, selected_mode
from .service import DomainError
from .web_accounts import browser_secure, browser_write

DEVICE_URL = "https://auth.openai.com/codex/device"
CODE_PATTERN = re.compile(r"[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,2}")
LOGIN_TIMEOUT = 600


class CodexSettings:
    def __init__(self, workspace, root, existing, binary):
        self.workspace = workspace
        self.root = Path(root)
        self.existing = Path(existing)
        self.binary = Path(binary)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("FACTORY_CODEX_AUTH_ROOT must be an owner-only directory")
        self.lock = Lock()
        self.owner = None
        self.code = None
        self.state = "idle"

    @classmethod
    def configured(cls):
        keys = (
            "FACTORY_CODEX_WORKSPACE_ID",
            "FACTORY_CODEX_AUTH_ROOT",
            "FACTORY_CODEX_AUTH_FILE",
            "FACTORY_CODEX_LOGIN_BINARY",
        )
        values = [os.getenv(key) for key in keys]
        if not any(values[:2] + values[3:]):
            return None
        if not all(values):
            raise ValueError("Incomplete Codex Web settings")
        return cls(*values)

    def status(self, actor_id):
        with self.lock:
            try:
                mode = selected_mode(self.root)
                chatgpt_credentials(selected_auth(self.root, self.existing))
                connected = True
            except (OSError, ValueError):
                mode, connected = None, False
            return {
                "available": True,
                "connected": connected,
                "mode": mode,
                "login": self.state if self.owner == actor_id else "idle",
                "user_code": self.code if self.owner == actor_id else None,
                "verification_url": DEVICE_URL if self.owner == actor_id and self.code else None,
            }

    def select(self, mode):
        with NamedTemporaryFile(dir=self.root, prefix="selection-", delete=False) as file:
            path = Path(file.name)
            try:
                file.write(mode.encode("ascii"))
                file.flush()
                os.replace(path, self.root / "selected")
            finally:
                path.unlink(missing_ok=True)

    def use_existing(self):
        with self.lock:
            if self.state == "pending":
                raise DomainError(409, "CODEX_LOGIN_IN_PROGRESS")
            try:
                chatgpt_credentials(self.existing)
            except (OSError, ValueError):
                raise DomainError(409, "CODEX_CHATGPT_LOGIN_REQUIRED") from None
            self.select("existing")
            self.state = "idle"
            self.owner = None

    def start(self, actor_id):
        with self.lock:
            if self.state == "pending":
                raise DomainError(409, "CODEX_LOGIN_IN_PROGRESS")
            self.owner, self.state, self.code = actor_id, "pending", None
            Thread(target=self.finish_login, daemon=True).start()

    def finish_login(self):
        try:
            with TemporaryDirectory(prefix="login-", dir=self.root) as directory:
                cache = Path(directory) / "auth.json"
                self.device_login(directory)
                chatgpt_credentials(cache)
                with self.lock:
                    os.replace(cache, self.root / "auth.json")
                    self.select("device")
                    self.state, self.code = "complete", None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
            with self.lock:
                self.state, self.code = "failed", None

    def device_login(self, directory):
        env = {
            key: os.environ[key] for key in ("PATH", "HOME", "USER", "LANG") if key in os.environ
        } | {"CODEX_HOME": directory}
        with subprocess.Popen(
            [
                str(self.binary),
                "app-server",
                "--listen",
                "stdio://",
                "-c",
                'cli_auth_credentials_store="file"',
                "-c",
                'forced_login_method="chatgpt"',
            ],
            cwd=directory,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as process:
            timer = Timer(LOGIN_TIMEOUT, process.kill)
            timer.daemon = True
            timer.start()

            def send(message):
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()

            try:
                send(
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "software_factory_login", "version": "1"},
                            "capabilities": {"experimentalApi": True},
                        },
                    }
                )
                login_id = None
                for line in process.stdout:
                    event = json.loads(line)
                    if "error" in event:
                        break
                    if event.get("id") == 1:
                        send({"method": "initialized"})
                        send(
                            {
                                "id": 2,
                                "method": "account/login/start",
                                "params": {"type": "chatgptDeviceCode"},
                            }
                        )
                    elif event.get("id") == 2:
                        result = event["result"]
                        code = result.get("userCode", "")
                        login_id = result.get("loginId")
                        if not (
                            login_id
                            and result.get("verificationUrl") == DEVICE_URL
                            and CODE_PATTERN.fullmatch(code)
                        ):
                            break
                        with self.lock:
                            self.code = code
                    elif login_id and event.get("method") == "account/login/completed":
                        result = event.get("params", {})
                        if result.get("loginId") == login_id:
                            if result.get("success") is True:
                                return
                            break
                raise RuntimeError("ChatGPT device login did not complete")
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()


def codex_settings_router(service, token_type, settings):
    router = APIRouter(prefix="/v1/operations/integrations/codex")

    def authorize(request, token, *, write=False):
        if write:
            browser_write(request)
        elif not browser_secure(request):
            raise DomainError(403, "HTTPS_REQUIRED")
        with service.db.transaction() as session:
            actor = service.principal(session, token, "USER")
            service.require_user_access(actor, "ADMIN")
        return actor

    @router.get("")
    def status(request: Request, response: Response, token: token_type):
        actor = authorize(request, token)
        response.headers["Cache-Control"] = "no-store"
        if settings is None or settings.workspace != actor.workspace_id:
            return {"available": False}
        return settings.status(actor.id)

    @router.post("/{action}")
    def change(action: str, request: Request, response: Response, token: token_type):
        actor = authorize(request, token, write=True)
        response.headers["Cache-Control"] = "no-store"
        if settings is None or settings.workspace != actor.workspace_id:
            raise DomainError(404, "CODEX_WEB_SETUP_UNAVAILABLE")
        if action == "existing":
            settings.use_existing()
        elif action == "login":
            settings.start(actor.id)
        else:
            raise DomainError(404, "NOT_FOUND")
        return settings.status(actor.id)

    return router
