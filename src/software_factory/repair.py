"""Stable failure sets and repair progress. Free text and line numbers are not identity."""

from .contracts import digest


def failure_set(checks, findings, evidence_ids):
    adopted = set(evidence_ids)
    selected = [c for c in checks if c["id"] in adopted]
    defects = []
    if any(not c["eligible"] or c["status"] in {"ERROR", "INCONCLUSIVE"} for c in selected):
        return None
    for check in selected:
        if check["status"] != "FAIL":
            continue
        if check["kind"] == "VERIFY":
            ids = check["metadata_body"].get("failed_test_ids", [])
            for error in ids or [f"EXIT_NONZERO:{check['exit_code']}"]:
                classname, _, symbol = error.partition("::")
                path = classname.replace(".", "/") + ".py" if symbol else ""
                defects.append(("check", check["key"], error, "high", path, symbol))
        else:
            defects.append(("criterion", check["key"], "CRITERION_FAILED", "high", "", ""))
    for finding in findings:
        if finding["id"] in adopted and finding["severity"] in {"critical", "high"}:
            if not finding["eligible"]:
                return None
            defects.append(
                (
                    "finding",
                    finding["criterion_id"],
                    finding.get("stable_code", "REVIEW_FINDING"),
                    finding["severity"],
                    finding["location"]["path"],
                    finding["location"].get("symbol") or "",
                )
            )
    return sorted(set(defects))


def ci_failure_set(checks):
    if any(c["status"] in {"ERROR", "INCONCLUSIVE", "PENDING"} for c in checks):
        return None
    return sorted(
        {
            ("ci", f"{c['provider']}:{c['app_id']}:{c['check_name']}", "CI_FAILED", "high", "", "")
            for c in checks
            if c["status"] == "FAIL"
        }
    )


def compare_failures(before, after):
    if before is None or after is None:
        return "INCONCLUSIVE"
    old, new = {tuple(v) for v in before}, {tuple(v) for v in after}
    return "PROGRESS" if new < old else "NO_PROGRESS"


def constraint_digest(snapshot):
    policy = snapshot["policy"]
    return digest(
        {
            "spec_hash": snapshot["task_spec_hash"],
            "profile_hash": snapshot["verification_profile_hash"],
            "constraints": snapshot["task_spec"]["task"]["constraints"],
            "allowed_write_paths": policy["allowed_write_paths"],
            "protected_paths": policy["protected_paths"],
            "protected_manifest": policy["protected_manifest"],
        }
    )
