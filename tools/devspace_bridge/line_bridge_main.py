"""LINE 安全橋接主程式。第一階段不執行任何遊戲操作。"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tools.devspace_bridge.line_bridge_common import (
    MAX_BODY,
    PAIR_SECONDS,
    Config,
    LineApi,
    LineBridgeError,
    cloudflared_path,
    install_cloudflared,
    load_config,
    runtime_path,
    save_config,
    stop_path,
    verify_signature,
    write_runtime,
)
from tools.devspace_bridge.line_bridge_status import CommandRouter

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


class Pairing:
    def __init__(self, enabled: bool) -> None:
        self.code = f"{secrets.randbelow(1_000_000):06d}" if enabled else ""
        self.expires = time.time() + PAIR_SECONDS if enabled else 0.0

    def renew_if_needed(self) -> None:
        if self.code and time.time() > self.expires:
            self.code = f"{secrets.randbelow(1_000_000):06d}"
            self.expires = time.time() + PAIR_SECONDS


class LineBridgeApp:
    def __init__(
        self,
        config: Config,
        api: LineApi | None = None,
        router: CommandRouter | None = None,
    ) -> None:
        self.config = config
        self.api = api or LineApi(config.access_token)
        self.router = router or CommandRouter()
        self.pairing = Pairing(not config.allowed_user_ids)
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def _remember(self, event: dict[str, Any]) -> bool:
        key = str(
            event.get("webhookEventId")
            or (event.get("message") or {}).get("id")
            or event.get("replyToken")
            or ""
        )
        if not key:
            return True
        with self.lock:
            if key in self.seen:
                return False
            self.seen[key] = time.time()
            if len(self.seen) > 1000:
                for old in sorted(self.seen, key=self.seen.get)[:200]:
                    self.seen.pop(old, None)
        return True

    def _pair(self, user_id: str, text: str) -> str | None:
        if user_id in self.config.allowed_user_ids:
            return None
        self.pairing.renew_if_needed()
        if not self.pairing.code:
            return "此 LINE 帳號未獲授權。"
        expected = "配對 " + self.pairing.code
        if not hmac.compare_digest(text.strip().encode("utf-8"), expected.encode("utf-8")):
            return "此 LINE 帳號尚未授權。請使用本機 LINE 橋接顯示的配對碼。"
        self.config = replace(
            self.config,
            allowed_user_ids=tuple(dict.fromkeys((*self.config.allowed_user_ids, user_id))),
        )
        save_config(self.config)
        self.pairing.code = ""
        self.pairing.expires = 0.0
        write_runtime(pairing_code=None, pairing_expires_at_unix=None)
        return "配對完成。現在可使用「狀態」、「視窗」、「重連狀態」、「莊園狀態」。"

    def process_event(self, event: dict[str, Any]) -> None:
        if not self._remember(event) or event.get("type") != "message":
            return
        message = event.get("message") or {}
        source = event.get("source") or {}
        if message.get("type") != "text" or source.get("type") != "user":
            return
        user_id = str(source.get("userId") or "")
        reply_token = str(event.get("replyToken") or "")
        if not user_id or not reply_token:
            return
        text = str(message.get("text") or "")
        response = self._pair(user_id, text)
        if response is None:
            response = self.router.handle(text)
        self.api.reply(reply_token, response)

    def process_payload(self, payload: dict[str, Any]) -> None:
        events = payload.get("events") or []
        if not isinstance(events, list):
            return
        for event in events:
            if isinstance(event, dict):
                self.process_event(event)


def make_handler(app: LineBridgeApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            signature = self.headers.get("x-line-signature", "")
            if not verify_signature(app.config.channel_secret, body, signature):
                self.send_error(401)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            threading.Thread(
                target=app.process_payload, args=(payload,), daemon=True
            ).start()

    return Handler


class QuickTunnel:
    def __init__(self, port: int) -> None:
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.lines: queue.Queue[str] = queue.Queue()

    def start(self, timeout: float = 30.0) -> str:
        executable = cloudflared_path()
        if not executable.is_file():
            raise LineBridgeError("尚未安裝 LINE 對外連線元件，請先執行「設定LINE橋接」")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [
                str(executable), "tunnel", "--url",
                f"http://127.0.0.1:{self.port}", "--no-autoupdate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )

        def read_lines() -> None:
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                self.lines.put(line.rstrip())

        threading.Thread(target=read_lines, daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LineBridgeError("LINE 對外連線元件提前結束")
            try:
                line = self.lines.get(timeout=0.25)
            except queue.Empty:
                continue
            match = TUNNEL_RE.search(line)
            if match:
                return match.group(0).rstrip("/")
        raise LineBridgeError("取得 LINE 對外網址逾時")

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def serve(config: Config, no_tunnel: bool = False) -> int:
    stop_request = stop_path()
    stop_request.parent.mkdir(parents=True, exist_ok=True)
    stop_request.unlink(missing_ok=True)
    app = LineBridgeApp(config)
    server = ThreadingHTTPServer(("127.0.0.1", config.port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tunnel: QuickTunnel | None = None
    endpoint: str | None = None
    active: bool | None = None
    try:
        if not no_tunnel:
            tunnel = QuickTunnel(config.port)
            public_url = tunnel.start()
            endpoint = public_url + "/webhook"
            app.api.set_webhook(endpoint)
            app.api.test_webhook(endpoint)
            info = app.api.webhook_info()
            active = bool(info.get("active")) if "active" in info else None
        write_runtime(
            pid=os.getpid(),
            local_url=f"http://127.0.0.1:{config.port}",
            webhook_url=endpoint,
            webhook_active=active,
            control_locked=True,
            pairing_code=app.pairing.code or None,
            pairing_expires_at_unix=app.pairing.expires if app.pairing.code else None,
        )
        while not stop_request.exists():
            time.sleep(0.5)
        return 0
    finally:
        server.shutdown()
        server.server_close()
        if tunnel:
            tunnel.stop()
        runtime_path().unlink(missing_ok=True)
        stop_request.unlink(missing_ok=True)


def push_message(config: Config, text: str) -> int:
    if not config.allowed_user_ids:
        raise LineBridgeError("尚未完成 LINE 帳號配對")
    api = LineApi(config.access_token)
    for user_id in config.allowed_user_ids:
        api.push(user_id, text)
    return len(config.allowed_user_ids)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-tunnel", action="store_true")
    parser.add_argument("--no-tunnel", action="store_true")
    parser.add_argument("--push")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    try:
        if args.stop:
            target = stop_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("stop\n", encoding="ascii")
            print("已送出 LINE 橋接停止請求")
            return 0
        if args.install_tunnel:
            print(f"外部連線元件已安裝：{install_cloudflared()}")
            return 0
        if args.status:
            print(CommandRouter().handle("狀態"))
            return 0
        config = load_config()
        if args.push is not None:
            print(f"已送出 {push_message(config, args.push)} 個 LINE 通知")
            return 0
        return serve(config, no_tunnel=args.no_tunnel)
    except LineBridgeError as exc:
        print(f"LINE 橋接錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
