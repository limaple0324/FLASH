"""LINE 橋接共用設定、安全儲存與官方介面。"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = 1
PORT = 8787
MAX_BODY = 1024 * 1024
MAX_STATUS = 64 * 1024
PAIR_SECONDS = 15 * 60
LINE_API = "https://api.line.me"
CF_VERSION = "2026.8.3"
CF_URL = f"https://github.com/cloudflare/cloudflared/releases/download/{CF_VERSION}/cloudflared-windows-amd64.exe"
CF_SHA256 = "83e726ed18ea78c5ad5213c4c3a3a27051393950d2bc8ed4de69bec12d14eaae"


class LineBridgeError(RuntimeError):
    pass


def state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "輔" / "Devspace") if base else (Path.home() / ".fu_devspace")


def config_path() -> Path:
    return state_root() / "line_bridge.json"


def runtime_path() -> Path:
    return state_root() / "line_runtime.json"


def stop_path() -> Path:
    return state_root() / "line_stop.request"


def cloudflared_path() -> Path:
    return state_root() / "bin" / "cloudflared.exe"


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _protect(text: str) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8")
    if os.name != "nt":
        return "plain:" + base64.b64encode(raw).decode("ascii")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)
    return "dpapi:" + base64.b64encode(protected).decode("ascii")


def _unprotect(text: str) -> str:
    if not text:
        return ""
    if text.startswith("plain:"):
        return base64.b64decode(text[6:]).decode("utf-8")
    if not text.startswith("dpapi:") or os.name != "nt":
        raise LineBridgeError("LINE 密鑰格式無效或無法在此系統解密")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = base64.b64decode(text[6:])
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        clear = ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)
    return clear.decode("utf-8")


@dataclass(frozen=True, slots=True)
class Config:
    channel_secret: str
    access_token: str
    allowed_user_ids: tuple[str, ...] = ()
    port: int = PORT

    def validate(self) -> None:
        if not self.channel_secret.strip():
            raise LineBridgeError("尚未設定 LINE 頻道密鑰")
        if not self.access_token.strip():
            raise LineBridgeError("尚未設定 LINE 存取權杖")
        if not 1024 <= int(self.port) <= 65535:
            raise LineBridgeError("LINE 橋接連接埠無效")


def save_config(config: Config, path: Path | None = None) -> None:
    config.validate()
    atomic_json(path or config_path(), {
        "version": VERSION,
        "channel_secret": _protect(config.channel_secret),
        "access_token": _protect(config.access_token),
        "allowed_user_ids": list(dict.fromkeys(config.allowed_user_ids)),
        "port": int(config.port),
    })


def load_config(path: Path | None = None) -> Config:
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LineBridgeError("尚未設定 LINE 橋接，請先執行「設定LINE橋接」") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LineBridgeError("LINE 設定檔無法讀取") from exc
    if raw.get("version") != VERSION:
        raise LineBridgeError("LINE 設定檔版本不支援")
    ids = raw.get("allowed_user_ids") or []
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
        raise LineBridgeError("LINE 授權名單格式無效")
    config = Config(
        _unprotect(str(raw.get("channel_secret") or "")),
        _unprotect(str(raw.get("access_token") or "")),
        tuple(dict.fromkeys(x for x in ids if x)),
        int(raw.get("port") or PORT),
    )
    config.validate()
    return config


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("ascii")
    return bool(signature) and hmac.compare_digest(expected, signature.strip())


class LineApi:
    def __init__(self, token: str) -> None:
        self.token = token

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            LINE_API + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read(65536)
        except urllib.error.HTTPError as exc:
            detail = exc.read(32768).decode("utf-8", "replace")
            raise LineBridgeError(f"LINE 介面回應錯誤 {exc.code}：{detail}") from exc
        except OSError as exc:
            raise LineBridgeError(f"LINE 連線失敗：{exc}") from exc
        return json.loads(body.decode("utf-8")) if body else {}

    def reply(self, token: str, text: str) -> None:
        self._request("POST", "/v2/bot/message/reply", {
            "replyToken": token,
            "messages": [{"type": "text", "text": text[:5000]}],
        })

    def push(self, user_id: str, text: str) -> None:
        self._request("POST", "/v2/bot/message/push", {
            "to": user_id,
            "messages": [{"type": "text", "text": text[:5000]}],
        })

    def set_webhook(self, endpoint: str) -> None:
        self._request("PUT", "/v2/bot/channel/webhook/endpoint", {"endpoint": endpoint})

    def test_webhook(self, endpoint: str) -> None:
        self._request("POST", "/v2/bot/channel/webhook/test", {"endpoint": endpoint})

    def webhook_info(self) -> dict[str, Any]:
        return self._request("GET", "/v2/bot/channel/webhook/endpoint")


def install_cloudflared(path: Path | None = None) -> Path:
    if os.name != "nt":
        raise LineBridgeError("自動安裝外部連線元件目前只支援 Windows")
    target = path or cloudflared_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".download")
    digest = hashlib.sha256()
    request = urllib.request.Request(CF_URL, headers={"User-Agent": "FuLineBridge/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if digest.hexdigest().lower() != CF_SHA256:
        tmp.unlink(missing_ok=True)
        raise LineBridgeError("外部連線元件雜湊驗證失敗，已拒絕安裝")
    tmp.replace(target)
    return target


def write_runtime(**changes: Any) -> None:
    path = runtime_path()
    value: dict[str, Any] = {"version": VERSION}
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(old, dict):
            value.update(old)
    except (OSError, json.JSONDecodeError):
        pass
    value.update(changes)
    value["version"] = VERSION
    value["updated_at_unix"] = time.time()
    atomic_json(path, value)
