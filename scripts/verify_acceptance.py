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

from software_factory.contracts import digest

SCENARIO_CRITERION = "SF-24-AC03"


def audited_scenarios(records):
    scenarios = acceptance_scenarios()
    if set(scenarios) != {f"AT-{number:02d}" for number in range(1, 13)} or not all(
        references and SCENARIO_CRITERION not in references for references in scenarios.values()
    ):
        raise ValueError("overall acceptance scenarios are incomplete or circular")
    matrix = {}
    for name, references in scenarios.items():
        criteria = {}
        for ac in references:
            if ac not in records:
                criteria[ac] = {"result": "未検証", "test_case_ids": [], "evidence_paths": []}
                continue
            path, record = records[ac]
            criteria[ac] = {
                "result": record["result"],
                "test_case_ids": record["test_case_ids"],
                "record": f"{path.parent.name}/{path.name}",
                "evidence_paths": record["evidence_paths"],
                "external_urls": record["external_urls"],
            }
        matrix[name] = {
            "result": "PASS"
            if all(row["result"] == "PASS" for row in criteria.values())
            else "未検証",
            "criteria": criteria,
        }
    return matrix


def completion_status(verified, scenarios):
    pending = sorted(set(task_criteria()) - verified)
    missing_scenarios = [name for name, record in scenarios.items() if record["result"] != "PASS"]
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


def verify_live_ci_matrix(matrix, manifest):
    expected = {(route, result) for route in ("POLL", "WEBHOOK") for result in ("PASS", "FAIL")}
    runs = matrix["runs"]
    if len(runs) != len(expected) or {(case["route"], case["status"]) for case in runs} != expected:
        raise ValueError("Both CI routes require independent pass and fail evidence")
    urls = set()
    run_ids = set()
    for case in runs:
        report = case["report"]
        signal = case["trigger"]
        if (
            report["revision"] != manifest["revision"]
            or signal["reconciliations"][-1]["trigger"] != case["route"]
            or any(row["event"] != "ping" for row in signal["deliveries"])
            != (case["route"] == "WEBHOOK")
        ):
            raise ValueError("CI evidence revision or observed trigger mismatch")
        if case["status"] == "PASS":
            result = report["result"]
            if (
                result["kind"] != "VERIFIED_DRAFT_PR"
                or report["ci_input_origin"] != "actual_github"
            ):
                raise ValueError("Successful CI did not produce a verified Draft PR")
            (check,) = result["checks"]
            draft = result["draft_pr"]
        else:
            if (
                report["run_phase"] != "REPAIR"
                or report["success_receipt"]
                or report["success_notification"]
            ):
                raise ValueError("Failed CI produced a success result or did not request repair")
            check, draft = report["check"], report["draft_pr"]
        if (
            check["status"] != case["status"]
            or not check["origin_verified"]
            or check["head_sha"] != draft["head_sha"]
            or check["tested_sha"] != draft["head_sha"]
            or check["base_sha"] != draft["base_sha"]
            or draft["url"] != f"https://github.com/iwata-n/software-factory/pull/{draft['number']}"
        ):
            raise ValueError("Recorded CI and Draft PR do not match")
        repo = "repos/iwata-n/software-factory"
        pull = json.loads(
            subprocess.check_output(
                ["gh", "api", f"{repo}/pulls/{int(draft['number'])}"], timeout=15
            )
        )
        job = json.loads(
            subprocess.check_output(
                ["gh", "api", f"{repo}/actions/jobs/{int(check['provider_run_id'])}"], timeout=15
            )
        )
        if (
            not pull["draft"]
            or pull["merged"]
            or pull["head"]["sha"] != job["head_sha"]
            or job["head_sha"] != draft["head_sha"]
            or pull["base"]["sha"] != draft["base_sha"]
            or job["conclusion"] != {"PASS": "success", "FAIL": "failure"}[case["status"]]
            or job["html_url"] != check["url"]
            or pull["html_url"] != draft["url"]
        ):
            raise ValueError("Actual GitHub job or PR differs from the recorded CI evidence")
        urls.update([check["url"], draft["url"]])
        run_ids.add(report["run_id"])
    if len(urls) != 8 or len(run_ids) != 4:
        raise ValueError("CI matrix reused a Run, PR or Check")
    return sorted(urls)


