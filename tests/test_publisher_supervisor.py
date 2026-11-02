from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from software_factory.publisher import PublicationJournal, PublisherSupervisor, PublisherUnavailable
from tests.acceptance.helpers import dump
from tests.acceptance.test_sf10 import pipeline as pipeline
from tests.acceptance.test_sf11 import source_bundle as source_bundle
from tests.test_assembler import runner as runner
from tests.test_publications import publication as publication


def supervisor(publication, directory, *, lose=None, checkpoint=None, backend=None):
    trace = {"calls": 0, "lost": False, "reports": []}
    pub, world = publication, publication.world

    class Provider:
        def publish(self, inputs):
            trace["calls"] += 1
            return pub.success(inputs["operation"])["pull_request"]

    def handler(request):
        result = world.client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={
                "Authorization": "Bearer " + pub.secret,
                "Content-Type": "application/json",
                **(
                    {"Idempotency-Key": request.headers["Idempotency-Key"]}
                    if "Idempotency-Key" in request.headers
                    else {}
                ),
            },
        )
        if request.url.path.endswith("/reports"):
            trace["reports"].append((request.content, request.headers["Idempotency-Key"]))
        if lose and request.url.path.endswith(lose) and not trace["lost"]:
            trace["lost"] = True
            raise httpx.ReadError("injected accepted response loss", request=request)
        return httpx.Response(result.status_code, content=result.content)

    client = httpx.Client(
        base_url="http://127.0.0.1",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer " + pub.secret},
    )
    agent = PublisherSupervisor(
        client, backend or Provider(), PublicationJournal(directory), checkpoint=checkpoint
    )
    return agent, trace


@pytest.mark.parametrize("boundary", ["/begin", "/reports"])
def test_accepted_control_plane_response_loss_replays_without_republishing(
    publication, tmp_path, boundary
):
    agent, trace = supervisor(publication, tmp_path / "publisher-state", lose=boundary)
    with pytest.raises(PublisherUnavailable):
        agent.run_once()
    result = agent.run_once()
    assert result["state"] == "SUCCEEDED" and trace["calls"] == 1
    assert len(dump(publication.world.database)["publication_observations"]) == 1
    if boundary == "/reports":
        assert len(trace["reports"]) == 2 and trace["reports"][0] == trace["reports"][1]
    assert agent.journal.read() is None


@pytest.mark.parametrize("boundary", ["publisher.before_external", "publisher.after_external"])
def test_restart_never_blindly_repeats_an_external_operation(publication, tmp_path, boundary):
    def fail(point):
        if point == boundary:
            raise RuntimeError("injected Publisher crash")

    agent, trace = supervisor(publication, tmp_path / "publisher-state", checkpoint=fail)
    with pytest.raises(RuntimeError, match="injected"):
        agent.run_once()
    agent.checkpoint = lambda name: None
    result = agent.run_once()
    assert result["state"] == ("UNKNOWN" if boundary.endswith("before_external") else "SUCCEEDED")
    assert trace["calls"] == (0 if boundary.endswith("before_external") else 1)
    assert agent.journal.read() is None


@pytest.mark.parametrize("trial", range(20))
def test_concurrent_supervisors_in_one_publisher_instance_do_not_send_twice(
    publication, tmp_path, trial
):
    entered, release = Event(), Event()
    state = {"calls": 0}

    class Provider:
        def publish(self, inputs):
            state["calls"] += 1
            entered.set()
            assert release.wait(5)
            return publication.success(inputs["operation"])["pull_request"]

    directory = tmp_path / ("publisher-state-" + str(trial))
    first, _ = supervisor(publication, directory, backend=Provider())
    second, _ = supervisor(
        publication, tmp_path / ("another-state-" + str(trial)), backend=Provider()
    )
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(first.run_once)
        assert entered.wait(5)
        try:
            with pytest.raises(PublisherUnavailable, match="PUBLISHER_ALREADY_RUNNING"):
                second.run_once()
        finally:
            release.set()
        assert running.result(timeout=10)["state"] == "SUCCEEDED"
    assert (
        state["calls"] == 1 and len(dump(publication.world.database)["publication_operations"]) == 1
    )


def test_incomplete_atomic_write_is_replaced_safely_after_restart(tmp_path):
    journal = PublicationJournal(tmp_path / "journal")
    with journal.lock():
        journal.save({"phase": "STARTED"})
        temporary = journal.root / "pending.tmp"
        temporary.write_text('{"partial":')
        temporary.chmod(0o600)
        journal.save({"phase": "RESULT"})
    assert journal.read() == {"phase": "RESULT"} and not temporary.exists()
