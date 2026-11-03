from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from software_factory.ci_poll import CIPoller
from software_factory.ci_webhook import GitHubWebhook, save_delivery
from software_factory.github_ci import check_status
from software_factory.models import Gate, Run, RunEvent
from software_factory.service import Service
from tests.test_assembler import runner as runner
from tests.test_publications import publication as publication

from .test_sf10 import pipeline as pipeline
from .test_sf11 import Reviewer, stage
from .test_sf11 import source_bundle as source_bundle
from .test_sf12_integration import begin_ci, pass_local
from .test_sf13 import assemble_repair, repair, review, value_patch
from .test_sf13_progress import synthetic_ci

pytestmark = pytest.mark.parametrize("source_bundle", ["ci"], indirect=True)
MERGE = "3" * 40
REPOSITORY_ID = 9001


@pytest.fixture
def waiting(publication, clock, request):
    pub = publication
    operation = pub.begin().json()
    result = pub.success(operation)
    merge = getattr(request, "param", MERGE)
    result["pull_request"].update(external_id="456", merge_sha=merge)
    reply = pub.report(operation, result)
    assert reply.status_code == 200, reply.text
    world = pub.world
    integration_id = uuid4()
    config = GitHubWebhook(
        workspace_id=world.workspace_id,
        repository_id=world.repository_id,
        github_repository_id=REPOSITORY_ID,
        github_repository=operation["target"]["repository"],
        secret="webhook-fixture-not-a-real-secret",
    )
    tokens = {integration_id: SecretStr("read-fixture-not-a-real-token")}
    integrations = {integration_id: config}
    context = world.run()["wait_context"]
    draft = context["draft"]
    pull = {
        "number": draft["number"],
        "id": int(draft["external_id"]),
        "draft": True,
        "state": "open",
        "merge_commit_sha": merge,
        **{
            side: {
                "repo": {"id": REPOSITORY_ID},
                "ref": draft[f"{side}_ref"],
                "sha": draft[f"{side}_sha"],
            }
            for side in ("base", "head")
        },
    }
    tested = merge or draft["head_sha"]
    commit = {"sha": tested, "parents": [{"sha": pull[side]["sha"]} for side in ("base", "head")]}
    check = {
        "id": 100,
        "name": "factory-tests",
        "app": {"id": 123},
        "head_sha": tested,
        "pull_requests": [pull],
        "status": "completed",
        "conclusion": "success",
        "completed_at": "2026-11-03T00:00:00Z",
    }
    stub = {"status": 200, "reads": 0, "checks": [check]}

    def respond(req):
        assert req.method == "GET" and req.url.host == "api.github.com"
        assert req.headers["Authorization"] == "Bearer read-fixture-not-a-real-token"
        stub["reads"] += 1
        if stub["status"] != 200:
            return httpx.Response(stub["status"])
        root = f"/repos/{config.github_repository}"
        if req.url.path == f"{root}/pulls/{draft['number']}":
            return httpx.Response(200, json=pull)
        if req.url.path == f"{root}/git/commits/{tested}":
            return httpx.Response(200, json=commit)
        assert req.url.path == f"{root}/commits/{tested}/check-runs"
        assert req.url.params["filter"] == "all"
        return httpx.Response(200, json={"check_runs": stub["checks"]})

    def poll():
        # Each tick uses fresh Service, Poller and HTTP client instances to cover restart.
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            CIPoller(Service(world.database), integrations, tokens).poll(client)

    def signal(delivery_id=None, action="completed"):
        body = (
            '{"repository":{"id":9001,"full_name":"'
            + config.github_repository
            + f'"}},"action":"{action}"}}'
        )
        save_delivery(
            Service(world.database),
            config,
            integration_id,
            delivery_id or uuid4(),
            "check_run",
            body.encode(),
        )

    return world, clock, poll, signal, pull, commit, check, stub


@pytest.mark.ac("SF-18-AC03", "SF-18-AC04")
@pytest.mark.parametrize("waiting", [None, MERGE], indirect=True)
def test_exact_published_head_or_verified_merge_is_persisted(waiting, evidence):
    world, _, poll, _, pull, _, check, _ = waiting
    poll()
    run = world.run()
    assert run["phase"] == "FINAL_GATE_READY"
    assert run["status"] == "WAITING_EXTERNAL"
    with world.database.transaction() as session:
        final = session.scalar(select(Gate).where(Gate.stage == "FINAL"))
        (adopted,) = final.metadata_body["checks"]
        assert adopted["base_sha"] == pull["base"]["sha"]
        assert adopted["head_sha"] == pull["head"]["sha"]
        assert adopted["tested_sha"] == check["head_sha"]
        assert adopted["provider_run_id"] == 100
        assert adopted["url"].endswith("/check-runs/100")
        assert adopted["status"] == final.status == "PASS"
        evidence("Fixed PR tuple and check evidence in final Gate", adopted)


@pytest.mark.ac("SF-18-AC03")
@pytest.mark.parametrize("fault", ["pr", "base", "head", "merge", "parents", "check_sha", "app"])
def test_changed_tuple_or_untrusted_check_cannot_pass(waiting, fault, evidence):
    world, _, poll, _, pull, commit, check, _ = waiting
    if fault == "pr":
        pull["id"] += 1
    elif fault in {"base", "head"}:
        pull[fault]["sha"] = "9" * 40
    elif fault == "merge":
        pull["merge_commit_sha"] = "9" * 40
    elif fault == "parents":
        commit["parents"].reverse()
    elif fault == "check_sha":
        check["head_sha"] = "9" * 40
    else:
        check["app"]["id"] += 1
    poll()
    run = world.run()
    assert run["phase"] == "WAIT_CI"
    assert run["wait_context"]["checks"] == []
    evidence("No adopted evidence for a mismatched target or app", {"fault": fault})


