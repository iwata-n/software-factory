"""Export a committed checkout as an immutable source-runner input, without publishing it."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from runner import entry
from software_factory.source_runner import SourceSnapshot, canonical_remote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--base-ref", default="local/self-host")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sha = entry.git("rev-parse", "--verify", args.revision + "^{commit}").decode().strip()
    entries = entry.records(sha)
    source_digest, archive, links = entry.pack(entries)
    value = SourceSnapshot(
        remote_url=canonical_remote(args.remote_url),
        base_ref=args.base_ref,
        source={
            "sha": sha,
            "tree": entry.git("rev-parse", sha + "^{tree}").decode().strip(),
            "source_digest": source_digest,
            "archive": base64.b64encode(archive).decode(),
            "symlink_paths": links,
            "commit_object": base64.b64encode(entry.git("cat-file", "commit", sha)).decode(),
            "file_manifest": {
                path: hashlib.sha256(body).hexdigest() for path, _mode, body in entries
            },
        },
    )
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.output.open("x") as output:
        output.write(value.model_dump_json() + "\n")
    print(json.dumps({"sha": sha, "base_ref": args.base_ref, "files": len(entries)}))


if __name__ == "__main__":
    main()
