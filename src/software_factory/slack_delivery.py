"""Durable, workspace-scoped Slack notifications backed by the Run Outbox."""

import os
import re
from dataclasses import dataclass
from typing import Literal

import httpx
from sqlalchemy import and_, or_, select

from .db import Database, db_now
from .models import OutboxEvent, Run, SlackDelivery, new_id
from .security import redact

NOTIFICATION = "SLACK_RUN_RESULT"
METADATA_TYPE = "software_factory_delivery"
MAX_ATTEMPTS = 3
CLAIM_SECONDS = 90
MESSAGE_ID = re.compile(r"[0-9]{1,18}\.[0-9]{1,12}")
GITHUB_REF = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull/[0-9]+|actions/runs/[0-9]+"
    r"(?:/job/[0-9]+)?)",
    re.ASCII,
)
GITHUB_CHECK_REF = re.compile(
    r"https://api\.github\.com/repos/[\w.-]+/[\w.-]+/check-runs/[0-9]+", re.ASCII
)
STOP_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")


def enqueue_result(session, run: Run, *, pr_url: str | None = None, ci_urls=()) -> None:
    """Call once on entering a result state, in the same transaction as the Run."""
    if run.status not in {"SUCCEEDED", "FAILED", "BLOCKED"}:
        raise ValueError("Slack result requires a reportable Run status")
    session.add(
        OutboxEvent(
            workspace_id=run.workspace_id,
            run_id=run.id,
            kind=NOTIFICATION,
            created_at=db_now(session),
            body={
                "task_id": run.task_id,
                "spec_version": run.spec_version,
                "run_id": run.id,
                "status": str(run.status),
                "candidate_id": run.current_candidate_id,
                "spent_microusd": run.spent_microusd,
                "cost_state": run.cost_state,
                "reason": (run.stop_reason if STOP_CODE.fullmatch(run.stop_reason or "") else None),
                "next_action": "Review the blocked Run" if run.status == "BLOCKED" else None,
                "pr_url": pr_url if pr_url and GITHUB_REF.fullmatch(pr_url) else None,
                "ci_urls": [
                    url
                    for url in ci_urls
                    if GITHUB_REF.fullmatch(url) or GITHUB_CHECK_REF.fullmatch(url)
                ][:20],
            },
        )
    )


@dataclass(frozen=True)
class Result:
    state: Literal["SENT", "RETRY", "FAILED", "UNKNOWN"]
    reason: str | None = None
    message_id: str | None = None
    retry_after: int = 0


