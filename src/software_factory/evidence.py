"""Independent evidence ingestion. Trust, targets and verdicts are assigned by the server."""

import io
import tarfile
from xml.etree import ElementTree

from pydantic import ValidationError

from .contracts import EvidenceReport
from .db import db_now
from .models import Artifact, Candidate, Check, Finding, Worker


def target_of(candidate):
    return {
        "candidate_id": candidate.id,
        **{
            key: getattr(candidate, key)
            for key in ("head_sha", "source_digest", "spec_hash", "profile_hash", "image_digest")
        },
    }


def classify_junit(body, expected_ids, execution_state, exit_code):
    """Derive a Check verdict from observed bytes, never from an Agent's claimed PASS."""
    result = {
        "status": "INCONCLUSIVE",
        "reason": "REPORT_MISSING",
        "test_ids": [],
        "counts": {"tests": 0, "failed": 0, "errors": 0, "skipped": 0},
        "failed_test_ids": [],
    }
    if execution_state in {"TIMEOUT", "START_FAILED"}:
        return result | {"status": "ERROR", "reason": execution_state}
    if exit_code is not None and exit_code < 0:
        return result | {"status": "ERROR", "reason": "PROCESS_SIGNALED"}
    if body is None:
        return result
    try:
        root = ElementTree.fromstring(body)
        if root.tag not in {"testsuite", "testsuites"}:
            raise ValueError("unexpected report")
        cases = list(root.iter("testcase"))
        if len(cases) > 10000:
            raise ValueError("too many test cases")
        ids = [c.attrib.get("classname", "") + "::" + c.attrib["name"] for c in cases]
        if len(set(ids)) != len(ids) or any(not c.attrib["name"] for c in cases):
            raise ValueError("ambiguous test IDs")
        counts = {
            "tests": len(cases),
            "failed": sum(c.find("failure") is not None for c in cases),
            "errors": sum(c.find("error") is not None for c in cases),
            "skipped": sum(c.find("skipped") is not None for c in cases),
        }
        result |= {
            "test_ids": ids,
            "counts": counts,
            "failed_test_ids": [
                name
                for name, case in zip(ids, cases, strict=True)
                if case.find("failure") is not None or case.find("error") is not None
            ],
        }
        skipped = {
            name for name, case in zip(ids, cases, strict=True) if case.find("skipped") is not None
        }
        if not cases:
            return result | {"reason": "ZERO_TESTS"}
        if counts["skipped"] == counts["tests"]:
            return result | {"reason": "ALL_SKIPPED"}
        if not expected_ids or not set(expected_ids).issubset(ids) or set(expected_ids) & skipped:
            return result | {"reason": "REQUIRED_TESTS_MISSING"}
        if exit_code is None:
            return result | {"reason": "EXIT_CODE_MISSING"}
        status = "FAIL" if exit_code != 0 or counts["failed"] or counts["errors"] else "PASS"
        return result | {"status": status, "reason": "TESTS_COMPLETED"}
    except (ElementTree.ParseError, ValueError, KeyError):
        return result | {"reason": "REPORT_INVALID"}


def check_view(item):
    return {
        "check_record_id": item.id,
        **{
            key: getattr(item, key)
            for key in (
                "candidate_id",
                "run_id",
                "job_id",
                "attempt_id",
                "kind",
                "key",
                "status",
                "origin",
                "eligible",
                "target",
                "started_at",
                "finished_at",
                "exit_code",
                "test_ids",
                "counts",
                "artifact_ids",
                "metadata_body",
            )
        },
    }


def finding_view(item):
    return {
        "finding_record_id": item.id,
        **{
            key: getattr(item, key)
            for key in (
                "candidate_id",
                "run_id",
                "job_id",
                "attempt_id",
                "finding_key",
                "criterion_id",
                "severity",
                "category",
                "stable_code",
                "location",
                "description",
                "artifact_ids",
                "origin",
                "eligible",
            )
        },
    }


