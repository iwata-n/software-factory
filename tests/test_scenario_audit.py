from pathlib import Path

import pytest


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_acceptance

    return verify_acceptance


def test_audited_scenarios_keep_missing_external_evidence_pending(verifier):
    required = {ac for refs in verifier.acceptance_scenarios().values() for ac in refs}
    records = {
        ac: (
            Path(ac[:5]) / f"{ac}.json",
            {
                "result": "PASS",
                "test_case_ids": ["executed-test"],
                "evidence_paths": ["junit.xml"],
                "external_urls": [],
            },
        )
        for ac in required
    }
    missing = "SF-19-AC01"
    records[missing][1]["result"] = "未検証"
    matrix = verifier.audited_scenarios(records)
    assert matrix["AT-08"]["result"] == "未検証"
    assert not verifier.completion_status(required - {missing}, matrix)["mvp_accepted"]
    assert all(row["result"] == "PASS" for name, row in matrix.items() if name != "AT-08")

    records[missing][1].update(
        result="PASS",
        evidence_paths=["junit.xml", "sf19-restart-verification.json"],
        external_urls=["https://github.com/iwata-n/software-factory/pull/1"],
    )
    matrix = verifier.audited_scenarios(records)
    assert all(row["result"] == "PASS" for row in matrix.values())
    assert matrix["AT-08"]["criteria"][missing]["evidence_paths"][-1].endswith(
        "sf19-restart-verification.json"
    )
    assert not verifier.completion_status(required, matrix)["mvp_accepted"]


def test_audited_scenarios_reject_self_reference(verifier, monkeypatch):
    scenarios = verifier.acceptance_scenarios()
    scenarios["AT-01"] = [verifier.SCENARIO_CRITERION]
    monkeypatch.setattr(verifier, "acceptance_scenarios", lambda: scenarios)
    with pytest.raises(ValueError, match="circular"):
        verifier.audited_scenarios({})
