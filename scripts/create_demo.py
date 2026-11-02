"""Register a fake-only example using the public API; never open a database connection."""

import argparse
import json
import os
from uuid import uuid4

import httpx

from software_factory.worker import validate_api_url

parser = argparse.ArgumentParser()
parser.add_argument("--workspace-id", required=True)
parser.add_argument("--api-url", default=os.environ.get("FACTORY_API_URL", "http://127.0.0.1:8000"))
args = parser.parse_args()
validate_api_url(args.api_url)
token = os.environ.get("FACTORY_USER_TOKEN")
if not token:
    parser.error("FACTORY_USER_TOKEN is required")

with httpx.Client(base_url=args.api_url, headers={"Authorization": f"Bearer {token}"}) as client:

    def post(path, body):
        response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
        response.raise_for_status()
        return response.json()

    verification = {"profile_id": "demo", "required_checks": ["unit_tests", "acceptance_review"]}
    project = post(
        f"/v1/workspaces/{args.workspace_id}/projects",
        {
            "name": "Fake Agent demo",
            "policy": {
                "allowed_write_paths": ["src", "tests"],
                "verification": verification,
                "execution_image_digest": "sha256:" + "0" * 64,
                "run_budget_microusd": 1_000_000,
                "job_reservation_microusd": 100_000,
            },
        },
    )
    repository = post(
        f"/v1/projects/{project['project_id']}/repositories",
        {
            "name": "simulation-only",
            "base_sha": "0" * 40,
        },
    )
    task = post(
        f"/v1/projects/{project['project_id']}/tasks",
        {
            "schema_version": 1,
            "task": {
                "title": "一覧画面の空状態を追加する",
                "repository_id": repository["repository_id"],
                "objective": "データ0件時に空状態を表示する",
                "acceptance_criteria": [
                    {"id": "AC-1", "description": "0件時は空状態の説明文を表示する"},
                    {"id": "AC-2", "description": "1件以上では既存の一覧を維持する"},
                ],
                "constraints": ["必須検証を削除しない"],
            },
            "verification": verification,
        },
    )
    run = post(f"/v1/tasks/{task['task_id']}/runs", {"spec_version": 1})
    print(
        json.dumps(
            {
                "run_id": run["run_id"],
                "status": run["status"],
                "run_url": args.api_url + "/v1/runs/" + run["run_id"],
            },
            ensure_ascii=False,
        )
    )
