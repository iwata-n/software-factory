"""An actual HTTP-only supervisor with a hanging adapter, for kill/recovery tests."""

import argparse
import json
import os
import sys

import httpx

from software_factory.adapters import AgentResult, Capabilities
from software_factory.worker import Supervisor


class HangingAdapter:
    capabilities = Capabilities()

    def __init__(self):
        self.value = None

    def start(self, job):
        print(
            json.dumps(
                {
                    "started": True,
                    "job_id": job["id"],
                    "attempt_id": job["attempt_id"],
                    "pid": os.getpid(),
                    "environment_keys": sorted(os.environ),
                    "database_modules": [
                        name
                        for name in sys.modules
                        if name.startswith(("psycopg", "sqlalchemy", "software_factory.db"))
                    ],
                }
            ),
            flush=True,
        )

    def events(self):
        return []

    def cancel(self):
        self.value = AgentResult("CANCELLED", {})

    def result(self):
        return self.value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.api_url,
        headers={"Authorization": "Bearer " + os.environ["FACTORY_WORKER_TOKEN"]},
    ) as client:
        Supervisor(client, HangingAdapter()).run_once()
