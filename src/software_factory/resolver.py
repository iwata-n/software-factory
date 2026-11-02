"""Trusted source-resolution boundary. Remote git execution belongs to SF-08's worker."""

import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx


class ReferenceNotFound(Exception):
    pass


class SourceRejected(ReferenceNotFound):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ResolutionUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SourceReference:
    repository_id: str
    base_ref: str
    registered_ref: str
    registered_sha: str
    remote_url: str | None = None


class SourceResolver(Protocol):
    def resolve(self, reference: SourceReference) -> str: ...


class RegisteredSourceResolver:
    """Uses administrator-pinned revisions without any network or git execution."""

    def resolve(self, reference: SourceReference) -> str:
        if reference.remote_url:
            raise ResolutionUnavailable
        if reference.base_ref != reference.registered_ref:
            raise ReferenceNotFound
        return reference.registered_sha


class HttpSourceResolver:
    """Control Plane uses this HTTP-only client; Git runs in another process/container."""

    def __init__(self, url: str, token: str, *, client=None):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme != "https"
                and not (
                    parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            raise ValueError("source runner requires HTTPS or loopback HTTP")
        self.client = client or httpx.Client(base_url=url, timeout=150, follow_redirects=False)
        self.token = token

    def request(self, operation: str, body: dict) -> dict:
        try:
            response = self.client.post(
                "/v1/sources/" + operation,
                json=body,
                headers={"Authorization": "Bearer " + self.token},
            )
        except httpx.TransportError:
            raise ResolutionUnavailable from None
        if (
            response.status_code in (401, 403)
            or response.status_code >= 500
            or response.is_redirect
        ):
            raise ResolutionUnavailable
        if response.status_code != 200:
            try:
                code = response.json().get("code", "")
            except (ValueError, AttributeError):
                code = ""
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code):
                raise SourceRejected(code)
            raise ReferenceNotFound
        try:
            result = response.json()
        except ValueError:
            raise ResolutionUnavailable from None
        if not isinstance(result, dict):
            raise ResolutionUnavailable
        if not valid_sha(result.get("sha")):
            raise ReferenceNotFound
        return result

    def resolve(self, reference: SourceReference) -> str:
        return self.resolve_details(reference)["sha"]

    def resolve_details(self, reference: SourceReference) -> dict:
        if not reference.remote_url:
            return {"sha": RegisteredSourceResolver().resolve(reference)}
        return self.request(
            "resolve", {"remote_url": reference.remote_url, "base_ref": reference.base_ref}
        )

    def fetch(self, remote_url: str, sha: str) -> dict:
        result = self.request("fetch", {"remote_url": remote_url, "sha": sha})
        if result["sha"] != sha:
            raise ReferenceNotFound
        return result


def configured_resolver() -> SourceResolver:
    if url := os.environ.get("FACTORY_SOURCE_RUNNER_URL"):
        return HttpSourceResolver(url, os.environ["FACTORY_SOURCE_RUNNER_TOKEN"])
    return RegisteredSourceResolver()


def valid_sha(value: object) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", value) is not None
    )