def verify_control_plane_restart(report, publisher, manifest):
    phases = report["stages"]
    evaluation = report["evaluation"]
    terminal = phases["SUCCEEDED"]
    waiting = phases["WAIT_CI"]
    unknown = phases["PUBLISH_UNKNOWN"]
    result = evaluation["result"]
    draft = result["draft_pr"]
    (check,) = result["checks"]
    if (
        report["revision"] != manifest["revision"]
        or evaluation["revision"] != manifest["revision"]
        or report["run_id"] != publisher["run_id"]
        or evaluation["run_id"] != report["run_id"]
        or set(phases) != {"PUBLISH_UNKNOWN", "WAIT_CI", "SUCCEEDED"}
        or unknown["next_reconcile_at"] is None
        or unknown["pull_request"] is not None
        or waiting["ci_deadline_at"] is None
        or waiting["pull_request"] != publisher["pull_request"]
        or terminal["pull_request"] != publisher["pull_request"]
        or terminal["result_id"] != result["result_id"]
        or result["kind"] != "VERIFIED_DRAFT_PR"
        or evaluation["ci_input_origin"] != "actual_github"
        or draft["url"] != publisher["pull_request"]
        or draft["head_sha"] != publisher["head_sha"]
        or check["status"] != "PASS"
        or not check["origin_verified"]
        or check["tested_sha"] != draft["head_sha"]
        or unknown["notification_keys"]
        or waiting["notification_keys"]
        or terminal["notification_keys"] != [evaluation["notification"]["delivery_key"]]
        or report["after_replay_digest"] != terminal["after_digest"]
        or not re.fullmatch(r"publish-report:[a-f0-9]{64}", report["publisher_report_key"])
    ):
        raise ValueError("Control Plane restart, CI, or notification evidence does not match")
    for sample in phases.values():
        stopped_pid, started_pid = sample["pids"]
        if (
            stopped_pid <= 0
            or started_pid <= 0
            or stopped_pid == started_pid
            or sample["exit_code"] != -9
            or sample["operation_key"] != publisher["operation_key"]
            or not re.fullmatch(r"[a-f0-9]{64}", sample["before_digest"])
            or sample["before_digest"] != sample["after_digest"]
        ):
            raise ValueError("Control Plane process or durable snapshot evidence does not match")
    job = json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                f"repos/iwata-n/software-factory/actions/jobs/{int(check['provider_run_id'])}",
            ],
            timeout=15,
        )
    )
    if (
        job["conclusion"] != "success"
        or job["head_sha"] != draft["head_sha"]
        or job["html_url"] != check["url"]
    ):
        raise ValueError("GitHub CI job differs from the restored successful result")
    return [draft["url"], check["url"]]


def verify_current_pr_drift(report, manifest):
    result = report["original_result"]
    draft = result["draft_pr"]
    current = report["current_pr"]
    rerun = report["current_ci"]
    if (
        report["revision"] != manifest["revision"]
        or report["run_id"] != draft["run_id"]
        or report["pr_url"] != draft["url"]
        or result["kind"] != "VERIFIED_DRAFT_PR"
        or report["historical_head"] != draft["head_sha"]
        or report["current_head"] == draft["head_sha"]
        or report["merged"] is not False
        or report["result_digests"] != [digest(result)] * 3
        or current
        != {
            "status": "OBSERVED",
            "state": "CLOSED",
            "draft": True,
            "head_sha": report["current_head"],
            "base_sha": draft["base_sha"],
            "target_matches": False,
        }
    ):
        raise ValueError("Current PR drift differs from the immutable successful result")

    def github(path):
        return json.loads(
            subprocess.check_output(
                ["gh", "api", f"repos/iwata-n/software-factory/{path}"], timeout=15
            )
        )

    pull = github(f"pulls/{int(draft['number'])}")
    commit = github(f"git/commits/{current['head_sha']}")
    workflow = github(f"actions/runs/{int(rerun['id'])}")
    (check,) = result["checks"]
    original_job = github(f"actions/jobs/{int(check['provider_run_id'])}")
    if (
        pull["html_url"] != draft["url"]
        or pull["state"] != "closed"
        or pull["merged"]
        or not pull["draft"]
        or pull["base"]["sha"] != draft["base_sha"]
        or pull["head"]["sha"] != current["head_sha"]
        or [parent["sha"] for parent in commit["parents"]] != [draft["head_sha"]]
        or original_job["head_sha"] != draft["head_sha"]
        or original_job["conclusion"] != "success"
        or original_job["html_url"] != check["url"]
        or workflow["head_sha"] != current["head_sha"]
        or workflow["event"] != "pull_request"
        or workflow["status"] != "completed"
        or workflow["path"] != ".github/workflows/factory-evaluation.yml"
        or workflow["html_url"] != rerun["url"]
        or workflow["conclusion"] != rerun["conclusion"]
    ):
        raise ValueError("GitHub does not confirm PR head drift, closure and the CI rerun")
    return [draft["url"], check["url"], rerun["url"]]


