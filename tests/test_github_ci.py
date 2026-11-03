import base64
import hashlib

import httpx
import pytest

from software_factory.github_ci import CIReadError, GitHubCIReader

HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
REPO = "example/factory"
WORKFLOW = ".github/workflows/verify.yml"
WORKFLOW_DATA = b"fixture workflow"


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (None, ["PASS"]),
        ("other_workflow", []),
        ("other_base", []),
        ("old_attempt", []),
        ("missing_head_proof", ["INCONCLUSIVE"]),
        ("workflow_changed", None),
    ],
)
def test_actions_check_requires_pinned_pull_request_workflow_and_head_proof(fault, expected):
    target = {
        "candidate_id": "candidate",
        "repository_id": "repository",
        "head_sha": HEAD,
        "base_sha": BASE,
    }
    draft = {
        **target,
        "origin_verified": True,
        "external_id": "17",
        "number": 5,
        "head_ref": "candidate",
        "base_ref": "main",
        "merge_sha": MERGE,
    }
    pr = {
        "id": 17,
        "number": 5,
        "draft": True,
        "state": "open",
        "merge_commit_sha": MERGE,
        "head": {"repo": {"id": 42}, "sha": HEAD, "ref": "candidate"},
        "base": {"repo": {"id": 42}, "sha": BASE, "ref": "main"},
    }
    context = {
        "draft": draft,
        "target": target,
        "required_ci": [
            {
                "provider": "github",
                "app_id": 15368,
                "check_name": "verify",
                "github_workflow_id": 77,
                "github_workflow_path": WORKFLOW,
            }
        ],
        "protected_manifest": {
            WORKFLOW: hashlib.sha256(WORKFLOW_DATA).hexdigest(),
        },
    }
    checks = [
        {
            "id": check_id,
            "name": "verify",
            "head_sha": HEAD,
            "app": {"id": 15368},
            "pull_requests": [pr],
            "check_suite": {"id": check_id},
            "status": "completed",
            "conclusion": "success",
        }
        for check_id in (101, 102)
    ]
    jobs = {
        f"/actions/jobs/{check['id']}": {
            **check,
            "run_id": check["id"],
            "run_attempt": 1,
            "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{check['id']}",
            "steps": [
                {"name": "Verify candidate head", "status": "completed", "conclusion": "success"}
            ],
        }
        for check in checks
    }
    run = {
        "id": 101,
        "workflow_id": 77,
        "path": WORKFLOW,
        "event": "pull_request",
        "repository": {"id": 42},
        "head_repository": {"id": 42},
        "check_suite_id": 101,
        "head_sha": HEAD,
        "pull_requests": [pr],
        "run_attempt": 1,
    }
    if fault == "other_workflow":
        run["workflow_id"] = 89
    elif fault == "other_base":
        run["pull_requests"] = [pr | {"base": pr["base"] | {"sha": "d" * 40}}]
    elif fault == "old_attempt":
        run["run_attempt"] = 2
    elif fault == "missing_head_proof":
        jobs["/actions/jobs/101"]["steps"] = []
    elif fault == "workflow_changed":
        context["protected_manifest"][WORKFLOW] = "0" * 64
    responses = {
        "/pulls/5": pr,
        f"/contents/{WORKFLOW}": {
            "encoding": "base64",
            "content": base64.b64encode(WORKFLOW_DATA).decode(),
        },
        f"/commits/{HEAD}/check-runs": {"check_runs": checks},
        **jobs,
        "/actions/runs/101": run,
        "/actions/runs/102": run | {"id": 102, "check_suite_id": 102, "event": "push"},
    }

    def respond(req):
        assert req.method == "GET" and req.url.host == "api.github.com"
        path = req.url.path.removeprefix(f"/repos/{REPO}")
        if path.startswith("/contents/"):
            assert req.url.params["ref"] in {BASE, HEAD}
        return httpx.Response(200, json=responses[path])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        reader = GitHubCIReader(client, REPO, 42, "test")
        if expected is None:
            with pytest.raises(CIReadError, match="GITHUB_CI_WORKFLOW_CHANGED"):
                reader.read(context)
            return
        results = reader.read(context)
        assert [item["status"] for item in results] == expected
        if results:
            assert results[0]["provider_run_id"] == 101
            assert results[0]["provider_reported_sha"] == HEAD
            assert results[0]["tested_sha"] == (HEAD if expected == ["PASS"] else None)
