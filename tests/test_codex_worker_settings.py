from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from software_factory.api import create_app
from software_factory.models import AuditEvent, Principal, Workspace

ORIGIN = "https://factory.example"
PATH = "/v1/operations/codex-workers"


def test_worker_count_defaults_to_ten_and_operator_changes_persist_and_are_audited(world):
    with world.database.transaction() as session:
        actor = session.scalar(
            select(Principal).where(
                Principal.token_hash.is_not(None),
                Principal.role == "USER",
                Principal.workspace_id == world.workspace_id,
            )
        )
        actor.user_access = "OPERATOR"
    headers = {
        "Authorization": f"Bearer {world.user_token}",
        "Origin": ORIGIN,
        "Idempotency-Key": str(uuid4()),
    }
    with TestClient(create_app(world.database), base_url=ORIGIN, headers=headers) as client:
        state = client.get(PATH)
        assert state.headers["Cache-Control"] == "no-store"
        assert state.json() == {"worker_count": 10, "max_worker_count": 32}
        saved = client.post(PATH, json={"worker_count": 12})
        assert saved.status_code == 200 and saved.json()["worker_count"] == 12
        assert client.post(PATH, json={"worker_count": 12}).json() == saved.json()
        assert client.post(PATH, json={"worker_count": 13}).json()["code"] == "IDEMPOTENCY_CONFLICT"
    # A new app reads the durable setting; another workspace keeps its own default.
    with TestClient(create_app(world.database), base_url=ORIGIN, headers=headers) as client:
        assert client.get(PATH).json()["worker_count"] == 12
        assert (
            client.get(PATH, headers={"Authorization": f"Bearer {world.other_user_token}"}).json()[
                "worker_count"
            ]
            == 10
        )
    with world.database.transaction() as session:
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.operation == "codex:workers")
        ).all()
        assert len(audit) == 1


@pytest.mark.parametrize("count", [0, 33, -1, True, "10", 1.5])
def test_worker_count_rejects_invalid_values(world, count):
    with TestClient(create_app(world.database), base_url=ORIGIN) as client:
        response = client.post(
            PATH,
            json={"worker_count": count},
            headers={
                "Authorization": f"Bearer {world.user_token}",
                "Origin": ORIGIN,
                "Idempotency-Key": str(uuid4()),
            },
        )
    assert response.status_code == 422
    with world.database.transaction() as session:
        assert session.get(Workspace, world.workspace_id).codex_worker_count == 10


def test_viewers_and_cross_origin_requests_cannot_resize_workers(world):
    with TestClient(create_app(world.database), base_url=ORIGIN) as client:
        headers = {
            "Authorization": f"Bearer {world.user_token}",
            "Origin": "https://wrong.example",
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post(PATH, json={"worker_count": 1}, headers=headers).status_code == 403
        with world.database.transaction() as session:
            actor = session.scalar(
                select(Principal).where(
                    Principal.workspace_id == world.workspace_id, Principal.role == "USER"
                )
            )
            actor.user_access = "VIEWER"
        headers["Origin"] = ORIGIN
        assert client.post(PATH, json={"worker_count": 1}, headers=headers).status_code == 403


@pytest.mark.parametrize("count", [1, 32])
def test_worker_count_boundaries(world, count):
    with TestClient(create_app(world.database), base_url=ORIGIN) as client:
        assert (
            client.post(
                PATH,
                json={"worker_count": count},
                headers={
                    "Authorization": f"Bearer {world.user_token}",
                    "Origin": ORIGIN,
                    "Idempotency-Key": str(uuid4()),
                },
            ).json()["worker_count"]
            == count
        )
