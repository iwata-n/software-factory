"""Credential-bearing GitHub publisher. It imports objects and never checks out or builds code."""

import base64
import hashlib
import io
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from .candidate_mount import readonly_tar
from .contracts import PublishedDraft, digest, path_within
from .resolver import valid_sha
from .sandbox import RunnerError
from .source_runner import SourcePolicy


class PublicationError(Exception):
    def __init__(self, reason, *, unknown=False, observed_head=None):
        super().__init__(reason)
        self.reason, self.unknown = reason, unknown
        self.observed_head = observed_head if valid_sha(observed_head) else None


def publication_target(operation, policy):
    target = operation["target"]
    if digest(target) != operation["target_hash"]:
        raise PublicationError("SOURCE_MISMATCH")
    try:
        remote = policy.authorize(target["remote_url"])
        identity = str(UUID(target["run_id"]))
    except ValueError:
        raise PublicationError("PUBLISH_POLICY_DENIED") from None
    if (
        urlsplit(remote).hostname != "github.com"
        or urlsplit(remote).path.strip("/").removesuffix(".git") != target["repository"]
        or identity != target["run_id"]
        or target["head_ref"] != "codex/factory/" + identity
        or target["base_ref"] == target["head_ref"]
        or digest(target["policy"]) != target["policy_hash"]
    ):
        raise PublicationError("PUBLISH_POLICY_DENIED")
    return target


def source_records(archive):
    try:
        packed = readonly_tar(archive)
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:") as bundle:
            entries = []
            for info in bundle:
                if info.issym():
                    body, mode = info.linkname.encode(), "120000"
                else:
                    body = bundle.extractfile(info).read()
                    mode = "100755" if info.mode & 0o111 else "100644"
                if info.name == ".gitmodules" or body.startswith(
                    b"version https://git-lfs.github.com/spec/v1\n"
                ):
                    raise PublicationError("PUBLISH_POLICY_DENIED")
                entries.append((info.name, mode, body))
        return sorted(entries)
    except (RunnerError, tarfile.TarError, ValueError, OSError, UnicodeError):
        raise PublicationError("SOURCE_MISMATCH") from None


