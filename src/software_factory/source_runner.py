"""Separate, read-only source runner API; this process has no database credentials."""

import base64
import hmac
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field

from .contracts import Contract, Sha, safe_path
from .sandbox import DockerRunner, RunnerError


class SourcePolicyDenied(ValueError):
    pass


def canonical_remote(url: str) -> str:
    if not isinstance(url, str) or not url.isascii() or any(c.isspace() for c in url):
        raise SourcePolicyDenied("SOURCE_URL_DENIED")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise SourcePolicyDenied("SOURCE_URL_DENIED") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)
    ):
        raise SourcePolicyDenied("SOURCE_URL_DENIED")
    path = parsed.path.removesuffix(".git")
    if any(part in (".", "..") or part.startswith("-") for part in path.split("/")[1:]):
        raise SourcePolicyDenied("SOURCE_URL_DENIED")
    return "https://" + parsed.hostname.lower() + path + ".git"


class SourcePolicy:
    def __init__(self, allowed_remotes: tuple[str, ...]):
        self.allowed = frozenset(canonical_remote(remote) for remote in allowed_remotes)

    def authorize(self, remote: str) -> str:
        canonical = canonical_remote(remote)
        if canonical not in self.allowed:
            raise SourcePolicyDenied("SOURCE_REPOSITORY_NOT_ALLOWED")
        return canonical


class ResolveInput(Contract):
    remote_url: str = Field(min_length=1, max_length=1000)
    base_ref: str = Field(min_length=1, max_length=200)


class FetchInput(Contract):
    remote_url: str = Field(min_length=1, max_length=1000)
    sha: Sha


class SourceSnapshot(Contract):
    """An operator-provisioned, immutable source bundle; never a path supplied by an agent."""

    remote_url: str
    base_ref: str = Field(pattern=r"^local/[A-Za-z0-9._/-]{1,180}$")
    source: dict


def configured_snapshot():
    name = os.environ.get("FACTORY_SOURCE_SNAPSHOT_FILE")
    if not name:
        return None
    path = Path(name)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_size > 16 * 1024 * 1024
        or any(parent.is_symlink() for parent in path.parents)
    ):
        raise ValueError("source snapshot must be an owner-only regular file")
    snapshot = SourceSnapshot.model_validate_json(path.read_text())
    if not safe_path(snapshot.base_ref):
        raise ValueError("source snapshot ref is not canonical")
    return snapshot


def create_source_app(
    policy: SourcePolicy,
    runner: DockerRunner,
    token: str,
    *,
    snapshot: SourceSnapshot | None = None,
) -> FastAPI:
    if len(token) < 20:
        raise ValueError("a dedicated source-runner credential is required")
    app = FastAPI(title="Factory read-only source runner")
    snapshot_remote = policy.authorize(snapshot.remote_url) if snapshot else None

    @app.exception_handler(RequestValidationError)
    def validation_error(request, error):
        return JSONResponse({"code": "VALIDATION_ERROR"}, status_code=422)

    def execute(remote, ref, supplied, *, pinned=False):
        if not supplied or not hmac.compare_digest(supplied, "Bearer " + token):
            return JSONResponse({"code": "UNAUTHENTICATED"}, status_code=401)
        try:
            url = policy.authorize(remote)
            if (
                snapshot
                and url == snapshot_remote
                and ref == (snapshot.source["sha"] if pinned else snapshot.base_ref)
            ):
                source = snapshot.source
                # The existing isolated inspector checks archive, tree, commit SHA and manifest.
                result = runner.execute_candidate(
                    {
                        "operation": "inspect_candidate",
                        "candidate": {
                            "head_sha": source["sha"],
                            "tree_sha": source["tree"],
                            "source_digest": source["source_digest"],
                            "metadata_body": {
                                key: source[key] for key in ("file_manifest", "commit_object")
                            },
                        },
                        "profile": {},
                    },
                    base64.b64decode(source["archive"], validate=True),
                )
                body = source | {"source_origin": "operator_snapshot"}
            else:
                if ref.startswith("local/"):
                    raise SourcePolicyDenied("LOCAL_SOURCE_SNAPSHOT_NOT_FOUND")
                result = runner.execute(
                    {
                        "operation": "fetch",
                        "url": url,
                        "ref": ref,
                        **({"sha": ref} if pinned else {}),
                    },
                    network=True,
                )
                body = result.body
            body = body | {
                "image_digest": result.image_digest,
                "container_id": result.container_id,
                "isolation": result.isolation,
            }
            if not pinned:
                body.pop("archive", None)
            return body
        except SourcePolicyDenied as error:
            return JSONResponse({"code": str(error), "category": "POLICY"}, status_code=422)
        except RunnerError as error:
            status = 503 if error.category == "INFRASTRUCTURE" else 422
            return JSONResponse(
                {"code": error.code, "category": error.category}, status_code=status
            )
        except (KeyError, ValueError):
            return JSONResponse(
                {"code": "SOURCE_SNAPSHOT_INVALID", "category": "POLICY"}, status_code=422
            )

    @app.post("/v1/sources/resolve")
    def resolve(data: ResolveInput, authorization: str | None = Header(default=None)):
        return execute(data.remote_url, data.base_ref, authorization)

    @app.post("/v1/sources/fetch")
    def fetch(data: FetchInput, authorization: str | None = Header(default=None)):
        return execute(data.remote_url, data.sha, authorization, pinned=True)

    return app


def main():
    import uvicorn

    policy = SourcePolicy(tuple(filter(None, os.environ["FACTORY_ALLOWED_REMOTES"].split(","))))
    runner = DockerRunner(os.environ["FACTORY_RUNNER_IMAGE_DIGEST"])
    app = create_source_app(
        policy, runner, os.environ["FACTORY_SOURCE_RUNNER_TOKEN"], snapshot=configured_snapshot()
    )
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ.get("FACTORY_SOURCE_RUNNER_PORT", "8010"))
    )


if __name__ == "__main__":
    main()
