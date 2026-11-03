"""Audit recorded test evidence; never replace absent evidence with a green assertion."""

import argparse
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from acceptance_support import (
    EXTERNAL_CRITERIA,
    acceptance_scenarios,
    required_criteria,
    sha,
    source_manifest,
    task_criteria,
)


def completion_status(verified):
    pending = sorted(set(task_criteria()) - verified)
    scenarios = acceptance_scenarios()
    if set(scenarios) != {f"AT-{number:02d}" for number in range(1, 13)} or not all(
        scenarios.values()
    ):
        raise ValueError("overall acceptance scenarios are incomplete")
    missing_scenarios = [
        name for name, references in scenarios.items() if set(references) - verified
    ]
    return {
        "pending_criteria": pending,
        "pending_scenarios": missing_scenarios,
        "mvp_accepted": not (pending or missing_scenarios),
    }


def verify_publisher_restart(report, manifest):
    match = re.fullmatch(
        r"https://github\.com/iwata-n/software-factory/pull/([1-9][0-9]*)",
        report["pull_request"],
    )
    requests = report["provider_requests"]
    if (
        not match
        or report["revision"] != manifest["revision"]
        or report["publisher_exit_signal"] != "SIGKILL"
        or report["journal_phase_at_crash"] != "STARTED"
        or report["states"] != ["IN_FLIGHT", "UNKNOWN", "RECONCILING", "SUCCEEDED"]
        or report["matching_prs_before"] != 1
        or report["matching_prs_after"] != 1
        or not requests
        or any(row["method"] != "GET" or row["status_code"] != 200 for row in requests)
    ):
        raise ValueError("Publisher recovery evidence is incomplete or for a different revision")
    pull = json.loads(
        subprocess.check_output(
            ["gh", "api", f"repos/iwata-n/software-factory/pulls/{match[1]}"], timeout=15
        )
    )
    markers = set((pull["body"] or "").splitlines())
    if (
        pull["html_url"] != report["pull_request"]
        or not pull["draft"]
        or pull["merged"]
        or pull["head"]["ref"] != f"codex/factory/{report['run_id']}"
        or pull["head"]["sha"] != report["head_sha"]
        or pull["base"]["sha"] != report["base_sha"]
        or f"<!-- factory-run:{report['run_id']} -->" not in markers
        or f"<!-- factory-operation:{report['operation_key']} -->" not in markers
    ):
        raise ValueError("actual GitHub Draft PR does not match the recovered publication")


