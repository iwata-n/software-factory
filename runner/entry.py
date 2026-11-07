"""Trusted runner: source/assembly never execute code; verification uses a fixed profile."""

import base64
import hashlib
import io
import json
import os
import posixpath
import re
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

MAX_SOURCE_BYTES = 128 * 1024 * 1024
GIT = [
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "http.followRedirects=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "filter.lfs.required=false",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "submodule.recurse=false",
]
ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_AUTHOR_NAME": "Software Factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "Software Factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}


class Refused(Exception):
    def __init__(self, code, category="POLICY"):
        self.code, self.category = code, category


def checkpoint(name):
    print("FACTORY_STAGE:" + name, file=sys.stderr, flush=True)
    if os.environ.get("FACTORY_TEST_PAUSE_AT") == name:
        while True:
            time.sleep(0.05)


def git(*args, input=None, check=True):
    result = subprocess.run(GIT + list(args), input=input, env=ENV, capture_output=True)
    if check and result.returncode:
        stderr = result.stderr.lower()
        if b"redirect" in stderr or re.search(rb"error:\s*30[12378]", stderr):
            raise Refused("SOURCE_REDIRECT_DENIED")
        if any(
            x in stderr
            for x in (
                b"authentication",
                b"403",
                b"401",
                b"access denied",
                b"could not read username",
                b"could not read password",
            )
        ):
            raise Refused("SOURCE_ACCESS_DENIED")
        if any(
            x in stderr
            for x in (b"couldn't find remote ref", b"not our ref", b"unadvertised object")
        ):
            raise Refused("SOURCE_REF_NOT_FOUND")
        raise Refused("SOURCE_FETCH_FAILED", "INFRASTRUCTURE")
    return result.stdout


def canonical_path(path):
    return (
        bool(path)
        and "\\" not in path
        and "\x00" not in path
        and not PurePosixPath(path).is_absolute()
        and all(p not in ("", ".", "..") for p in path.split("/"))
    )


