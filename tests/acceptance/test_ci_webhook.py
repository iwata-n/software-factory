import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from software_factory.api import create_app
from software_factory.models import CIWebhookDelivery, Repository

SECRET = "fixture-webhook-signing-secret-only"
NAME = "fixture/ci"


@pytest.fixture
def webhook(world, monkeypatch):
    integration_id = str(uuid4())
    config = {
        "workspace_id": world.workspace_id,
        "repository_id": world.repository_id,
        "github_repository_id": 9001,
        "github_repository": NAME,
        "secret": SECRET,
    }
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOKS", json.dumps({integration_id: config}))
    with world.database.transaction() as session:
        session.get(Repository, world.repository_id).remote_url = f"https://github.com/{NAME}.git"

    def send(body, *, delivery=None, secret=SECRET, event="workflow_run", integration=None):
        encoded = json.dumps(body).encode()
        headers = {
            "X-Hub-Signature-256": "sha256="
            + hmac.new(secret.encode(), encoded, "sha256").hexdigest(),
            "X-GitHub-Delivery": delivery or str(uuid4()),
            "X-GitHub-Event": event,
        }
        with TestClient(create_app(world.database)) as http:
            return http.post(
                f"/v1/webhooks/github/{integration or integration_id}",
                content=encoded,
                headers=headers,
            )

    return send


@pytest.mark.ac("SF-18-AC01", "SF-18-AC02")
def test_signed_delivery_is_durable_and_conflicting_reuse_is_rejected(world, webhook, evidence):
    delivery = str(uuid4())
    body = {"repository": {"id": 9001, "full_name": NAME}, "untrusted": "discard this text"}
    first = webhook(body, delivery=delivery)
    replay = webhook(body, delivery=delivery)
    conflict = webhook({**body, "untrusted": "changed"}, delivery=delivery)
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json() == {"accepted": True}
    assert conflict.status_code == 409
    with world.database.transaction() as session:
        (saved,) = list(session.scalars(select(CIWebhookDelivery)))
        assert saved.delivery_id == delivery
        assert saved.repository_id == world.repository_id
        assert SECRET not in repr(vars(saved))
        assert "discard this text" not in repr(vars(saved))
    evidence(
        "one durable signed delivery, exact replay and conflicting reuse rejected",
        {
            "delivery_id": delivery,
            "rows": 1,
            "initial": 202,
            "replay": 202,
            "conflict": 409,
            "payload_and_secret_retained": False,
        },
    )


@pytest.mark.ac("SF-18-AC01")
@pytest.mark.parametrize(
    ("repository", "secret", "status"),
    [
        ({"id": 9001, "full_name": NAME}, "wrong", 401),
        ({"id": 9002, "full_name": NAME}, SECRET, 404),
        ({"id": 9001, "full_name": "other/ci"}, SECRET, 404),
    ],
)
def test_invalid_delivery_does_not_enter_inbox(
    world, webhook, repository, secret, status, evidence
):
    before = world.run()
    response = webhook({"repository": repository}, secret=secret)
    assert world.run() == before
    assert response.status_code == status
    with world.database.transaction() as session:
        assert session.scalar(select(CIWebhookDelivery)) is None
    evidence("rejected delivery has no Inbox or Run changes", {"status": status, "inbox_rows": 0})


@pytest.mark.ac("SF-18-AC02")
def test_concurrent_replays_commit_one_inbox_row(world, webhook, evidence):
    delivery = str(uuid4())
    body = {"repository": {"id": 9001, "full_name": NAME}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(webhook, body, delivery=delivery) for _ in range(2)]
        assert [request.result().status_code for request in requests] == [202, 202]
    with world.database.transaction() as session:
        assert len(list(session.scalars(select(CIWebhookDelivery)))) == 1
    evidence("concurrent identical delivery inserts once", {"delivery_id": delivery, "rows": 1})


@pytest.mark.ac("SF-18-AC01")
def test_integration_cannot_cross_workspace(world, webhook, monkeypatch, evidence):
    import os

    integrations = json.loads(os.environ["FACTORY_GITHUB_WEBHOOKS"])
    next(iter(integrations.values()))["workspace_id"] = world.other_workspace_id
    monkeypatch.setenv("FACTORY_GITHUB_WEBHOOKS", json.dumps(integrations))
    before = world.run()
    assert webhook({"repository": {"id": 9001, "full_name": NAME}}).status_code == 404
    with world.database.transaction() as session:
        assert session.scalar(select(CIWebhookDelivery)) is None
    assert world.run() == before
    evidence("foreign Workspace integration is rejected", {"status": 404, "inbox_rows": 0})


@pytest.mark.ac("SF-18-AC01")
def test_signed_delivery_for_unknown_integration_has_no_side_effects(world, webhook, evidence):
    before = world.run()
    response = webhook({"repository": {"id": 9001, "full_name": NAME}}, integration=uuid4())
    assert response.status_code == 404
    assert world.run() == before
    with world.database.transaction() as session:
        assert session.scalar(select(CIWebhookDelivery)) is None
    evidence("unknown Integration is rejected", {"status": 404, "inbox_rows": 0})
