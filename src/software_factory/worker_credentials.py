"""Worker-local credential rotation and crash recovery, without Control Plane DB access."""

import base64
import fcntl
import hmac
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx

RENEW_BEFORE_SECONDS = 3600
METADATA_PATH = "/v1/worker/credentials"


def rotated_token(token: str, key: str) -> str:
    derived = hmac.digest(
        token.encode(), b"software-factory/worker-rotation/v1\0" + key.encode(), "sha256"
    )
    return base64.urlsafe_b64encode(derived).decode().rstrip("=")


class WorkerCredentials:
    def __init__(self, path):
        path = Path(path)
        self.path = path.parent.resolve() / path.name
        directory = self.path.parent.stat()
        if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise ValueError("Worker credential directory must be private and owned")
        self.refresh_at = 0.0

    @staticmethod
    def validate_private_file(fd):
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise ValueError("Worker credential file must be private, owned and regular")

    @contextmanager
    def lock(self):
        fd = os.open(
            self.path.with_name(self.path.name + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            self.validate_private_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def read(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            self.validate_private_file(stream.fileno())
            try:
                state = json.load(stream)
            except ValueError:
                raise ValueError("Invalid Worker credential file") from None
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("worker_id"), str)
            or not isinstance(state.get("worker_token"), str)
            or not 20 <= len(state["worker_token"]) <= 300
            or any(char.isspace() for char in state["worker_token"])
            or (
                "pending_key" in state
                and (not isinstance(state["pending_key"], str) or len(state["pending_key"]) != 32)
            )
        ):
            raise ValueError("Invalid Worker credential file")
        return state

    def save(self, state):
        fd, temporary = tempfile.mkstemp(prefix=".worker-credential-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def prepare(self, client):
        if time.monotonic() < self.refresh_at:
            return
        state = self.read()
        token = state["worker_token"]
        metadata = (
            self.recover(client, state) if "pending_key" in state else self.metadata(client, token)
        )
        if metadata["worker_id"] != state["worker_id"]:
            raise ValueError("Worker credential identity changed")
        if "pending_key" in state:
            token = self.commit_rotation(state, metadata)
        elif not metadata["current"]:
            raise ValueError("Worker credential file contains a predecessor token")
        elif metadata["expires_in_seconds"] <= RENEW_BEFORE_SECONDS:
            state["pending_key"] = uuid4().hex
            self.save(state)
            metadata = self.recover(client, state)
            token = self.commit_rotation(state, metadata)
        client.headers["Authorization"] = f"Bearer {token}"
        self.refresh_at = time.monotonic() + max(
            0, metadata["expires_in_seconds"] - RENEW_BEFORE_SECONDS
        )

    @staticmethod
    def metadata(client, token):
        response = client.get(
            METADATA_PATH, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def recover(self, client, state):
        current, key = state["worker_token"], state["pending_key"]
        successor = rotated_token(current, key)
        try:
            metadata = self.metadata(client, successor)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 401:
                raise
            response = client.post(
                METADATA_PATH + "/rotate",
                headers={"Authorization": f"Bearer {current}", "Idempotency-Key": key},
                timeout=10,
            )
            response.raise_for_status()
            if response.json()["worker_token"] != successor:
                raise ValueError("Unexpected Worker rotation credential") from None
            metadata = self.metadata(client, successor)
        return metadata

    def commit_rotation(self, state, metadata):
        if metadata["worker_id"] != state["worker_id"] or not metadata["current"]:
            raise ValueError("Worker rotation did not return the current credential")
        token = rotated_token(state["worker_token"], state["pending_key"])
        self.save({"worker_id": state["worker_id"], "worker_token": token})
        return token
