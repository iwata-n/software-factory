import socket
from threading import Thread

import pytest

from software_factory import model_egress


def test_connect_allows_only_the_configured_host_and_tls_port(monkeypatch):
    requests = []

    def dial(host):
        requests.append(host)
        client, upstream = socket.socketpair()

        def echo():
            with upstream:
                upstream.sendall(upstream.recv(100))

        Thread(target=echo, daemon=True).start()
        return client

    monkeypatch.setattr(model_egress, "dial", dial)
    with model_egress.gateway(("127.0.0.1", 0), {"model.example"}) as gateway:
        thread = Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        try:
            for target in ("untrusted.example:443", "model.example:80", "169.254.169.254:443"):
                with socket.create_connection(gateway.server_address, timeout=2) as client:
                    client.sendall(f"CONNECT {target} HTTP/1.0\r\n\r\n".encode())
                    assert client.makefile("rb").readline().split()[1] == b"403"
            assert requests == []
            with socket.create_connection(gateway.server_address, timeout=2) as client:
                client.sendall(b"CONNECT model.example:443 HTTP/1.0\r\n\r\n")
                response = client.makefile("rb")
                assert response.readline().split()[1] == b"200"
                for line in response:
                    if line == b"\r\n":
                        break
                client.sendall(b"test tunnel")
                assert response.read(11) == b"test tunnel"
            assert requests == ["model.example"]
        finally:
            gateway.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize(
    "private", ["127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "224.0.0.1"]
)
def test_mixed_dns_answers_fail_closed(monkeypatch, private):
    monkeypatch.setattr(
        model_egress.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (None, None, None, None, ("93.184.216.34", 443)),
            (None, None, None, None, (private, 443)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        model_egress.public_addresses("model.example")