@pytest.mark.ac("SF-18-AC05")
def test_missing_webhook_poll_recovers_429_and_503_on_durable_cadence(waiting, evidence):
    world, clock, poll, _, _, _, _, stub = waiting
    deadline = world.run()["wait_context"]["deadline_at"]
    stub["status"] = 429
    poll()
    clock.value += 59
    poll()
    assert stub["reads"] == 1
    assert world.claim().status_code == 204
    assert world.run()["reserved_microusd"] == 0
    clock.value += 1
    stub["status"] = 503
    poll()
    assert stub["reads"] == 2
    stub["status"] = 200
    clock.value += 60
    poll()
    run = world.run()
    assert run["phase"] == "FINAL_GATE_READY"
    assert run["wait_context"]["deadline_at"] == deadline
    evidence("Missing delivery and API failures recover without extending the deadline", run)


@pytest.mark.ac("SF-18-AC05")
def test_exact_deadline_wins_without_an_http_request(waiting, evidence):
    world, clock, poll, _, _, _, _, stub = waiting
    clock.value = world.run()["wait_context"]["deadline_at"]
    poll()
    run = world.run()
    assert stub["reads"] == 0
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "CI_TIMEOUT")
    evidence("Deadline wins at equality", run)


@pytest.mark.ac("SF-18-AC02", "SF-18-AC04")
def test_delayed_signal_cannot_restore_old_pass_or_duplicate_final_gate(waiting, evidence):
    world, clock, poll, signal, _, _, check, stub = waiting
    # A new execution is running even though the previous one passed.
    rerun = check | {"id": 101, "status": "in_progress", "conclusion": None, "completed_at": None}
    stub["checks"] = [rerun, check]
    poll()
    assert world.run()["phase"] == "WAIT_CI"

    clock.value += 1
    stub["checks"] = [check]
    signal(action="created")
    poll()
    assert world.run()["phase"] == "WAIT_CI"

    clock.value += 60
    stub["checks"] = [check, check | {"id": 101}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: poll(), range(2)))
    assert world.run()["phase"] == "FINAL_GATE_READY"
    with world.database.transaction() as session:
        (final,) = list(session.scalars(select(Gate).where(Gate.stage == "FINAL")))
        (event,) = list(
            session.scalars(select(RunEvent).where(RunEvent.kind == "FINAL_GATE_DECIDED"))
        )
        assert "github-check-101" in final.evidence_ids
        assert "github-check-100" not in final.evidence_ids
        evidence("Only the newer execution finalized the Gate", event.body)


@pytest.mark.ac("SF-18-AC02")
def test_inbox_signal_advances_poll_without_waiting_sixty_seconds(waiting, evidence):
    world, clock, poll, signal, _, _, check, stub = waiting
    stub["checks"] = [check | {"status": "queued", "conclusion": None}]
    poll()
    clock.value += 1
    stub["checks"] = [check]
    poll()
    assert world.run()["phase"] == "WAIT_CI"
    signal()
    poll()
    assert world.run()["phase"] == "FINAL_GATE_READY"
    evidence("Inbox signal triggers the same API reconciliation as a periodic poll", world.run())


@pytest.mark.ac("SF-18-AC04")
@pytest.mark.parametrize(
    ("state", "conclusion", "expected"),
    [
        ("queued", None, "PENDING"),
        ("in_progress", None, "PENDING"),
        ("completed", "success", "PASS"),
        ("completed", "failure", "FAIL"),
        ("completed", "timed_out", "ERROR"),
        ("completed", "cancelled", "INCONCLUSIVE"),
        ("completed", "skipped", "INCONCLUSIVE"),
        ("completed", "neutral", "INCONCLUSIVE"),
    ],
)
def test_provider_status_mapping(source_bundle, state, conclusion, expected, evidence):
    status = check_status({"status": state, "conclusion": conclusion})
    assert status == expected
    evidence("GitHub state mapping", {"state": state, "conclusion": conclusion, "status": status})


@pytest.mark.ac("SF-18-AC05")
def test_repaired_candidate_still_stops_with_ci_timeout(pipeline, clock, evidence):
    world = pipeline.world
    local = pass_local(pipeline)
    service = world.client.app.state.service
    context = begin_ci(pipeline, local)
    with world.database.transaction() as session:
        service.gates.final(
            session, session.get(Run, world.run_id), synthetic_ci(context, ["factory-tests"], 1)
        )
    repair(pipeline, value_patch("2", "2 # retry"))
    assemble_repair(pipeline)
    assert stage(pipeline, "VERIFY")[0]["accepted"]
    assert review(pipeline, Reviewer())[0]["accepted"]
    candidate_id = world.run()["current_candidate_id"]
    with world.database.transaction() as session:
        local = session.scalar(
            select(Gate).where(Gate.candidate_id == candidate_id, Gate.stage == "LOCAL")
        )
    context = begin_ci(pipeline, local)
    clock.value = context["deadline_at"]
    Service(world.database).reconcile()
    run = world.run()
    assert (run["status"], run["stop_reason"]) == ("BLOCKED", "CI_TIMEOUT")
    evidence("A repaired Candidate has the same CI timeout semantics", run)
