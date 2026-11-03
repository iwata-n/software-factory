"""Read one allowed Jira Cloud issue and deliver its dedicated TaskSpec field."""

import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

from .jira_input import JiraInput, JiraMapping, configured_jira
from .security import redact
from .worker import validate_api_url


def read_issue(client, mapping: JiraMapping, issue_key, email, jira_token):
    if not re.fullmatch(rf"{re.escape(mapping.project_key)}-[0-9]{{1,20}}", issue_key):
        raise ValueError("Issue must belong to the configured Jira project")
    response = client.get(
        f"{mapping.site}/rest/api/3/issue/{issue_key}",
        params={"fields": f"project,updated,{mapping.field_id}"},
        auth=httpx.BasicAuth(email, jira_token),
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    response.raise_for_status()
    try:
        issue = response.json()
        fields = issue["fields"]
        if issue["key"] != issue_key or fields["project"]["key"] != mapping.project_key:
            raise ValueError("Jira response identity differs from the requested issue")
        updated = datetime.fromisoformat(fields["updated"])
        if updated.tzinfo is None:
            raise ValueError("Jira response timestamp is missing its time zone")
        version = (updated - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
        return JiraInput(
            site=mapping.site,
            project_key=mapping.project_key,
            issue_id=issue["id"],
            issue_key=issue_key,
            source_version=version,
            fields={mapping.field_id: fields.get(mapping.field_id)},
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid Jira issue response") from None


def import_issue(client, mapping, issue_key, api_url, email, jira_token, relay_token):
    validate_api_url(api_url)
    delivery = read_issue(client, mapping, issue_key, email, jira_token)
    body = delivery.model_dump()
    if redact(body, (jira_token, relay_token)) != body:
        raise ValueError("Jira issue data contains sensitive input")
    reply = client.post(
        f"{api_url.rstrip('/')}/v1/integrations/jira/{mapping.integration_id}/issues",
        headers={"Authorization": f"Bearer {relay_token}"},
        json=body,
        follow_redirects=False,
    )
    reply.raise_for_status()
    result = reply.json()
    return {
        "state": result["state"],
        "issue_url": result["issue_url"],
        "task_id": result["task_id"],
        "spec_version": result["spec_version"],
        "source_version": result["source_version"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_key")
    parser.add_argument(
        "--api-url", default=os.environ.get("FACTORY_API_URL", "http://127.0.0.1:8000")
    )
    args = parser.parse_args()
    config = configured_jira()
    email = os.environ.get("FACTORY_JIRA_EMAIL")
    jira_token = os.environ.get("FACTORY_JIRA_API_TOKEN")
    if not config or not email or not jira_token:
        parser.error("Jira mapping, relay token, email and Jira API token are required")
    mapping, relay_token = config
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            result = import_issue(
                client, mapping, args.issue_key, args.api_url, email, jira_token, relay_token
            )
    except httpx.HTTPStatusError as error:
        raise SystemExit(
            f"Jira relay request rejected (HTTP {error.response.status_code})"
        ) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise SystemExit("Jira relay could not read or import the configured issue") from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