def verify_model_egress(report, manifest, record):
    cases = record["observed"]
    runners = [
        observation
        for case in cases
        for observation in case["observations"]
        if isinstance(observation["observed"], dict)
        and "direct_public_private_metadata_routes" in observation["observed"]
    ]
    if (
        report["revision"] != manifest["revision"]
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", report["image"])
        or report["allowed_host"] != "chatgpt.com"
        or report["verified_tls"] not in {"TLSv1.2", "TLSv1.3"}
        or report["cross_host_tls"] != "DENIED"
        or report["blocked_connect"]
        != {
            "auth.openai.com:443": 403,
            "chatgpt.com:80": 403,
            "169.254.169.254:443": 403,
        }
        or len(runners) != 2
        or any(
            row["observed"]["direct_public_private_metadata_routes"] != "ENETUNREACH"
            for row in runners
        )
        or not any(
            row["observed"].get("direct_external_dns") == "DENIED"
            and row.get("image_digest") == report["image"]
            for row in runners
        )
        or sum("test_mixed_dns_answers_fail_closed" in case["test_case_id"] for case in cases) != 5
        or any(case["result"] != "PASS" for case in cases)
    ):
        raise ValueError("Live model egress evidence is incomplete or inconsistent")


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
        if ac not in {"C-05", "SF-18-AC06"}:
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
    matrix = directory / EXTERNAL_CRITERIA["SF-18-AC06"]
    if matrix.exists():
        urls = verify_live_ci_matrix(json.loads(matrix.read_text()), manifest)
        path, record = records["SF-18-AC06"]
        record.update(
            result="PASS",
            external_urls=urls,
            external_verification={"filename": matrix.name, "sha256": sha(matrix.read_bytes())},
            evidence_paths=list(dict.fromkeys([*record["evidence_paths"], matrix.name])),
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
    control_restart = directory / EXTERNAL_CRITERIA["SF-19-AC06"]
    if control_restart.exists():
        if not restart.exists():
            raise ValueError("Control Plane restart evidence requires the matching Publisher proof")
        urls = verify_control_plane_restart(
            json.loads(control_restart.read_text()), report, manifest
        )
        for criterion in ("SF-19-AC03", "SF-19-AC06"):
            path, record = records[criterion]
            record.update(
                result="PASS",
                external_urls=urls,
                external_verification={
                    "filename": control_restart.name,
                    "sha256": sha(control_restart.read_bytes()),
                },
                evidence_paths=list(
                    dict.fromkeys([*record["evidence_paths"], restart.name, control_restart.name])
                ),
            )
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    drift = directory / EXTERNAL_CRITERIA["SF-19-AC05"]
    if drift.exists():
        urls = verify_current_pr_drift(json.loads(drift.read_text()), manifest)
        path, record = records["SF-19-AC05"]
        record.update(
            result="PASS",
            external_urls=urls,
            external_verification={"filename": drift.name, "sha256": sha(drift.read_bytes())},
            evidence_paths=list(dict.fromkeys([*record["evidence_paths"], drift.name])),
        )
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    egress = directory / EXTERNAL_CRITERIA["SF-21-AC03"]
    if egress.exists():
        path, record = records["SF-21-AC03"]
        verify_model_egress(json.loads(egress.read_text()), manifest, record)
        record.update(
            result="PASS",
            external_verification={"filename": egress.name, "sha256": sha(egress.read_bytes())},
            evidence_paths=list(dict.fromkeys([*record["evidence_paths"], egress.name])),
        )
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    for ac, (_, record) in records.items():
        if record["result"] != "PASS":
            if allow_ci_pending and ac in EXTERNAL_CRITERIA and record["result"] == "未検証":
                pending.append(ac)
            else:
                raise ValueError(f"{ac}: {record['result']}")
    scenarios = audited_scenarios(records)
    scenario_result = (
        "PASS" if all(row["result"] == "PASS" for row in scenarios.values()) else "未検証"
    )
    (directory / "audited-scenarios.json").write_text(
        json.dumps(
            {
                "ac_id": SCENARIO_CRITERION,
                "revision": manifest["revision"],
                "code_digest": manifest["code_digest"],
                "result": scenario_result,
                "scenarios": scenarios,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    criteria = [
        {"ac_id": ac, "result": record["result"]} for ac, (_, record) in records.items()
    ] + [{"ac_id": SCENARIO_CRITERION, "result": scenario_result}]
    result = {
        "criteria": criteria,
        "code_digest": manifest["code_digest"],
        "external_pending": pending,
        **completion_status(
            {row["ac_id"] for row in criteria if row["result"] == "PASS"}, scenarios
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