def audit(directory: Path, *, allow_ci_pending: bool = False) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    current = source_manifest()
    if manifest["code_digest"] != current["code_digest"]:
        raise ValueError("evidence inputs differ from the current source/test/configuration tree")
    if not re.match(r"^PostgreSQL 17\.", manifest["environment"]["postgresql"] or ""):
        raise ValueError("actual PostgreSQL 17 evidence is required")
    junit = ET.parse(directory / "junit.xml").getroot()
    cases = list(junit.iter("testcase"))
    if not cases or any(list(junit.iter(tag)) for tag in ("failure", "error", "skipped")):
        raise ValueError("JUnit must contain successful executed cases without skips")
    case_ids = {
        node.attrib["classname"].replace(".", "/") + ".py::" + node.attrib["name"] for node in cases
    }
    records = {}
    for ac in required_criteria() + [f"C-{i:02d}" for i in range(1, 7)]:
        path = directory / (ac[:5] if ac.startswith("SF-") else "common") / (ac + ".json")
        record = json.loads(path.read_text())
        if (
            record["ac_id"] != ac
            or record["code_digest"] != manifest["code_digest"]
            or record["revision"] != manifest["revision"]
        ):
            raise ValueError(f"{ac}: record identity or code digest mismatch")
        for field in (
            "revision",
            "fixture_digest",
            "configuration_version",
            "expected",
            "observed",
            "environment",
            "executed_at",
            "evidence_paths",
        ):
            if field not in record:
                raise ValueError(f"{ac}: missing {field}")
        if ac != "C-05":
            if not record["test_case_ids"] or not set(record["test_case_ids"]) <= case_ids:
                raise ValueError(f"{ac}: no corresponding executed JUnit cases")
            if not all(
                case.get("result") == "PASS" and case.get("observations")
                for case in record["observed"]
            ):
                raise ValueError(f"{ac}: case failed, unexecuted, or lacks observations")
            expected_fixture_digest = sha(
                json.dumps(
                    {
                        "code_digest": manifest["code_digest"],
                        "case_parameters": {
                            case["test_case_id"]: case.get("fixture_parameters", {})
                            for case in record["observed"]
                        },
                    },
                    sort_keys=True,
                ).encode()
            )
            if record["fixture_digest"] != expected_fixture_digest:
                raise ValueError(f"{ac}: fixture digest mismatch")
        records[ac] = (path, record)
    shared = records["C-05"][1]
    shared.update(
        result="PASS",
        expected="all records resolve to current inputs and executed JUnit cases",
        observed={
            "records": len(records),
            "junit_cases": len(cases),
            "all_schema_fields_present": True,
        },
        test_case_ids=["scripts/verify_acceptance.py::audit"],
        fixture_digest=sha((directory / "manifest.json").read_bytes()),
    )
    records["C-05"][0].write_text(json.dumps(shared, indent=2, ensure_ascii=False))
    external_file = directory / "github-verification.json"
    if external_file.exists():
        external = json.loads(external_file.read_text())
        if external["head_sha"] != manifest["github_sha"] or external["conclusion"] != "success":
            raise ValueError("GitHub run must be successful for the recorded exact SHA")
        if external["artifact_zip_sha256"] != sha(Path(external["artifact_zip_path"]).read_bytes()):
            raise ValueError("downloaded artifact ZIP digest mismatch")
        ci = records["SF-01-AC06"][1]
        ci.update(
            result="PASS",
            external_urls=[external["run_url"], external["artifact_url"]],
            external_verification=external,
        )
        records["SF-01-AC06"][0].write_text(json.dumps(ci, indent=2, ensure_ascii=False))
    pending = []
    publication = directory / "publication-verification.json"
    if publication.exists():
        certificate = json.loads(publication.read_text())
        report_path = directory / certificate["evaluation_file"]
        if (
            certificate["code_digest"] != manifest["code_digest"]
            or certificate["implementation_sha"] != manifest["base_sha"]
            or certificate["result"] != "PASS"
            or certificate["evaluation_file"] != "publication-evaluation.json"
            or certificate["evaluation_sha256"] != sha(report_path.read_bytes())
            or set(certificate["criteria"]) != {"SF-17-AC01", "SF-17-AC02", "SF-17-AC03"}
        ):
            raise ValueError(
                "publication evidence does not match this implementation and real evaluation"
            )
        if certificate.get("evidence_scope") not in {"LOCAL_FULL", "PUBLIC_GITHUB"}:
            raise ValueError("publication evidence scope is missing")
        if (
            certificate["evidence_scope"] == "LOCAL_FULL"
            and "publication-files/database.json" not in certificate["evidence_files"]
        ):
            raise ValueError("publication DB evidence is missing")
        for relative, fingerprint in certificate["evidence_files"].items():
            if (
                not relative.startswith("publication-files/")
                or ".." in relative.split("/")
                or "\\" in relative
                or sha((directory / relative).read_bytes()) != fingerprint
            ):
                raise ValueError("publication DB/Artifact evidence does not match its certificate")
        for ac in certificate["criteria"]:
            if ac not in records:
                raise ValueError("external publication evidence has no matching acceptance record")
            path, record = records[ac]
            record.update(
                result="PASS",
                external_urls=certificate["external_urls"],
                external_verification=certificate,
            )
            record["evidence_paths"] = list(
                dict.fromkeys(
                    record["evidence_paths"]
                    + [
                        "publication-verification.json",
                        certificate["evaluation_file"],
                    ]
                )
            )
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    restart = directory / EXTERNAL_CRITERIA["SF-19-AC01"]
    if restart.exists():
        report = json.loads(restart.read_text())
        verify_publisher_restart(report, manifest)
        path, record = records["SF-19-AC01"]
        record.update(
            result="PASS",
            external_urls=[report["pull_request"]],
            external_verification={"filename": restart.name, "sha256": sha(restart.read_bytes())},
            evidence_paths=list(dict.fromkeys([*record["evidence_paths"], restart.name])),
        )
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    for ac, (_, record) in records.items():
        if record["result"] != "PASS":
            if allow_ci_pending and ac in EXTERNAL_CRITERIA and record["result"] == "未検証":
                pending.append(ac)
            else:
                raise ValueError(f"{ac}: {record['result']}")
    result = {
        "criteria": [
            {"ac_id": ac, "result": record["result"]} for ac, (_, record) in records.items()
        ],
        "code_digest": manifest["code_digest"],
        "external_pending": pending,
        **completion_status(
            {ac for ac, (_, record) in records.items() if record["result"] == "PASS"}
        ),
    }
    (directory / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--allow-ci-pending", action="store_true")
    parser.add_argument(
        "--require-mvp", action="store_true", help="require every criterion and overall scenario"
    )
    args = parser.parse_args()
    result = audit(args.directory, allow_ci_pending=args.allow_ci_pending)
    print(
        json.dumps(
            {
                "verified_records": len(result["criteria"]),
                "external_pending": result["external_pending"],
                "pending_criteria": result["pending_criteria"],
                "pending_scenarios": result["pending_scenarios"],
                "mvp_accepted": result["mvp_accepted"],
            }
        )
    )
    if args.require_mvp and (not result["mvp_accepted"] or result["external_pending"]):
        raise SystemExit(1)