class GitObjectPublisher:
    def __init__(self, policy: SourcePolicy, github_token: str):
        self.policy, self._token = policy, github_token

    def _git(self, root, *args, body=None, network=False):
        basic = base64.b64encode(("x-access-token:" + self._token).encode()).decode()
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(root),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + basic,
        }
        command = ["git", "-C", str(root)]
        for setting in (
            "core.hooksPath=/dev/null",
            "core.fsmonitor=false",
            "credential.helper=",
            "http.followRedirects=false",
            "protocol.allow=never",
            "protocol.https.allow=always",
            "filter.lfs.required=false",
            "filter.lfs.smudge=",
            "filter.lfs.process=",
            "submodule.recurse=false",
            "gc.auto=0",
        ):
            command += ["-c", setting]
        try:
            result = subprocess.run(
                command + list(args),
                input=body,
                env=environment,
                capture_output=True,
                timeout=90 if network else 20,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PublicationError(
                "PROVIDER_UNAVAILABLE" if network else "SOURCE_MISMATCH", unknown=network
            ) from None
        if result.returncode:
            details = result.stderr + result.stdout
            if network and (b"stale info" in details or b"[rejected]" in details):
                raise PublicationError("REMOTE_HEAD_CONFLICT")
            raise PublicationError(
                "PROVIDER_UNAVAILABLE" if network else "SOURCE_MISMATCH", unknown=network
            )
        return result.stdout

    @contextmanager
    def prepare(self, inputs):
        operation = inputs["operation"]
        target = publication_target(operation, self.policy)
        sources = inputs["sources"]
        if not 1 <= len(sources) <= 3 or sources[-1]["candidate"] != target["candidate"]:
            raise PublicationError("SOURCE_MISMATCH")
        with tempfile.TemporaryDirectory(prefix="factory-publisher-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            self._git(root, "init", "-q", ".")
            self._git(root, "check-ref-format", "refs/heads/" + target["base_ref"])
            self._git(root, "check-ref-format", "refs/heads/" + target["head_ref"])
            previous_head, previous_id = target["candidate"]["base_sha"], None
            for source in sources:
                candidate = source["candidate"]
                metadata = candidate["metadata_body"]
                if (
                    candidate["run_id"] != target["run_id"]
                    or candidate["base_sha"] != target["candidate"]["base_sha"]
                    or candidate["spec_hash"] != target["spec_hash"]
                    or candidate["profile_hash"] != target["profile_hash"]
                    or candidate["image_digest"] != target["candidate"]["image_digest"]
                    or metadata["parent_sha"] != previous_head
                    or metadata.get("parent_candidate_id") != previous_id
                ):
                    raise PublicationError("SOURCE_MISMATCH")
                try:
                    archive = base64.b64decode(source["archive"], validate=True)
                    commit = base64.b64decode(metadata["commit_object"], validate=True)
                except ValueError:
                    raise PublicationError("SOURCE_MISMATCH") from None
                records = source_records(archive)
                fingerprint, manifest, index = hashlib.sha256(), {}, []
                self._git(root, "read-tree", "--empty")
                for name, mode, body in records:
                    value = hashlib.sha256(body)
                    fingerprint.update(
                        name.encode() + b"\0" + mode.encode() + b"\0" + value.digest()
                    )
                    manifest[name] = value.hexdigest()
                    oid = self._git(
                        root, "hash-object", "-w", "--no-filters", "--stdin", body=body
                    ).strip()
                    index.append(mode.encode() + b" " + oid + b"\t" + name.encode() + b"\0")
                self._git(root, "update-index", "-z", "--index-info", body=b"".join(index))
                tree = self._git(root, "write-tree").decode().strip()
                headers = commit.split(b"\n\n", 1)[0].splitlines()
                if (
                    fingerprint.hexdigest() != candidate["source_digest"]
                    or manifest != metadata["file_manifest"]
                    or tree != candidate["tree_sha"]
                    or not headers
                    or headers[0] != ("tree " + tree).encode()
                    or [h for h in headers if h.startswith(b"parent ")]
                    != [("parent " + previous_head).encode()]
                ):
                    raise PublicationError("SOURCE_MISMATCH")
                head = (
                    self._git(root, "hash-object", "-t", "commit", "-w", "--stdin", body=commit)
                    .decode()
                    .strip()
                )
                if head != candidate["head_sha"]:
                    raise PublicationError("SOURCE_MISMATCH")
                for name, expected in target["policy"].get("protected_manifest", {}).items():
                    if manifest.get(name) != expected:
                        raise PublicationError("PUBLISH_POLICY_DENIED")
                previous_head, previous_id = head, candidate["candidate_id"]
            self._fetch(root, target)
            changed = (
                self._git(
                    root,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    "--no-renames",
                    target["candidate"]["base_sha"],
                    previous_head,
                    "--",
                )
                .decode()
                .split("\0")
            )
            protected = target["policy"]["protected_paths"] + list(
                target["policy"].get("protected_manifest", {})
            )
            whole_project = "." in target["policy"]["allowed_write_paths"]
            if not whole_project:
                protected += [".github", "migrations", "auth", "billing", "src/auth", "src/billing"]
            for name in filter(None, changed):
                if (
                    (
                        not whole_project
                        and name.rsplit("/", 1)[-1]
                        in {
                            "conftest.py",
                            "pytest.ini",
                            "tox.ini",
                            "setup.cfg",
                            "pyproject.toml",
                            ".coveragerc",
                        }
                    )
                    or any(part.lower() == ".git" for part in name.split("/"))
                    or any(path_within(name, path) for path in protected)
                    or not any(
                        path_within(name, path) for path in target["policy"]["allowed_write_paths"]
                    )
                ):
                    raise PublicationError("PUBLISH_POLICY_DENIED")
            if not any(changed):
                raise PublicationError("SOURCE_MISMATCH")
            yield root

    def _fetch(self, root, target):
        remote = self.policy.authorize(target["remote_url"])
        self._git(
            root,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            remote,
            target["candidate"]["base_sha"],
            network=True,
        )

    def push(self, root, operation):
        target = publication_target(operation, self.policy)
        remote = self.policy.authorize(target["remote_url"])
        ref = "refs/heads/" + target["head_ref"]
        expected = target["expected_remote_head"] or ""
        self._git(
            root,
            "push",
            "--porcelain",
            "--no-verify",
            "--recurse-submodules=no",
            "--force-with-lease=" + ref + ":" + expected,
            remote,
            target["candidate"]["head_sha"] + ":" + ref,
            network=True,
        )


class GitHubDraftPublisher:
    def __init__(self, policy, token, *, client=None, git=None):
        self.policy = policy
        self._client = client or httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self.git = git or GitObjectPublisher(policy, token)
        self.trace = []

    def _request(self, method, target, suffix="", **kwargs):
        remote = self.policy.authorize(target["remote_url"])
        if urlsplit(remote).path.strip("/").removesuffix(".git") != target["repository"]:
            raise PublicationError("PUBLISH_POLICY_DENIED")
        allowed = (
            method == "GET"
            and (
                suffix == ""
                or suffix == "/pulls"
                or suffix.startswith("/git/ref/heads/")
                or suffix.startswith("/pulls/")
                and suffix.removeprefix("/pulls/").isdigit()
            )
            or method == "POST"
            and suffix == "/pulls"
        )
        if not allowed or ".." in suffix or "?" in suffix or "#" in suffix:
            raise PublicationError("PUBLISH_POLICY_DENIED")
        trace = {
            "method": method,
            "resource": "/repos/" + target["repository"] + suffix,
            "status_code": None,
        }
        self.trace.append(trace)
        try:
            response = self._client.request(
                method, "/repos/" + target["repository"] + suffix, **kwargs
            )
        except httpx.HTTPError:
            raise PublicationError("PROVIDER_UNAVAILABLE", unknown=True) from None
        trace["status_code"] = response.status_code
        if response.status_code == 404 and method == "GET" and suffix.startswith("/git/ref/heads/"):
            return None
        if response.is_redirect:
            raise PublicationError("PUBLISH_POLICY_DENIED")
        if response.status_code >= 400:
            raise PublicationError("PROVIDER_UNAVAILABLE", unknown=True)
        try:
            return response.json()
        except ValueError:
            raise PublicationError("PROVIDER_UNAVAILABLE", unknown=True) from None

    def ref(self, target, name):
        result = self._request("GET", target, "/git/ref/heads/" + quote(name, safe=""))
        return result["object"]["sha"] if result else None

    def _normalize(self, value, target):
        candidate = target["candidate"]
        if (
            value.get("draft") is not True
            or value.get("state") != "open"
            or value["base"]["repo"]["full_name"] != target["repository"]
            or value["head"]["repo"]["full_name"] != target["repository"]
            or value["base"]["ref"] != target["base_ref"]
            or value["head"]["ref"] != target["head_ref"]
            or value["base"]["sha"] != candidate["base_sha"]
            or value["head"]["sha"] != candidate["head_sha"]
        ):
            raise PublicationError("PR_CONFLICT", observed_head=value.get("head", {}).get("sha"))
        return PublishedDraft(
            repository=target["repository"],
            number=value["number"],
            external_id=str(value["id"]),
            url=value["html_url"],
            base_ref=value["base"]["ref"],
            base_sha=value["base"]["sha"],
            head_ref=value["head"]["ref"],
            head_sha=value["head"]["sha"],
            merge_sha=value.get("merge_commit_sha"),
            draft=True,
            state="OPEN",
        ).model_dump()

    def _wait_head(self, target):
        observed = None
        for attempt in range(4):
            observed = self.ref(target, target["head_ref"])
            if observed == target["candidate"]["head_sha"]:
                return
            if observed != target["expected_remote_head"]:
                raise PublicationError("REMOTE_HEAD_CONFLICT", observed_head=observed)
            if attempt < 3:
                time.sleep(2**attempt)
        raise PublicationError("PUBLISH_UNKNOWN", unknown=True, observed_head=observed)

    def _wait_draft(self, target, number):
        for attempt in range(4):
            self._wait_head(target)
            try:
                return self._normalize(
                    self._request("GET", target, "/pulls/" + str(number)), target
                )
            except PublicationError as error:
                if (
                    error.reason != "PR_CONFLICT"
                    or error.observed_head != target["expected_remote_head"]
                ):
                    raise
                if attempt == 3:
                    raise PublicationError(
                        "PUBLISH_UNKNOWN", unknown=True, observed_head=error.observed_head
                    ) from None
            time.sleep(2**attempt)

    def _operation_marker(self, operation):
        return "<!-- factory-operation:" + operation["operation_key"] + " -->"

    def _run_marker(self, target):
        return "<!-- factory-run:" + target["run_id"] + " -->"

    def _require_pull_identity(self, value, operation):
        target = operation["target"]
        markers = (value.get("body") or "").splitlines()
        if target["pull_request_number"] is not None:
            matches = value["number"] == target["pull_request_number"] and str(
                value["id"]
            ) == target.get("pull_request_external_id")
        else:
            matches = markers.count(self._operation_marker(operation)) == 1
        if not matches or markers.count(self._run_marker(target)) != 1:
            raise PublicationError("PR_CONFLICT")

    def reconcile(self, operation):
        """Resolve an uncertain write with one bounded, read-only provider probe."""
        self.trace.clear()
        target = publication_target(operation, self.policy)
        pulls = self._request(
            "GET",
            target,
            "/pulls",
            params={
                "state": "all",
                "head": target["repository"].split("/")[0] + ":" + target["head_ref"],
                "per_page": 100,
            },
        )
        if not isinstance(pulls, list) or len(pulls) != 1:
            raise PublicationError("PUBLISH_UNKNOWN", unknown=True)
        number = pulls[0]["number"]
        if type(number) is not int or number < 1:
            raise PublicationError("PR_CONFLICT")
        pull = self._request("GET", target, "/pulls/" + str(number))
        if pull["id"] != pulls[0]["id"] or pull["number"] != number:
            raise PublicationError("PR_CONFLICT")
        self._require_pull_identity(pull, operation)
        result = self._normalize(pull, target)
        if result["url"] != f"https://github.com/{result['repository']}/pull/{number}":
            raise PublicationError("PR_CONFLICT")
        if self.ref(target, target["base_ref"]) != result["base_sha"]:
            raise PublicationError("BASE_CHANGED")
        observed = self.ref(target, target["head_ref"])
        if observed != result["head_sha"]:
            raise PublicationError("REMOTE_HEAD_CONFLICT", observed_head=observed)
        return result

    def publish(self, inputs):
        self.trace.clear()
        operation = inputs["operation"]
        target = publication_target(operation, self.policy)
        repository = self._request("GET", target)
        if repository["default_branch"] == target["head_ref"]:
            raise PublicationError("PUBLISH_POLICY_DENIED")
        if self.ref(target, target["base_ref"]) != target["candidate"]["base_sha"]:
            raise PublicationError("BASE_CHANGED")
        head = self.ref(target, target["head_ref"])
        if head not in {target["expected_remote_head"], target["candidate"]["head_sha"]}:
            raise PublicationError("REMOTE_HEAD_CONFLICT", observed_head=head)
        pulls = self._request(
            "GET",
            target,
            "/pulls",
            params={
                "state": "all",
                "head": target["repository"].split("/")[0] + ":" + target["head_ref"],
                "base": target["base_ref"],
                "per_page": 100,
            },
        )
        if len(pulls) > 1 or (
            target["pull_request_number"]
            and (len(pulls) != 1 or pulls[0]["number"] != target["pull_request_number"])
        ):
            raise PublicationError("PR_CONFLICT")
        if pulls and (pulls[0].get("draft") is not True or pulls[0].get("state") != "open"):
            raise PublicationError("PR_CONFLICT")
        if pulls:
            self._require_pull_identity(pulls[0], operation)
        with self.git.prepare(inputs) as root:
            if head != target["candidate"]["head_sha"]:
                self.git.push(root, operation)
        self._wait_head(target)
        if self.ref(target, target["base_ref"]) != target["candidate"]["base_sha"]:
            raise PublicationError("BASE_CHANGED")
        if not pulls:
            result = self._request(
                "POST",
                target,
                "/pulls",
                json={
                    "head": target["head_ref"],
                    "base": target["base_ref"],
                    "draft": True,
                    "maintainer_can_modify": False,
                    "title": "Software Factory: " + target["run_id"],
                    "body": (
                        "Simulation / transport fixture.\n\n"
                        if target["policy"].get("agent_adapter", "fake") == "fake"
                        else ""
                    )
                    + "Candidate `"
                    + target["candidate"]["head_sha"]
                    + "` passed Local Gate.\n\n"
                    + self._run_marker(target)
                    + "\n"
                    + self._operation_marker(operation),
                },
            )
            number = result["number"]
        else:
            number = pulls[0]["number"]
        return self._wait_draft(target, number)
