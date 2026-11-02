"""Projection of already-public GitHub facts; no local DB or Artifact data is read."""

import argparse
import json
from pathlib import Path
from urllib.parse import quote

from acceptance_support import source_manifest
from collect_publication_evidence import api


def project(repository, number):
    identity = source_manifest()
    if api(repository, "")["private"] is not False:
        raise ValueError("public evidence requires an already-public repository")
    pull = api(repository, "pulls/" + str(number))
    if (
        pull["draft"] is not True
        or pull["state"] != "open"
        or pull["merged"] is not False
        or pull["base"]["sha"] != identity["base_sha"]
        or pull["head"]["repo"]["full_name"] != repository
        or pull["base"]["repo"]["full_name"] != repository
        or not pull["head"]["ref"].startswith("codex/factory/")
        or "Simulation / transport fixture." not in (pull.get("body") or "")
    ):
        raise ValueError("public PR is not the fixed publication transport scenario")
    commits = api(repository, "pulls/" + str(number) + "/commits")
    if len(commits) != 3:
        raise ValueError("public scenario requires two candidates and one competing commit")
    chain, previous = [], identity["base_sha"]
    for index, entry in enumerate(commits):
        commit = api(repository, "git/commits/" + entry["sha"])
        if [parent["sha"] for parent in commit["parents"]] != [previous]:
            raise ValueError("public commit chain is not linear from this implementation")
        author = "Software Factory" if index < 2 else "Human concurrency fixture"
        if commit["author"]["name"] != author:
            raise ValueError("public commit is outside the transport fixture")
        chain.append(
            {"sha": commit["sha"], "tree_sha": commit["tree"]["sha"], "parent_sha": previous}
        )
        previous = commit["sha"]
    head = api(repository, "git/ref/heads/" + quote(pull["head"]["ref"], safe=""))["object"]["sha"]
    base = api(repository, "git/ref/heads/" + quote(pull["base"]["ref"], safe=""))["object"]["sha"]
    if head != previous or pull["head"]["sha"] != previous or base != identity["base_sha"]:
        raise ValueError("public competing commit or implementation base was not preserved")
    matches = api(
        repository,
        "pulls?state=all&head="
        + quote(repository.split("/")[0] + ":" + pull["head"]["ref"], safe="")
        + "&base="
        + quote(pull["base"]["ref"], safe=""),
    )
    if len(matches) != 1 or matches[0]["number"] != number:
        raise ValueError("public GitHub state does not identify one Draft PR")
    return {
        "schema_version": 1,
        "evidence_scope": "PUBLIC_GITHUB",
        "implementation_sha": identity["base_sha"],
        "code_digest": identity["code_digest"],
        "repository": repository,
        "pull_request_number": number,
        "pull_request_url": pull["html_url"],
        "draft": True,
        "state": "open",
        "merged": False,
        "base_ref": pull["base"]["ref"],
        "base_sha": base,
        "head_ref": pull["head"]["ref"],
        "head_sha": head,
        "commits": chain,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(project(args.repository, args.pull_request), indent=2))
    print(json.dumps({"output": str(args.output), "scope": "PUBLIC_GITHUB"}))


if __name__ == "__main__":
    main()
