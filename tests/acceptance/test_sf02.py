import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from software_factory.contracts import digest
from software_factory.db import db_now
from software_factory.models import Enrollment, Repository, SpecVersion, Task
from software_factory.service import issue_token, token_hash
from tests.conftest import example_policy, example_spec

from .helpers import dump


@pytest.mark.ac("SF-02-AC01")
def test_crud_has_scoped_parent_references(world, evidence):
    project = world.post(
        f"/v1/workspaces/{world.workspace_id}/projects",
        {"name": "second", "policy": example_policy()},
    )
    assert project.status_code == 201
    pid = project.json()["project_id"]
    repo = world.post(f"/v1/projects/{pid}/repositories", {"name": "repo", "base_sha": "b" * 40})
    assert repo.status_code == 201
    rid = repo.json()["repository_id"]
    task = world.post(f"/v1/projects/{pid}/tasks", example_spec(rid))
    assert task.status_code == 201
    tid = task.json()["task_id"]
    assert world.get(f"/v1/tasks/{tid}").status_code == 200
    with world.database.transaction() as session:
        saved = session.get(Task, tid)
        repository = session.get(Repository, rid)
        assert saved.workspace_id == repository.workspace_id == world.workspace_id
        assert saved.project_id == repository.project_id == pid
        assert saved.latest_spec_version == 1
    evidence(
        "201/201/201, matching parent references and initial version 1",
        {
            "project_id": pid,
            "repository_id": rid,
            "task_id": tid,
            "workspace_id": world.workspace_id,
        },
    )


@pytest.mark.ac("SF-02-AC02", "C-03")
def test_parallel_versions_are_contiguous_and_replayed_once(world, evidence):
    before = world.get(f"/v1/tasks/{world.task_id}").json()["spec_versions"][0]
    barrier = Barrier(2)

    def add(number):
        body = example_spec(world.repository_id)
        body["task"]["objective"] = f"version objective {number}"
        barrier.wait(timeout=5)
        return world.post(
            f"/v1/tasks/{world.task_id}/spec-versions", body, key=f"version-{number}"
        ), body

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, range(2)))
    assert {r.json()["spec_version"] for r, _ in results} == {2, 3}
    for number, (response, body) in enumerate(results):
        assert response.status_code == 201
        assert (
            world.post(
                f"/v1/tasks/{world.task_id}/spec-versions", body, key=f"version-{number}"
            ).json()
            == response.json()
        )
    versions = world.get(f"/v1/tasks/{world.task_id}").json()["spec_versions"]
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert versions[0] == before
    evidence("versions 1,2,3; v1 immutable; replay adds no rows", versions, task_id=world.task_id)


@pytest.mark.ac("SF-02-AC03", "C-02")
def test_cross_tenant_and_project_links_fail_atomically(world, evidence):
    pid = world.post(
        f"/v1/workspaces/{world.workspace_id}/projects",
        {"name": "other-project", "policy": example_policy()},
    ).json()["project_id"]
    before = dump(world.database)
    for token, project in [(world.other_user_token, world.project_id), (world.user_token, pid)]:
        assert (
            world.post(
                f"/v1/projects/{project}/tasks", example_spec(world.repository_id), token=token
            ).status_code
            == 404
        )
    assert dump(world.database) == before
    with pytest.raises(IntegrityError), world.database.transaction() as session:
        session.add(
            Repository(
                workspace_id=world.other_workspace_id,
                project_id=world.project_id,
                name="invalid",
                base_ref="main",
                base_sha="c" * 40,
            )
        )
        session.flush()
    assert dump(world.database) == before
    evidence(
        "404, FK violation, zero committed partial task/spec rows",
        {"before_digest": digest(before), "after_digest": digest(dump(world.database))},
    )


