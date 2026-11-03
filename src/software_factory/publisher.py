"""Dedicated Publisher process with a durable journal; no model or build execution."""

import argparse
import fcntl
import ipaddress
import json
import os
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .contracts import PublicationReport, digest
from .publisher_backend import GitHubDraftPublisher, PublicationError
from .source_runner import SourcePolicy


class PublisherUnavailable(Exception):
    pass


def secret_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size > 4096
        ):
            raise ValueError("credential file must be a private, owned regular file")
        value = os.read(descriptor, 4097).decode().strip()
        if len(value) < 20 or any(c.isspace() for c in value):
            raise ValueError("invalid credential file")
        return value
    finally:
        os.close(descriptor)


class PublicationJournal:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("publisher journal cannot contain symlinks")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("publisher journal must be private and owned")

    @contextmanager
    def lock(self):
        fd = os.open(self.root / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise ValueError("publisher journal lock cannot be hard-linked")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PublisherUnavailable("PUBLISHER_ALREADY_RUNNING") from None
            yield
        finally:
            os.close(fd)

    def read(self):
        path = self.root / "pending.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if (
                info.st_nlink != 1
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > 2 * 1024 * 1024
            ):
                raise ValueError("invalid publisher journal")
            return json.loads(os.read(descriptor, 2 * 1024 * 1024 + 1))
        finally:
            os.close(descriptor)

    def save(self, value):
        temporary = self.root / "pending.tmp"
        if temporary.exists() or temporary.is_symlink():
            info = temporary.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise ValueError("invalid incomplete publisher journal write")
            temporary.unlink()
        descriptor = os.open(
            self.root / "pending.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.root / "pending.tmp", self.root / "pending.json")
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            (self.root / "pending.tmp").unlink(missing_ok=True)
            raise

    def clear(self):
        (self.root / "pending.json").unlink(missing_ok=True)


class PublisherSupervisor:
    def __init__(self, client, backend, journal, *, checkpoint=None):
        self.client, self.backend = client, backend
        credential = client.headers.get("Authorization")
        if not credential:
            raise ValueError("Publisher client needs its dedicated authentication header")
        namespace = digest({"control_plane": str(client.base_url), "credential": credential})
        base = Path(tempfile.gettempdir()).resolve() / ("factory-publisher-" + str(os.getuid()))
        PublicationJournal(
            base
        )  # Verify ownership and private permissions before creating children.
        self.journal = SharedPublicationJournal(journal, PublicationJournal(base / namespace))
        self.checkpoint = checkpoint or (lambda name: None)

    def request(self, method, path, *, body=None, key=None):
        try:
            response = self.client.request(
                method, path, json=body, headers={"Idempotency-Key": key} if key else {}
            )
        except httpx.HTTPError:
            raise PublisherUnavailable("CONTROL_PLANE_UNAVAILABLE") from None
        if response.status_code >= 500 or response.status_code == 429:
            raise PublisherUnavailable("CONTROL_PLANE_UNAVAILABLE")
        return response

    def run_once(self):
        with self.journal.lock():
            pending = self.journal.read()
            if pending is None:
                reconciliations = self.request("GET", "/v1/publisher/reconciliations")
                self.accept(reconciliations)
                if operations := reconciliations.json()["operations"]:
                    return self.reconcile(operations[0])
                queued = self.request("GET", "/v1/publisher/queue")
                self.accept(queued)
                if not queued.json()["operations"]:
                    return {"processed": False}
                item = queued.json()["operations"][0]
                pending = {
                    "job_id": item["job_id"],
                    "phase": "BEGIN",
                    "begin": {
                        "expected_run_revision": item["target"]["run_revision"],
                        "target_hash": item["target_hash"],
                    },
                    "begin_key": "publish-begin:" + digest(item),
                }
                self.journal.save(pending)
            if pending["phase"] == "BEGIN":
                result = self.request(
                    "POST",
                    f"/v1/publisher/jobs/{pending['job_id']}/begin",
                    body=pending["begin"],
                    key=pending["begin_key"],
                )
                if result.status_code in {403, 404, 409}:
                    self.journal.clear()
                    return {"processed": False, "state": "DISPATCH_REJECTED"}
                self.accept(result)
                operation = result.json()
                pending = {"operation": operation, "phase": "AUTHORIZED"}
                self.journal.save(pending)
            operation = pending["operation"]
            if pending["phase"] == "AUTHORIZED":
                response = self.request(
                    "GET", f"/v1/publisher/operations/{operation['operation_id']}/inputs"
                )
                if (
                    response.status_code == 409
                    and response.json().get("code") == "WORKSPACE_EXECUTION_STOPPED"
                ):
                    report = {"status": "CONFLICT", "reason": "PUBLISH_POLICY_DENIED"}
                    provider_requests = []
                else:
                    self.accept(response)
                    inputs = response.json()
                    # Durable before any provider call. A crash from this point recovers
                    # through read-only reconciliation, never by blindly repeating a write.
                    pending["phase"] = "STARTED"
                    self.journal.save(pending)
                    self.checkpoint("publisher.before_external")
                    try:
                        pull = self.backend.publish(inputs)
                        report = {"status": "SUCCEEDED", "pull_request": pull}
                    except PublicationError as error:
                        report = {
                            "status": "UNKNOWN" if error.unknown else "CONFLICT",
                            "reason": error.reason,
                            "observed_head_sha": error.observed_head,
                        }
                    except (ValueError, KeyError, TypeError):
                        report = {"status": "UNKNOWN", "reason": "PROVIDER_UNAVAILABLE"}
                    provider_requests = getattr(self.backend, "trace", [])
                pending["report"] = {
                    "delivery_id": "publish-result:" + operation["operation_id"],
                    "target_hash": operation["target_hash"],
                    **report,
                    "provider_requests": provider_requests,
                }
                pending["phase"] = "RESULT"
                self.journal.save(pending)
                self.checkpoint("publisher.after_external")
            elif pending["phase"] == "STARTED":
                pending["report"] = {
                    "delivery_id": "publish-result:" + operation["operation_id"],
                    "target_hash": operation["target_hash"],
                    "status": "UNKNOWN",
                    "reason": "PUBLISH_UNKNOWN",
                }
                pending["phase"] = "RESULT"
                self.journal.save(pending)
            return self.deliver(pending)

    def reconcile(self, operation):
        operation_id = operation["operation_id"]
        attempt = operation["reconcile_attempts"] + (operation["state"] == "UNKNOWN")
        claim = self.request(
            "POST",
            f"/v1/publisher/operations/{operation_id}/reconcile",
            body={"target_hash": operation["target_hash"], "attempt": attempt},
            key=f"publish-reconcile:{operation_id}:{attempt}",
        )
        if (
            claim.status_code == 409
            and claim.json()["code"] == "PUBLICATION_RECONCILIATION_NOT_DUE"
        ):
            return {"processed": False}
        self.accept(claim)
        self.checkpoint("publisher.before_reconcile")
        try:
            result = {"status": "SUCCEEDED", "pull_request": self.backend.reconcile(claim.json())}
        except PublicationError as error:
            result = {
                "status": "UNKNOWN",
                "reason": error.reason,
                "observed_head_sha": error.observed_head,
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            result = {"status": "UNKNOWN", "reason": "PROVIDER_UNAVAILABLE"}
        pending = {
            "phase": "RESULT",
            "operation": operation,
            "report": {
                "delivery_id": f"publish-reconcile-result:{operation_id}:{attempt}",
                "target_hash": operation["target_hash"],
                "reconcile_attempt": attempt,
                "provider_requests": getattr(self.backend, "trace", []),
                **result,
            },
        }
        self.journal.save(pending)
        self.checkpoint("publisher.after_reconcile")
        return self.deliver(pending)

    def deliver(self, pending):
        operation_id = pending["operation"]["operation_id"]
        report = PublicationReport.model_validate(pending["report"]).payload()
        result = self.request(
            "POST",
            f"/v1/publisher/operations/{operation_id}/reports",
            body=report,
            key="publish-report:" + digest(report),
        )
        if (
            report.get("reconcile_attempt") is not None
            and result.status_code == 409
            and result.json()["code"]
            in {"PUBLICATION_RECONCILIATION_STALE", "PUBLICATION_DELIVERY_CONFLICT"}
        ):
            self.journal.clear()
            return {"processed": False}
        self.accept(result)
        self.journal.clear()
        return {
            "processed": True,
            "operation_id": operation_id,
            "state": result.json()["operation"]["state"],
        }

    @staticmethod
    def accept(response):
        if response.status_code >= 300:
            raise PublisherUnavailable("CONTROL_PLANE_REQUEST_REJECTED")


class SharedPublicationJournal:
    """One credential has one durable dispatch slot even with different CLI working directories."""

    def __init__(self, local, shared):
        self.local, self.shared = local, shared

    @contextmanager
    def lock(self):
        with self.shared.lock(), self.local.lock():
            yield

    def read(self):
        return self.shared.read() or self.local.read()

    def save(self, value):
        self.shared.save(value)
        self.local.save(value)

    def clear(self):
        self.local.clear()
        self.shared.clear()


def main():
    parser = argparse.ArgumentParser(
        description="Trusted Draft PR publisher; no model or build tasks"
    )
    parser.add_argument("--control-plane-url", default="http://127.0.0.1:8000")
    parser.add_argument("--publisher-token-file", required=True)
    authentication = parser.add_mutually_exclusive_group(required=True)
    authentication.add_argument("--github-token-file")
    authentication.add_argument("--github-use-gh", action="store_true")
    parser.add_argument("--allowed-remote", action="append", required=True)
    parser.add_argument("--state-dir", default=".local/publisher")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    target = urlsplit(args.control_plane_url)
    try:
        if (
            not ipaddress.ip_address(target.hostname).is_loopback
            or target.scheme != "http"
            or (
                target.username
                or target.password
                or target.query
                or target.fragment
                or target.path not in {"", "/"}
            )
        ):
            parser.error(
                "this MVP Publisher connects only to a literal loopback HTTP Control Plane"
            )
    except ValueError:
        parser.error("invalid loopback Control Plane URL")
    token = secret_file(args.publisher_token_file)
    if args.github_token_file:
        provider_token = secret_file(args.github_token_file)
    else:
        provider_token = subprocess.check_output(
            ["gh", "auth", "token", "--hostname", "github.com"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    backend = GitHubDraftPublisher(SourcePolicy(tuple(args.allowed_remote)), provider_token)
    client = httpx.Client(
        base_url=args.control_plane_url,
        timeout=30,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + token},
    )
    supervisor = PublisherSupervisor(client, backend, PublicationJournal(args.state_dir))
    while True:
        try:
            result = supervisor.run_once()
            if result.get("processed") or args.once:
                print(json.dumps(result), flush=True)
        except (PublisherUnavailable, httpx.HTTPError):
            print(json.dumps({"state": "CONTROL_PLANE_UNAVAILABLE"}), flush=True)
            if args.once:
                raise SystemExit(1) from None
        if args.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
