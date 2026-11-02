import os
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, select, text

from software_factory.db import Database
from software_factory.models import Attempt, Job, Run


@contextmanager
def schema_database(parent, revision="head"):
    schema = "acceptance_" + uuid4().hex
    with parent.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = parent.engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    database = Database(url.render_as_string(hide_password=False))
    try:
        with patch.dict(
            os.environ, {"FACTORY_DATABASE_URL": url.render_as_string(hide_password=False)}
        ):
            command.upgrade(Config("alembic.ini"), revision)
            yield database
    finally:
        database.engine.dispose()
        with parent.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def dump(database):
    with database.engine.connect() as connection:
        metadata = MetaData()
        metadata.reflect(bind=connection)
        result = {}
        for name, table in metadata.tables.items():
            result[name] = sorted(
                [dict(row) for row in connection.execute(select(table)).mappings()],
                key=lambda row: str(sorted(row.items())),
            )
        return result


def current_attempt(world):
    with world.database.transaction() as session:
        return session.scalar(select(Attempt).order_by(Attempt.started_at.desc()).limit(1))


def jobs(world):
    with world.database.transaction() as session:
        return list(session.scalars(select(Job).where(Job.run_id == world.run_id)))


def reset_run(world):
    """Create a new isolated run using the same versioned task."""
    response = world.post(f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1})
    assert response.status_code == 202, response.text
    world.run_id = response.json()["run_id"]
    return world.run_id


def cancel_other_runs(world):
    with world.database.transaction() as session:
        for run in session.scalars(select(Run).where(Run.id != world.run_id)):
            run.status = "CANCELLED"
        for job in session.scalars(select(Job).where(Job.run_id != world.run_id)):
            job.state = "CANCELLED"


def worker_client(world, *, lose_completion=False, after_claim=None, completion_statuses=()):
    state = {"completion_requests": [], "lost": False, "paths": []}
    transient_statuses = list(completion_statuses)

    def handler(request):
        state["paths"].append(request.url.path)
        if request.url.path.endswith("/complete") and transient_statuses:
            state["completion_requests"].append(
                {
                    "key": request.headers["idempotency-key"],
                    "body": request.content.decode(),
                }
            )
            return httpx.Response(transient_statuses.pop(0), json={"code": "TRANSIENT"})
        response = world.client.request(
            request.method,
            request.url.path + ("?" + request.url.query.decode() if request.url.query else ""),
            content=request.content,
            headers={
                "Authorization": f"Bearer {world.worker_token}",
                "Content-Type": request.headers.get("Content-Type", "application/json"),
                **(
                    {"Idempotency-Key": request.headers["Idempotency-Key"]}
                    if "Idempotency-Key" in request.headers
                    else {}
                ),
            },
        )
        if request.url.path.endswith("/complete"):
            state["completion_requests"].append(
                {
                    "key": request.headers["idempotency-key"],
                    "body": request.content.decode(),
                }
            )
            if lose_completion and not state["lost"] and response.status_code == 200:
                state["lost"] = True
                raise httpx.ReadError("accepted response dropped", request=request)
        if request.url.path.endswith("/claims") and response.status_code == 200 and after_claim:
            after_claim(response.json())
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": "application/json"},
        )

    return httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)), state
