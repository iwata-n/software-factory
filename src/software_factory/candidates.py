"""Immutable Candidate creation by managed assemblers; no Git or generated code runs here."""

import base64
import hashlib

from pydantic import ValidationError
from sqlalchemy import func, select

from .contracts import CandidateReport
from .db import db_now
from .models import Artifact, Candidate, Worker, new_id


def candidate_view(item: Candidate) -> dict:
    return {
        "candidate_id": item.id,
        "code_digest": item.source_digest,
        **{
            key: getattr(item, key)
            for key in (
                "run_id",
                "generation",
                "assembly_job_id",
                "producer_attempt_id",
                "patch_artifact_id",
                "source_artifact_id",
                "base_sha",
                "head_sha",
                "tree_sha",
                "source_digest",
                "spec_hash",
                "profile_hash",
                "image_digest",
                "metadata_body",
                "created_at",
            )
        },
    }


class CandidateCommands:
    def __init__(self, service):
        self.service = service

    def validate(self, session, actor, attempt, job, run, raw, artifact_ids):
        from .service import DomainError, owned

        if not owned(session, Worker, actor.id, actor.workspace_id).managed:
            raise DomainError(403, "MANAGED_ASSEMBLER_REQUIRED")
        try:
            report = CandidateReport.model_validate(raw)
        except ValidationError:
            raise DomainError(422, "CANDIDATE_REPORT_INVALID") from None
        if (
            report.base_sha != run.base_sha
            or report.spec_hash != run.snapshot["task_spec_hash"]
            or report.profile_hash != run.snapshot["verification_profile_hash"]
        ):
            raise DomainError(409, "CANDIDATE_TARGET_MISMATCH")
        if report.image_digest != run.snapshot["execution_image_digest"]:
            raise DomainError(409, "IMAGE_DIGEST_MISMATCH")
        isolation = report.isolation
        if (
            isolation.get("image") != report.image_digest
            or isolation.get("network") != "none"
            or isolation.get("mounts") != []
            or not isolation.get("read_only_root")
            or isolation.get("user") != "65532:65532"
            or isolation.get("cap_drop") != ["ALL"]
            or "no-new-privileges" not in isolation.get("security_opt", [])
        ):
            raise DomainError(409, "ASSEMBLER_ISOLATION_MISMATCH")
        if report.patch_artifact_id != job.payload.get("patch_artifact_id"):
            raise DomainError(409, "ASSEMBLY_INPUT_MISMATCH")
        patch = owned(session, Artifact, report.patch_artifact_id, actor.workspace_id)
        source = owned(session, Artifact, report.source_artifact_id, actor.workspace_id)
        if (
            patch.run_id != run.id
            or patch.status != "AVAILABLE"
            or patch.kind != "PATCH"
            or source.run_id != run.id
            or source.attempt_id != attempt.id
            or source.status != "AVAILABLE"
            or source.kind != "SOURCE"
            or source.id not in artifact_ids
        ):
            raise DomainError(409, "CANDIDATE_ARTIFACT_MISMATCH")
        try:
            commit = base64.b64decode(report.commit_object, validate=True)
        except ValueError:
            raise DomainError(422, "COMMIT_ENCODING_INVALID") from None
        identity = (hashlib.sha1 if len(report.head_sha) == 40 else hashlib.sha256)(
            b"commit " + str(len(commit)).encode() + b"\0" + commit
        ).hexdigest()
        parent = report.parent_sha or report.base_sha
        expected_parent = job.payload.get("parent_sha", run.base_sha)
        if (
            identity != report.head_sha
            or parent != expected_parent
            or not commit.startswith(("tree " + report.tree_sha + "\n").encode())
            or ("\nparent " + parent + "\n").encode() not in b"\n" + commit
        ):
            raise DomainError(409, "CANDIDATE_COMMIT_MISMATCH")
        return report

    def create(self, session, actor, attempt, job, run, report):
        from .service import enqueue, event

        generation = (
            session.scalar(
                select(func.max(Candidate.generation)).where(
                    Candidate.workspace_id == run.workspace_id, Candidate.run_id == run.id
                )
            )
            or 0
        ) + 1
        item = Candidate(
            id=new_id(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            generation=generation,
            assembly_job_id=job.id,
            producer_attempt_id=attempt.id,
            **{
                key: getattr(report, key)
                for key in (
                    "patch_artifact_id",
                    "source_artifact_id",
                    "base_sha",
                    "head_sha",
                    "tree_sha",
                    "source_digest",
                    "spec_hash",
                    "profile_hash",
                    "image_digest",
                )
            },
            metadata_body={
                "commit_object": report.commit_object,
                "parent_sha": report.parent_sha or report.base_sha,
                "file_manifest": report.file_manifest,
                "changed_paths": report.changed_paths,
                "isolation": report.isolation,
            },
            created_at=db_now(session),
        )
        session.add(item)
        session.flush()
        self.service.checkpoint("candidate.created")
        run.current_candidate_id = item.id
        payload = {
            "candidate_id": item.id,
            "source_artifact_id": item.source_artifact_id,
            "input_artifact_ids": [item.source_artifact_id],
        }
        for kind in ("VERIFY", "REVIEW"):
            enqueue(
                session,
                run,
                kind,
                depends_on=(job.id,),
                step_key=f"{kind.lower()}:{generation}",
                payload=payload,
            )
        event(
            session,
            run,
            "CANDIDATE_AVAILABLE",
            {
                "candidate_id": item.id,
                "head_sha": item.head_sha,
                "source_digest": item.source_digest,
            },
        )
        return item