class SlackClient:
    def __init__(self, client: httpx.Client, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.token = token

    def post(self, channel: str, key: str, body: dict) -> Result:
        lines = [
            f"Run {body['run_id']} · {body['status']}",
            f"Task {body['task_id']} · spec {body['spec_version']}",
            f"Cost {body['spent_microusd']} microUSD ({body['cost_state']})",
        ]
        for name in ("candidate_id", "reason", "next_action", "pr_url"):
            if value := body.get(name):
                lines.append(f"{name} {value}")
        lines.extend(body.get("ci_urls", []))
        try:
            response = self.client.post(
                "https://slack.com/api/chat.postMessage",
                headers=self.headers,
                json={
                    "channel": channel,
                    "text": redact("\n".join(lines), (self.token,)),
                    "mrkdwn": False,
                    "parse": "none",
                    "unfurl_links": False,
                    "unfurl_media": False,
                    "metadata": {"event_type": METADATA_TYPE, "event_payload": {"key": key}},
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return Result("RETRY", "CONNECT_FAILED")
        except httpx.RequestError:
            return Result("UNKNOWN", "AMBIGUOUS_RESPONSE")

        if response.status_code == 429:
            return Result("RETRY", "RATE_LIMITED", retry_after=retry_after(response))
        if 400 <= response.status_code < 500:
            return Result("FAILED", "SLACK_REJECTED")
        body = response_json(response)
        if response.is_success and body.get("ok") is True and body.get("channel") == channel:
            if message_id := slack_message_id(body):
                return Result("SENT", message_id=message_id)
        if body.get("ok") is False and body.get("error") in {
            "ratelimited",
            "service_unavailable",
        }:
            return Result("RETRY", "SLACK_UNAVAILABLE", retry_after=retry_after(response))
        if (
            response.is_success
            and body.get("ok") is False
            and body.get("error")
            in {
                "invalid_auth",
                "token_revoked",
                "account_inactive",
                "missing_scope",
                "channel_not_found",
                "not_in_channel",
                "no_permission",
                "is_archived",
                "invalid_arguments",
                "invalid_metadata_schema",
            }
        ):
            return Result("FAILED", "SLACK_REJECTED")
        # An unstructured 5xx or Slack internal error can follow a committed post.
        return Result("UNKNOWN", "AMBIGUOUS_RESPONSE")

    def find(self, channel: str, key: str) -> str | None:
        try:
            response = self.client.get(
                "https://slack.com/api/conversations.history",
                headers=self.headers,
                params={"channel": channel, "include_all_metadata": "true", "limit": 100},
            )
        except httpx.RequestError:
            return None
        body = response_json(response)
        if not response.is_success or body.get("ok") is not True:
            return None
        messages = body.get("messages")
        if not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("metadata") == {
                "event_type": METADATA_TYPE,
                "event_payload": {"key": key},
            }:
                return slack_message_id(message)
        return None


def response_json(response: httpx.Response) -> dict:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def slack_message_id(body: dict) -> str | None:
    ts = body.get("ts")
    return ts if isinstance(ts, str) and MESSAGE_ID.fullmatch(ts) else None


def retry_after(response: httpx.Response) -> int:
    header = response.headers.get("Retry-After", "")
    # Slack provides a delay in seconds.
    return int(header) if re.fullmatch(r"[0-9]{1,12}", header) else 0


class SlackDispatcher:
    def __init__(self, db: Database, slack: SlackClient, workspace_id: str, channel: str):
        if not re.fullmatch(r"[CGD][A-Z0-9]{1,31}", channel):
            raise ValueError("Invalid Slack channel ID")
        self.db = db
        self.slack = slack
        self.workspace_id = workspace_id
        self.channel = channel

    def deliver_one(self) -> str | None:
        with self.db.transaction() as session:
            now = db_now(session)
            row = session.execute(
                select(OutboxEvent, SlackDelivery)
                .outerjoin(SlackDelivery, SlackDelivery.outbox_id == OutboxEvent.id)
                .where(
                    OutboxEvent.workspace_id == self.workspace_id,
                    OutboxEvent.kind == NOTIFICATION,
                    or_(
                        SlackDelivery.outbox_id.is_(None),
                        and_(
                            SlackDelivery.state.in_(("RETRY", "SENDING")),
                            SlackDelivery.due_at <= now,
                        ),
                    ),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(1)
                .with_for_update(of=OutboxEvent, skip_locked=True)
            ).first()
            if row is None:
                return None
            outbox, delivery = row
            # Reload the ledger after locking Outbox. A concurrent claim may have committed
            # after the join's statement snapshot was taken.
            delivery = session.get(SlackDelivery, outbox.id, populate_existing=True)
            if delivery and (delivery.state not in {"RETRY", "SENDING"} or delivery.due_at > now):
                return None
            recover = delivery is not None and delivery.state == "SENDING"
            if delivery is None:
                delivery = SlackDelivery(outbox_id=outbox.id, channel=self.channel, attempts=0)
                session.add(delivery)
            if delivery.channel != self.channel:
                delivery.state, delivery.reason = "UNKNOWN", "DESTINATION_CHANGED"
                return delivery.state
            if not recover:
                delivery.attempts += 1
            delivery.state, delivery.due_at, delivery.claim_id = (
                "SENDING",
                now + CLAIM_SECONDS,
                new_id(),
            )
            key, payload, claim, channel = (
                outbox.id,
                outbox.body,
                delivery.claim_id,
                delivery.channel,
            )

        result = (
            Result("UNKNOWN", "SEND_INTERRUPTED")
            if recover
            else self.slack.post(channel, key, payload)
        )
        if result.state == "UNKNOWN":
            if message_id := self.slack.find(channel, key):
                result = Result("SENT", message_id=message_id)

        with self.db.transaction() as session:
            # All writers use the Outbox row as the mutex, including finalization.
            session.get(OutboxEvent, key, with_for_update=True)
            delivery = session.get(SlackDelivery, key)
            if delivery.claim_id != claim or delivery.state != "SENDING":
                return delivery.state
            delivery.state = result.state
            delivery.message_id = result.message_id
            delivery.reason = result.reason
            if result.state == "RETRY":
                if delivery.attempts >= MAX_ATTEMPTS:
                    delivery.state = "FAILED"
                    delivery.reason = "RETRIES_EXHAUSTED"
                else:
                    delay = max(2**delivery.attempts, result.retry_after)
                    delivery.due_at = db_now(session) + delay
            return delivery.state


def main() -> None:
    """Drain currently due notifications. Invoke periodically in the configured environment."""
    workspace = os.environ["FACTORY_SLACK_WORKSPACE_ID"]
    channel = os.environ["FACTORY_SLACK_CHANNEL_ID"]
    token = os.environ["FACTORY_SLACK_BOT_TOKEN"]
    if not workspace or not token:
        raise SystemExit("Slack workspace and token are required")
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        dispatcher = SlackDispatcher(Database(), SlackClient(client, token), workspace, channel)
        while dispatcher.deliver_one() is not None:
            pass


if __name__ == "__main__":
    main()
