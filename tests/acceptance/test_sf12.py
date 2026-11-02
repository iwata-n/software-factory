from itertools import permutations, product

import pytest

from software_factory.quality import final_gate, local_gate

TARGET = {
    "candidate_id": "candidate-1",
    "head_sha": "a" * 40,
    "base_sha": "b" * 40,
    "source_digest": "c" * 64,
    "spec_hash": "d" * 64,
    "profile_hash": "e" * 64,
    "image_digest": "sha256:" + "f" * 64,
    "policy_hash": "1" * 64,
    "repository_id": "repository-1",
}


def observation(kind, key, status="PASS", **changes):
    return {
        "id": kind + key,
        "candidate_id": TARGET["candidate_id"],
        "kind": kind,
        "key": key,
        "status": status,
        "eligible": True,
        "target": {
            k: TARGET[k]
            for k in (
                "candidate_id",
                "head_sha",
                "source_digest",
                "spec_hash",
                "profile_hash",
                "image_digest",
            )
        },
        "attempt_id": kind + "-attempt",
        "attempt_no": 1,
        "origin": "managed_verifier" if kind == "VERIFY" else "managed_reviewer",
        **changes,
    }


@pytest.mark.ac("SF-12-AC01", "SF-12-AC02")
@pytest.mark.parametrize(
    "verify,review,blocking,mismatch",
    list(
        product(
            ["PASS", "FAIL", "ERROR", "INCONCLUSIVE"],
            ["PASS", "FAIL", "ERROR", "INCONCLUSIVE"],
            [False, True],
            [False, True],
        )
    ),
)
def test_local_gate_complete_priority_table(verify, review, blocking, mismatch, evidence):
    checks = [
        observation("VERIFY", "unit", verify, eligible=not mismatch),
        observation("REVIEW", "AC-1", review),
    ]
    findings = (
        [
            {
                "id": "finding-1",
                "attempt_id": "REVIEW-attempt",
                "candidate_id": TARGET["candidate_id"],
                "eligible": True,
                "origin": "managed_reviewer",
                "severity": "high",
            }
        ]
        if blocking
        else []
    )
    result = local_gate(TARGET, ["unit"], ["AC-1"], checks, findings)
    expected = (
        "INCONCLUSIVE"
        if mismatch
        else "ERROR"
        if "ERROR" in (verify, review)
        else "FAIL"
        if blocking or "FAIL" in (verify, review)
        else "INCONCLUSIVE"
        if "INCONCLUSIVE" in (verify, review)
        else "PASS"
    )
    assert result.status == expected
    evidence(
        "exhaustive mismatch > ERROR > confirmed FAIL > missing/unknown > all PASS",
        {
            "verify": verify,
            "review": review,
            "blocking": blocking,
            "mismatch": mismatch,
            "expected": expected,
            "actual": result.status,
            "digest": result.evidence_digest,
        },
    )


@pytest.mark.ac("SF-12-AC01", "SF-12-AC02", "SF-12-AC03")
@pytest.mark.parametrize(
    "status,expected",
    [
        ("PASS", "INCONCLUSIVE"),
        ("FAIL", "FAIL"),
        ("ERROR", "ERROR"),
        ("INCONCLUSIVE", "INCONCLUSIVE"),
    ],
)
def test_missing_does_not_hide_confirmed_fail_or_error(status, expected, evidence):
    records = [observation("VERIFY", "unit", status)]
    done = local_gate(TARGET, ["unit"], ["AC-1"], records, [])
    waiting = local_gate(TARGET, ["unit"], ["AC-1"], records, [], jobs_pending=True)
    assert done.status == expected and waiting.status == "WAIT"
    evidence(
        "required pending job waits; once terminal, missing evidence obeys the defined priority",
        {"known": status, "terminal": done.status, "pending": waiting.status},
    )


@pytest.mark.ac("SF-12-AC01", "SF-12-AC04")
def test_optional_checks_order_and_previous_attempts_cannot_change_gate(evidence):
    records = [
        observation("VERIFY", "unit", "FAIL", id="old", attempt_no=1),
        observation("VERIFY", "unit", id="new", attempt_no=2),
        observation("REVIEW", "AC-1"),
        observation("VERIFY", "optional", "ERROR", id="optional", eligible=False),
    ]
    decisions = [
        local_gate(TARGET, ["unit"], ["AC-1"], list(order), []) for order in permutations(records)
    ]
    assert all(d.status == "PASS" for d in decisions)
    assert len({d.evidence_digest for d in decisions}) == 1
    assert decisions[0].evidence_ids == ("REVIEWAC-1", "new")
    evidence(
        "mandatory latest attempts only; all 24 arrival orders have one evidence set/hash",
        {"permutations": len(decisions), "decision": decisions[0].__dict__},
    )


def final_inputs():
    local = {
        "id": "local-gate-1",
        "status": "PASS",
        "target": TARGET.copy(),
        "evidence_digest": "2" * 64,
    }
    draft = {
        "id": "draft-pr-1",
        "origin_verified": True,
        "draft": True,
        "state": "OPEN",
        **{k: TARGET[k] for k in ("candidate_id", "repository_id", "base_sha", "head_sha")},
        "merge_sha": "3" * 40,
    }
    required = [{"provider": "github", "app_id": 123, "check_name": "factory-tests"}]
    ci = [
        {
            "id": "ci-1",
            **required[0],
            "origin_verified": True,
            "status": "PASS",
            "base_sha": TARGET["base_sha"],
            "head_sha": TARGET["head_sha"],
            "tested_sha": TARGET["head_sha"],
            "merge_parents": [],
        }
    ]
    return local, draft, required, ci


