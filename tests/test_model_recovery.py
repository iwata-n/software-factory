from sqlalchemy import select

from software_factory.models import Job


def test_operator_can_retry_fixed_model_failures_within_the_original_run_and_attempt_limit(world):
    for attempt in range(1, 4):
        claimed = world.claim().json()
        assert claimed["job"]["run_id"] == world.run_id
        lease = claimed["lease"]
        failed = world.post(
            f"/v1/worker/leases/{lease['id']}/fail",
            {
                "fence": lease["fence"],
                "category": "POLICY",
                "reason": "MODEL_CHANGE_PATH_DENIED",
                "usage_microusd": 0,
            },
            token=world.worker_token,
        )
        assert failed.status_code == 200, failed.text
        run = world.run()
        target = world.get(f"/v1/runs/{world.run_id}/interventions").json()
        body = {
            "action": "resolve_blocked",
            "subject_id": world.run_id,
            "target_hash": target["target_hash"],
            "expected_run_revision": run["state_revision"],
            "reason": "モデルの変更範囲判定を修正したため同じ実装を再試行する",
        }
        path = f"/v1/runs/{world.run_id}/interventions"
        assert world.post(path, body, token=world.other_user_token).status_code == 404
        response = world.post(path, body)
        if attempt < 3:
            assert response.status_code == 200, response.text
            assert world.run()["status"] == "QUEUED"
        else:
            assert response.status_code == 409
            assert response.json()["code"] == "INFRA_ATTEMPTS_EXHAUSTED"
    with world.database.transaction() as s:
        jobs = s.scalars(select(Job).where(Job.run_id == world.run_id)).all()
        assert len(jobs) == 1 and jobs[0].attempt_no == 3
