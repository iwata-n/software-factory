"""Deterministic quality decisions. No database, clock, filesystem or Agent access."""

from dataclasses import dataclass

from .contracts import digest

TARGET_FIELDS = (
    "candidate_id",
    "head_sha",
    "source_digest",
    "spec_hash",
    "profile_hash",
    "image_digest",
)


@dataclass(frozen=True)
class GateDecision:
    status: str
    reason: str
    evidence_ids: tuple[str, ...]
    evidence_digest: str


def decision(status, reason, target, evidence):
    ordered = sorted(evidence, key=lambda item: (item["id"], digest(item)))
    return GateDecision(
        status,
        reason,
        tuple(item["id"] for item in ordered),
        digest({"target": target, "evidence": ordered}),
    )


def local_gate(target, required_checks, required_criteria, checks, findings, *, jobs_pending=False):
    if jobs_pending:
        return decision("WAIT", "MANDATORY_JOBS_PENDING", target, [])
    mandatory = {("VERIFY", key) for key in required_checks} | {
        ("REVIEW", key) for key in required_criteria
    }
    selected, groups, extras, ambiguous = {}, {}, [], False
    for item in checks:
        key = (item["kind"], item["key"])
        if key in mandatory:
            groups.setdefault(key, {})[digest(item)] = item
    for key, values in groups.items():
        latest = max(c["attempt_no"] for c in values.values())
        current = sorted(
            [c for c in values.values() if c["attempt_no"] == latest],
            key=lambda c: (c["id"], digest(c)),
        )
        selected[key] = current[-1]
        ambiguous |= len(current) != 1
        extras += current[:-1]
    evidence = list(selected.values()) + extras
    attempts = {c["attempt_id"] for c in evidence if c["kind"] == "REVIEW"}
    adopted_findings = [f for f in findings if f["attempt_id"] in attempts]
    evidence += adopted_findings
    expected = {key: target[key] for key in TARGET_FIELDS}
    mismatch = ambiguous or any(
        not c["eligible"]
        or c["target"] != expected
        or c["origin"] != ("managed_verifier" if c["kind"] == "VERIFY" else "managed_reviewer")
        for c in selected.values()
    )
    mismatch |= any(
        not f["eligible"]
        or f["candidate_id"] != target["candidate_id"]
        or f["origin"] != "managed_reviewer"
        for f in adopted_findings
    )
    if mismatch:
        return decision("INCONCLUSIVE", "EVIDENCE_TARGET_MISMATCH", target, evidence)
    statuses = {c["status"] for c in selected.values()}
    if "ERROR" in statuses:
        return decision("ERROR", "VERIFICATION_INFRASTRUCTURE_ERROR", target, evidence)
    if "FAIL" in statuses or any(f["severity"] in {"critical", "high"} for f in adopted_findings):
        return decision("FAIL", "QUALITY_REQUIREMENTS_FAILED", target, evidence)
    if not mandatory or set(selected) != mandatory or statuses != {"PASS"}:
        return decision("INCONCLUSIVE", "MANDATORY_EVIDENCE_MISSING", target, evidence)
    return decision("PASS", "LOCAL_REQUIREMENTS_PASSED", target, evidence)


def final_gate(target, local, draft, required_ci, checks, *, now, deadline):
    expected = {(c["provider"], c["app_id"], c["check_name"]): c for c in required_ci}
    selected, extras, ambiguous = {}, [], False
    groups = {}
    for item in checks:
        key = (item["provider"], item["app_id"], item["check_name"])
        if key in expected:
            groups.setdefault(key, {})[digest(item)] = item
    for key, values in groups.items():
        latest = max(c.get("run_sequence", 0) for c in values.values())
        current = sorted(
            [c for c in values.values() if c.get("run_sequence", 0) == latest],
            key=lambda c: (c["id"], digest(c)),
        )
        ambiguous |= len(current) != 1
        selected[key] = current[-1]
        extras += current[:-1]
    evidence = list(selected.values()) + extras
    if local:
        evidence.append(local)
    if draft:
        evidence.append(draft)
    evidence.append({"id": "ci-deadline", "deadline_at": deadline, "expired": now >= deadline})
    if not expected:
        return decision("INCONCLUSIVE", "CI_PROFILE_MISSING", target, evidence)
    if now >= deadline:
        return decision("INCONCLUSIVE", "CI_TIMEOUT", target, evidence)
    pending = selected.keys() != expected.keys() or any(
        c["status"] == "PENDING" for c in [*selected.values(), *extras]
    )
    if pending:
        return decision("WAIT", "CI_PENDING", target, evidence)
    if ambiguous:
        return decision("INCONCLUSIVE", "CI_IDENTITY_AMBIGUOUS", target, evidence)
    if (
        not local
        or local["status"] != "PASS"
        or local["target"] != target
        or not draft
        or draft["origin_verified"] is not True
        or draft["draft"] is not True
        or draft["state"] != "OPEN"
        or any(
            draft[k] != target[k] for k in ("candidate_id", "repository_id", "base_sha", "head_sha")
        )
    ):
        return decision("INCONCLUSIVE", "PUBLISH_TARGET_MISMATCH", target, evidence)
    for key, item in selected.items():
        requirement = expected[key]
        if requirement.get("github_workflow_id") and (
            item.get("github_workflow_id") != requirement["github_workflow_id"]
            or item.get("github_workflow_path") != requirement["github_workflow_path"]
            or item.get("github_event") != "pull_request"
            or item["tested_sha"] != target["head_sha"]
        ):
            return decision("INCONCLUSIVE", "CI_IDENTITY_MISMATCH", target, evidence)
        if (
            item["origin_verified"] is not True
            or item["head_sha"] != target["head_sha"]
            or item["base_sha"] != target["base_sha"]
            or (
                item["tested_sha"] != target["head_sha"]
                and (
                    item["tested_sha"] != draft.get("merge_sha")
                    or item["merge_parents"] != [target["base_sha"], target["head_sha"]]
                )
            )
        ):
            return decision("INCONCLUSIVE", "CI_TARGET_MISMATCH", target, evidence)
    statuses = {c["status"] for c in selected.values()}
    if "ERROR" in statuses:
        return decision("ERROR", "CI_INFRASTRUCTURE_ERROR", target, evidence)
    if "FAIL" in statuses:
        return decision("FAIL", "CI_FAILED", target, evidence)
    if statuses != {"PASS"}:
        return decision("INCONCLUSIVE", "CI_INCONCLUSIVE", target, evidence)
    return decision("PASS", "FINAL_REQUIREMENTS_PASSED", target, evidence)
