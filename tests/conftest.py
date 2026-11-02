import itertools
import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from software_factory.api import create_app
from software_factory.db import Database, db_now
from software_factory.models import Enrollment, Principal, Workspace
from software_factory.service import issue_token, token_hash


@pytest.fixture(scope="session")
def migrated_database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset; PostgreSQL integration tests were not run")
    # A random schema keeps the tests separate from all existing application data.
    schema = "factory_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    old_url = os.environ.get("FACTORY_DATABASE_URL")
    os.environ["FACTORY_DATABASE_URL"] = scoped_url.render_as_string(hide_password=False)
    database = Database(os.environ["FACTORY_DATABASE_URL"])
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield database
    finally:
        database.engine.dispose()
        if old_url is None:
            os.environ.pop("FACTORY_DATABASE_URL", None)
        else:
            os.environ["FACTORY_DATABASE_URL"] = old_url
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def database(migrated_database):
    with migrated_database.engine.begin() as connection:
        connection.execute(text("TRUNCATE workspaces CASCADE"))
    return migrated_database


def example_policy():
    return {
        "allowed_write_paths": ["src", "tests"],
        "protected_paths": [".github", "migrations", "tests/required"],
        "verification": {"profile_id": "default", "required_checks": ["unit_tests", "review"]},
        "execution_image_digest": "sha256:" + "0" * 64,
        "run_budget_microusd": 1_000_000,
        "job_reservation_microusd": 100_000,
    }


def example_spec(repository_id):
    return {
        "schema_version": 1,
        "task": {
            "title": "Add empty state",
            "repository_id": repository_id,
            "objective": "Display an empty state when no records exist",
            "base_ref": "main",
            "acceptance_criteria": [{"id": "AC-1", "description": "Show the empty state"}],
            "constraints": ["Do not remove required checks"],
        },
        "verification": {"profile_id": "default", "required_checks": ["unit_tests", "review"]},
    }


@dataclass
class World:
    client: TestClient
    database: Database
    workspace_id: str
    other_workspace_id: str
    user_token: str = field(repr=False)
    other_user_token: str = field(repr=False)
    worker_token: str = field(repr=False)
    worker_id: str
    project_id: str
    repository_id: str
    task_id: str
    run_id: str

    def post(self, path, body, *, token=None, key=None):
        return self.client.post(
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {token or self.user_token}",
                "Idempotency-Key": key or str(uuid4()),
            },
        )

    def get(self, path, *, token=None):
        return self.client.get(
            path, headers={"Authorization": f"Bearer {token or self.user_token}"}
        )

    def claim(self, *, token=None):
        return self.post(
            "/v1/worker/claims", {"protocol_version": 1}, token=token or self.worker_token
        )

    def run(self):
        return self.get(f"/v1/runs/{self.run_id}").json()

    def make_worker(self, *, capabilities=None):
        enrollment_token = issue_token()
        with self.database.transaction() as session:
            session.add(
                Enrollment(
                    workspace_id=self.workspace_id,
                    token_hash=token_hash(enrollment_token),
                    expires_at=db_now(session) + 600,
                    capabilities=capabilities or ["PLAN", "IMPLEMENT", "adapter:fake"],
                )
            )
        return self.client.post(
            "/v1/worker/enroll",
            json={
                "token": enrollment_token,
                "name": "another-worker",
            },
        ).json()


@pytest.fixture
def world(database):
    user_token, other_token, enrollment_token = [issue_token() for _ in range(3)]
    with database.transaction() as session:
        workspace, other = Workspace(name="test"), Workspace(name="other")
        session.add_all([workspace, other])
        session.flush()
        session.add_all(
            [
                Principal(
                    workspace_id=workspace.id, role="USER", token_hash=token_hash(user_token)
                ),
                Principal(workspace_id=other.id, role="USER", token_hash=token_hash(other_token)),
                Enrollment(
                    workspace_id=workspace.id,
                    token_hash=token_hash(enrollment_token),
                    capabilities=["PLAN", "IMPLEMENT", "adapter:fake"],
                    expires_at=db_now(session) + 600,
                ),
            ]
        )
        workspace_id, other_id = workspace.id, other.id
    client = TestClient(create_app(database, reconcile_interval=3600))
    # Tests drive reconciliation explicitly to make fault timing reproducible.
    keys = itertools.count()

    def post(path, body):
        reply = client.post(
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {user_token}",
                "Idempotency-Key": str(next(keys)),
            },
        )
        assert reply.status_code < 300, reply.text
        return reply.json()

    worker = client.post(
        "/v1/worker/enroll",
        json={
            "token": enrollment_token,
            "name": "test-worker",
        },
    )
    assert worker.status_code == 201, worker.text
    project_id = post(
        f"/v1/workspaces/{workspace_id}/projects",
        {
            "name": "example",
            "policy": example_policy(),
        },
    )["project_id"]
    repository_id = post(
        f"/v1/projects/{project_id}/repositories",
        {
            "name": "example",
            "base_sha": "a" * 40,
        },
    )["repository_id"]
    task_id = post(f"/v1/projects/{project_id}/tasks", example_spec(repository_id))["task_id"]
    run_id = post(f"/v1/tasks/{task_id}/runs", {"spec_version": 1})["run_id"]
    yield World(
        client,
        database,
        workspace_id,
        other_id,
        user_token,
        other_token,
        worker.json()["worker_token"],
        worker.json()["worker_id"],
        project_id,
        repository_id,
        task_id,
        run_id,
    )
    client.close()
