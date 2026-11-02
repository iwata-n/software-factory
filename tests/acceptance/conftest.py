import json
import os
import platform
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from scripts.acceptance_support import required_criteria, sha, source_manifest
from software_factory.security import redact


class Reporter:
    def __init__(self):
        self.manifest = source_manifest()
        self.output = Path(os.environ.get("FACTORY_ACCEPTANCE_OUTPUT", ".local/acceptance/current"))
        self.cases = {}
        self.expected = {}
        self.observations = {}
        self.postgres_version = None
        self.legacy = {}

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        if not os.environ.get("TEST_DATABASE_URL"):
            raise pytest.UsageError(
                "TEST_DATABASE_URL is required for acceptance tests; skipping is forbidden"
            )
        for item in items:
            ids = [ac for marker in item.iter_markers("ac") for ac in marker.args]
            self.expected[item.nodeid] = ids

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        if report.when == "call" or report.failed or report.skipped:
            previous = self.cases.get(item.nodeid)
            if previous and previous["result"] == "FAIL":
                return
            result = "FAIL" if report.failed else "未検証" if report.skipped else "PASS"
            self.cases[item.nodeid] = {
                "test_case_id": item.nodeid,
                "result": result,
                "duration_seconds": report.duration,
                "observations": self.observations.get(item.nodeid, []),
                "fixture_parameters": json.loads(
                    json.dumps(
                        getattr(getattr(item, "callspec", None), "params", {}),
                        default=lambda value: getattr(value, "__qualname__", type(value).__name__),
                    )
                ),
            }

    def pytest_sessionfinish(self, session):
        self.output.mkdir(parents=True, exist_ok=True)
        metadata = {
            **self.manifest,
            "executed_at": datetime.now(UTC).isoformat(),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "postgresql": self.postgres_version,
                "runner_image_digest": os.environ.get("FACTORY_RUNNER_IMAGE_DIGEST"),
            },
            "github_sha": os.environ.get("GITHUB_SHA"),
            "github_run_url": (
                f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                f"{os.environ['GITHUB_RUN_ID']}"
                if os.environ.get("GITHUB_RUN_ID")
                else None
            ),
        }
        (self.output / "manifest.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False)
        )
        all_ids = required_criteria() + [f"C-{n:02d}" for n in range(1, 7)]
        results = []
        for ac in all_ids:
            names = [name for name, ids in self.expected.items() if ac in ids]
            cases = [
                self.cases.get(name, {"test_case_id": name, "result": "未検証"}) for name in names
            ]
            cases = redact(cases)
            status = (
                "FAIL"
                if any(c["result"] == "FAIL" for c in cases)
                else "PASS"
                if cases and all(c["result"] == "PASS" for c in cases)
                else "未検証"
            )
            if ac == "SF-01-AC06" and status == "PASS":
                status = (
                    "未検証"  # A completed external run + downloaded artifact must also be audited.
                )
            if ac == "C-01":
                legacy = [case for name, case in self.cases.items() if not self.expected.get(name)]
                if not legacy or any(c["result"] != "PASS" for c in legacy):
                    status = "FAIL" if any(c["result"] == "FAIL" for c in legacy) else "未検証"
            record = {
                "ac_id": ac,
                "revision": metadata["revision"],
                "code_digest": metadata["code_digest"],
                "result": status,
                "executed_at": metadata["executed_at"],
                "environment": metadata["environment"],
                "fixture_digest": sha(
                    json.dumps(
                        {
                            "code_digest": self.manifest["code_digest"],
                            "case_parameters": {
                                case["test_case_id"]: case.get("fixture_parameters", {})
                                for case in cases
                            },
                        },
                        sort_keys=True,
                    ).encode()
                ),
                "configuration_version": "factory-acceptance-2",
                "test_case_ids": names,
                "expected": "all collected cases and the criterion's documented assertions pass",
                "observed": redact(cases),
                "evidence_paths": ["manifest.json", "junit.xml"],
                "external_urls": [],
            }
            target = self.output / (ac[:5] if ac.startswith("SF-") else "common")
            target.mkdir(exist_ok=True)
            (target / f"{ac}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
            results.append({"ac_id": ac, "result": status})
        (self.output / "summary.json").write_text(json.dumps({"criteria": results}, indent=2))


def pytest_configure(config):
    config.pluginmanager.register(Reporter(), "factory-acceptance-reporter")


@pytest.fixture(scope="session", autouse=True)
def require_postgresql17(migrated_database, pytestconfig):
    with migrated_database.engine.connect() as connection:
        version = connection.scalar(text("SHOW server_version_num"))
        assert int(version) // 10000 == 17, "Acceptance requires actual PostgreSQL 17"
        reporter = pytestconfig.pluginmanager.get_plugin("factory-acceptance-reporter")
        reporter.postgres_version = connection.scalar(text("SELECT version()"))


@pytest.fixture
def evidence(request, pytestconfig):
    reporter = pytestconfig.pluginmanager.get_plugin("factory-acceptance-reporter")
    records = reporter.observations.setdefault(request.node.nodeid, [])

    def observe(expected, observed, **identifiers):
        records.append(redact({"expected": expected, "observed": observed, **identifiers}))

    return observe


@pytest.fixture
def clock(database):
    from software_factory.db import db_now

    with database.transaction() as session:
        initial = db_now(session)

    class Clock:
        value = initial

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    current = Clock()
    database.sessions.configure(info={"clock": current})
    try:
        yield current
    finally:
        database.sessions.configure(info={"clock": None})
