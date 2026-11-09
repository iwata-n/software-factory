import json
from types import SimpleNamespace

import pytest

from scripts import acceptance_reporter


@pytest.fixture
def reporter(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ACCEPTANCE_OUTPUT", str(tmp_path / "evidence"))
    monkeypatch.setattr(
        acceptance_reporter,
        "source_manifest",
        lambda: {"revision": "fixture", "code_digest": "a" * 64},
    )
    monkeypatch.setattr(acceptance_reporter, "required_criteria", lambda: ["SF-02-AC01"])
    return acceptance_reporter.Reporter()


def worker_output(reporter, cases, expected=None):
    return SimpleNamespace(
        workeroutput={
            "factory_acceptance": {
                "manifest": reporter.manifest,
                "expected": expected or {"case_a": ["SF-02-AC01"], "case_b": ["SF-02-AC01"]},
                "cases": {
                    name: {
                        "test_case_id": name,
                        "result": result,
                        "observations": [{"expected": "executed", "observed": result}],
                    }
                    for name, result in cases.items()
                },
                "postgres_version": "PostgreSQL 17.6 fixture",
            }
        }
    )


@pytest.mark.parametrize(
    ("second_cases", "result"),
    [({"case_b": "PASS"}, "PASS"), ({"case_b": "FAIL"}, "FAIL"), ({}, "未検証")],
)
def test_controller_keeps_every_worker_result_and_never_passes_missing_cases(
    reporter, second_cases, result
):
    reporter.pytest_testnodedown(worker_output(reporter, {"case_a": "PASS"}), None)
    reporter.pytest_testnodedown(worker_output(reporter, second_cases), None)
    reporter.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace()))
    record = json.loads((reporter.output / "SF-02/SF-02-AC01.json").read_text())
    assert record["result"] == result
    assert record["test_case_ids"] == ["case_a", "case_b"]
    assert {case["test_case_id"] for case in record["observed"]} == {"case_a", "case_b"}
    assert record["environment"]["postgresql"] == "PostgreSQL 17.6 fixture"


def test_worker_transfers_results_without_writing_shared_files(reporter):
    reporter.enabled = True
    reporter.cases = {"case_a": {"result": "PASS"}}
    config = SimpleNamespace(workerinput={}, workeroutput={})
    reporter.pytest_sessionfinish(SimpleNamespace(config=config))
    assert config.workeroutput["factory_acceptance"]["cases"] == reporter.cases
    assert not reporter.output.exists()


def test_worker_restart_does_not_replace_a_failure_with_a_pass(reporter):
    reporter.pytest_testnodedown(worker_output(reporter, {"case_a": "FAIL"}), None)
    reporter.pytest_testnodedown(worker_output(reporter, {"case_a": "PASS"}), None)
    assert reporter.cases["case_a"]["result"] == "FAIL"


@pytest.mark.parametrize("field", ["manifest", "expected", "postgres_version"])
def test_workers_with_different_inputs_or_environment_cannot_merge(reporter, field):
    reporter.pytest_testnodedown(worker_output(reporter, {"case_a": "PASS"}), None)
    node = worker_output(reporter, {"case_b": "PASS"})
    payload = node.workeroutput["factory_acceptance"]
    payload[field] = {"different": "input"} if field != "postgres_version" else "PostgreSQL 16"
    with pytest.raises(pytest.UsageError, match="different"):
        reporter.pytest_testnodedown(node, None)


def test_timing_includes_fixtures_and_teardown_failure_is_not_lost(reporter):
    reporter.enabled = True
    item = SimpleNamespace(nodeid="case_a")
    for phase, duration, failed in [("setup", 2, False), ("call", 3, False), ("teardown", 4, True)]:
        report = SimpleNamespace(when=phase, duration=duration, failed=failed, skipped=False)
        hook = reporter.pytest_runtest_makereport(item, None)
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(SimpleNamespace(get_result=lambda report=report: report))
    case = reporter.cases["case_a"]
    assert case["result"] == "FAIL"
    assert case["duration_seconds"] == 9
    assert case["phase_durations_seconds"] == {"setup": 2, "call": 3, "teardown": 4}


def test_unit_only_run_leaves_acceptance_evidence_untouched(reporter):
    reporter.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace()))
    assert not reporter.output.exists()


def test_parallel_report_preserves_pending_criteria_and_scenario_results(reporter, monkeypatch):
    monkeypatch.setattr(acceptance_reporter, "task_criteria", lambda: ["SF-02-AC01", "SF-20-AC01"])
    monkeypatch.setattr(
        acceptance_reporter,
        "acceptance_scenarios",
        lambda: {"AT-01": ["SF-02-AC01", "SF-20-AC01"]},
    )
    expected = {"case_a": ["SF-02-AC01"], "case_b": ["SF-20-AC01"]}
    for name in expected:
        reporter.pytest_testnodedown(worker_output(reporter, {name: "PASS"}, expected), None)
    reporter.pytest_sessionfinish(SimpleNamespace(config=SimpleNamespace()))
    pending = json.loads((reporter.output / "pending-criteria.json").read_text())
    assert pending["criteria"] == [
        {"ac_id": "SF-20-AC01", "result": "未検証", "automated_result": "PASS", "cases": 1}
    ]
    scenarios = json.loads((reporter.output / "acceptance-scenarios.json").read_text())
    assert scenarios["scenarios"]["AT-01"]["result"] == "未検証"
