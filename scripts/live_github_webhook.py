"""Temporary, path-limited ingress for the live GitHub evaluation harness."""

import json
import subprocess
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

from software_factory.ci_webhook import MAX_BODY


@contextmanager
def github_webhook_ingress(repository, control_plane, path, secret):
    status = subprocess.check_output(["tailscale", "funnel", "status"], text=True, timeout=10)
    if status.strip() != "No serve config":
        raise RuntimeError("Live webhook rehearsal requires a clean Tailscale Serve configuration")
    node = json.loads(subprocess.check_output(["tailscale", "status", "--json"], timeout=10))
    dns = node["Self"]["DNSName"].rstrip(".")
    if node["BackendState"] != "Running" or not dns.endswith(".ts.net"):
        raise RuntimeError("Tailscale Funnel requires a running node with MagicDNS")
    endpoint = f"https://{dns}:8443{path}"

    class Relay(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_error(404)

        def do_POST(self):
            if self.path != path:
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if not 0 < size <= MAX_BODY:
                self.send_error(413)
                return
            self.connection.settimeout(10)
            body = self.rfile.read(size)
            headers = {
                key: self.headers[key]
                for key in ("X-Hub-Signature-256", "X-GitHub-Event", "X-GitHub-Delivery")
                if key in self.headers
            }
            try:
                reply = httpx.post(
                    control_plane + path, content=body, headers=headers, timeout=10, trust_env=False
                )
            except httpx.HTTPError:
                self.send_error(502)
                return
            self.send_response(reply.status_code)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as server:
        relay = Thread(target=server.serve_forever, daemon=True)
        relay.start()
        hook_id = None
        hooks = f"repos/{repository}/hooks"
        with subprocess.Popen(
            ["tailscale", "funnel", "--yes", "--https=8443", str(server.server_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as tunnel:
            try:
                deadline = time.monotonic() + 40
                with httpx.Client(timeout=2, trust_env=False) as client:
                    while time.monotonic() < deadline:
                        if tunnel.poll() is not None:
                            raise RuntimeError("Tailscale Funnel stopped before becoming reachable")
                        try:
                            if client.get(endpoint).status_code == 404:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("Temporary webhook ingress is unreachable")
                hook = subprocess.check_output(
                    ["gh", "api", "--method", "POST", hooks, "--input", "-", "--jq", ".id"],
                    input=json.dumps(
                        {
                            "name": "web",
                            "active": True,
                            "events": ["check_run", "workflow_run"],
                            "config": {
                                "url": endpoint,
                                "content_type": "json",
                                "secret": secret,
                                "insecure_ssl": "0",
                            },
                        }
                    ),
                    text=True,
                    timeout=20,
                )
                hook_id = int(hook)
                yield
            finally:
                try:
                    if hook_id is not None:
                        subprocess.run(
                            ["gh", "api", "--method", "DELETE", f"{hooks}/{hook_id}"],
                            check=True,
                            timeout=20,
                        )
                finally:
                    if tunnel.poll() is None:
                        tunnel.terminate()
                    server.shutdown()
                    relay.join(timeout=3)
