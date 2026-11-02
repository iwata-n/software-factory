"""Download an actual successful GitHub Actions artifact through the authenticated gh CLI."""

import argparse
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

from acceptance_support import sha, source_manifest
from verify_acceptance import audit


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run = api(f"repos/{args.repository}/actions/runs/{args.run_id}")
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise SystemExit("GitHub run is not completed successfully; evidence not accepted")
    artifacts = api(f"repos/{args.repository}/actions/runs/{args.run_id}/artifacts")["artifacts"]
    matches = [a for a in artifacts if a["name"] == "sf01-07-acceptance" and not a["expired"]]
    if len(matches) != 1:
        raise SystemExit("expected exactly one unexpired acceptance artifact")
    artifact = matches[0]
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / "github-artifact.zip"
    with archive.open("wb") as output:
        subprocess.run(["gh", "api", artifact["archive_download_url"]], stdout=output, check=True)
    with ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = (args.output / info.filename).resolve()
            if (
                not target.is_relative_to(args.output.resolve())
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise SystemExit("unsafe path or symlink in artifact")
        bundle.extractall(args.output)
    manifest = json.loads((args.output / "manifest.json").read_text())
    if (
        manifest["github_sha"] != run["head_sha"]
        or manifest["code_digest"] != source_manifest()["code_digest"]
    ):
        raise SystemExit("artifact target does not match run SHA/current inputs")
    certificate = {
        "repository": args.repository,
        "head_sha": run["head_sha"],
        "conclusion": run["conclusion"],
        "run_url": run["html_url"],
        "artifact_url": f"{run['html_url']}/artifacts/{artifact['id']}",
        "artifact_zip_path": str(archive.resolve()),
        "artifact_zip_sha256": sha(archive.read_bytes()),
    }
    (args.output / "github-verification.json").write_text(json.dumps(certificate, indent=2))
    result = audit(args.output)
    print(
        json.dumps(
            {
                "run_url": run["html_url"],
                "verified_records": len(result["criteria"]),
                "external_pending": result["external_pending"],
            }
        )
    )


if __name__ == "__main__":
    main()
