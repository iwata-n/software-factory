from types import SimpleNamespace

from software_factory.codex_worker_pool import DEFAULT_CODEX_WORKERS, CodexWorkerPool


def test_pool_resizes_and_drains_busy_workers_without_terminating_them(tmp_path):
    desired = [DEFAULT_CODEX_WORKERS]
    started, retired = [], []

    def provision(slot, drain):
        process = SimpleNamespace(code=None)
        process.poll = lambda: process.code
        started.append((slot, drain, process))
        return process

    pool = CodexWorkerPool(tmp_path, lambda: desired[0], provision, retired.append)
    pool.tick()
    assert len(started) == len(pool.workers) == 10
    desired[0] = 2
    pool.tick()
    assert len(started) == 10 and not retired
    assert all(drain.exists() == (slot >= 2) for slot, drain, _ in started)
    # A busy process stays alive until its current work and usage delivery finish.
    for slot, _, process in started:
        if slot >= 2:
            process.code = 0
    pool.tick()
    assert len(pool.workers) == 2 and len(retired) == 8
    desired[0] = 3
    pool.tick()
    assert len(pool.workers) == 3 and len(started) == 11
    assert not started[-1][1].exists()


def test_resize_can_cancel_a_pending_drain(tmp_path):
    count = [2]
    pool = CodexWorkerPool(
        tmp_path, lambda: count[0], lambda *_: SimpleNamespace(poll=lambda: None), lambda _: None
    )
    pool.tick()
    count[0] = 1
    pool.tick()
    drain = pool.workers[1][1]
    assert drain.exists()
    count[0] = 2
    pool.tick()
    assert not drain.exists()


def test_failed_provision_is_retried_with_backoff_without_exposing_secrets(
    tmp_path, monkeypatch, capsys
):
    from software_factory import codex_worker_pool

    clock = [0]
    monkeypatch.setattr(codex_worker_pool.time, "monotonic", lambda: clock[0])
    attempts = []

    def provision(*_):
        attempts.append(clock[0])
        raise RuntimeError("Bearer do-not-log")

    pool = CodexWorkerPool(tmp_path, lambda: 1, provision, lambda _: None)
    pool.tick()
    pool.tick()
    clock[0] = 2
    pool.tick()
    assert attempts == [0, 2]
    assert "do-not-log" not in capsys.readouterr().out
