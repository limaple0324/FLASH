"""LINE 橋接唯讀狀態查詢。"""
from __future__ import annotations

import csv
import ctypes
import json
import os
import subprocess
from typing import Any

from tools.devspace_bridge.line_bridge_common import MAX_STATUS, state_root

OBSERVABLE = frozenset({"flash.exe", "flashplayer.exe", "flashplayer_11_sa.exe", "輔v0.2.exe"})
LOCKED = frozenset({"重連", "強制重連", "執行重連", "莊園", "執行莊園", "莊園執行", "停止莊園"})


def process_snapshot() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=10, shell=False,
        )
    except OSError:
        return []
    found: list[dict[str, Any]] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2 or row[0].casefold() not in OBSERVABLE:
            continue
        try:
            found.append({"name": row[0], "pid": int(row[1])})
        except ValueError:
            pass
    return found


def window_snapshot(pids: set[int]) -> list[dict[str, Any]]:
    if os.name != "nt" or not pids:
        return []
    user32 = ctypes.windll.user32
    found: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in pids:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        found.append({"hwnd": int(hwnd), "pid": int(pid.value), "title": buffer.value})
        return True

    user32.EnumWindows(callback, 0)
    return found


def optional_status(name: str) -> dict[str, Any] | None:
    path = state_root() / "status" / f"{name}.json"
    try:
        if not path.is_file() or path.stat().st_size > MAX_STATUS:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def status_text() -> str:
    processes = process_snapshot()
    windows = window_snapshot({int(item["pid"]) for item in processes})
    return "\n".join([
        "輔遠端狀態",
        f"Devspace 橋接：{'在線' if (state_root() / 'bridge.pid').is_file() else '未偵測到'}",
        f"可觀察遊戲程序：{len(processes)}",
        f"可觀察視窗：{len(windows)}",
        "遠端遊戲控制：鎖定",
    ])


def windows_text() -> str:
    processes = process_snapshot()
    windows = window_snapshot({int(item["pid"]) for item in processes})
    if not windows:
        return "目前沒有偵測到可觀察的遊戲視窗。"
    lines = [f"目前偵測到 {len(windows)} 個視窗："]
    for item in windows[:40]:
        title = item["title"].strip() or "（無標題）"
        lines.append(f"• {title}｜PID {item['pid']}")
    return "\n".join(lines)


def snapshot_text(name: str, label: str) -> str:
    value = optional_status(name)
    if not value:
        return f"{label}目前沒有可讀取的狀態快照。\n不會因此執行任何遊戲操作。"
    state = value.get("state") or value.get("status") or "未知"
    detail = value.get("message") or value.get("detail") or ""
    updated = value.get("updated_at") or value.get("updated_at_unix") or ""
    lines = [f"{label}狀態：{state}"]
    if detail:
        lines.append(f"說明：{detail}")
    if updated:
        lines.append(f"更新：{updated}")
    return "\n".join(lines)


def help_text() -> str:
    return "可用指令：\n狀態\n視窗\n重連狀態\n莊園狀態\n\n重連與莊園執行指令目前安全鎖定。"


class CommandRouter:
    def handle(self, text: str) -> str:
        command = text.strip()
        if command in LOCKED:
            return "此遠端控制目前安全鎖定，未執行任何遊戲操作。"
        if command == "狀態":
            return status_text()
        if command == "視窗":
            return windows_text()
        if command == "重連狀態":
            return snapshot_text("smart_reconnect", "智慧重連")
        if command == "莊園狀態":
            return snapshot_text("manor", "莊園")
        if command in {"幫助", "指令", "功能"}:
            return help_text()
        return "不允許的指令。\n\n" + help_text()
