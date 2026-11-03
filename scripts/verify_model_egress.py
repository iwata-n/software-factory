"""Exercise the pinned Linux model network with a credential-free TLS handshake."""

import argparse
import json
import re
from pathlib import Path

from acceptance_support import source_manifest

from software_factory.codex_container import model_network
from software_factory.sandbox import DockerRunner

PROBE = r"""
import json
import socket
import ssl

def connect(target):
    connection = socket.create_connection(("model-gateway", 8080), timeout=5)
    try:
        connection.sendall(f"CONNECT {target} HTTP/1.0\r\n\r\n".encode())
        header = b""
        while len(header) < 8192 and not header.endswith(b"\r\n\r\n"):
            byte = connection.recv(1)
            if not byte:
                raise OSError("CONNECT response ended before the headers")
            header += byte
        return connection, int(header.split()[1])
    except BaseException:
        connection.close()
        raise

blocked = {}
for target in ("auth.openai.com:443", "chatgpt.com:80", "169.254.169.254:443"):
    connection, status = connect(target)
    connection.close()
    assert status == 403
    blocked[target] = status

context = ssl.create_default_context()
connection, status = connect("chatgpt.com:443")
with connection:
    assert status == 200
    with context.wrap_socket(connection, server_hostname="chatgpt.com") as tls:
        version = tls.version()

connection, status = connect("chatgpt.com:443")
with connection:
    assert status == 200
    try:
        context.wrap_socket(connection, server_hostname="auth.openai.com")
    except (ssl.SSLError, ConnectionResetError):
        pass
    else:
        raise AssertionError("unlisted TLS server name was accepted")

print(json.dumps({"allowed_host": "chatgpt.com", "verified_tls": version,
                  "blocked_connect": blocked, "cross_host_tls": "DENIED"}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image):
        parser.error("--image must be a pinned Docker content digest")
    with model_network(args.image, ["chatgpt.com"]) as (network, _):
        result = DockerRunner.docker(
            "run",
            "--rm",
            "--network",
            network,
            "--entrypoint",
            "python",
            args.image,
            "-c",
            PROBE,
            timeout=30,
        )
        result.check_returncode()
        evidence = json.loads(result.stdout)
    evidence.update(revision=source_manifest()["revision"], image=args.image)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "live-egress.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
