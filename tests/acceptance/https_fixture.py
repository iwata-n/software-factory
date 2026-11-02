"""Real TLS endpoints on an isolated Docker network, used to observe Git network policy."""

import subprocess
import time
from contextlib import contextmanager
from uuid import uuid4

from software_factory.sandbox import DockerRunner

SERVER = r"""
import http.server, os, ssl, time
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        print("REQUEST " + self.path, flush=True)
        mode = os.environ["FIXTURE_MODE"]
        if mode == "slow":
            time.sleep(30)
        self.send_response(302 if mode == "redirect" else 401 if mode == "auth" else 200)
        if mode == "redirect":
            self.send_header("Location", "https://second.fixture.test/owner/repo.git/info/refs?service=git-upload-pack")
        if mode == "auth":
            self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
        self.send_header("Content-Length", "1024" if mode == "interrupted" else "0")
        self.end_headers()
        if mode == "interrupted":
            self.wfile.write(b"invalid partial git transfer")
    def log_message(self, *args): pass
server=http.server.HTTPServer(("0.0.0.0",443), Handler)
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/tls/cert.pem","/tls/key.pem")
server.socket=ctx.wrap_socket(server.socket,server_side=True)
print("READY",flush=True)
server.serve_forever()
"""


def docker(*args):
    return subprocess.check_output(["docker", *args], stderr=subprocess.STDOUT, text=True).strip()


@contextmanager
def endpoints(tmp_path, image_digest, mode):
    tls = tmp_path / "tls"
    tls.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=first.fixture.test",
            "-addext",
            "subjectAltName=DNS:first.fixture.test,DNS:second.fixture.test",
            "-keyout",
            str(tls / "key.pem"),
            "-out",
            str(tls / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    (tls / "key.pem").chmod(0o444)
    network = "factory-tls-" + uuid4().hex
    docker("network", "create", "--internal", network)
    containers = []
    try:
        for hostname, server_mode in (
            ("first.fixture.test", mode),
            ("second.fixture.test", "canary"),
        ):
            name = "factory-tls-" + uuid4().hex
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--network-alias",
                hostname,
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--memory",
                "128m",
                "--pids-limit",
                "16",
                "--security-opt",
                "no-new-privileges",
                "--mount",
                f"type=bind,src={tls},dst=/tls,readonly",
                "--env",
                "FIXTURE_MODE=" + server_mode,
                "--entrypoint",
                "python",
                image_digest,
                "-u",
                "-c",
                SERVER,
            )
            containers.append(name)
            for _ in range(30):
                if "READY" in docker("logs", name):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("TLS fixture did not start: " + docker("logs", name))
        runner = DockerRunner(
            image_digest, source_network=network, source_ca_pem=(tls / "cert.pem").read_text()
        )
        yield runner, containers
    finally:
        for name in containers:
            docker("rm", "--force", name)
        docker("network", "rm", network)
