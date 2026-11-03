"""Read-only GitHub verification of the publication and its required check runs."""

import base64
import hashlib
import re

import httpx

from .contracts import GITHUB_ACTIONS_APP_ID

SHA = re.compile(r"[0-9a-f]{40}")
CONCLUSIONS = {"success": "PASS", "failure": "FAIL", "timed_out": "ERROR"}
HEAD_PROOF_STEP = "Verify candidate head"
PENDING = {"queued", "in_progress", "requested", "waiting", "pending"}


class CIReadError(Exception):
    """GitHub did not provide a usable snapshot of the fixed CI target."""


def check_status(check):
    status = check["status"]
    if status in PENDING:
        return "PENDING"
    if status == "completed":
        return CONCLUSIONS.get(check["conclusion"], "INCONCLUSIVE")
    return "INCONCLUSIVE"


class GitHubCIReader:
    def __init__(self, client, repository, repository_id, token):
        self.client = client
        self.repository = repository
        self.repository_id = repository_id
        self.headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get(self, path, **params):
        try:
            response = self.client.get(
                f"https://api.github.com/repos/{self.repository}{path}",
                params=params,
                headers=self.headers,
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json(), "next" in response.links
        except (httpx.HTTPError, ValueError):
            raise CIReadError("GITHUB_CI_UNAVAILABLE") from None

    @staticmethod
    def matches_side(side, draft, name, repository_id):
        repo = side["repo"]
        return (
            repo["id"] == repository_id
            and side["ref"] == draft[f"{name}_ref"]
            and side["sha"] == draft[f"{name}_sha"]
        )

    @classmethod
    def matches_pr(cls, value, draft, repository_id):
        return (
            str(value["id"]) == draft["external_id"]
            and value["number"] == draft["number"]
            and all(
                cls.matches_side(value[side], draft, side, repository_id)
                for side in ("base", "head")
            )
        )

    def verify_pr(self, draft):
        value, _ = self.get(f"/pulls/{draft['number']}")
        if (
            not self.matches_pr(value, draft, self.repository_id)
            or value["draft"] is not True
            or value["state"] != "open"
            or (draft["merge_sha"] and value["merge_commit_sha"] != draft["merge_sha"])
        ):
            raise CIReadError("GITHUB_CI_TARGET_CHANGED")

    def observe_pr(self, draft):
        """Read the same PR by provider identity, allowing its state and refs to change."""
        value, _ = self.get(f"/pulls/{draft['number']}")
        try:
            base = value["base"]
            head = value["head"]
            if (
                str(value["id"]) != draft["external_id"]
                or value["number"] != draft["number"]
                or base["repo"]["id"] != self.repository_id
            ):
                raise CIReadError("GITHUB_CI_TARGET_CHANGED")
            if (
                value["state"] not in {"open", "closed"}
                or type(value["draft"]) is not bool
                or type(value["merged"]) is not bool
                or not all(SHA.fullmatch(side["sha"]) for side in (head, base))
            ):
                raise CIReadError("GITHUB_CI_INVALID_RESPONSE")
            matches = self.matches_side(base, draft, "base", self.repository_id) and (
                head["repo"] is not None
                and self.matches_side(head, draft, "head", self.repository_id)
            )
            return {
                "status": "OBSERVED",
                "state": "MERGED" if value["merged"] else value["state"].upper(),
                "draft": value["draft"],
                "head_sha": head["sha"],
                "base_sha": base["sha"],
                "target_matches": matches,
            }
        except (KeyError, TypeError, AttributeError):
            raise CIReadError("GITHUB_CI_INVALID_RESPONSE") from None

    def read(self, context):
        try:
            return self.read_snapshot(context)
        except (KeyError, TypeError, AttributeError):
            raise CIReadError("GITHUB_CI_INVALID_RESPONSE") from None

    def read_snapshot(self, context):
        draft, target = context["draft"], context["target"]
        if (
            draft["origin_verified"] is not True
            or type(draft["number"]) is not int
            or draft["number"] <= 0
            or any(
                draft[key] != target[key]
                for key in ("repository_id", "candidate_id", "head_sha", "base_sha")
            )
        ):
            raise CIReadError("GITHUB_CI_TARGET_CHANGED")
        tested = draft["merge_sha"] or target["head_sha"]
        parents = [target["base_sha"], target["head_sha"]]
        if not all(SHA.fullmatch(sha) for sha in [tested, *parents]):
            raise CIReadError("GITHUB_CI_TARGET_CHANGED")
        self.verify_pr(draft)
        required = {
            (check["app_id"], check["check_name"]): check
            for check in context["required_ci"]
            if check["provider"] == "github"
        }
        native = {key: spec for key, spec in required.items() if key[0] != GITHUB_ACTIONS_APP_ID}
        actions = {
            key: spec
            for key, spec in required.items()
            if key[0] == GITHUB_ACTIONS_APP_ID and spec.get("github_workflow_id")
        }
        if native and draft["merge_sha"]:
            commit, _ = self.get(f"/git/commits/{tested}")
            if commit["sha"] != tested or [p["sha"] for p in commit["parents"]] != parents:
                raise CIReadError("GITHUB_CI_TARGET_CHANGED")
        manifest = context.get("protected_manifest", {})
        for path in {spec["github_workflow_path"] for spec in actions.values()}:
            for sha in (target["base_sha"], target["head_sha"]):
                self.verify_workflow(path, sha, manifest.get(path))
        checks = self.read_checks(draft, target, tested, parents, native)
        checks.update(self.read_checks(draft, target, target["head_sha"], [], actions))
        self.verify_pr(draft)
        return [checks[key] for key in sorted(checks)]

    def verify_workflow(self, path, head, expected_digest):
        if not expected_digest:
            raise CIReadError("GITHUB_CI_WORKFLOW_UNPINNED")
        workflow, _ = self.get(f"/contents/{path}", ref=head)
        if workflow["encoding"] != "base64":
            raise CIReadError("GITHUB_CI_INVALID_RESPONSE")
        try:
            data = base64.b64decode("".join(workflow["content"].splitlines()), validate=True)
        except ValueError:
            raise CIReadError("GITHUB_CI_INVALID_RESPONSE") from None
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise CIReadError("GITHUB_CI_WORKFLOW_CHANGED")

    def read_checks(self, draft, target, tested, parents, required):
        checks = {}
        if not required:
            return checks
        workflow_runs = {}
        for page in range(1, 51):
            data, more = self.get(
                f"/commits/{tested}/check-runs", filter="all", per_page=100, page=page
            )
            for check in data["check_runs"]:
                app_id = (check.get("app") or {}).get("id")
                if type(app_id) is not int or (app_id, check["name"]) not in required:
                    continue
                if check["head_sha"] != tested:
                    continue
                if tested == target["head_sha"] and not any(
                    self.matches_pr(pr, draft, self.repository_id) for pr in check["pull_requests"]
                ):
                    continue
                result = self.evidence(check, target, tested, parents)
                if app_id == GITHUB_ACTIONS_APP_ID:
                    workflow = self.workflow_evidence(
                        check, required[(app_id, check["name"])], draft, workflow_runs
                    )
                    if workflow is None:
                        continue
                    result.update(workflow)
                key = (result["app_id"], result["check_name"], result["run_sequence"])
                if key in checks and checks[key] != result:
                    raise CIReadError("GITHUB_CI_INVALID_RESPONSE")
                checks[key] = result
            if not more:
                break
        else:
            raise CIReadError("GITHUB_CI_PAGE_LIMIT")
        return checks

    def workflow_evidence(self, check, spec, draft, workflow_runs):
        check_id = check["id"]
        job, _ = self.get(f"/actions/jobs/{check_id}")
        run_id = job["run_id"]
        if type(run_id) is not int or run_id <= 0:
            raise CIReadError("GITHUB_CI_INVALID_RESPONSE")
        if run_id not in workflow_runs:
            workflow_runs[run_id], _ = self.get(f"/actions/runs/{run_id}")
        run = workflow_runs[run_id]
        if (
            run["id"] != run_id
            or run["workflow_id"] != spec["github_workflow_id"]
            or run["path"] != spec["github_workflow_path"]
            or run["event"] != "pull_request"
            or run["repository"]["id"] != self.repository_id
            or run["head_repository"]["id"] != self.repository_id
            or run["check_suite_id"] != check["check_suite"]["id"]
            or run["head_sha"] != draft["head_sha"]
            or not any(
                self.matches_pr(pr, draft, self.repository_id) for pr in run["pull_requests"]
            )
            or job["id"] != check_id
            or job["run_attempt"] != run["run_attempt"]
            or job["name"] != check["name"]
            or job["head_sha"] != draft["head_sha"]
            or job["check_run_url"]
            != (f"https://api.github.com/repos/{self.repository}/check-runs/{check_id}")
            or check_status(job) != check_status(check)
        ):
            return None
        proof = [step for step in job["steps"] if step["name"] == HEAD_PROOF_STEP]
        status = check_status(job)
        head_verified = (
            len(proof) == 1
            and proof[0]["status"] == "completed"
            and proof[0]["conclusion"] == "success"
        )
        if status != "PENDING" and not head_verified:
            status = "INCONCLUSIVE"
        return {
            "status": status,
            "provider_reported_sha": check["head_sha"],
            "tested_sha": draft["head_sha"] if head_verified else None,
            "github_workflow_id": run["workflow_id"],
            "github_workflow_path": run["path"],
            "github_event": run["event"],
            "github_workflow_run_id": run_id,
            "github_run_attempt": run["run_attempt"],
            "url": f"https://github.com/{self.repository}/actions/runs/{run_id}/job/{check_id}",
        }

    def evidence(self, check, target, tested, parents):
        check_id = check["id"]
        if type(check_id) is not int or check_id <= 0:
            raise CIReadError("GITHUB_CI_INVALID_RESPONSE")
        return {
            "id": f"github-check-{check_id}",
            "provider": "github",
            "provider_run_id": check_id,
            "run_sequence": check_id,
            "provider_started_at": check.get("started_at"),
            "provider_completed_at": check.get("completed_at"),
            "app_id": check["app"]["id"],
            "check_name": check["name"],
            "origin_verified": True,
            "candidate_id": target["candidate_id"],
            "repository_id": target["repository_id"],
            "head_sha": target["head_sha"],
            "base_sha": target["base_sha"],
            "tested_sha": tested,
            "merge_parents": parents if tested != target["head_sha"] else [],
            "status": check_status(check),
            "url": f"https://api.github.com/repos/{self.repository}/check-runs/{check_id}",
        }
