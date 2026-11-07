from pathlib import Path


def test_support_service_restart_keeps_web_api_running(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import serve_self_host as host

    clock = [0]

    class Server:
        def poll(self):
            return None if clock[0] < 3 else 0

    class Stopped:
        def poll(self):
            return 1

    class Healthy:
        def poll(self):
            return None

    calls = []

    def restart(index):
        calls.append(index)
        return Healthy()

    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        host.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    host.monitor_services(Server(), [Stopped()], restart)
    assert calls == [0] and clock[0] == 3


def test_repeated_worker_restart_failure_is_bounded_and_does_not_log_exception_text(
    monkeypatch, capsys
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import serve_self_host as host

    clock = [0]

    class Server:
        def poll(self):
            return None if clock[0] < 4 else 0

    class Stopped:
        def poll(self):
            return 1

    calls = []

    def restart(index):
        calls.append(clock[0])
        raise RuntimeError("Bearer do-not-log")

    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        host.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    host.monitor_services(Server(), [Stopped()], restart)
    assert calls == [0, 2]
    assert "do-not-log" not in capsys.readouterr().out