class EvidenceCommands:
    def __init__(self, service):
        self.service = service

    def validate(self, session, actor, attempt, job, run, raw, artifact_ids):
        from .service import DomainError, owned

        if not owned(session, Worker, actor.id, actor.workspace_id).managed:
            raise DomainError(403, "MANAGED_EVIDENCE_REQUIRED")
        try:
            report = EvidenceReport.model_validate(raw)
        except ValidationError:
            raise DomainError(422, "EVIDENCE_REPORT_INVALID") from None
        candidate = owned(session, Candidate, report.target.candidate_id, actor.workspace_id)
        if candidate.run_id != run.id:
            raise DomainError(404, "NOT_FOUND")
        if (job.kind == "VERIFY" and (report.criteria or report.findings)) or (
            job.kind == "REVIEW" and report.checks
        ):
            raise DomainError(422, "EVIDENCE_KIND_MISMATCH")
        checks = run.snapshot["verification_profile"]["required_checks"]
        criteria = {c["id"] for c in run.snapshot["task_spec"]["task"]["acceptance_criteria"]}
        if any(c.check_id not in checks for c in report.checks) or any(
            c.criterion_id not in criteria for c in report.criteria
        ):
            raise DomainError(422, "UNKNOWN_EVIDENCE_KEY")
        submitted = set(artifact_ids)
        permitted = submitted | set(job.payload.get("input_artifact_ids", []))

        def artifact(aid, kind=None, own=False):
            item = owned(session, Artifact, aid, actor.workspace_id)
            if (
                aid not in (submitted if own else permitted)
                or item.run_id != run.id
                or item.status != "AVAILABLE"
                or (own and item.attempt_id != attempt.id)
                or (kind and item.kind != kind)
            ):
                raise DomainError(409, "EVIDENCE_ARTIFACT_MISMATCH")
            return item

        for observed in report.checks:
            artifact(observed.log_artifact_id, "LOG", own=True)
            if observed.junit_artifact_id:
                artifact(observed.junit_artifact_id, "JUNIT", own=True)
        for criterion in report.criteria:
            for aid in criterion.evidence_artifact_ids:
                artifact(aid)
        source = None
        for finding in report.findings:
            if finding.candidate_id != candidate.id or finding.criterion_id not in criteria:
                raise DomainError(409, "FINDING_TARGET_MISMATCH")
            if finding.location.path not in candidate.metadata_body["file_manifest"]:
                raise DomainError(409, "FINDING_LOCATION_INVALID")
            if source is None:
                source = self.service.artifacts.content(
                    session, actor, candidate.source_artifact_id
                )
            try:
                with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as bundle:
                    info = bundle.getmember(finding.location.path)
                    if not info.isfile() or info.size > 128 * 1024 * 1024:
                        raise ValueError("not a bounded regular source file")
                    lines = max(1, len(bundle.extractfile(info).read().splitlines()))
                if finding.location.end_line > lines:
                    raise ValueError("line outside file")
            except (ValueError, KeyError, tarfile.TarError):
                raise DomainError(409, "FINDING_LOCATION_INVALID") from None
            for aid in finding.evidence_artifact_ids:
                artifact(aid)
        return report

    def save(self, session, actor, attempt, job, run, report):
        from .service import event, owned

        candidate = owned(session, Candidate, report.target.candidate_id, actor.workspace_id)
        isolation = report.isolation
        readonly_mounts = isolation.get("mounts", [])
        eligible = (
            report.target.model_dump() == target_of(candidate)
            and candidate.id == job.payload["candidate_id"]
            and candidate.id == run.current_candidate_id
            and isolation.get("image") == candidate.image_digest
            and isolation.get("network") == "none"
            and isolation.get("read_only_root") is True
            and isolation.get("user") == "65532:65532"
            and isolation.get("cap_drop") == ["ALL"]
            and "no-new-privileges" in isolation.get("security_opt", [])
            and len(readonly_mounts) == 1
            and readonly_mounts[0].get("Type") == "volume"
            and readonly_mounts[0].get("Destination") == "/candidate"
            and readonly_mounts[0].get("RW") is False
        )
        if (
            job.kind == "VERIFY"
            and report.checks
            and all(c.execution_state == "START_FAILED" for c in report.checks)
            and isolation == {"start_failed": True}
            and report.target.model_dump() == target_of(candidate)
            and candidate.id == job.payload["candidate_id"] == run.current_candidate_id
        ):
            eligible = True
        origin = "managed_verifier" if job.kind == "VERIFY" else "managed_reviewer"
        common = dict(
            workspace_id=run.workspace_id,
            run_id=run.id,
            candidate_id=candidate.id,
            job_id=job.id,
            attempt_id=attempt.id,
            origin=origin,
            eligible=eligible,
        )
        now = db_now(session)
        if job.kind == "VERIFY":
            commands = {
                c["check_id"]: c for c in run.snapshot["verification_profile"].get("commands", [])
            }
            observed = {c.check_id: c for c in report.checks}
            for key in run.snapshot["verification_profile"]["required_checks"]:
                if key in run.snapshot["verification_profile"].get(
                    "review_check_ids", ["review", "acceptance_review"]
                ):
                    continue
                item = observed.get(key)
                command = commands.get(key)
                xml = (
                    self.service.artifacts.content(session, actor, item.junit_artifact_id)
                    if item and item.junit_artifact_id
                    else None
                )
                verdict = classify_junit(
                    xml,
                    command["expected_test_ids"] if command else [],
                    item.execution_state if item else "EXECUTED",
                    item.exit_code if item else None,
                )
                session.add(
                    Check(
                        **common,
                        kind="VERIFY",
                        key=key,
                        status=verdict["status"],
                        target=report.target.model_dump(),
                        started_at=item.started_at if item else attempt.started_at,
                        finished_at=item.finished_at if item else now,
                        exit_code=item.exit_code if item else None,
                        test_ids=verdict["test_ids"],
                        counts=verdict["counts"],
                        artifact_ids=(
                            [aid for aid in (item.log_artifact_id, item.junit_artifact_id) if aid]
                            if item
                            else []
                        ),
                        metadata_body={
                            "reason": verdict["reason"],
                            "failed_test_ids": verdict["failed_test_ids"],
                            "command": command,
                            "isolation": isolation,
                        },
                    )
                )
        else:
            observed = {c.criterion_id: c for c in report.criteria}
            for definition in run.snapshot["task_spec"]["task"]["acceptance_criteria"]:
                key = definition["id"]
                item = observed.get(key)
                # A claimed PASS without concrete references is not acceptance evidence.
                status = item.status if item else "INCONCLUSIVE"
                if item and item.status in {"PASS", "FAIL"} and not item.evidence_artifact_ids:
                    status = "INCONCLUSIVE"
                session.add(
                    Check(
                        **common,
                        kind="REVIEW",
                        key=key,
                        status=status,
                        target=report.target.model_dump(),
                        started_at=attempt.started_at,
                        finished_at=now,
                        exit_code=None,
                        test_ids=[],
                        counts={},
                        artifact_ids=item.evidence_artifact_ids if item else [],
                        metadata_body={
                            "explanation": item.explanation if item else "AC review missing",
                            "isolation": isolation,
                        },
                    )
                )
            for finding in report.findings:
                session.add(
                    Finding(
                        **common,
                        finding_key=finding.id,
                        criterion_id=finding.criterion_id,
                        severity=finding.severity,
                        category=finding.category,
                        stable_code=finding.stable_code,
                        location=finding.location.model_dump(),
                        description=finding.description,
                        artifact_ids=finding.evidence_artifact_ids,
                    )
                )
        session.flush()
        self.service.checkpoint("evidence.created")
        event(
            session,
            run,
            "EVIDENCE_RECORDED",
            {
                "candidate_id": candidate.id,
                "kind": job.kind,
                "origin": origin,
                "eligible": eligible,
            },
        )
        # Retired results remain history; they cannot advance the current Candidate.
        if run.current_candidate_id != job.payload["candidate_id"]:
            run.status = "QUEUED"
        else:
            self.service.gates.local(session, run)
