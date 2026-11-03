"""HTTPS CONNECT gateway for a model runner on a private container network."""

import argparse
import ipaddress
import re
import select
import socket
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def public_addresses(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    # Reject mixed results as well. Connect using the checked sockaddr, without a second lookup.
    ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ValueError("non-public model address")
    return addresses


def dial(host):
    for family, kind, protocol, _, address in public_addresses(host):
        upstream = socket.socket(family, kind, protocol)
        try:
            upstream.settimeout(5)
            upstream.connect(address)
        except OSError:
            upstream.close()
        else:
            return upstream
    raise OSError("model destination unreachable")


def read_client_hello(client, expected_host):
    """Use OpenSSL to parse the first ClientHello without terminating the tunnel."""
    incoming = ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    observed = None

    def capture_name(_connection, server_name, _context):
        nonlocal observed
        observed = server_name
        return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME

    context.set_servername_callback(capture_name)
    handshake = context.wrap_bio(incoming, ssl.MemoryBIO(), server_side=True)
    prefix = bytearray()
    deadline = time.monotonic() + 5
    while len(prefix) < 65536 and time.monotonic() < deadline:
        data = client.recv(min(4096, 65536 - len(prefix)))
        if not data:
            break
        prefix.extend(data)
        incoming.write(data)
        try:
            handshake.do_handshake()
        except ssl.SSLWantReadError:
            continue
        except ssl.SSLError:
            if observed and observed.lower() == expected_host:
                return prefix
        break
    raise ValueError("TLS server name does not match the permitted destination")


def forward(client, upstream):
    while True:
        readable, _, _ = select.select((client, upstream), (), (), 30)
        if not readable:
            return
        for origin in readable:
            data = origin.recv(65536)
            if not data:
                return
            destination = upstream if origin is client else client
            destination.sendall(data)


def gateway(address, allowed_hosts):
    allowed_hosts = frozenset(allowed_hosts)
    if not allowed_hosts or any(
        not HOST.fullmatch(host) or host.rsplit(".", 1)[-1].isdigit() for host in allowed_hosts
    ):
        raise ValueError("exact DNS hostnames are required")

    class Handler(BaseHTTPRequestHandler):
        rbufsize = 0

        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *args):
            # Never log targets supplied by clients or application bytes.
            pass

        def do_CONNECT(self):
            host, separator, port = self.path.partition(":")
            if separator != ":" or port != "443" or host not in allowed_hosts:
                self.send_error(403)
                return
            self.send_response(200)
            self.end_headers()
            try:
                client_hello = read_client_hello(self.connection, host)
                with dial(host) as upstream:
                    upstream.sendall(client_hello)
                    forward(self.connection, upstream)
            except (OSError, ValueError):
                return

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-host", action="append", required=True)
    args = parser.parse_args()
    with gateway((args.listen, args.port), frozenset(args.allow_host)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
