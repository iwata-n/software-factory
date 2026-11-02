"""Upload an exact projection of already-public GitHub facts; no DB or Artifact export."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from public_publication_evidence import project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-file", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    value = json.loads(args.public_file.read_bytes())
    public = project(args.repository, value["pull_request_number"])
    if value != public:
        raise SystemExit("payload must exactly equal the fixed projection of public GitHub data")
    descriptor, name = tempfile.mkstemp(prefix="factory-public-evidence-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"content": json.dumps(public, sort_keys=True), "encoding": "utf-8"}, stream)
        response = json.loads(
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
                "blob_sha": response["sha"],
                "implementation_sha": public["implementation_sha"],
                "pr_marker": "<!-- factory-publication-evidence: "
                + response["sha"]
                + " implementation: "
                + public["implementation_sha"]
                + " -->",
            }
        )
    )


if __name__ == "__main__":
    main()
