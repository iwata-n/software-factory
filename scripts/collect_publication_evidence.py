"""Independently verify real GitHub refs/PRs against a fixed Publisher evaluation."""

import argparse
import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from acceptance_support import sha, source_manifest


def api(repository, suffix):
    return json.loads(
        subprocess.check_output(["gh", "api", "repos/" + repository + "/" + suffix], text=True)
    )


def evidence_files(report):
    expected = report["artifact_manifest"] | {"database.json": report["database_sha256"]}
    for name in expected:
        if (
            PurePosixPath(name).is_absolute()
            or "\\" in name
            or "\0" in name
            or any(p in {"", ".", ".."} for p in name.split("/"))
            or name != "database.json"
            and not name.startswith("artifacts/")
        ):
            raise ValueError("invalid evaluation evidence path")
    return expected


def verify(repository, report):
    identity = source_manifest()
    if (
        report["evaluation"] != "SF17_PUBLICATION_TRANSPORT"
        or report["publication_origin"] != "actual_github"
        or report["code_digest"] != identity["code_digest"]
        or report["base_sha"] != identity["base_sha"]
        or report["repository"] != repository
    ):
        raise ValueError("real publication inputs do not match this implementation")
    first, second, final = [
        report[key] for key in ("first_publication", "second_publication", "final_publication")
    ]
    number = first["pull_request"]["number"]
    if any(p["pull_request"]["number"] != number for p in (second, final)):
        raise ValueError("same-Run publication did not retain a single PR")
    ref = "codex/factory/" + report["run_id"]
    actual = api(repository, "pulls/" + str(number))
    head = api(repository, "git/ref/heads/" + quote(ref, safe=""))["object"]["sha"]
    base = api(repository, "git/ref/heads/" + quote(report["base_ref"], safe=""))["object"]["sha"]
    matches = api(
        repository,
        "pulls?state=all&head="
        + quote(repository.split("/")[0] + ":" + ref, safe="")
        + "&base="
        + quote(report["base_ref"], safe=""),
    )
    if (
        len(matches) != 1
        or matches[0]["number"] != number
        or actual["draft"] is not True
        or actual["state"] != "open"
        or actual["merged"] is not False
        or actual["base"]["repo"]["full_name"] != repository
        or actual["head"]["repo"]["full_name"] != repository
        or actual["base"]["sha"] != report["base_sha"]
        or base != report["base_sha"]
        or actual["head"]["ref"] != ref
        or actual["head"]["sha"] != report["human_fixture_sha"]
        or head != report["human_fixture_sha"]
        or report["final_run"]["status"] != "BLOCKED"
        or report["final_run"]["stop_reason"] != "REMOTE_HEAD_CONFLICT"
    ):
        raise ValueError("GitHub does not prove one Draft PR and a preserved human commit")
    previous = report["base_sha"]
    for publication in (first, second):
        operation = publication["operations"][-1]
        candidate = operation["target"]["candidate"]
        commit = api(repository, "git/commits/" + candidate["head_sha"])
        if (
            operation["state"] != "SUCCEEDED"
            or publication["pull_request"]["head_sha"] != candidate["head_sha"]
            or commit["tree"]["sha"] != candidate["tree_sha"]
            or [p["sha"] for p in commit["parents"]] != [previous]
        ):
            raise ValueError("actual commit/tree/parent does not match the managed Candidate")
        previous = candidate["head_sha"]
    human = api(repository, "git/commits/" + report["human_fixture_sha"])
    if [p["sha"] for p in human["parents"]] != [previous] or final["operations"][-1][
        "state"
    ] != "BLOCKED":
        raise ValueError("human commit conflict history is inconsistent")
    requests = report["provider_requests"]
    if (
        not requests
        or any(
            r["method"] not in {"GET", "POST"}
            or not (
                r["resource"] == "/repos/" + repository
                or r["resource"].startswith("/repos/" + repository + "/")
            )
            or r["method"] == "POST"
            and r["resource"] != "/repos/" + repository + "/pulls"
            for r in requests
        )
        or sum(r["method"] == "POST" for r in requests) != 1
    ):
        raise ValueError("provider trace includes unexpected writes or duplicate PR creation")
    return {
        "result": "PASS",
        "code_digest": identity["code_digest"],
        "implementation_sha": identity["base_sha"],
        "criteria": ["SF-17-AC01", "SF-17-AC02", "SF-17-AC03"],
        "external_urls": [actual["html_url"]],
        "remote_head": head,
        "base_sha": base,
        "pull_request_number": number,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evaluation", type=Path)
    source.add_argument("--evaluation-blob")
    source.add_argument("--discover", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.discover:
        identity = source_manifest()
        pulls = api(args.repository, "commits/" + identity["base_sha"] + "/pulls")
        matches = []
        pattern = (
            r"<!-- factory-publication-evidence: ([0-9a-f]{40}) implementation: ([0-9a-f]{40}) -->"
        )
        for pull in pulls:
            if pull["head"]["sha"] == identity["base_sha"]:
                matches += [
                    blob
                    for blob, implementation in re.findall(pattern, pull.get("body") or "")
                    if implementation == identity["base_sha"]
                ]
        if not matches:
            print(json.dumps({"result": "PENDING", "reason": "NO_EVALUATION_FOR_THIS_COMMIT"}))
            return
        if len(set(matches)) != 1:
            raise SystemExit("ambiguous publication evaluation references")
        args.evaluation_blob = matches[0]
    if args.evaluation:
        raw = args.evaluation.read_bytes()
    else:
        if len(args.evaluation_blob) != 40 or any(
            c not in "0123456789abcdef" for c in args.evaluation_blob
        ):
            raise SystemExit("invalid publication evidence blob SHA")
        blob = api(args.repository, "git/blobs/" + args.evaluation_blob)
        if blob["encoding"] != "base64" or blob["size"] > 32 * 1024 * 1024:
            raise SystemExit("invalid publication evidence blob")
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if oid != args.evaluation_blob or len(raw) != blob["size"]:
            raise SystemExit("GitHub evidence blob identity mismatch")
    parsed = json.loads(raw)
    packaged = parsed.get("schema_version") == 1 and "evaluation" in parsed and "files" in parsed
    report = parsed["evaluation"] if packaged else parsed
    expected = evidence_files(report)
    files = {}
    for name, fingerprint in expected.items():
        value = (
            base64.b64decode(parsed["files"][name], validate=True)
            if packaged
            else (args.evaluation.parent / name).read_bytes()
        )
        if sha(value) != fingerprint:
            raise ValueError("evaluation DB/Artifact evidence digest mismatch")
        files[name] = value
    certificate = verify(args.repository, report)
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / "publication-evaluation.json"
    encoded = json.dumps(report, sort_keys=True, ensure_ascii=False).encode()
    destination.write_bytes(encoded)
    certificate |= {
        "evaluation_file": destination.name,
        "evaluation_sha256": sha(encoded),
        "evidence_files": {},
    }
    for name, value in files.items():
        relative = "publication-files/" + name
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        certificate["evidence_files"][relative] = sha(value)
    (args.output / "publication-verification.json").write_text(json.dumps(certificate, indent=2))
    print(json.dumps({"result": "PASS", "external_urls": certificate["external_urls"]}))


if __name__ == "__main__":
    main()
