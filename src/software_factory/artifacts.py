"""Lease-bound artifact lifecycle. Pending bytes are never externally readable."""

import os
from pathlib import Path

from sqlalchemy import select

from .db import db_now
from .models import Artifact, AuditEvent, new_id
from .security import redact, sensitive_patch
from .storage import LocalArtifactStorage, StorageError, content_hash

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def view(artifact: Artifact) -> dict:
    return {
        key: getattr(artifact, key)
        for key in (
            "id",
            "workspace_id",
            "run_id",
            "job_id",
            "attempt_id",
            "fence",
            "kind",
            "status",
            "size_bytes",
            "sha256",
            "stored_size_bytes",
            "stored_sha256",
            "created_at",
            "expires_at",
            "finalized_at",
        )
    }


class ArtifactCommands:
    def __init__(self, service, storage=None):
        self.service = service
        self._storage = storage

    @property
    def storage(self):
        if self._storage is None:
            self._storage = LocalArtifactStorage(
                Path(os.environ.get("FACTORY_ARTIFACT_ROOT", ".local/artifacts"))
            )
        return self._storage

    def active(self, session, actor, lease_id, fence):
        from .service import DomainError

        attempt, job, run = self.service.lease(session, actor, lease_id, fence)
        if run.status != "RUNNING" or not self.service.repository_allowed(session, run):
            raise DomainError(409, "ARTIFACT_SUBMISSION_STOPPED")
        return attempt, job, run

    def owned(self, session, actor, lease_id, artifact_id, fence):
        from .service import DomainError, owned

        artifact = owned(session, Artifact, artifact_id, actor.workspace_id)
        attempt, job, run = self.active(session, actor, lease_id, fence)
        if artifact.attempt_id != attempt.id:
            raise DomainError(404, "NOT_FOUND")
        if artifact.status == "DELETED" or artifact.expires_at <= db_now(session):
            raise DomainError(409, "ARTIFACT_RESERVATION_EXPIRED")
        return artifact, attempt, job, run

    def reserve(self, session, actor, lease_id, data):
        from .service import DomainError

        if data.size_bytes > MAX_ARTIFACT_BYTES:
            raise DomainError(413, "ARTIFACT_TOO_LARGE")
        attempt, job, run = self.active(session, actor, lease_id, data.fence)
        now = db_now(session)
        artifact = Artifact(
            id=new_id(),
            workspace_id=actor.workspace_id,
            run_id=run.id,
            job_id=job.id,
            attempt_id=attempt.id,
            **data.model_dump(),
            created_at=now,
            expires_at=now + 3600,
        )
        session.add(artifact)
        session.flush()
        return view(artifact)

    def upload(self, session, actor, lease_id, artifact_id, fence, body: bytes):
        from .service import DomainError, request_secrets

        artifact, *_ = self.owned(session, actor, lease_id, artifact_id, fence)
        if len(body) > MAX_ARTIFACT_BYTES:
            raise DomainError(413, "ARTIFACT_TOO_LARGE")
        if len(body) != artifact.size_bytes or content_hash(body) != artifact.sha256:
            raise DomainError(409, "ARTIFACT_CONTENT_MISMATCH")
        stored = body
        if artifact.kind == "PATCH":
            try:
                patch = body.decode("utf-8")
            except UnicodeDecodeError:
                raise DomainError(422, "PATCH_ENCODING_INVALID") from None
            if (
                not patch.startswith(("diff --git ", "--- "))
                or "\x00" in patch
                or "GIT binary patch" in patch
                or "Binary files " in patch
            ):
                raise DomainError(422, "PATCH_FORMAT_UNSUPPORTED")
        if artifact.kind == "LOG":
            try:
                stored = redact(body.decode("utf-8"), request_secrets.get()).encode()
            except UnicodeDecodeError:
                raise DomainError(422, "LOG_ENCODING_INVALID") from None
        elif artifact.kind == "PATCH":
            if sensitive_patch(patch, request_secrets.get()):
                raise DomainError(409, "SENSITIVE_ARTIFACT_REJECTED")
        elif any(secret.encode() in body for secret in request_secrets.get()) or redact(
            body.decode("utf-8", errors="replace")
        ) != body.decode("utf-8", errors="replace"):
            raise DomainError(409, "SENSITIVE_ARTIFACT_REJECTED")
        try:
            self.storage.put(actor.workspace_id, artifact.id, stored)
        except StorageError:
            raise DomainError(409, "ARTIFACT_STORAGE_CONFLICT") from None
        if artifact.status != "AVAILABLE":
            artifact.status = "UPLOADED"
        artifact.stored_size_bytes, artifact.stored_sha256 = len(stored), content_hash(stored)
        return view(artifact)

    def finalize(self, session, actor, lease_id, artifact_id, data):
        from .service import DomainError, event

        artifact, _, _, run = self.owned(session, actor, lease_id, artifact_id, data.fence)
        if artifact.status == "AVAILABLE":
            return view(artifact)
        if artifact.status != "UPLOADED":
            raise DomainError(409, "ARTIFACT_NOT_UPLOADED")
        try:
            body = self.storage.read(actor.workspace_id, artifact.id)
        except StorageError:
            raise DomainError(409, "ARTIFACT_BYTES_UNAVAILABLE") from None
        if len(body) != artifact.stored_size_bytes or content_hash(body) != artifact.stored_sha256:
            raise DomainError(409, "ARTIFACT_CONTENT_MISMATCH")
        artifact.status, artifact.finalized_at = "AVAILABLE", db_now(session)
        event(
            session,
            run,
            "ARTIFACT_AVAILABLE",
            {"artifact_id": artifact.id, "sha256": artifact.stored_sha256},
        )
        return view(artifact)

    def validate_completion(self, session, actor, attempt, ids):
        from .service import DomainError, owned

        if len(ids) != len(set(ids)):
            raise DomainError(409, "ARTIFACT_DUPLICATE")
        result = []
        for item in ids:
            artifact = owned(session, Artifact, item, actor.workspace_id)
            if artifact.attempt_id != attempt.id or artifact.status != "AVAILABLE":
                raise DomainError(409, "ARTIFACT_NOT_AVAILABLE_FOR_ATTEMPT")
            result.append(artifact)
        return result

    def content(self, session, actor, artifact_id):
        from .service import DomainError, owned

        artifact = owned(session, Artifact, artifact_id, actor.workspace_id)
        if artifact.status == "DELETED" and artifact.finalized_at is not None:
            raise DomainError(404, "EVIDENCE_RETENTION_EXPIRED")
        if artifact.status != "AVAILABLE":
            raise DomainError(404, "NOT_FOUND")
        try:
            body = self.storage.read(actor.workspace_id, artifact.id)
        except StorageError:
            raise DomainError(409, "ARTIFACT_BYTES_UNAVAILABLE") from None
        if content_hash(body) != artifact.stored_sha256:
            raise DomainError(409, "ARTIFACT_CORRUPTED")
        return body

    def collect(self, session, workspace_id):
        now, removed = db_now(session), 0
        for artifact in session.scalars(
            select(Artifact).where(
                Artifact.workspace_id == workspace_id,
                Artifact.status.in_(["RESERVED", "UPLOADED"]),
                Artifact.expires_at <= now,
            )
        ):
            self.storage.delete(workspace_id, artifact.id)
            artifact.status = "DELETED"
            session.add(
                AuditEvent(
                    workspace_id=workspace_id,
                    actor_id="local-admin",
                    operation="artifact.collect",
                    body={"artifact_id": artifact.id},
                    created_at=now,
                )
            )
            removed += 1
        return removed
