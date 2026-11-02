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
    for prefix in ("src", "tests", "scripts", "migrations", ".github"):
        for path in sorted((ROOT / prefix).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[path.relative_to(ROOT).as_posix()] = sha(path.read_bytes())
    for name in ("pyproject.toml", "uv.lock", "README.md", "alembic.ini", "compose.yaml"):
        files[name] = sha((ROOT / name).read_bytes())
    plan = (ROOT / "docs/Implementation_Plan.md").read_text()
    criteria = [
        line for line in plan.splitlines() if re.match(r"^\| (SF-0[1-7]-AC\d{2}|C-0[1-6]) \|", line)
    ]
    settings = plan.split("### MVPの固定試験設定", 1)[1].split("\nSF-10は", 1)[0]
    criteria.append(settings)
    files["docs/Implementation_Plan.md#SF01-07-criteria"] = sha("\n".join(criteria).encode())
    fingerprint = sha(json.dumps(files, sort_keys=True).encode())
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return {
        "base_sha": base,
        "code_digest": fingerprint,
        "files": files,
        "revision": f"{base}+inputs-{fingerprint[:16]}",
    }


def required_criteria() -> list[str]:
    return [
        f"SF-{task:02d}-AC{number:02d}"
        for task in range(1, 8)
        for number in range(1, (7 if task == 5 else 6) + 1)
    ]
