import json

from scripts.summarize_codex_batch import row, summarize


def test_incomplete_runs_and_faults_do_not_disappear_from_batch(tmp_path):
    missing = tmp_path / "missing"
    (tmp_path / "evaluation.json").write_text(
        json.dumps(
            {
                "evaluation_task": "EVAL-02",
                "run_id": "incomplete-1",
                "result": "IN_PROGRESS",
                "started_at": "2026-11-03T13:00:00+00:00",
            }
        )
    )
    (tmp_path / "run.json").write_text(
        json.dumps({"status": "CANCELLED", "stop_reason": "SPEC_CHANGE_REQUIRED"})
    )
    (tmp_path / "events.json").write_text(
        json.dumps({"events": [{"kind": "USER_CANCEL_REASON", "revision": 5}]})
    )
    fault = {"evidence": "connection-loss.json", "report": {"result": "PASS"}}
    history = {"run_id": "old-run", "accepted": False, "result": "FAIL"}
    result = summarize([row(tmp_path), row(missing)], [fault], [history])
    incomplete, absent = result["runs"]
    assert incomplete["duration_seconds"] is None
    assert incomplete["cancelled"] and incomplete["spec_change_required"]
    assert incomplete["interventions"] == [{"kind": "USER_CANCEL_REASON", "revision": 5}]
    assert absent["result"] == "NO_REPORT"
    assert result["run_count"] == 1
    assert result["success_rate"] == {"numerator": 0, "denominator": 2}
    assert result["period"] == {
        "started_at": "2026-11-03T13:00:00+00:00",
        "finished_at": None,
        "complete": False,
    }
    assert result["historical_runs"] == [history] and result["fault_injection"] == [fault]
    assert not result["evaluation_batch_accepted"]


def test_verified_drafts_without_delivery_are_reported_but_do_not_pass():
    rows = [
        {
            "task": f"EVAL-0{index}",
            "run_id": f"run-{index}",
            "revision": "revision",
            "input_digest": "input",
            "engine_manifest_hash": "manifest",
            "fixture_base_sha": "base",
            "verified_draft": True,
            "accepted": False,
        }
        for index in (1, 2, 3)
    ]
    result = summarize(rows)
    assert result["verified_drafts"] == 3
    assert result["success_rate"] == {"numerator": 0, "denominator": 3}
    assert not result["evaluation_batch_accepted"]
