import socket
import ssl
from threading import Thread

import pytest

from software_factory import model_egress


@pytest.mark.ac("SF-21-AC03")
def test_connect_allows_only_the_configured_host_and_tls_port(monkeypatch, evidence):
    requests = []

    def client_hello(host):
        outgoing = ssl.MemoryBIO()
        handshake = ssl.create_default_context().wrap_bio(
            ssl.MemoryBIO(), outgoing, server_hostname=host
        )
        with pytest.raises(ssl.SSLWantReadError):
            handshake.do_handshake()
        return outgoing.read()

    def dial(host):
        requests.append(host)
        client, upstream = socket.socketpair()

        def echo():
            with upstream:
                upstream.sendall(upstream.recv(65536))

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
            for host in ("untrusted.example", None, "model.example"):
                with socket.create_connection(gateway.server_address, timeout=2) as client:
                    client.sendall(b"CONNECT model.example:443 HTTP/1.0\r\n\r\n")
                    response = client.makefile("rb")
                    assert response.readline().split()[1] == b"200"
                    for line in response:
                        if line == b"\r\n":
                            break
                    hello = client_hello(host)
                    client.sendall(hello[:5])
                    client.sendall(hello[5:])
                    if host == "model.example":
                        assert response.read(len(hello)) == hello
                    else:
                        assert response.read() == b""
                        assert requests == []
            assert requests == ["model.example"]
        finally:
            gateway.shutdown()
            thread.join(timeout=2)
    evidence(
        "Only matching TLS server name reached the permitted upstream",
        {"dialed": requests},
    )


@pytest.mark.ac("SF-21-AC03")
@pytest.mark.parametrize(
    "private", ["127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "224.0.0.1"]
)
def test_mixed_dns_answers_fail_closed(monkeypatch, private, evidence):
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
    evidence(
        "Mixed public and private DNS answers fail closed",
        {"rejected_address": private},
    )


@pytest.mark.ac("SF-21-AC03")
def test_model_dial_reuses_checked_sockaddr_without_second_dns_lookup(monkeypatch, evidence):
    resolutions, connections = [], []
    address = ("93.184.216.34", 443)

    def resolve(host, *args, **kwargs):
        resolutions.append(host)
        if len(resolutions) > 1:
            pytest.fail("a second DNS lookup could change the destination")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)]

    class Upstream:
        def settimeout(self, seconds):
            assert seconds == 5

        def connect(self, endpoint):
            connections.append(endpoint)

    upstream = Upstream()
    monkeypatch.setattr(model_egress.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(model_egress.socket, "socket", lambda *args: upstream)
    assert model_egress.dial("model.example") is upstream
    assert resolutions == ["model.example"] and connections == [address]
    evidence(
        "The dial uses the validated socket address",
        {"lookups": resolutions, "endpoints": connections},
    )


def test_model_network_cleans_up_in_reverse_order_when_caller_fails(monkeypatch):
    from subprocess import CompletedProcess

    from software_factory.codex_container import model_network
    from software_factory.sandbox import DockerRunner

    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(DockerRunner, "docker", staticmethod(docker))
    with pytest.raises(RuntimeError, match="injected"):
        with model_network("sha256:" + "a" * 64, ["chatgpt.com"]) as (network, proxy_url):
            assert proxy_url == "http://model-gateway:8080"
            assert calls[0] == ("network", "create", "--internal", network)
            raise RuntimeError("injected")
    assert calls[-3] == ("rm", "--force", calls[2][calls[2].index("--name") + 1])
    assert calls[-2] == ("network", "rm", calls[1][-1])
    assert calls[-1] == ("network", "rm", network)
