from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools.devspace_bridge import line_bridge_common as common
from tools.devspace_bridge import line_bridge_main as main
from tools.devspace_bridge import line_bridge_status as status


def config(allowed: tuple[str, ...] = ("U-ok",)) -> common.Config:
    return common.Config("secret", "token", allowed, 18787)


def test_signature_exact_raw_body() -> None:
    body = b'{"events":[]}'
    sig = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert common.verify_signature("secret", body, sig)
    assert not common.verify_signature("secret", body + b" ", sig)
    assert not common.verify_signature("secret", body, "")


def test_config_roundtrip_hides_plain_secrets(tmp_path: Path) -> None:
    path = tmp_path / "line.json"
    original = config()
    common.save_config(original, path)
    raw = path.read_text(encoding="utf-8")
    assert '"secret"' not in raw
    assert '"token"' not in raw
    assert common.load_config(path) == original


def test_pairing_requires_exact_code_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "line.json"
    cfg = config(())
    common.save_config(cfg, path)
    monkeypatch.setattr(main, "save_config", lambda value: common.save_config(value, path))
    monkeypatch.setattr(main, "write_runtime", lambda **_kwargs: None)
    app = main.LineBridgeApp(cfg, api=FakeApi())
    code = app.pairing.code
    wrong = "000001" if code == "000000" else "000000"
    assert not app._pair("U1", f"配對 {wrong}").startswith("配對完成")
    assert app._pair("U1", f"配對 {code}").startswith("配對完成")
    assert common.load_config(path).allowed_user_ids == ("U1",)


@pytest.mark.parametrize(
    "command",
    ["重連", "強制重連", "執行重連", "莊園", "執行莊園", "莊園執行", "停止莊園"],
)
def test_game_commands_are_locked(command: str) -> None:
    text = status.CommandRouter().handle(command)
    assert "安全鎖定" in text
    assert "未執行任何遊戲操作" in text


def test_unknown_command_fails_closed() -> None:
    text = status.CommandRouter().handle("shutdown /s")
    assert "不允許的指令" in text
    assert "可用指令" in text


class FakeApi:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    def reply(self, token: str, text: str) -> None:
        self.replies.append((token, text))


def event(source_type: str = "user") -> dict:
    source = {"type": source_type}
    if source_type == "user":
        source["userId"] = "U-ok"
    return {
        "type": "message",
        "webhookEventId": "evt-1",
        "replyToken": "reply-1",
        "source": source,
        "message": {"type": "text", "id": "msg-1", "text": "幫助"},
    }


def test_authorized_message_routes_once() -> None:
    api = FakeApi()
    app = main.LineBridgeApp(config(), api=api)
    app.process_payload({"events": [event(), event()]})
    assert len(api.replies) == 1
    assert "可用指令" in api.replies[0][1]


def test_group_source_is_ignored() -> None:
    api = FakeApi()
    app = main.LineBridgeApp(config(), api=api)
    app.process_payload({"events": [event("group")]})
    assert api.replies == []


def _server(app: main.LineBridgeApp):
    server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_invalid_webhook_signature_returns_401() -> None:
    server, thread = _server(main.LineBridgeApp(config(), api=FakeApi()))
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=b'{"events":[]}',
            headers={"x-line-signature": "bad"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=3)
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_valid_empty_webhook_returns_200() -> None:
    server, thread = _server(main.LineBridgeApp(config(), api=FakeApi()))
    try:
        port = server.server_address[1]
        body = b'{"events":[]}'
        signature = base64.b64encode(
            hmac.new(b"secret", body, hashlib.sha256).digest()
        ).decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=body,
            headers={"x-line-signature": signature},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
