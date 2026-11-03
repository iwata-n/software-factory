"""Input identity shared by the test reporter and the independent evidence audit."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_CRITERIA = {
    "SF-01-AC06": "github-verification.json",
    "SF-17-AC01": "publication-verification.json",
    "SF-17-AC02": "publication-verification.json",
    "SF-17-AC03": "publication-verification.json",
    "SF-19-AC01": "sf19-verification.json",
    "SF-19-AC06": "sf19-verification.json",
}


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_manifest() -> dict:
    files = {}
    for prefix in (
        "src",
        "tests",
        "scripts",
        "migrations",
        ".github",
        "runner",
        "config",
        "examples",
    ):
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
    criteria.extend(line for line in plan.splitlines() if re.match(r"^\| AT-\d{2} ", line))
    files["docs/Implementation_Plan.md#criteria"] = sha("\n".join(criteria).encode())
    fingerprint = sha(json.dumps(files, sort_keys=True).encode())
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return {
        "base_sha": base,
        "code_digest": fingerprint,
        "files": files,
        "revision": f"{base}+inputs-{fingerprint[:16]}",
    }


def task_criteria() -> list[str]:
    return re.findall(
        r"^\| (SF-\d{2}-AC\d{2}) \|",
        (ROOT / "docs/Implementation_Plan.md").read_text(),
        re.M,
    )


def acceptance_scenarios() -> dict[str, list[str]]:
    plan = (ROOT / "docs/Implementation_Plan.md").read_text()
    return {
        scenario: re.findall(r"SF-\d{2}-AC\d{2}|C-0[1-6]", references)
        for scenario, references in re.findall(r"^\| (AT-\d{2}) [^|]+\| ([^|]+) \|", plan, re.M)
    }


def required_criteria() -> list[str]:
    # A boundary criterion can be audited once its complete acceptance matrix is collected.
    complete_boundary_criteria = {"SF-18-AC03", "SF-21-AC02"}
    implemented = {
        int(path.stem.removeprefix("test_sf"))
        for path in (ROOT / "tests/acceptance").glob("test_sf[0-9][0-9].py")
    }
    return [
        ac
        for ac in task_criteria()
        if int(ac[3:5]) in implemented or ac in complete_boundary_criteria
    ]
