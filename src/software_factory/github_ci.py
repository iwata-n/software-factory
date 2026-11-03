"""Read-only GitHub verification of the publication and its required check runs."""

import re

import httpx

SHA = re.compile(r"[0-9a-f]{40}")
CONCLUSIONS = {"success": "PASS", "failure": "FAIL", "timed_out": "ERROR"}
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

    def matches_side(self, side, draft, name):
        repo = side["repo"]
        return (
            repo["id"] == self.repository_id
            and side["ref"] == draft[f"{name}_ref"]
            and side["sha"] == draft[f"{name}_sha"]
        )

    def matches_pr(self, value, draft):
        return (
            str(value["id"]) == draft["external_id"]
            and value["number"] == draft["number"]
            and all(self.matches_side(value[side], draft, side) for side in ("base", "head"))
        )

    def verify_pr(self, draft):
        value, _ = self.get(f"/pulls/{draft['number']}")
        if (
            not self.matches_pr(value, draft)
            or value["draft"] is not True
            or value["state"] != "open"
            or (draft["merge_sha"] and value["merge_commit_sha"] != draft["merge_sha"])
        ):
            raise CIReadError("GITHUB_CI_TARGET_CHANGED")

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
        if draft["merge_sha"]:
            commit, _ = self.get(f"/git/commits/{tested}")
            if commit["sha"] != tested or [p["sha"] for p in commit["parents"]] != parents:
                raise CIReadError("GITHUB_CI_TARGET_CHANGED")
        required = {
            (check["app_id"], check["check_name"])
            for check in context["required_ci"]
            if check["provider"] == "github"
        }
        checks = {}
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
                if not draft["merge_sha"] and not any(
                    self.matches_pr(pr, draft) for pr in check["pull_requests"]
                ):
                    continue
                result = self.evidence(check, target, draft, tested, parents)
                key = (result["app_id"], result["check_name"], result["run_sequence"])
                if key in checks and checks[key] != result:
                    raise CIReadError("GITHUB_CI_INVALID_RESPONSE")
                checks[key] = result
            if not more:
                break
        else:
            raise CIReadError("GITHUB_CI_PAGE_LIMIT")
        self.verify_pr(draft)
        return [checks[key] for key in sorted(checks)]

    def evidence(self, check, target, draft, tested, parents):
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
            "merge_parents": parents if draft["merge_sha"] else [],
            "status": check_status(check),
            "url": f"https://api.github.com/repos/{self.repository}/check-runs/{check_id}",
        }