@pytest.mark.ac("SF-02-AC04", "C-02")
def test_every_public_and_worker_route_enforces_roles(world, evidence):
    lease = world.claim().json()["lease"]
    lid = lease["id"]
    fence = lease["fence"]
    from tests.test_control_plane import completion

    assignment = {"lease": lease, "job": {"kind": "IMPLEMENT", "snapshot": {}}}
    public = [
        (
            "POST",
            f"/v1/workspaces/{world.workspace_id}/projects",
            {"name": "x", "policy": example_policy()},
        ),
        (
            "POST",
            f"/v1/projects/{world.project_id}/repositories",
            {"name": "x", "base_sha": "a" * 40},
        ),
        ("POST", f"/v1/projects/{world.project_id}/tasks", example_spec(world.repository_id)),
        ("POST", f"/v1/tasks/{world.task_id}/spec-versions", example_spec(world.repository_id)),
        ("POST", f"/v1/tasks/{world.task_id}/runs", {"spec_version": 1}),
        (
            "POST",
            f"/v1/runs/{world.run_id}/cancel",
            {"expected_run_revision": world.run()["state_revision"], "reason": "scope check"},
        ),
        *[
            ("GET", path, None)
            for path in [
                f"/v1/tasks/{world.task_id}",
                f"/v1/runs/{world.run_id}",
                f"/v1/runs/{world.run_id}/plan",
                f"/v1/runs/{world.run_id}/graph",
                f"/v1/runs/{world.run_id}/event-history",
                f"/v1/workspaces/{world.workspace_id}/workers",
            ]
        ],
    ]
    worker = [
        ("POST", "/v1/worker/claims", {"protocol_version": 1}),
        ("POST", f"/v1/worker/leases/{lid}/heartbeat", {"fence": fence}),
        ("POST", f"/v1/worker/leases/{lid}/complete", completion(assignment)),
        (
            "POST",
            f"/v1/worker/leases/{lid}/fail",
            {"fence": fence, "category": "INFRASTRUCTURE", "reason": "scope"},
        ),
        (
            "POST",
            f"/v1/worker/leases/{lid}/events",
            {"fence": fence, "events": [{"sequence": 1, "kind": "LOG", "body": {}}]},
        ),
    ]
    beta_enrollment = issue_token()
    with world.database.transaction() as session:
        session.add(
            Enrollment(
                workspace_id=world.other_workspace_id,
                token_hash=token_hash(beta_enrollment),
                capabilities=["PLAN", "IMPLEMENT", "adapter:fake"],
                expires_at=db_now(session) + 600,
            )
        )
    beta_worker = world.client.post(
        "/v1/worker/enroll",
        json={
            "token": beta_enrollment,
            "name": "workspace-beta-worker",
        },
    ).json()["worker_token"]
    before = dump(world.database)
    observed = []
    for routes, wrong in [(public, world.worker_token), (worker, world.user_token)]:
        for method, path, body in routes:
            for token, status in [(None, 401), (wrong, 403)]:
                headers = {"Idempotency-Key": str(uuid4())}
                if token:
                    headers["Authorization"] = "Bearer " + token
                response = world.client.request(method, path, json=body, headers=headers)
                assert response.status_code == status, (path, response.text)
                observed.append(
                    {"path": path, "expected_status": status, "status": response.status_code}
                )
    for routes, foreign_token in [(public, world.other_user_token), (worker, beta_worker)]:
        for method, path, body in routes:
            expected = 204 if path == "/v1/worker/claims" else 404
            response = world.client.request(
                method,
                path,
                json=body,
                headers={
                    "Authorization": "Bearer " + foreign_token,
                    "Idempotency-Key": str(uuid4()),
                },
            )
            assert response.status_code == expected, (path, response.text)
            observed.append(
                {
                    "path": path,
                    "expected_status": expected,
                    "status": response.status_code,
                    "scope": "other-workspace",
                }
            )
    assert dump(world.database) == before
    evidence("all route/role combinations reject with zero mutations", observed)


@pytest.mark.ac("SF-02-AC05")
@pytest.mark.parametrize("iteration", range(20))
def test_enrollment_is_atomically_single_use(world, clock, evidence, iteration):
    secret = issue_token()
    with world.database.transaction() as session:
        session.add(
            Enrollment(
                workspace_id=world.workspace_id,
                token_hash=token_hash(secret),
                capabilities=["PLAN", "adapter:fake"],
                expires_at=clock() + 600,
            )
        )
    barrier = Barrier(2)

    def enroll(_):
        barrier.wait(timeout=5)
        return world.client.post("/v1/worker/enroll", json={"token": secret, "name": "parallel"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enroll, range(2)))
    assert sorted(r.status_code for r in results) == [201, 401]
    evidence([201, 401], sorted(r.status_code for r in results), iteration=iteration)


@pytest.mark.ac("SF-02-AC05")
@pytest.mark.parametrize(("offset", "status"), [(-0.001, 201), (0, 401), (0.001, 401)])
def test_enrollment_expiry_boundary(world, clock, evidence, offset, status):
    secret = issue_token()
    expiry = clock() + 600
    with world.database.transaction() as session:
        session.add(
            Enrollment(
                workspace_id=world.workspace_id,
                token_hash=token_hash(secret),
                capabilities=["PLAN", "adapter:fake"],
                expires_at=expiry,
            )
        )
    clock.value = expiry + offset
    response = world.client.post("/v1/worker/enroll", json={"token": secret, "name": "boundary"})
    assert response.status_code == status
    evidence(status, response.status_code, db_now=clock(), expires_at=expiry)


@pytest.mark.ac("SF-02-AC06", "C-02")
def test_invalid_specs_and_connection_pool_reuse(world, evidence):
    body = example_spec(world.repository_id)
    variants = [
        body | {"unknown": "field"},
        body | {"execution": {"max_implementation_units": 2}},
        body | {"policy_request": {"merge": "allowed"}},
    ]
    duplicate = copy.deepcopy(body)
    duplicate["task"]["acceptance_criteria"] *= 2
    variants.append(duplicate)
    empty = copy.deepcopy(body)
    empty["task"]["objective"] = " "
    variants.append(empty)
    before = dump(world.database)
    assert all(
        world.post(f"/v1/projects/{world.project_id}/tasks", v).status_code == 422 for v in variants
    )
    assert dump(world.database) == before
    reused = []

    def checkout(connection, record, proxy):
        reused.append(id(connection))

    event.listen(world.database.engine, "checkout", checkout)
    try:
        for _ in range(100):
            assert world.get(f"/v1/tasks/{world.task_id}").status_code == 200
            assert (
                world.get(f"/v1/tasks/{world.task_id}", token=world.other_user_token).status_code
                == 404
            )
    finally:
        event.remove(world.database.engine, "checkout", checkout)
    assert len(set(reused)) < len(reused)
    with world.database.transaction() as session:
        assert len(list(session.scalars(select(SpecVersion)))) == 1
    evidence(
        "invalid input rows=0; 100 A/B pool switches without leakage",
        {
            "invalid_cases": len(variants),
            "pool_checkouts": len(reused),
            "connections": len(set(reused)),
        },
    )
