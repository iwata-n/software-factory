import importlib
from pathlib import Path

import pytest


@pytest.fixture
def publication_certificate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    collector = importlib.import_module("collect_publication_evidence")
    base, first, second, human = (letter * 40 for letter in ("b", "a", "d", "e"))
    repository, ref = "owner/repo", "codex/factory/run-1"
    actual = {
        "number": 1,
        "html_url": "https://github.com/owner/repo/pull/1",
        "draft": True,
        "state": "open",
        "merged": False,
        "base": {"repo": {"full_name": repository}, "sha": base},
        "head": {"repo": {"full_name": repository}, "ref": ref, "sha": human},
    }

    def published(head):
        return {
            "pull_request": {"number": 1, "head_sha": head},
            "operations": [
                {
                    "state": "SUCCEEDED",
                    "target": {"candidate": {"head_sha": head, "tree_sha": "f" * 40}},
                }
            ],
        }

    report = {
        "evaluation": "SF17_PUBLICATION_TRANSPORT",
        "publication_origin": "actual_github",
        "code_digest": "c" * 64,
        "base_sha": base,
        "repository": repository,
        "base_ref": "main",
        "run_id": "run-1",
        "human_fixture_sha": human,
        "first_publication": published(first),
        "second_publication": published(second),
        "final_publication": {"pull_request": {"number": 1}, "operations": [{"state": "BLOCKED"}]},
        "final_run": {"status": "BLOCKED", "stop_reason": "REMOTE_HEAD_CONFLICT"},
        "provider_requests": [
            {"method": "GET", "resource": "/repos/owner/repo"},
            {"method": "POST", "resource": "/repos/owner/repo/pulls"},
        ],
    }

    def api(repo, suffix):
        assert repo == repository
        if suffix == "pulls/1":
            return actual
        if suffix.startswith("pulls?"):
            return [actual]
        if suffix.startswith("git/ref/heads/"):
            return {"object": {"sha": base if suffix.endswith("main") else human}}
        if suffix.startswith("git/commits/"):
            head = suffix.rsplit("/", 1)[1]
            return {
                "tree": {"sha": "f" * 40},
                "parents": [{"sha": {first: base, second: first, human: second}[head]}],
            }
        raise AssertionError("unexpected verification endpoint")

    monkeypatch.setattr(collector, "api", api)
    monkeypatch.setattr(
        collector, "source_manifest", lambda: {"code_digest": "c" * 64, "base_sha": base}
    )
    return collector, report, actual


def test_external_certificate_checks_the_primary_pr_ref_and_commit_chain(publication_certificate):
    collector, report, _ = publication_certificate
    certificate = collector.verify("owner/repo", report)
    assert certificate["result"] == "PASS" and certificate["remote_head"] == "e" * 40
    assert certificate["criteria"] == ["SF-17-AC01", "SF-17-AC02", "SF-17-AC03"]


@pytest.mark.parametrize(
    "change", ["implementation", "head", "draft", "other_write", "duplicate_create"]
)
def test_external_certificate_cannot_promote_mismatched_or_unsafe_observations(
    publication_certificate, change
):
    collector, report, actual = publication_certificate
    if change == "implementation":
        report["code_digest"] = "0" * 64
    elif change == "head":
        actual["head"]["sha"] = "0" * 40
    elif change == "draft":
        actual["draft"] = False
    elif change == "other_write":
        report["provider_requests"].append(
            {"method": "POST", "resource": "/repos/owner/repo/deployments"}
        )
    else:
        report["provider_requests"].append(
            {"method": "POST", "resource": "/repos/owner/repo/pulls"}
        )
    with pytest.raises(ValueError):
        collector.verify("owner/repo", report)
