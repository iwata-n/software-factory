"""Summarize every real Codex evaluation run, including incomplete and failed runs."""

import argparse
import json
from datetime import datetime
from pathlib import Path

TASKS = {"EVAL-01", "EVAL-02", "EVAL-03"}


def read(directory, filename):
    path = directory / filename
    return json.loads(path.read_text()) if path.is_file() else None


def row(directory):
    report = read(directory, "evaluation.json")
    if report is None:
        return {"evidence": str(directory), "result": "NO_REPORT", "accepted": False}
    run = read(directory, "terminal-run.json") or read(directory, "run.json") or {}
    graph = read(directory, "terminal-graph.json") or read(directory, "graph.json") or {}
    publication = report.get("publication", {})
    result = publication.get("result", {})
    draft = result.get("draft_pr", {})
    notification = publication.get("notification", {})
    current_pr = publication.get("current_pr_after_restart", {})
    checks = result.get("checks", [])
    head = report.get("candidate_head_sha")
    repairs = sum(job["kind"] == "REPAIR" for job in graph.get("jobs", [])) if graph else None
    code_edits = report.get("human_code_edits")
    verified = (
        report["result"] == "PASS"
        and report.get("implementation_unchanged") is True
        and run.get("snapshot", {}).get("agent_adapter") == "codex_cli"
        and publication.get("ci_input_origin") == "actual_github"
        and result.get("kind") == "VERIFIED_DRAFT_PR"
        and result.get("candidate_id") == report.get("candidate_id")
        and head
        and draft.get("head_sha") == head
        and draft.get("draft") is True
        and draft.get("state") == "OPEN"
        and "merge_sha" in draft
        and draft["merge_sha"] is None
        and current_pr.get("target_matches") is True
        and bool(checks)
        and all(check["status"] == "PASS" and check["tested_sha"] == head for check in checks)
        and code_edits == 0
        and repairs is not None
        and repairs <= 2
    )
    elapsed = datetime.fromisoformat(report["finished_at"]) - datetime.fromisoformat(
        report["started_at"]
    )
    return {
        "evidence": str(directory),
        "task": report["evaluation_task"],
        "revision": report["implementation"]["base_sha"],
        "input_digest": report["implementation"]["code_digest"],
        "engine_manifest_hash": report["engine_manifest_hash"],
        "fixture_base_sha": report.get("base_sha"),
        "run_id": report.get("run_id"),
        "result": report["result"],
        "error": report.get("error"),
        "started_at": report["started_at"],
        "finished_at": report["finished_at"],
        "duration_seconds": elapsed.total_seconds(),
        "candidate_sha": head,
        "pr_url": draft.get("url"),
        "ci_urls": [check["url"] for check in checks],
        "notification_state": notification.get("state"),
        "notification_message_id": notification.get("message_id"),
        "spent_microusd": report.get("spent_microusd", run.get("spent_microusd")),
        "human_code_edits": code_edits,
        "repairs": repairs,
        "stop_reason": run.get("stop_reason"),
        "verified_draft": bool(verified),
        "accepted": bool(
            verified and notification.get("state") == "SENT" and notification.get("message_id")
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    args = parser.parse_args()
    rows = [row(directory) for directory in args.directories]
    identifiers = [record.get("run_id") for record in rows]
    identity = {
        tuple(record.get(key) for key in ("revision", "input_digest", "engine_manifest_hash"))
        for record in rows
    }
    tasks = {record.get("task") for record in rows}
    complete_batch = (
        len(rows) == len(TASKS)
        and tasks == TASKS
        and None not in identifiers
        and len(set(identifiers)) == len(TASKS)
        and len(identity) == 1
    )
    accepted = sum(record["accepted"] for record in rows)
    verified = sum(record.get("verified_draft", False) for record in rows)
    print(
        json.dumps(
            {
                "evidence_slots": len(rows),
                "run_count": sum(identifier is not None for identifier in identifiers),
                "verified_drafts": verified,
                "accepted_runs": accepted,
                "missing_tasks": sorted(TASKS - tasks),
                "evaluation_batch_accepted": complete_batch and accepted == len(TASKS),
                "runs": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
