"""Real process harness for managed-stage crash boundaries; never imported by production."""

import json
import os
import sys
import time


def app_factory():
    from software_factory.api import create_app
    from software_factory.db import Database
    from software_factory.storage import LocalArtifactStorage

    with open(os.environ["FACTORY_TEST_SOURCE_FILE"]) as stream:
        source = json.load(stream)

    class Source:
        def resolve_details(self, reference):
            return source

        def fetch(self, remote, sha):
            assert sha == source["sha"]
            return source

    return create_app(
        Database(),
        resolver=Source(),
        reconcile_interval=3600,
        storage=LocalArtifactStorage(os.environ["FACTORY_ARTIFACT_ROOT"]),
    )


def main():
    import httpx

    from software_factory.pipeline_worker import PipelineAdapter
    from software_factory.sandbox import DockerRunner
    from software_factory.worker import Supervisor

    stage = os.environ.get("FACTORY_TEST_STAGE", "")

    class Transport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request):
            response = self.inner.handle_request(request)
            if response.status_code == 200 and (
                stage == "patch_reading"
                and "/inputs/" in request.url.path
                or stage == "reported"
                and request.url.path.endswith("/complete")
            ):
                print("CHECKPOINT:" + stage, flush=True)
                while True:
                    time.sleep(0.05)
            return response

    class Runner(DockerRunner):
        def execute(self, request, **kwargs):
            if stage in {"patch_applied", "before_commit"}:
                kwargs["pause_at"] = stage
            return super().execute(request, **kwargs)

    assert not any(
        name in sys.modules for name in ("software_factory.db", "software_factory.models")
    )
    assert not any(
        name in os.environ
        for name in (
            "FACTORY_DATABASE_URL",
            "TEST_DATABASE_URL",
            "FACTORY_USER_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
        )
    )
    with httpx.Client(
        base_url=os.environ["FACTORY_API_URL"],
        transport=Transport(),
        headers={"Authorization": "Bearer " + os.environ["FACTORY_WORKER_TOKEN"]},
    ) as client:
        result = Supervisor(
            client,
            PipelineAdapter(Runner(os.environ["FACTORY_RUNNER_IMAGE_DIGEST"])),
            heartbeat_interval=0.2,
        ).run_once()
        print(json.dumps({"accepted": bool(result and result["accepted"])}), flush=True)


if __name__ == "__main__":
    main()
