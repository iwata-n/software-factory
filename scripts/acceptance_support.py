"""Input identity shared by the test reporter and the independent evidence audit."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_manifest() -> dict:
    files = {}
    for prefix in ("src", "tests", "scripts", "migrations", ".github", "runner", "config"):
        for path in sorted((ROOT / prefix).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[path.relative_to(ROOT).as_posix()] = sha(path.read_bytes())
    for name in (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "alembic.ini",
        "compose.yaml",
        ".python-version",
        ".env.example",
        ".gitignore",
    ):
        files[name] = sha((ROOT / name).read_bytes())
    plan = (ROOT / "docs/Implementation_Plan.md").read_text()
    criteria = [
        line for line in plan.splitlines() if re.match(r"^\| (SF-\d{2}-AC\d{2}|C-0[1-6]) \|", line)
    ]
    settings = plan.split("### MVPの固定試験設定", 1)[1].split("\nSF-10は", 1)[0]
    criteria.append(settings)
    files["docs/Implementation_Plan.md#criteria"] = sha("\n".join(criteria).encode())
    fingerprint = sha(json.dumps(files, sort_keys=True).encode())
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return {
        "base_sha": base,
        "code_digest": fingerprint,
        "files": files,
        "revision": f"{base}+inputs-{fingerprint[:16]}",
    }


def required_criteria() -> list[str]:
    # A task enters the executed ledger only when its acceptance module exists.
    # The implementation plan retains unimplemented/unverified tasks separately.
    implemented = {
        int(path.stem.removeprefix("test_sf"))
        for path in (ROOT / "tests/acceptance").glob("test_sf[0-9][0-9].py")
    }
    criteria = re.findall(
        r"^\| (SF-(\d{2})-AC\d{2}) \|", (ROOT / "docs/Implementation_Plan.md").read_text(), re.M
    )
    return [ac for ac, task in criteria if int(task) in implemented]