def records(sha):
    total, entries = 0, []
    for record in git("ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        try:
            name = path.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("SOURCE_PATH_ENCODING") from None
        if not canonical_path(name) or any(p.lower() == ".git" for p in name.split("/")):
            raise Refused("SOURCE_PATH_DENIED")
        if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
            raise Refused("SUBMODULE_NOT_ALLOWED")
        if name == ".gitmodules":
            raise Refused("SUBMODULE_NOT_ALLOWED")
        size = int(git("cat-file", "-s", oid.decode()))
        total += size
        if total > MAX_SOURCE_BYTES:
            raise Refused("SOURCE_TOO_LARGE")
        body = git("cat-file", "blob", oid.decode())
        if body.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            raise Refused("LFS_NOT_ALLOWED")
        entries.append((name, mode.decode(), body))
    return entries


def pack(entries):
    fingerprint = hashlib.sha256()
    archive = io.BytesIO()
    links = []
    with tarfile.open(fileobj=archive, mode="w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for path, mode, body in sorted(entries):
            fingerprint.update(
                path.encode() + b"\0" + mode.encode() + b"\0" + hashlib.sha256(body).digest()
            )
            info = tarfile.TarInfo(path)
            info.mode, info.mtime = int(mode, 8) & 0o777, 0
            if mode == "120000":
                info.type, info.linkname = tarfile.SYMTYPE, body.decode("utf-8")
                links.append(path)
            else:
                info.size = len(body)
            bundle.addfile(info, io.BytesIO(body) if info.isfile() else None)
    return fingerprint.hexdigest(), archive.getvalue(), links


def fetch(data):
    ref = data["ref"]
    if (
        not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}", ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".lock"))
    ):
        raise Refused("SOURCE_REF_INVALID")
    # This trusted program receives only a server-allowlisted HTTPS URL.
    if not data["url"].startswith("https://"):
        raise Refused("SOURCE_URL_DENIED")
    if certificate := data.get("source_ca_pem"):
        # Set only by the administrator-owned runner, never by the public source request schema.
        Path("/tmp/source-ca.pem").write_text(certificate)
        GIT.extend(["-c", "http.sslCAInfo=/tmp/source-ca.pem"])
    git("init", "-q", ".")
    git("fetch", "--no-tags", "--depth=1", "--", data["url"], ref)
    sha = git("rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
    if data.get("sha") and sha != data["sha"]:
        raise Refused("SOURCE_SHA_MISMATCH")
    tree = git("rev-parse", sha + "^{tree}").decode().strip()
    source_digest, archive, links = pack(records(sha))
    return {
        "sha": sha,
        "tree": tree,
        "source_digest": source_digest,
        "archive": base64.b64encode(archive).decode(),
        "symlink_paths": links,
        "commit_object": base64.b64encode(git("cat-file", "commit", sha)).decode(),
    }


def unpack_source(source):
    archive = base64.b64decode(source["archive"], validate=True)
    entries, total = [], 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for info in bundle:
            if not canonical_path(info.name) or any(
                p.lower() == ".git" for p in info.name.split("/")
            ):
                raise Refused("SOURCE_PATH_DENIED")
            if info.isdir():
                continue
            if info.isfile():
                total += info.size
                if total > MAX_SOURCE_BYTES:
                    raise Refused("SOURCE_TOO_LARGE")
                body = bundle.extractfile(info).read()
                mode = "100755" if info.mode & 0o111 else "100644"
            elif info.issym():
                body, mode = info.linkname.encode(), "120000"
            else:
                raise Refused("SOURCE_LINK_DENIED")
            entries.append((info.name, mode, body))
    if len({p for p, _, _ in entries}) != len(entries):
        raise Refused("SOURCE_DUPLICATE_PATH")
    validate_links(entries)
    return entries


def validate_links(entries):
    links = {path: body.decode() for path, mode, body in entries if mode == "120000"}
    for path, _, _ in entries:
        if any(path.startswith(link + "/") for link in links):
            raise Refused("SOURCE_LINK_DENIED")
    for path, target in links.items():
        seen = {path}
        current = path
        while current in links:
            target = links[current]
            if target.startswith("/") or "\\" in target:
                raise Refused("SOURCE_LINK_DENIED")
            current = posixpath.normpath(posixpath.join(posixpath.dirname(current), target))
            if not canonical_path(current) or current in seen:
                raise Refused("SOURCE_LINK_DENIED")
            seen.add(current)


def assemble(data):
    source = data["source"]
    entries = unpack_source(source)
    source_digest, _, _ = pack(entries)
    if source_digest != source["source_digest"]:
        raise Refused("BASE_SOURCE_DIGEST_MISMATCH")
    git("init", "-q", ".")
    for path, mode, body in entries:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode != "120000":
            target.write_bytes(body)
            target.chmod(int(mode, 8) & 0o777)
        oid = git("hash-object", "-w", "--no-filters", "--stdin", input=body).decode().strip()
        git("update-index", "--add", "--cacheinfo", mode, oid, path)
    for path, mode, body in entries:
        if mode == "120000":
            Path(path).symlink_to(body.decode())
    if git("write-tree").decode().strip() != source["tree"]:
        raise Refused("BASE_TREE_MISMATCH")
    commit = base64.b64decode(source["commit_object"], validate=True)
    if not commit.startswith(("tree " + source["tree"] + "\n").encode()):
        raise Refused("BASE_TREE_MISMATCH")
    sha = git("hash-object", "-t", "commit", "-w", "--stdin", input=commit).decode().strip()
    if sha != source["sha"]:
        raise Refused("BASE_SHA_MISMATCH")
    Path(".git/shallow").write_text(sha + "\n")
    git("symbolic-ref", "HEAD", "refs/heads/main")
    git("update-ref", "refs/heads/main", sha)
    git("update-index", "--refresh")
    try:
        patch = base64.b64decode(data["patch"], validate=True)
        text = patch.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise Refused("PATCH_ENCODING_INVALID") from None
    if not text.startswith(("diff --git ", "--- ")) or any(
        x in text for x in ("GIT binary patch", "Binary files ", "\x00")
    ):
        raise Refused("PATCH_FORMAT_UNSUPPORTED")
    # Git's own index-aware patch validator rejects traversal, .git edits and symlink writes.
    checked = subprocess.run(
        GIT + ["apply", "--check", "--index", "--whitespace=nowarn", "-"],
        input=patch,
        env=ENV,
        capture_output=True,
    )
    if checked.returncode:
        raise Refused("PATCH_APPLY_DENIED")
    git("apply", "--index", "--whitespace=nowarn", "-", input=patch)
    checkpoint("patch_applied")
    paths = [
        name.decode()
        for name in git("diff", "--cached", "--no-renames", "--name-only", "-z", sha).split(b"\0")
        if name
    ]
    protected = data["protected_paths"] + list(data.get("protected_manifest", {}))
    baseline = [".github", "migrations", "auth", "billing", "src/auth", "src/billing"]
    whole_project = "." in data["allowed_write_paths"]
    if whole_project:
        baseline = []
    for path in paths:
        if not whole_project and path.rsplit("/", 1)[-1] in {
            "conftest.py",
            "pytest.ini",
            "tox.ini",
            "setup.cfg",
            "pyproject.toml",
            ".coveragerc",
        }:
            raise Refused("PROTECTED_VERIFICATION_CHANGE")
        if any(path == root or path.startswith(root + "/") for root in protected + baseline):
            raise Refused("PROTECTED_VERIFICATION_CHANGE")
        if not canonical_path(path) or not any(
            root == "." or path == root or path.startswith(root + "/")
            for root in data["allowed_write_paths"]
        ):
            raise Refused("POLICY_DENIED")
    tree = git("write-tree").decode().strip()
    candidate_entries = records(tree)
    validate_links(candidate_entries)
    for path, expected in data.get("protected_manifest", {}).items():
        actual = next(
            (
                hashlib.sha256(body).hexdigest()
                for name, _, body in candidate_entries
                if name == path
            ),
            None,
        )
        if actual != expected:
            raise Refused("PROTECTED_VERIFICATION_CHANGE")
    candidate_digest, archive, links = pack(candidate_entries)
    if candidate_digest == source_digest:
        raise Refused("NO_CODE_CHANGE")
    # Reproducible commit: its identity depends on the parent and resulting tree, not wall time.
    ENV["GIT_AUTHOR_DATE"] = ENV["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00Z"
    checkpoint("before_commit")
    git("commit", "-qm", "Software Factory candidate")
    head = git("rev-parse", "HEAD").decode().strip()
    return {
        "base_sha": sha,
        "head_sha": head,
        "tree": tree,
        "source_digest": candidate_digest,
        "code_digest": candidate_digest,
        "base_source_digest": source_digest,
        "file_manifest": {
            name: hashlib.sha256(body).hexdigest() for name, _, body in candidate_entries
        },
        "changed_paths": paths,
        "archive": base64.b64encode(archive).decode(),
        "symlink_paths": links,
        "commit_object": base64.b64encode(git("cat-file", "commit", head)).decode(),
    }


def verify(data):
    candidate = data["candidate"]
    root = Path("/candidate")
    entries = []
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if not canonical_path(name) or any(p.lower() == ".git" for p in name.split("/")):
            raise Refused("CANDIDATE_PATH_DENIED")
        if path.is_symlink():
            entries.append((name, "120000", os.readlink(path).encode()))
        elif path.is_file():
            entries.append(
                (name, "100755" if path.stat().st_mode & 0o111 else "100644", path.read_bytes())
            )
        elif not path.is_dir():
            raise Refused("CANDIDATE_FILE_TYPE_DENIED")
    validate_links(entries)
    fingerprint = hashlib.sha256()
    manifest = {}
    git("init", "-q", ".")
    for name, mode, body in entries:
        fingerprint.update(
            name.encode() + b"\0" + mode.encode() + b"\0" + hashlib.sha256(body).digest()
        )
        manifest[name] = hashlib.sha256(body).hexdigest()
        oid = git("hash-object", "-w", "--no-filters", "--stdin", input=body).decode().strip()
        git("update-index", "--add", "--cacheinfo", mode, oid, name)
    if (
        fingerprint.hexdigest() != candidate["source_digest"]
        or manifest != candidate["metadata_body"]["file_manifest"]
        or git("write-tree").decode().strip() != candidate["tree_sha"]
    ):
        raise Refused("CANDIDATE_CONTENT_MISMATCH")
    commit = base64.b64decode(candidate["metadata_body"]["commit_object"], validate=True)
    if (
        not commit.startswith(("tree " + candidate["tree_sha"] + "\n").encode())
        or git("hash-object", "-t", "commit", "--stdin", input=commit).decode().strip()
        != candidate["head_sha"]
    ):
        raise Refused("CANDIDATE_COMMIT_MISMATCH")
    for name, expected in data["profile"].get("protected_manifest", {}).items():
        if manifest.get(name) != expected:
            raise Refused("PROTECTED_VERIFICATION_CHANGE")
    observations = []
    if data["operation"] == "verify":
        for command in data["profile"].get("commands", []):
            report = Path(command["junit_path"])
            if not canonical_path(command["junit_path"]):
                raise Refused("REPORT_PATH_DENIED")
            if any(p.is_symlink() for p in (report, *report.parents)):
                raise Refused("REPORT_PATH_DENIED")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.unlink(missing_ok=True)
            log_path = Path("/tmp/check.log")
            log_path.unlink(missing_ok=True)
            started = time.time()
            state, exit_code = "EXECUTED", None
            with log_path.open("wb") as log:
                process = None
                try:
                    process = subprocess.Popen(
                        command["argv"],
                        cwd="/work",
                        env={
                            **ENV,
                            "PYTHONPATH": "/candidate:/candidate/src",
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                        },
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    exit_code = process.wait(timeout=command["timeout_seconds"])
                except subprocess.TimeoutExpired:
                    state = "TIMEOUT"
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                    exit_code = process.returncode
                except OSError:
                    state = "START_FAILED"
            junit = None
            if (
                not any(p.is_symlink() for p in (report, *report.parents))
                and report.is_file()
                and report.stat().st_size <= 16 * 1024 * 1024
            ):
                junit = base64.b64encode(report.read_bytes()).decode()
            with log_path.open("rb") as log:
                raw_log = log.read(8 * 1024 * 1024)
            observations.append(
                {
                    "check_id": command["check_id"],
                    "execution_state": state,
                    "exit_code": exit_code,
                    "started_at": started,
                    "finished_at": time.time(),
                    "log": base64.b64encode(raw_log).decode(),
                    "junit": junit,
                }
            )
    return {
        "checks": observations,
        "source_digest": fingerprint.hexdigest(),
        "file_manifest": manifest,
    }


def main():
    try:
        data = json.loads(sys.stdin.buffer.read(1024 * 1024))
        if data.get("operation") == "fetch":
            result = fetch(data)
        elif data.get("operation") == "assemble":
            result = assemble(data)
        elif data.get("operation") in {"verify", "inspect_candidate"}:
            result = verify(data)
        else:
            raise Refused("RUNNER_OPERATION_UNSUPPORTED")
        print(json.dumps({"ok": True, **result}))
    except Refused as error:
        print(json.dumps({"ok": False, "code": error.code, "category": error.category}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