@pytest.mark.ac("SF-12-AC06")
@pytest.mark.parametrize("mode", ["head", "merge"])
def test_final_pass_requires_exact_head_or_verified_merge_tuple(mode, evidence):
    local, draft, required, ci = final_inputs()
    if mode == "merge":
        ci[0]["tested_sha"] = draft["merge_sha"]
        ci[0]["merge_parents"] = [TARGET["base_sha"], TARGET["head_sha"]]
    result = final_gate(TARGET, local, draft, required, ci, now=10, deadline=20)
    assert result.status == "PASS"
    evidence(
        "final PASS requires exact local/draft/CI target and verified merge relationship",
        {"mode": mode, "decision": result.__dict__, "ci": ci, "draft": draft},
    )


@pytest.mark.ac("SF-12-AC05", "SF-12-AC06")
@pytest.mark.parametrize(
    "fault",
    [
        "local_target",
        "local_fail",
        "draft_head",
        "draft_base",
        "draft_candidate",
        "not_draft",
        "draft_closed",
        "draft_origin",
        "ci_head",
        "ci_base",
        "ci_tested",
        "merge_sha",
        "merge_parents",
        "ci_origin",
        "app",
        "provider",
        "check_name",
    ],
)
def test_final_mismatch_never_succeeds(fault, evidence):
    local, draft, required, ci = final_inputs()
    if fault == "local_target":
        local["target"]["policy_hash"] = "0" * 64
    elif fault == "local_fail":
        local["status"] = "FAIL"
    elif fault.startswith("draft_") and fault[6:] in {"head", "base", "candidate"}:
        draft[{"head": "head_sha", "base": "base_sha", "candidate": "candidate_id"}[fault[6:]]] = (
            "wrong"
        )
    elif fault == "not_draft":
        draft["draft"] = False
    elif fault == "draft_closed":
        draft["state"] = "CLOSED"
    elif fault == "draft_origin":
        draft["origin_verified"] = False
    elif fault == "ci_origin":
        ci[0]["origin_verified"] = False
    elif fault in {"ci_head", "ci_base", "ci_tested"}:
        ci[0][{"ci_head": "head_sha", "ci_base": "base_sha", "ci_tested": "tested_sha"}[fault]] = (
            "0" * 40
        )
    elif fault.startswith("merge_"):
        ci[0]["tested_sha"] = draft["merge_sha"]
        ci[0]["merge_parents"] = [TARGET["base_sha"], TARGET["head_sha"]]
        if fault == "merge_sha":
            ci[0]["tested_sha"] = "0" * 40
        else:
            ci[0]["merge_parents"] = ["0" * 40, TARGET["head_sha"]]
    elif fault == "app":
        ci[0]["app_id"] = 999
    elif fault == "provider":
        ci[0]["provider"] = "untrusted"
    else:
        ci[0]["check_name"] = "another-check"
    result = final_gate(TARGET, local, draft, required, ci, now=10, deadline=20)
    assert result.status in {"INCONCLUSIVE", "WAIT"}
    evidence(
        "mismatched approval/PR/provider identity or tested head/merge never passes",
        {"fault": fault, "status": result.status, "reason": result.reason},
    )


@pytest.mark.ac("SF-12-AC02", "SF-12-AC03", "SF-12-AC06")
@pytest.mark.parametrize(
    "status,expected",
    [
        ("PASS", "PASS"),
        ("FAIL", "FAIL"),
        ("ERROR", "ERROR"),
        ("INCONCLUSIVE", "INCONCLUSIVE"),
        ("PENDING", "WAIT"),
    ],
)
def test_final_states_and_deadline(status, expected, evidence):
    local, draft, required, ci = final_inputs()
    ci[0]["status"] = status
    before = final_gate(TARGET, local, draft, required, ci, now=19.999, deadline=20)
    at = final_gate(TARGET, local, draft, required, ci, now=20, deadline=20)
    assert before.status == expected
    assert at.status == "INCONCLUSIVE" and at.reason == "CI_TIMEOUT"
    evidence(
        "CI verdicts stay distinct and the persisted deadline is a strict upper boundary",
        {"input": status, "before": before.__dict__, "at_deadline": at.__dict__},
    )


@pytest.mark.ac("SF-12-AC04", "SF-12-AC06")
def test_final_ci_run_selection_is_order_independent_and_never_reuses_old_pass(evidence):
    local, draft, required, ci = final_inputs()
    old = ci[0] | {"id": "old-ci", "run_sequence": 1, "status": "PASS"}
    new = ci[0] | {"id": "new-ci", "run_sequence": 2, "status": "FAIL"}
    results = [
        final_gate(TARGET, local, draft, required, list(order), now=10, deadline=20)
        for order in permutations([old, new])
    ]
    assert all(r.status == "FAIL" for r in results)
    assert len({r.evidence_digest for r in results}) == 1
    ambiguous = [
        final_gate(TARGET, local, draft, required, list(order), now=10, deadline=20)
        for order in permutations([old, new | {"run_sequence": 1}])
    ]
    assert all(r.status == "INCONCLUSIVE" for r in ambiguous)
    assert len({r.evidence_digest for r in ambiguous}) == 1
    assert ambiguous[0].evidence_digest != results[0].evidence_digest
    duplicate = final_gate(TARGET, local, draft, required, [new, new], now=10, deadline=20)
    assert duplicate.status == "FAIL" and duplicate.evidence_digest == results[0].evidence_digest
    evidence(
        "new CI run supersedes an old PASS; ambiguous same-generation runs cannot pass",
        {"newest": results[0].status, "ambiguous": ambiguous[0].status},
    )
