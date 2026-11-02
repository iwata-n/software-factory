"""Store sanitized evaluation evidence as a Git blob; no branch is updated."""

import argparse
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from acceptance_support import sha, source_manifest
from collect_publication_evidence import evidence_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    report = json.loads(args.evaluation.read_bytes())
    identity = source_manifest()
    if (
        report["repository"] != args.repository
        or report["code_digest"] != identity["code_digest"]
        or report["base_sha"] != identity["base_sha"]
    ):
        raise SystemExit(
            "evaluation target does not match this exact implementation and repository"
        )
    files = {}
    for name, fingerprint in evidence_files(report).items():
        value = (args.evaluation.parent / name).read_bytes()
        if sha(value) != fingerprint:
            raise SystemExit("evaluation DB/Artifact evidence digest mismatch")
        files[name] = base64.b64encode(value).decode()
    package = json.dumps(
        {"schema_version": 1, "evaluation": report, "files": files},
        ensure_ascii=False,
        sort_keys=True,
    )
    if len(package.encode()) > 32 * 1024 * 1024:
        raise SystemExit("evaluation evidence package exceeds 32 MiB")
    descriptor, name = tempfile.mkstemp(prefix="factory-evidence-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"content": package, "encoding": "utf-8"}, stream)
        result = json.loads(
            subprocess.check_output(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    "repos/" + args.repository + "/git/blobs",
                    "--input",
                    name,
                ],
                text=True,
            )
        )
    finally:
        Path(name).unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "blob_sha": result["sha"],
                "implementation_sha": identity["base_sha"],
                "pr_marker": "<!-- factory-publication-evidence: "
                + result["sha"]
                + " implementation: "
                + identity["base_sha"]
                + " -->",
            }
        )
    )


if __name__ == "__main__":
    main()
