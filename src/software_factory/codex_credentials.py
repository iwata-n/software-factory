"""Credential files shared by a local setup process and isolated Codex workers."""

import json
import os
import stat
from pathlib import Path

MAX_AUTH_SIZE = 1024 * 1024


def private_bytes(path, *, limit=MAX_AUTH_SIZE):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if (
            info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= limit
        ):
            raise ValueError("Invalid private file")
        content = file.read(limit + 1)
        if len(content) != info.st_size:
            raise ValueError("Private file changed while reading")
        return content


def chatgpt_credentials(path):
    content = private_bytes(path)
    data = json.loads(content)
    if (
        not isinstance(data, dict)
        or data.get("auth_mode") != "chatgpt"
        or data.get("OPENAI_API_KEY")
    ):
        raise ValueError("ChatGPT sign-in required")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not all(
        isinstance(tokens.get(key), str) and tokens[key]
        for key in ("access_token", "refresh_token")
    ):
        raise ValueError("ChatGPT sign-in required")
    return content


def selected_mode(root):
    try:
        choice = private_bytes(Path(root) / "selected", limit=8).decode("ascii")
    except FileNotFoundError:
        return "existing"
    if choice not in {"existing", "device"}:
        raise ValueError("Invalid credential selection")
    return choice


def selected_auth(root, existing):
    return Path(root) / "auth.json" if selected_mode(root) == "device" else Path(existing)
