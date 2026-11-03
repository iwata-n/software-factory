"""Serve the fixed Codex evaluation on a trusted private IPv4 network."""

import argparse
import hmac
import ipaddress
import json
import secrets
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EVALUATIONS = ROOT / ".local/evaluation"

PAGE = """<!doctype html>
<html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Software Factory デモ</title>
<style>
body{font:1rem/1.65 system-ui,sans-serif;margin:0;color:#182624;background:#f1f5f3}
main{max-width:640px;margin:auto;padding:28px 18px}
section{background:white;border:1px solid #cad9d3;border-radius:14px;padding:20px}
h1{font-size:1.65rem;margin:.25rem 0}h2{font-size:1.1rem}
button{font:inherit;min-height:48px;padding:0 18px;background:#075744;color:white;
border:0;border-radius:8px;width:100%;font-weight:700}button:disabled{opacity:.5}
pre{overflow:auto;background:#edf3f0;padding:12px;border-radius:8px;font-size:.85rem}
</style>
<main><section>
<small>SOFTWARE FACTORY</small>
<h1>実Codexデモ</h1>
<p>空の一覧に「データがありません」と表示する変更を、Codexが作成します。
独立テストとレビューを行い、公開準備まで進めます。</p>
<button id="start" disabled>デモを実行</button>
<p id="status" role="status">接続中…</p>
<div id="result" hidden><h2>生成されたコード</h2><pre id="code"></pre></div>
</section></main>
<script>
const token = location.hash.slice(1);
const button = document.getElementById('start');
const status = document.getElementById('status');
const result = document.getElementById('result');
const code = document.getElementById('code');
const headers = {Authorization: 'Bearer ' + token};
let running = false;
function show(data) {
  running = data.running;
  button.disabled = running;
  status.textContent = data.message;
  result.hidden = !data.code;
  code.textContent = data.code || '';
}
async function request(path, method) {
  const response = await fetch(path, {method, headers, cache: 'no-store'});
  if (response.status === 401) throw Error('Macに表示された試用リンクを開いてください。');
  if (!response.ok && response.status !== 409) throw Error('Macとの通信に失敗しました。');
  show(await response.json());
}
async function refresh() {
  try { await request('/status', 'GET'); }
  catch (error) { status.textContent = error.message; button.disabled = true; }
  finally { setTimeout(refresh, running ? 1000 : 3000); }
}
button.onclick = async () => {
  button.disabled = true;
  try { await request('/run', 'POST'); }
  catch (error) { status.textContent = error.message; }
};
refresh();
</script></html>"""


class Demo:
    def __init__(self):
        self.lock = Lock()
        self.state = {"running": False, "message": "実行ボタンを押してください。"}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def run(self):
        output = None
        try:
            with subprocess.Popen(
                ["sh", "scripts/try_codex.sh"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ) as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    if line.startswith("結果の保存先: "):
                        candidate = Path(line.removeprefix("結果の保存先: ").strip()).resolve()
                        if candidate.is_relative_to(EVALUATIONS.resolve()):
                            output = candidate
                    elif line.startswith('{"stage": "baseline"'):
                        self.update(
                            message="変更前のテストを確認しました。Codexが計画と変更を作成中です。"
                        )
                    elif line.startswith('{"stage": "codex_cli"'):
                        self.update(
                            message="変更を受け取りました。独立テストとレビューを実行中です。"
                        )
                success = process.wait() == 0
            if success and output is not None:
                report = json.loads((output / "evaluation.json").read_text())
                if report["result"] == "PASS":
                    self.update(
                        running=False,
                        message="合格しました。公開準備まで進みました。",
                        code=(output / "generated-app.py").read_text(),
                    )
                    return
        except (OSError, ValueError, KeyError) as error:
            print(f"Demo error: {type(error).__name__}", flush=True)
        self.update(
            running=False, message="実行に失敗しました。Macのターミナルで詳細を確認してください。"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not args.host.is_loopback and not any(
        args.host in ipaddress.ip_network(network) for network in private_ranges
    ):
        parser.error("--host must be a private LAN or loopback IPv4 address")
    host = f"{args.host}:{args.port}"
    token = secrets.token_urlsafe(24)
    demo = Demo()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, content, mime="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def allowed(self):
            if self.headers.get("Host") != host:
                self.reply(HTTPStatus.BAD_REQUEST, b"{}")
                return False
            value = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(value, f"Bearer {token}".encode()):
                self.reply(HTTPStatus.UNAUTHORIZED, b"{}")
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/" and self.headers.get("Host") == host:
                self.reply(HTTPStatus.OK, PAGE.encode(), "text/html")
            elif path == "/status":
                if self.allowed():
                    with demo.lock:
                        body = json.dumps(demo.state, ensure_ascii=False).encode()
                    self.reply(HTTPStatus.OK, body)
            else:
                self.reply(HTTPStatus.NOT_FOUND, b"{}")

        def do_POST(self):
            if self.path != "/run":
                self.reply(HTTPStatus.NOT_FOUND, b"{}")
                return
            if not self.allowed():
                return
            with demo.lock:
                status = HTTPStatus.CONFLICT
                if not demo.state["running"]:
                    demo.state = {"running": True, "message": "一時DBと検証環境を準備中です。"}
                    Thread(target=demo.run, daemon=False).start()
                    status = HTTPStatus.ACCEPTED
                body = json.dumps(demo.state, ensure_ascii=False).encode()
            self.reply(status, body)

    with ThreadingHTTPServer((str(args.host), args.port), Handler) as server:
        print(f"スマホの試用リンク: http://{host}/#{token}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
