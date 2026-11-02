import copy
import subprocess
from pathlib import Path

import httpx
import pytest

from software_factory.contracts import digest
from software_factory.publisher_backend import (
    GitHubDraftPublisher,
    GitObjectPublisher,
    PublicationError,
)
from software_factory.source_runner import SourcePolicy
from tests.acceptance.test_sf10 import pipeline as pipeline
from tests.acceptance.test_sf11 import source_bundle as source_bundle
from tests.test_assembler import runner as runner
from tests.test_publications import publication as publication


@pytest.fixture
def backend(publication, tmp_path):
    pub = publication
    response = pub.begin()
    assert response.status_code == 200
    operation = response.json()
    inputs = pub.world.get(
        f"/v1/publisher/operations/{operation['operation_id']}/inputs", token=pub.secret
    ).json()
    bare = tmp_path / "publication-remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "--no-local", str(Path.cwd()), str(bare)],
        check=True,
        capture_output=True,
    )
    policy = SourcePolicy((operation["target"]["remote_url"],))
    state = {"http": [], "pushes": 0, "pulls": [], "race": False, "roots": []}

    def remote_head():
        result = subprocess.run(
            [
                "git",
                "--git-dir",
                str(bare),
                "rev-parse",
                "--verify",
                "refs/heads/" + operation["target"]["head_ref"],
            ],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    class LocalGit(GitObjectPublisher):
        def _fetch(self, root, target):
            self._git(
                root,
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--depth=1",
                str(bare),
                target["candidate"]["base_sha"],
            )
            assert set(p.name for p in root.iterdir()) == {".git"}
            state["roots"].append(root)

        def push(self, root, supplied):
            target = supplied["target"]
            ref = "refs/heads/" + target["head_ref"]
            if state["race"]:
                subprocess.run(
                    [
                        "git",
                        "--git-dir",
                        str(bare),
                        "update-ref",
                        ref,
                        target["candidate"]["base_sha"],
                    ],
                    check=True,
                    capture_output=True,
                )
            state["pushes"] += 1
            self._git(
                root,
                "-c",
                "protocol.file.allow=always",
                "push",
                "--porcelain",
                "--force-with-lease=" + ref + ":" + (target["expected_remote_head"] or ""),
                str(bare),
                target["candidate"]["head_sha"] + ":" + ref,
                network=True,
            )

    def pull(number):
        target = operation["target"]
        return {
            "id": 2345,
            "number": number,
            "draft": True,
            "state": "open",
            "html_url": "https://github.com/iwata-n/software-factory/pull/123",
            "base": {
                "repo": {"full_name": target["repository"]},
                "ref": target["base_ref"],
                "sha": target["candidate"]["base_sha"],
            },
            "head": {
                "repo": {"full_name": target["repository"]},
                "ref": target["head_ref"],
                "sha": remote_head(),
            },
            "merge_commit_sha": None,
        }

    def handler(request):
        state["http"].append((request.method, request.url.path))
        prefix = "/repos/iwata-n/software-factory"
        if request.url.path == prefix:
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/ref/heads/" in request.url.path:
            ref = request.url.path.split("/git/ref/heads/")[1]
            head = operation["target"]["candidate"]["base_sha"] if ref == "main" else remote_head()
            return httpx.Response(200 if head else 404, json={"object": {"sha": head}})
        if request.url.path == prefix + "/pulls":
            if request.method == "POST":
                assert not state["pulls"]
                state["pulls"].append(123)
                return httpx.Response(201, json=pull(123))
            return httpx.Response(200, json=[pull(n) for n in state["pulls"]])
        if request.url.path == prefix + "/pulls/123":
            return httpx.Response(200, json=pull(123))
        raise AssertionError("unexpected provider route")

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    api = GitHubDraftPublisher(
        policy,
        "ghp_PUBLISHER_ONLY_CANARY",
        client=client,
        git=LocalGit(policy, "ghp_PUBLISHER_ONLY_CANARY"),
    )
    return pub, api, inputs, state, remote_head


def test_exact_candidate_objects_push_once_without_checkout_or_build(backend):
    pub, api, inputs, state, head = backend
    result = api.publish(inputs)
    assert result["head_sha"] == head() == inputs["operation"]["target"]["candidate"]["head_sha"]
    assert result["draft"] is True and result["number"] == 123
    assert state["pushes"] == 1 and state["pulls"] == [123]
    assert api.publish(inputs) == result
    assert state["pushes"] == 1 and state["pulls"] == [123]
    assert all(not root.exists() for root in state["roots"])
    report = pub.success(inputs["operation"]) | {"pull_request": result}
    assert pub.report(inputs["operation"], report).status_code == 200
    assert pub.world.run()["phase"] == "WAIT_CI"


def test_human_commit_racing_after_preflight_is_preserved_by_explicit_lease(backend):
    _, api, inputs, state, head = backend
    state["race"] = True
    with pytest.raises(PublicationError) as error:
        api.publish(inputs)
    assert error.value.reason == "REMOTE_HEAD_CONFLICT"
    assert head() == inputs["operation"]["target"]["candidate"]["base_sha"]
    assert state["pulls"] == []
    assert not [item for item in state["http"] if item[0] == "POST"]
    assert all(not root.exists() for root in state["roots"])


@pytest.mark.parametrize("change", ["archive", "manifest", "tree", "commit", "write_policy"])
def test_source_or_actual_diff_mismatch_cannot_be_pushed(backend, change):
    _, api, inputs, state, head = backend
    supplied = copy.deepcopy(inputs)
    source = supplied["sources"][-1]
    candidate = source["candidate"]
    target = supplied["operation"]["target"]
    if change == "archive":
        source["archive"] = "not-base64"
    elif change == "manifest":
        candidate["metadata_body"]["file_manifest"]["src/example.py"] = "0" * 64
        target["candidate"] = candidate
    elif change == "tree":
        candidate["tree_sha"] = "0" * 40
        target["candidate"] = candidate
    elif change == "commit":
        candidate["metadata_body"]["commit_object"] = "YQ=="
        target["candidate"] = candidate
    else:
        target["policy"]["allowed_write_paths"] = ["docs"]
        target["policy_hash"] = digest(target["policy"])
    supplied["operation"]["target_hash"] = digest(target)
    with pytest.raises(PublicationError):
        api.publish(supplied)
    assert state["pushes"] == 0 and head() is None and state["pulls"] == []


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("PUT", "/pulls/123/merge"),
        ("POST", "/deployments"),
        ("POST", "/git/refs"),
        ("PATCH", "/pulls/123"),
    ],
)
def test_provider_operation_allowlist_never_sends_merge_deploy_or_arbitrary_write(
    backend, method, suffix
):
    _, api, inputs, state, _ = backend
    with pytest.raises(PublicationError) as error:
        api._request(method, inputs["operation"]["target"], suffix)
    assert error.value.reason == "PUBLISH_POLICY_DENIED" and state["http"] == []


def test_provider_repository_cannot_be_changed_independently_of_the_allowlisted_remote(backend):
    _, api, inputs, state, _ = backend
    target = copy.deepcopy(inputs["operation"]["target"])
    target["repository"] = "other/target"
    with pytest.raises(PublicationError) as error:
        api._request("POST", target, "/pulls", json={})
    assert error.value.reason == "PUBLISH_POLICY_DENIED" and state["http"] == []
