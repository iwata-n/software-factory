"""Summarize every real Codex evaluation run, including incomplete and failed runs."""

import argparse
import json
from datetime import datetime
from pathlib import Path

TASK_TYPES = {"EVAL-01": "empty_state", "EVAL-02": "input_validation", "EVAL-03": "formatting"}
TASKS = set(TASK_TYPES)
INTERVENTIONS = {"APPROVAL_DECIDED", "USER_CANCEL_REASON", "BLOCK_RESOLVED"}


def read(directory, filename):
    path = directory / filename
    return json.loads(path.read_text()) if path.is_file() else None


def row(directory):
    report = read(directory, "evaluation.json")
    if report is None:
        return {"evidence": str(directory), "result": "NO_REPORT", "accepted": False}
    run = read(directory, "terminal-run.json") or read(directory, "run.json") or {}
    graph = read(directory, "terminal-graph.json") or read(directory, "graph.json") or {}
    publication = report.get("publication") or {}
    result = publication.get("result", {})
    draft = result.get("draft_pr", {})
    notification = publication.get("notification", {})
    current_pr = publication.get("current_pr_after_restart", {})
    checks = result.get("checks", [])
    head = report.get("candidate_head_sha")
    repairs = sum(job["kind"] == "REPAIR" for job in graph.get("jobs", [])) if graph else None
    code_edits = report.get("human_code_edits")
    verified = (
        report.get("result") == "PASS"
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
    started_at, finished_at = report.get("started_at"), report.get("finished_at")
    elapsed = (
        (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
        if started_at and finished_at
        else None
    )
    history = read(directory, "terminal-events.json") or read(directory, "events.json") or {}
    events = history.get("events", [])
    status, reason = run.get("status"), run.get("stop_reason")
    task = report.get("evaluation_task")
    implementation = report.get("implementation") or {}
    return {
        "evidence": str(directory),
        "task": task,
        "task_type": TASK_TYPES.get(task),
        "revision": implementation.get("base_sha"),
        "input_digest": implementation.get("code_digest"),
        "engine_manifest_hash": report.get("engine_manifest_hash"),
        "fixture_base_sha": report.get("base_sha"),
        "run_id": report.get("run_id"),
        "result": report.get("result", "INCOMPLETE"),
        "run_status": status,
        "error": report.get("error"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": elapsed,
        "cancelled": status == "CANCELLED" if status else None,
        "spec_change_required": (
            reason == "SPEC_CHANGE_REQUIRED"
            or any(event["kind"] == "SPEC_CHANGE_REQUESTED" for event in events)
        )
        if status
        else None,
        "interventions": [
            {"kind": event["kind"], "revision": event["revision"]}
            for event in events
            if event["kind"] in INTERVENTIONS
        ]
        if history
        else None,
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
            verified
            and report.get("notifications_sent") == 1
            and report.get("slack_history_confirmed") is True
            and notification.get("state") == "SENT"
            and notification.get("message_id")
        ),
    }


def summarize(rows, faults=(), history=()):
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
        and all(all(parts) for parts in identity)
        and all(record.get("fixture_base_sha") for record in rows)
    )
    accepted = sum(record["accepted"] for record in rows)
    verified = sum(record.get("verified_draft", False) for record in rows)
    starts = [
        datetime.fromisoformat(record["started_at"]) for record in rows if record.get("started_at")
    ]
    finishes = [
        datetime.fromisoformat(record["finished_at"])
        for record in rows
        if record.get("finished_at")
    ]
    return {
        "evidence_slots": len(rows),
        "run_count": len({identifier for identifier in identifiers if identifier is not None}),
        "verified_drafts": verified,
        "accepted_runs": accepted,
        "success_rate": {"numerator": accepted, "denominator": len(rows)},
        "period": {
            "started_at": min(starts).isoformat() if starts else None,
            "finished_at": max(finishes).isoformat() if finishes else None,
            "complete": len(starts) == len(finishes) == len(rows),
        },
        "missing_tasks": sorted(TASKS - tasks),
        "evaluation_batch_accepted": complete_batch and accepted == len(TASKS),
        "runs": rows,
        "historical_runs": list(history),
        "fault_injection": list(faults),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--historical-run", type=Path, action="append", default=[])
    parser.add_argument("--fault-evidence", type=Path, action="append", default=[])
    args = parser.parse_args()
    faults = [
        {
            "evidence": str(path),
            "report": json.loads(path.read_text()) if path.is_file() else None,
        }
        for path in args.fault_evidence
    ]
    summary = summarize(
        [row(directory) for directory in args.directories],
        faults,
        [row(directory) for directory in args.historical_run],
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
