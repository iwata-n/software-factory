"""Trusted source-resolution boundary. Remote git execution belongs to SF-08's worker."""

import re
from dataclasses import dataclass
from typing import Protocol


class ReferenceNotFound(Exception):
    pass


class ResolutionUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SourceReference:
    repository_id: str
    base_ref: str
    registered_ref: str
    registered_sha: str


class SourceResolver(Protocol):
    def resolve(self, reference: SourceReference) -> str: ...


class RegisteredSourceResolver:
    """Uses administrator-pinned revisions without any network or git execution."""

    def resolve(self, reference: SourceReference) -> str:
        if reference.base_ref != reference.registered_ref:
            raise ReferenceNotFound
        return reference.registered_sha


def valid_sha(value: object) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", value) is not None
    )
