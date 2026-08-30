# -*- coding: utf-8 -*-
"""
魔力學堂「智慧重連」背景低耗版

核心策略：
1. 優先使用 Windows 背景擷取與背景訊息點擊；若畫面驗證證實舊 Flash 拒絕背景輸入，
   才短暫切到遊戲送出真實滑鼠點擊，並立即還原滑鼠與原前景視窗。
2. 正常監看先做低成本的中央彈窗外形檢查；只有疑似斷線彈窗出現才做模板/OCR。
3. OCR 只在需要時執行；OpenCV 固定單執行緒，降低 CPU 尖峰。
4. 正常監看採較低頻率，進入重連流程才短暫提高掃描頻率。
5. V6.3 改為「高速狀態機」：正常監看低頻，進入重連後依目前階段切換高頻小範圍辨識。
6. V8.1 保留 V6.3 已驗證的高速狀態機；只有「按下強制登入」後固定等待 20 秒。
7. 畫面先正規化成 Flash 自己的邏輯客戶區，再做所有模板/OCR；不同螢幕 DPI 與視窗縮放不再滲入上層流程。
8. 背景點擊由「邏輯畫布座標 → 實體螢幕點 → 目標 Flash 子視窗原生邏輯座標」自動轉換，不再人工乘 0.667/1.5。
9. V8.2 斷線辨識改成「邏輯畫布＋原始 PrintWindow 雙畫面」；邏輯畫布漏判時直接用未縮放原始畫面再辨識，避免跨 DPI 正規化失真。
10. OCR 改成流程優先排程；監看階段 OCR 忙碌時直接略過，不再阻塞選線/選角。
11. 城鎮/一般斷線全程走遊戲內重連；只有已判定為戰鬥場景斷線才允許依原規則重開捷徑。
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import json
import logging
import os
import re
import sys
import threading
import time
try:
    import msvcrt
except Exception:
    msvcrt = None
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

import dpi_policy
import fishing_profiles
import manor_runtime
import window_geometry
from user_activity_guard import USER_ACTIVITY_GUARD
from runtime_paths import (
    APP_DATA_DIR,
    IS_FROZEN,
    RESOURCE_DIR,
    USER_DATA_DIR,
    sanitized_record,
)

if os.name != "nt":
    raise SystemExit("此程式只支援 Windows（視窗自動點擊需要 Windows API）。")

try:
    import win32api
    import win32con
    import win32gui
    import win32ui
except Exception as e:
    if IS_FROZEN:
        raise SystemExit(
            "免安裝封裝不完整或已損壞：Windows 介面模組無法載入。"
            "請重新解壓完整 ZIP 或重新取得 EXE；安裝 Python 無法修復此問題。"
        ) from e
    raise SystemExit("開發環境缺少 pywin32，請安裝專案鎖定的必要套件。") from e


_ULONG_PTR = wintypes.WPARAM


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

FROZEN = IS_FROZEN
BASE_DIR = APP_DATA_DIR
CONFIG_PATH = APP_DATA_DIR / "config.json"
TEMPLATE_DIR = RESOURCE_DIR / "templates"
LOG_DIR = APP_DATA_DIR / "logs"
PID_PATH = APP_DATA_DIR / "monitor.pid"
STOP_SIGNAL_PATH = APP_DATA_DIR / "stop.signal"
STATUS_PATH = APP_DATA_DIR / "runtime_status.json"
FISHING_PROGRESS_PATH = APP_DATA_DIR / "fishing_progress.json"
FISHING_BACKGROUND_ONLY = True
LEGACY_BINDINGS_PATH = APP_DATA_DIR / "bindings.json"
BINDINGS_PATH = USER_DATA_DIR / "bindings.json"
BINDINGS_LOCK_PATH = USER_DATA_DIR / "bindings.lock"
IDENTITY_PATH = USER_DATA_DIR / "identity_profiles.json"
LOG_DIR.mkdir(exist_ok=True)

_fishing_progress_lock = threading.RLock()


def _read_fishing_progress() -> dict:
    with _fishing_progress_lock:
        try:
            obj = json.loads(FISHING_PROGRESS_PATH.read_text(encoding="utf-8-sig"))
            rows = obj.get("profiles", {}) if isinstance(obj, dict) else {}
            return dict(rows) if isinstance(rows, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


def _write_fishing_progress(key: str, value: dict) -> None:
    if not key:
        return
    with _fishing_progress_lock:
        rows = _read_fishing_progress()
        rows[str(key)] = dict(value)
        payload = {"version": 1, "updated_at": time.time(), "profiles": rows}
        try:
            FISHING_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = FISHING_PROGRESS_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, FISHING_PROGRESS_PATH)
        except OSError:
            pass

# V8.0：控制程序本身固定使用 Per-Monitor-V2 DPI 感知。
# 這只決定「我們呼叫 Windows API 時看到什麼座標」；Flash 自己的 DPI 感知由後面的
# PhysicalToLogicalPointForPerMonitorDPI 針對目標視窗個別處理，不再人工猜倍率。
try:
    _pmv2 = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    if not ctypes.windll.user32.SetProcessDpiAwarenessContext(_pmv2):
        raise OSError('SetProcessDpiAwarenessContext failed')
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# 限制 OpenCV 不要為每個遊戲視窗開很多 CPU 執行緒。
try:
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
except Exception:
    pass


# -----------------------------
# 記錄
# -----------------------------

def build_logger() -> logging.Logger:
    logger = logging.getLogger("智慧重連")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")

    if sys.stdout is not None:
        # 背景程序被控制台重導向到檔案時，Windows 可能讓 stdout 使用 CP950。
        # 強制改成 UTF-8，避免中文紀錄被寫成亂碼。
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    log_file = LOG_DIR / f"智慧重連_{time.strftime('%Y%m%d_%H%M%S')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


LOG = build_logger()


# Portable runtime policy. These flags are also checked by --self-test so a
# future packaging change cannot silently restore the old zero-input behavior.
RUNTIME_DPI_COMPAT_ENABLED = True
UNBOUND_WINDOW_BASE_MONITORING = True
# 全程只允許目標視窗背景訊息。即使舊 config 仍保存 true，也不得搶前景、滑鼠或鍵盤。
FOREGROUND_PHYSICAL_FALLBACK = False
MINIMIZED_WINDOW_MONITORING = False


# -----------------------------
# 設定
# -----------------------------

DEFAULT_CONFIG = {
    "視窗標題包含": ["Adobe Flash Player 11"],
    "背景模式": True,
    "高速狀態機": True,
    "背景擷取並行數": 1,
    "全域監看擷取最短間隔秒": 0.20,
    "正常監測間隔秒": 1.80,
    "同時重連時閒置監測間隔秒": 2.40,
    "同時斷線發現加速秒": 2.8,
    "重連流程掃描間隔秒": 0.08,
    "高速等待登入掃描秒": 0.07,
    "高速等待登入後掃描秒": 0.07,
    "高速選線掃描秒": 0.07,
    "高速等待角色掃描秒": 0.08,
    "高速選角掃描秒": 0.08,
    "高速進入遊戲掃描秒": 0.08,
    "高速整理掃描秒": 0.08,
    "自動戰鬥切換最短間隔秒": 1.35,
    "自動戰鬥最多切換次數": 3,
    "自動戰鬥彈窗關閉後穩定秒": 0.80,
    "自動戰鬥無X最低相似度": 0.68,
    "自動戰鬥有X最低相似度": 0.68,
    "自動戰鬥無X確認幀": 2,
    "自動戰鬥X修正重試秒": 12.0,
    "自動戰鬥常駐未知警告秒": 10.0,
    "自動戰鬥常駐OCR最短間隔秒": 3.0,
    "互動背景移入等待秒": 0.075,
    "互動背景按下秒": 0.090,
    "背景擷取失敗重試秒": 0.80,
    "視窗發現掃描間隔秒": 3.00,
    "狀態檔更新間隔秒": 1.50,
    "啟動自我檢查秒": 2.0,
    "啟動自我檢查有效幀": 3,
    "斷線確認重試間隔秒": 2.0,
    "斷線OCR最短間隔秒": 1.80,
    "斷線確認按鈕快篩最低相似度": 0.78,
    "斷線視覺確認鈕最低黃字比例": 0.004,
    "斷線弱整框最低相似度": 0.36,
    "背景點擊驗證等待秒": 0.14,
    "斷線確認轉頁驗證秒": 2.20,
    "斷線確認消失連續幀": 2,
    "斷線確認消失最低變化": 5.0,
    "統一邏輯畫布": True,
    "DPI即時相容": True,
    "允許前景實體輸入備援": False,
    "前景實體點擊移入等待秒": 0.055,
    "前景實體點擊按下秒": 0.085,
    "前景實體點擊後等待秒": 0.120,
    "最小化視窗持續監測": False,
    "最小化監測探測間隔秒": 3.0,
    "最小化還原繪圖等待秒": 0.60,
    "最小化探測擷取重試次數": 3,
    "釣魚點擊後驗證秒": 20.0,
    "釣魚雙擊後驗證秒": 10.0,
    "釣魚座標點擊次數": 2,
    "釣魚雙擊間隔秒": 0.12,
    "釣魚目前分頁確認秒": 3.0,
    "釣魚目前分頁模板最低相似度": 0.58,
    "釣魚發送頻道確認秒": 3.0,
    "釣魚發送頻道模板最低相似度": 0.58,
    "釣魚連結出現等待秒": 6.0,
    "釣魚發送重試秒": 4.0,
    "釣魚成功後複查秒": 10.0,
    "釣魚狀態消失確認次數": 3,
    "釣魚轉圖地圖列變化比例": 0.070,
    "釣魚轉圖場景變化比例": 0.115,
    "釣魚轉圖連續確認幀": 2,
    "釣魚轉圖穩定地圖列差": 0.038,
    "釣魚轉圖穩定確認幀": 2,
    "釣魚轉圖穩定後等待秒": 2.0,
    "釣魚轉圖最長等待秒": 30.0,
    "未綁定視窗基礎監測": True,
    "唯一候選自動綁定": True,
    "辨識基準寬度": 900,
    "強制登入重試秒": 6.0,
    "強制登入快速重試秒": 2.4,
    "強制登入點擊驗證等待秒": 0.34,
    "強制登入後固定等待秒": 20.0,
    "強制登入後禁止重點秒": 20.0,
    "強制登入後轉頁觀察秒": 3.0,
    "強制登入仍在確認幀": 3,
    "登入後畫面最長等待秒": 30.0,
    "線路畫面標題最低相似度": 0.58,
    "線路畫面連續確認幀": 2,
    "線路畫面穩定等待秒": 0.45,
    "高速線路畫面穩定等待秒": 0.12,
    "線路OCR重試次數": 3,
    "線路OCR重試間隔秒": 0.35,
    "線路單次OCR高信心": 0.80,
    "選線未生效重試秒": 1.50,
    "選線最多重試次數": 3,
    "線路按鈕模板最低相似度": 0.54,
    "線路辨識失敗備援": 1,
    "啟用OCR": True,
    "OCR最低信心": 0.50,
    "只辨識不點擊": False,
    "角色找不到等待秒": 6.0,
    "角色OCR重試間隔秒": 0.45,
    "高速角色OCR重試間隔秒": 0.20,
    "角色畫面連續確認幀": 2,
    "進入遊戲按鈕重試秒": 0.90,
    "高速進入遊戲按鈕重試秒": 1.05,
    "進入遊戲點擊後快速驗證秒": 0.32,
    "進入遊戲畫面驗證間隔秒": 0.34,
    "進入遊戲實際重試最短秒": 2.50,
    "進入遊戲每輪最多輸入次數": 3,
    "進入遊戲成功連續確認幀": 2,
    "角色找不到選第一格": False,  # V5.6 已停用：辨識不到角色時禁止亂選第一格。
    "進入遊戲最長等待秒": 30.0,
    "彈窗關閉後等待秒": 0.12,
    "高速彈窗關閉後等待秒": 0.08,
    "進入遊戲彈窗觀察秒": 5.00,
    "進入遊戲彈窗安靜確認秒": 2.00,
    "進入遊戲彈窗消失確認幀": 3,
    "自動戰鬥切換重試秒": 0.55,
    "高速自動戰鬥切換重試秒": 0.30,
    "自動戰鬥辨識最低相似度": 0.64,
    "進入遊戲整理警告重複秒": 10.0,
    "登入頁深度辨識間隔秒": 0.70,
    "線路頁深度辨識間隔秒": 0.45,
    "流程中斷線複查間隔秒": 1.20,
    "遮擋檢查間隔秒": 0.65,
    "自動保存診斷截圖": True,
    "診斷截圖冷卻秒": 4.0,
    "戰鬥斷線重新啟動": True,
    "戰鬥場景模板最低相似度": 0.82,
    "戰鬥場景記憶秒": 5.0,
    "戰鬥重開等待新視窗秒": 35.0,
    "戰鬥重開後關閉舊視窗": True,
    "捷徑設定": [
        {
            "名稱": "遊戲1",
            "捷徑路徑": "",
            "啟動": False,
            "戰鬥斷線允許重開": True,
            "優先角色": "",
            "角色模板": ""
        }
    ]
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


CONFIG = load_config()


# -----------------------------
# 視窗 ↔ 捷徑綁定
# -----------------------------

BINDINGS_FILE_LOCK = threading.Lock()


def get_window_pid(hwnd: int) -> int:
    try:
        _tid, pid = win32gui.GetWindowThreadProcessId(int(hwnd))
        return int(pid)
    except Exception:
        return 0


def get_window_dpi(hwnd: int) -> int:
    try:
        fn = ctypes.windll.user32.GetDpiForWindow
        fn.argtypes = [wintypes.HWND]
        fn.restype = wintypes.UINT
        v = int(fn(int(hwnd)))
        return v if v >= 48 else 96
    except Exception:
        return 96


def get_monitor_dpi(hwnd: int) -> int:
    try:
        mon = ctypes.windll.user32.MonitorFromWindow(int(hwnd), 2)
        if mon:
            x = wintypes.UINT(); y = wintypes.UINT()
            fn = ctypes.windll.shcore.GetDpiForMonitor
            fn.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
            fn.restype = ctypes.c_long
            if int(fn(mon, 0, ctypes.byref(x), ctypes.byref(y))) == 0 and int(x.value) >= 48:
                return int(x.value)
    except Exception:
        pass
    return get_window_dpi(hwnd)


@contextmanager
def binding_file_lock(timeout: float = 2.0):
    """控制台與背景程序共用的跨程序鎖；檔案本身仍採原子 replace。"""
    if msvcrt is None:
        yield
        return
    BINDINGS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(BINDINGS_LOCK_PATH, "a+b")
    locked = False
    try:
        if f.tell() == 0:
            f.write(b"\\0"); f.flush()
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                time.sleep(0.02)
        if not locked:
            raise TimeoutError("綁定資料鎖定逾時")
        yield
    finally:
        if locked:
            try:
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        try:
            f.close()
        except Exception:
            pass


def _profile_key(path_text: str) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    try:
        text = os.path.abspath(os.path.expandvars(os.path.expanduser(text)))
    except Exception:
        pass
    return os.path.normcase(text)


def _read_identities_unlocked() -> Dict[str, dict]:
    try:
        if not IDENTITY_PATH.exists():
            return {}
        obj = json.loads(IDENTITY_PATH.read_text(encoding="utf-8-sig"))
        raw = obj.get("profiles", obj) if isinstance(obj, dict) else {}
        return {str(k): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _overlay_identity(item: dict, identities: Dict[str, dict]) -> dict:
    item = dict(item or {})
    key = _profile_key(item.get("shortcut_path", ""))
    ident = identities.get(key, {}) if key else {}
    if ident:
        if "shortcut_name" in ident:
            item["shortcut_name"] = str(ident.get("shortcut_name", "") or "")
        if "preferred_role" in ident:
            item["preferred_role"] = str(ident.get("preferred_role", "") or "")
    return item


def _migrate_legacy_bindings_once():
    try:
        if BINDINGS_PATH.exists() and BINDINGS_PATH.stat().st_size > 20:
            return
        if not LEGACY_BINDINGS_PATH.exists() or LEGACY_BINDINGS_PATH.stat().st_size <= 20:
            return
        obj = json.loads(LEGACY_BINDINGS_PATH.read_text(encoding="utf-8-sig"))
        BINDINGS_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


_migrate_legacy_bindings_once()


def _read_binding_store_unlocked() -> Tuple[Dict[int, dict], Dict[str, dict]]:
    raw_out: Dict[int, dict] = {}
    profiles: Dict[str, dict] = {}
    try:
        if not BINDINGS_PATH.exists():
            return raw_out, profiles
        obj = json.loads(BINDINGS_PATH.read_text(encoding="utf-8-sig"))
        raw = obj.get("bindings", obj) if isinstance(obj, dict) else {}
        prof = obj.get("profiles", {}) if isinstance(obj, dict) else {}
        if isinstance(prof, dict):
            profiles = {str(k): sanitized_record(v) for k, v in prof.items() if isinstance(v, dict)}
        identities = _read_identities_unlocked()
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                try:
                    hwnd = int(k)
                except Exception:
                    continue
                raw_out[hwnd] = _overlay_identity(sanitized_record(v), identities)
    except Exception:
        return {}, {}
    return raw_out, profiles


def _write_binding_store_unlocked(bindings: Dict[int, dict], profiles: Dict[str, dict]):
    payload = {
        "version": 4,
        "updated_at": time.time(),
        "bindings": {str(int(k)): sanitized_record(v) for k, v in bindings.items() if isinstance(v, dict)},
        "profiles": {str(k): sanitized_record(v) for k, v in profiles.items() if isinstance(v, dict)},
    }
    tmp = BINDINGS_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, BINDINGS_PATH)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _is_live_game_window(hwnd: int) -> bool:
    """精確 HWND 仍存在且仍是本遊戲 Flash 視窗時才承認綁定。

    不再因 PID 欄位偶發不一致就把同一個仍存活的 HWND 判成未綁定；HWND 本身若已失效則不沿用。
    """
    try:
        if not win32gui.IsWindow(int(hwnd)):
            return False
        title = win32gui.GetWindowText(int(hwnd)) or ""
        keys = [str(x) for x in CONFIG.get("視窗標題包含", ["Adobe Flash Player 11"]) if str(x)]
        if keys and not any(k in title for k in keys):
            return False
        l, t, r, b = win32gui.GetClientRect(int(hwnd))
        return (r - l) >= 300 and (b - t) >= 180
    except Exception:
        return False


def load_window_bindings() -> Dict[int, dict]:
    """讀取目前仍存活的精確 HWND 綁定。

    使用者只重啟控制台/監測程序時，Flash HWND 仍相同，因此綁定必須立即沿用。
    PID 只作診斷，不再作清除條件，避免控制台與背景程序看到不同 PID 資訊時角色被洗成「未知」。
    """
    with BINDINGS_FILE_LOCK:
        try:
            with binding_file_lock():
                raw, _profiles = _read_binding_store_unlocked()
                out: Dict[int, dict] = {}
                for hwnd, item in raw.items():
                    if not _is_live_game_window(hwnd):
                        continue
                    cur = dict(item)
                    cur["pid"] = get_window_pid(hwnd) or int(cur.get("pid", 0) or 0)
                    out[int(hwnd)] = cur
                return out
        except Exception:
            # 讀檔/鎖競爭失敗時回空；worker 端有連續遺失保護，不會一幀就解除既有身分。
            return {}


def save_window_bindings(bindings: Dict[int, dict]):
    """背景程序只有戰鬥重開轉移 HWND 時會寫；永遠保留控制台的 profiles/角色資料。"""
    with BINDINGS_FILE_LOCK:
        with binding_file_lock():
            current, profiles = _read_binding_store_unlocked()
            identities = _read_identities_unlocked()
            clean: Dict[int, dict] = {}
            for hwnd, item in bindings.items():
                cur = dict(item)
                cur["pid"] = get_window_pid(int(hwnd))
                cur = _overlay_identity(cur, identities)
                clean[int(hwnd)] = cur
            _write_binding_store_unlocked(clean, profiles)


def transfer_window_binding(old_hwnd: int, new_hwnd: int):
    """戰鬥斷線由程式自己重開時，精確把原本身分搬到新 HWND。"""
    with BINDINGS_FILE_LOCK:
        try:
            with binding_file_lock():
                raw, profiles = _read_binding_store_unlocked()
                item = raw.pop(int(old_hwnd), None)
                if not item:
                    return
                item = _apply_process_identity_to_item(int(new_hwnd), dict(item))
                item["pid"] = get_window_pid(int(new_hwnd))
                item["last_hwnd"] = int(new_hwnd)
                item["bound_at"] = time.time()
                raw[int(new_hwnd)] = item
                pkey = str(item.get("profile_key", "") or _profile_key(item.get("shortcut_path", "")))
                if pkey:
                    prof = dict(profiles.get(pkey, {}))
                    prof.update({k:v for k,v in item.items() if k != "pid"})
                    prof["profile_key"] = pkey
                    prof["updated_at"] = time.time()
                    profiles[pkey] = prof
                _write_binding_store_unlocked(raw, profiles)
        except Exception as e:
            LOG.warning("轉移視窗綁定失敗：%s", e)

def _apply_process_identity_to_item(hwnd: int, item: dict) -> dict:
    cur = dict(item or {})
    try:
        ident = dpi_policy.window_process_identity(int(hwnd))
    except Exception:
        ident = {}
    if ident.get("process_exe"):
        cur["process_exe"] = str(ident.get("process_exe") or "")
    if ident.get("process_identity"):
        cur["process_identity"] = str(ident.get("process_identity") or "")
    return cur


def backfill_live_binding_process_identities() -> int:
    """One-time migration for bindings created before V11.6. No window is restarted."""
    changed = 0
    with BINDINGS_FILE_LOCK:
        try:
            with binding_file_lock():
                raw, profiles = _read_binding_store_unlocked()
                dirty = False
                for hwnd, item in list(raw.items()):
                    if not _is_live_game_window(int(hwnd)):
                        continue
                    if str(item.get("process_identity", "") or ""):
                        continue
                    new_item = _apply_process_identity_to_item(int(hwnd), item)
                    if not str(new_item.get("process_identity", "") or ""):
                        continue
                    raw[int(hwnd)] = new_item
                    pkey = str(new_item.get("profile_key", "") or _profile_key(new_item.get("shortcut_path", "")))
                    if pkey:
                        prof = dict(profiles.get(pkey, {}))
                        prof.update({k:v for k,v in new_item.items() if k != "pid"})
                        prof["profile_key"] = pkey
                        prof["updated_at"] = time.time()
                        profiles[pkey] = prof
                    dirty = True
                    changed += 1
                if dirty:
                    _write_binding_store_unlocked(raw, profiles)
        except Exception as e:
            LOG.warning("V11.6 舊綁定程序身分補全失敗：%s", e)
    return changed


_AUTO_REBIND_LAST_SCAN = 0.0


def auto_rebind_profiles_to_live_windows(hwnds: List[int], claimed_hwnds: set[int]) -> int:
    """Re-associate a naturally restarted Flash window by exact process identity.

    Never guesses between multiple candidates. This is what makes a one-time binding
    survive HWND/PID changes without reopening or changing the user's shortcut/window.
    The relatively expensive process-command-line scan is batched and throttled.
    """
    global _AUTO_REBIND_LAST_SCAN
    now = time.monotonic()
    if now - float(_AUTO_REBIND_LAST_SCAN) < 5.0:
        return 0
    _AUTO_REBIND_LAST_SCAN = now
    rebound = 0
    with BINDINGS_FILE_LOCK:
        try:
            with binding_file_lock():
                raw, profiles = _read_binding_store_unlocked()
                dirty = False
                live_bound_profile_keys = set()
                for old_hwnd, item in raw.items():
                    if _is_live_game_window(int(old_hwnd)):
                        pk = str(item.get("profile_key", "") or _profile_key(item.get("shortcut_path", "")))
                        if pk:
                            live_bound_profile_keys.add(pk)
                candidate_hwnds = [
                    int(h) for h in hwnds
                    if int(h) not in claimed_hwnds and int(h) not in raw and _is_live_game_window(int(h))
                ]
                identities = dpi_policy.window_process_identities(candidate_hwnds) if candidate_hwnds else {}
                for hwnd in candidate_hwnds:
                    ident = dict(identities.get(int(hwnd), {}) or {})
                    ident_key = str(ident.get("process_identity", "") or "")
                    if not ident_key:
                        continue
                    matches = []
                    for pk, prof in profiles.items():
                        if not isinstance(prof, dict):
                            continue
                        if pk in live_bound_profile_keys:
                            continue
                        if str(prof.get("process_identity", "") or "") == ident_key:
                            matches.append((str(pk), dict(prof)))
                    if len(matches) != 1:
                        continue
                    pk, prof = matches[0]
                    # Remove only stale HWND records for the same exact profile.
                    for old_hwnd, old_item in list(raw.items()):
                        old_pk = str(old_item.get("profile_key", "") or _profile_key(old_item.get("shortcut_path", "")))
                        if old_pk == pk and not _is_live_game_window(int(old_hwnd)):
                            raw.pop(int(old_hwnd), None)
                    item = dict(prof)
                    item.update({
                        "pid": get_window_pid(hwnd),
                        "last_hwnd": hwnd,
                        "bound_at": time.time(),
                        "process_exe": str(ident.get("process_exe", "") or item.get("process_exe", "")),
                        "process_identity": ident_key,
                        "profile_key": pk,
                    })
                    raw[hwnd] = item
                    prof.update({k:v for k,v in item.items() if k != "pid"})
                    prof["updated_at"] = time.time()
                    profiles[pk] = prof
                    live_bound_profile_keys.add(pk)
                    dirty = True
                    rebound += 1
                    LOG.info("[%s] V11.6 自動接回自然重啟視窗：HWND=%s；不需重新綁定。", str(item.get("shortcut_name", "") or "已綁定"), hwnd)

                # 程序命令列在不同 Windows/捷徑環境可能產生不同身分雜湊。
                # 若現場「只剩一個未綁定 Flash」且「只剩一份完整未使用設定」，
                # 候選是唯一的，允許自動接回；多一個候選就完全不猜。
                remaining_hwnds = [h for h in candidate_hwnds if int(h) not in raw]
                available_profiles = []
                for pk, prof in profiles.items():
                    if not isinstance(prof, dict) or str(pk) in live_bound_profile_keys:
                        continue
                    if not str(prof.get("shortcut_path", "") or "").strip():
                        continue
                    if not str(prof.get("preferred_role", "") or "").strip():
                        continue
                    available_profiles.append((str(pk), dict(prof)))
                if (
                    bool(CONFIG.get("唯一候選自動綁定", True))
                    and len(remaining_hwnds) == 1
                    and len(available_profiles) == 1
                ):
                    hwnd = int(remaining_hwnds[0])
                    pk, prof = available_profiles[0]
                    ident = dict(identities.get(hwnd, {}) or {})
                    live_exe = str(ident.get("process_exe", "") or "").strip()
                    saved_exe = str(prof.get("process_exe", "") or "").strip()
                    same_host = True
                    if live_exe and saved_exe:
                        same_host = Path(live_exe).name.casefold() == Path(saved_exe).name.casefold()
                    if same_host:
                        for old_hwnd, old_item in list(raw.items()):
                            old_pk = str(old_item.get("profile_key", "") or _profile_key(old_item.get("shortcut_path", "")))
                            if old_pk == pk and not _is_live_game_window(int(old_hwnd)):
                                raw.pop(int(old_hwnd), None)
                        item = dict(prof)
                        item.update({
                            "pid": get_window_pid(hwnd),
                            "last_hwnd": hwnd,
                            "bound_at": time.time(),
                            "process_exe": live_exe or saved_exe,
                            "process_identity": str(ident.get("process_identity", "") or item.get("process_identity", "")),
                            "profile_key": pk,
                        })
                        raw[hwnd] = item
                        prof.update({k: v for k, v in item.items() if k != "pid"})
                        prof["updated_at"] = time.time()
                        profiles[pk] = prof
                        live_bound_profile_keys.add(pk)
                        dirty = True
                        rebound += 1
                        LOG.info(
                            "[%s] 唯一視窗＋唯一完整設定，自動接回 HWND=%s；沒有多候選猜測。",
                            str(item.get("shortcut_name", "") or "已綁定"), hwnd,
                        )
                if dirty:
                    _write_binding_store_unlocked(raw, profiles)
        except Exception as e:
            LOG.warning("V11.6 自動接回綁定失敗：%s", e)
    return rebound

def shortcut_display_name(path_text: str, fallback: str = "") -> str:
    path_text = str(path_text or "").strip()
    if path_text:
        try:
            return Path(path_text).stem or fallback
        except Exception:
            pass
    return fallback


# -----------------------------
# OCR（按需啟動）
# -----------------------------

@dataclass
class OCRItem:
    text: str
    score: float
    box: np.ndarray  # shape (4, 2)

    @property
    def center(self) -> Tuple[int, int]:
        x = int(np.mean(self.box[:, 0]))
        y = int(np.mean(self.box[:, 1]))
        return x, y


class OCRReader:
    """有限並行 OCR。

    V9.1：重連流程不再共用單一 OCR busy gate。每個 engine 都是獨立
    RapidOCR 實例；預設 2 路，讓兩個同時重連的視窗可各自辨識。
    平常監看仍是低優先：沒有空閒 engine 或已有流程等候者就直接略過。
    """
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.engines = []
        self._gate = threading.Condition()
        self._available = []
        self._flow_waiters = 0
        self._stats = {}
        if not enabled:
            LOG.info("OCR（文字辨識）已由設定關閉。")
            return

        factory = None
        engine_name = ""
        err = None
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            factory = RapidOCR
            engine_name = "RapidOCR（快速文字辨識）/ONNXRuntime"
        except Exception as e:
            err = e

        if factory is None:
            try:
                from rapidocr import RapidOCR  # type: ignore
                factory = RapidOCR
                engine_name = "RapidOCR（快速文字辨識）"
            except Exception as e:
                err = e

        if factory is None:
            self.enabled = False
            if FROZEN:
                LOG.warning("OCR（文字辨識）無法載入；封裝可能不完整、已損壞或有檔案遭防毒隔離。")
            else:
                LOG.warning("OCR（文字辨識）無法載入；請依專案鎖定檔安裝開發依賴。")
            if err:
                LOG.debug("OCR 載入錯誤：%r", err)
            return

        # 只完成模組載入，不在「開始監測」瞬間建立 ONNX 模型。
        # RapidOCR 建構會建立多個推論 Session，舊版因此在 10 個遊戲都還正常時也會出現 CPU 尖峰。
        # 第一次真正需要 OCR（選線 / 選角 / 斷線文字備援）時才初始化；仍維持兩路流程 OCR。
        self._factory = factory
        self._engine_name = engine_name
        self._wanted = max(1, min(4, int(CONFIG.get("流程OCR並行數", 2))))
        self._init_lock = threading.Lock()
        LOG.info("OCR（文字辨識）已載入，模型延後到第一次需要辨識時初始化；流程並行=%d。", self._wanted)

    def _ensure_engines(self) -> bool:
        if not self.enabled:
            return False
        if self.engines:
            return True
        with self._init_lock:
            if self.engines:
                return True
            factory = getattr(self, "_factory", None)
            if factory is None:
                self.enabled = False
                return False
            wanted = max(1, int(getattr(self, "_wanted", 2)))
            for index in range(wanted):
                try:
                    # rapidocr_onnxruntime >= 1.3.14 可限制 ONNX 執行緒。
                    # 每個 OCR engine 固定 1+1 執行緒，避免兩路 OCR 各自吃滿所有 CPU 核心。
                    try:
                        engine = factory(intra_op_num_threads=1, inter_op_num_threads=1)
                    except TypeError:
                        engine = factory()
                    self.engines.append(engine)
                except Exception as e:
                    LOG.warning("OCR 第 %d 路初始化失敗：%s", index + 1, e)
                    break
            if not self.engines:
                self.enabled = False
                LOG.warning("OCR（文字辨識）初始化失敗；仍可使用已提供的畫面模板。")
                return False
            self._available = list(range(len(self.engines)))
            LOG.info("OCR（文字辨識）模型已初始化：%s；流程並行=%d；每路 ONNX 執行緒限制=1。", self._engine_name, len(self.engines))
            return True

    def reset_thread_stats(self):
        tid = threading.get_ident()
        with self._gate:
            self._stats[tid] = [0.0, 0.0, 0]

    def thread_stats(self) -> Tuple[float, float, int]:
        tid = threading.get_ident()
        with self._gate:
            row = self._stats.get(tid, [0.0, 0.0, 0])
            return float(row[0]), float(row[1]), int(row[2])

    def _record_stats(self, wait_s: float, run_s: float):
        tid = threading.get_ident()
        with self._gate:
            row = self._stats.setdefault(tid, [0.0, 0.0, 0])
            row[0] += max(0.0, float(wait_s))
            row[1] += max(0.0, float(run_s))
            row[2] += 1

    def _acquire_engine(self, low_priority: bool) -> Tuple[Optional[int], float]:
        started = time.monotonic()
        with self._gate:
            if low_priority:
                if self._flow_waiters > 0 or not self._available:
                    return None, 0.0
                return self._available.pop(), 0.0
            self._flow_waiters += 1
            try:
                while not self._available:
                    self._gate.wait(timeout=0.10)
                idx = self._available.pop()
            finally:
                self._flow_waiters = max(0, self._flow_waiters - 1)
            return idx, max(0.0, time.monotonic() - started)

    def _release_engine(self, index: int):
        with self._gate:
            if index not in self._available:
                self._available.append(index)
            self._gate.notify_all()

    def read(self, image_bgr: np.ndarray, offset: Tuple[int, int] = (0, 0), priority: str = "flow") -> List[OCRItem]:
        if not self.enabled or image_bgr.size == 0:
            return []
        if not self._ensure_engines():
            return []
        ox, oy = offset
        is_low = str(priority).lower() in ("low", "monitor", "background")
        index, wait_s = self._acquire_engine(is_low)
        if index is None:
            return []
        run_started = time.monotonic()
        try:
            try:
                raw = self.engines[index](image_bgr)
            except Exception as e:
                LOG.warning("OCR（文字辨識）執行失敗：%s", e)
                return []
        finally:
            run_s = max(0.0, time.monotonic() - run_started)
            self._release_engine(index)
            if not is_low:
                self._record_stats(wait_s, run_s)

        # rapidocr_onnxruntime 常見回傳：(result, elapsed)
        result = raw[0] if isinstance(raw, tuple) else raw
        if result is None:
            return []

        items: List[OCRItem] = []

        # 新版物件格式容錯。
        if hasattr(result, "boxes") and hasattr(result, "txts"):
            boxes = getattr(result, "boxes")
            txts = getattr(result, "txts")
            scores = getattr(result, "scores", [1.0] * len(txts))
            iterable = zip(boxes, txts, scores)
        else:
            iterable = result

        try:
            for entry in iterable:
                if entry is None or len(entry) < 3:
                    continue
                box, text, score = entry[0], entry[1], entry[2]
                text = str(text).strip()
                if not text:
                    continue
                try:
                    score_f = float(score)
                except Exception:
                    score_f = 0.0
                arr = np.asarray(box, dtype=np.float32).reshape(-1, 2)
                if arr.shape[0] < 4:
                    continue
                arr[:, 0] += ox
                arr[:, 1] += oy
                items.append(OCRItem(text=text, score=score_f, box=arr[:4]))
        except Exception:
            return []
        return items


OCR: Optional[OCRReader] = None  # V5.4：延後到 main() 初始化，避免啟動前無聲退出。


def normalize_game_text(text: str) -> str:
    """Normalize OCR variants used by the Traditional-Chinese Flash client."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.translate(str.maketrans({
        "級": "级", "釣": "钓", "魚": "鱼", "發": "发", "送": "送",
        "臺": "台", "裏": "里", "裡": "里",
    }))
    return re.sub(r"[\s\[\]【】()（）<>《》:：,，.。…·_\-]+", "", value).casefold()


def normalize_fishing_link_text(text: str) -> str:
    """Normalize common OCR glyph substitutions in two-digit fishing labels."""
    return normalize_game_text(text).translate(str.maketrans({
        "i": "1", "l": "1", "|": "1", "!": "1", "丨": "1",
        "o": "0", "〇": "0",
    }))


def _ocr_box_bounds(item: OCRItem) -> Tuple[int, int, int, int]:
    xs = item.box[:, 0]
    ys = item.box[:, 1]
    return int(np.min(xs)), int(np.min(ys)), int(np.max(xs)), int(np.max(ys))


def _ocr_frame_region(
    frame: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    priority: str,
) -> List[OCRItem]:
    """OCR a region at a size derived from the current frame, then map boxes back."""
    if OCR is None or not OCR.enabled:
        return []
    h, w = frame.shape[:2]
    x0, x1 = max(0, int(x0)), min(w, int(x1))
    y0, y1 = max(0, int(y0)), min(h, int(y1))
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return []
    scale = max(0.70, min(2.40, 900.0 / max(1.0, float(w))))
    if abs(scale - 1.0) <= 0.04:
        return OCR.read(roi, offset=(x0, y0), priority=priority)
    resized = cv2.resize(
        roi,
        (max(2, int(round(roi.shape[1] * scale))), max(2, int(round(roi.shape[0] * scale)))),
        interpolation=cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA,
    )
    items = OCR.read(resized, priority=priority)
    mapped = []
    for item in items:
        box = np.asarray(item.box, dtype=np.float32).copy()
        box[:, 0] = box[:, 0] / scale + x0
        box[:, 1] = box[:, 1] / scale + y0
        mapped.append(OCRItem(text=item.text, score=item.score, box=box))
    return mapped


def _chat_horizontal_lines(frame: np.ndarray, y_hint: Optional[int] = None) -> List[Tuple[int, int, int, int]]:
    """Find long horizontal chat-input borders in the current frame size."""
    h, w = frame.shape[:2]
    y0 = max(0, int(h * 0.62))
    gray = cv2.cvtColor(frame[y0:], cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 55, 165)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180.0,
        threshold=max(14, int(w * 0.035)),
        minLineLength=max(28, int(w * 0.18)),
        maxLineGap=max(4, int(w * 0.018)),
    )
    out: List[Tuple[int, int, int, int]] = []
    if lines is None:
        return out
    tolerance = max(10, int(h * 0.045))
    # OpenCV 4 commonly returns (N, 1, 4), while OpenCV 5 may return (N, 4).
    # Flatten only the container dimensions so both APIs yield one x1,y1,x2,y2 row.
    line_array = np.asarray(lines)
    if line_array.size == 0 or line_array.size % 4:
        return out
    for raw in line_array.reshape(-1, 4):
        x1, ly1, x2, ly2 = (int(v) for v in raw)
        ly1 += y0
        ly2 += y0
        if x2 < x1:
            x1, x2 = x2, x1
            ly1, ly2 = ly2, ly1
        if abs(ly2 - ly1) > 3 or x2 - x1 < max(28, int(w * 0.18)):
            continue
        cy = (ly1 + ly2) // 2
        if y_hint is not None and abs(cy - int(y_hint)) > tolerance:
            continue
        out.append((x1, cy, x2, cy))
    return out


def detect_chat_controls(frame: np.ndarray) -> Optional[Tuple[Tuple[int, int], Tuple[int, int], str]]:
    """Locate the live chat input and send button; never return fixed screen coordinates."""
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    send_center: Optional[Tuple[int, int]] = None
    route = ""
    if OCR is not None and OCR.enabled:
        y0 = max(0, int(h * 0.60))
        items = _ocr_frame_region(frame, 0, y0, w, h, priority="flow")
        candidates = []
        for item in items:
            txt = normalize_game_text(item.text)
            if "发送" in txt or "發送" in str(item.text):
                candidates.append(item)
        if candidates:
            chosen = max(candidates, key=lambda item: (item.center[1], item.center[0], item.score))
            send_center = chosen.center
            route = f"OCR:{chosen.text}/{chosen.score:.2f}"

    lines = _chat_horizontal_lines(frame, send_center[1] if send_center else None)
    input_line = None
    if lines:
        eligible = []
        for line in lines:
            x1, cy, x2, _ = line
            if send_center and x2 >= send_center[0] + int(w * 0.02):
                continue
            # The input line must end in the left/centre portion, not be a full
            # bottom frame border or a chat-log separator.
            if x2 > int(w * 0.70) or x1 > int(w * 0.35):
                continue
            eligible.append(line)
        if eligible:
            input_line = max(eligible, key=lambda line: (line[2] - line[0], line[1]))

    if input_line is None:
        return None
    x1, line_y, x2, _ = input_line
    if send_center is None:
        # Visual fallback derives the send point from the detected input border,
        # then verifies it stays inside the current frame. No physical pixel
        # constant or old-window geometry is used.
        send_center = (
            min(w - 2, x2 + max(16, int(w * 0.020))),
            max(1, min(h - 2, line_y)),
        )
        route = "聊天框邊界"
    input_center = (
        max(2, min(w - 2, int(round((x1 + x2) / 2.0)))),
        max(2, min(h - 2, int(send_center[1]))),
    )
    if input_center[0] >= send_center[0] - max(8, int(w * 0.01)):
        return None
    return input_center, (int(send_center[0]), int(send_center[1])), route


def detect_current_chat_tab(frame: np.ndarray) -> Tuple[str, Optional[Tuple[int, int]], str]:
    """Locate the top-row 「目前」 tab and verify whether its fill is red.

    The click point is derived from the user-supplied live chat-bar contexts at
    the current game-canvas scale.  The lower 「目前↑」 sender selector is not
    inside these templates, so it cannot be confused with the requested tab.
    """
    if frame is None or frame.size == 0 or TB is None:
        return "未知", None, "沒有有效畫面或模板庫"
    h, w = frame.shape[:2]
    roi = (0.0, 0.80, 0.62, 1.0)
    threshold = max(0.50, float(CONFIG.get("釣魚目前分頁模板最低相似度", 0.58)))
    candidates = []
    specs = (
        ("聊天目前分頁_紅色內容", 82.5 / 320.0, 16.5 / 35.0, "紅色內容"),
        ("聊天目前分頁_未選內容", 82.5 / 441.0, 16.5 / 65.0, "未選內容"),
    )
    for name, point_rx, point_ry, label in specs:
        if name not in TB.data:
            continue
        best = None
        for match in (
            TB.match(frame, name, 0.0, roi=roi, scale_spread=(0.88, 0.94, 1.00, 1.06, 1.12)),
            TB.match_absolute(frame, name, 0.0, roi=roi, scales=(0.78, 0.86, 0.94, 1.00, 1.08, 1.18, 1.30)),
        ):
            if match and (best is None or match.score > best.score):
                best = match
        if best is not None:
            px = int(round(best.x + best.w * point_rx))
            py = int(round(best.y + best.h * point_ry))
            candidates.append((float(best.score), (px, py), label))

    if not candidates:
        return "未知", None, "找不到目前分頁內容"
    score, point, label = max(candidates, key=lambda item: item[0])
    if score < threshold:
        return "未知", None, f"目前分頁內容分數不足:{score:.3f}"

    px, py = point
    scale = max(0.60, w / 896.0)
    rx = max(8, int(round(15 * scale)))
    ry = max(5, int(round(7 * scale)))
    x0, x1 = max(0, px - rx), min(w, px + rx + 1)
    y0, y1 = max(0, py - ry), min(h, py + ry + 1)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return "未知", None, "目前分頁色塊超出畫面"
    blue, green, red = cv2.split(patch)
    red_ratio = float(((red > 90) & (red > green * 1.18) & (red > blue * 1.08)).mean())
    cyan_ratio = float(((blue > 80) & (green > 75) & (blue > red * 1.10)).mean())
    evidence = f"{label}/{score:.3f}/紅={red_ratio:.2f}/藍={cyan_ratio:.2f}"
    if red_ratio >= 0.20 and red_ratio > cyan_ratio * 1.30:
        return "紅色已選", point, evidence
    if cyan_ratio >= 0.20 and cyan_ratio > red_ratio * 1.30:
        return "未選", point, evidence
    return "未知", point, evidence


def detect_sender_chat_channel(
    frame: np.ndarray,
) -> Tuple[str, Optional[Tuple[int, int]], Optional[Tuple[int, int]], str]:
    """Locate and classify the independent lower-left chat sender channel.

    Returns ``(state, selector_point, current_menu_point, evidence)`` where
    state is one of ``目前已選`` / ``其他頻道`` / ``選單已開`` / ``未知``.
    All points come from the live frame/template match or the detected input
    border.  No physical screen coordinate is stored.
    """
    if frame is None or frame.size == 0 or TB is None:
        return "未知", None, None, "沒有有效畫面或模板庫"
    h, w = frame.shape[:2]
    roi = (0.0, 0.68, 0.62, 1.0)
    threshold = max(0.50, float(CONFIG.get("釣魚發送頻道模板最低相似度", 0.58)))

    # The expanded menu is unique and must win before the two collapsed
    # contexts; it partly covers the upper chat tabs in the supplied frame.
    menu_best: Optional[Match] = None
    if "聊天發送頻道_展開選單" in TB.data:
        for match in (
            TB.match(frame, "聊天發送頻道_展開選單", 0.0, roi=roi,
                     scale_spread=(0.88, 0.94, 1.00, 1.06, 1.12)),
            TB.match_absolute(frame, "聊天發送頻道_展開選單", 0.0, roi=roi,
                              scales=(0.78, 0.86, 0.94, 1.00, 1.08, 1.18, 1.30)),
        ):
            if match and (menu_best is None or match.score > menu_best.score):
                menu_best = match
    if menu_best is not None and menu_best.score >= threshold:
        # User sample: the first item is 「目前」. Ratios keep the point valid
        # at any game-canvas scale and avoid hard-coded screen coordinates.
        current_point = (
            int(round(menu_best.x + menu_best.w * (23.0 / 57.0))),
            int(round(menu_best.y + menu_best.h * (12.0 / 126.0))),
        )
        selector_point = (
            int(round(menu_best.x + menu_best.w * (23.0 / 57.0))),
            int(round(menu_best.y + menu_best.h * (115.0 / 126.0))),
        )
        return (
            "選單已開",
            selector_point,
            current_point,
            f"展開選單/{menu_best.score:.3f}",
        )

    specs = (
        ("聊天發送頻道_目前內容", 23.0 / 441.0, 50.0 / 65.0),
        ("聊天發送頻道_世界內容", 23.0 / 437.0, 49.0 / 64.0),
    )
    contexts = []
    for name, point_rx, point_ry in specs:
        if name not in TB.data:
            continue
        best: Optional[Match] = None
        for match in (
            TB.match(frame, name, 0.0, roi=roi,
                     scale_spread=(0.88, 0.94, 1.00, 1.06, 1.12)),
            TB.match_absolute(frame, name, 0.0, roi=roi,
                              scales=(0.78, 0.86, 0.94, 1.00, 1.08, 1.18, 1.30)),
        ):
            if match and (best is None or match.score > best.score):
                best = match
        if best is not None:
            point = (
                int(round(best.x + best.w * point_rx)),
                int(round(best.y + best.h * point_ry)),
            )
            contexts.append((float(best.score), point, name))

    if contexts:
        contexts.sort(key=lambda value: value[0], reverse=True)
        score, point, name = contexts[0]
        if score >= threshold:
            # The upper red/current tab changes independently and occupies most
            # of both context templates. Never infer the lower sender state from
            # the whole-context winner. Reclassify only the tiny lower button.
            scale = max(0.60, w / 896.0)
            px, py = point
            # The full-row context can legitimately choose the other template:
            # the upper tabs occupy most pixels and move independently from the
            # tiny lower selector.  Its inferred point may therefore be 2-4 px
            # away from the real lower button even on a 900x572 canvas.  A
            # same-sized, single-position comparison turned a verified
            # ``目前↑`` into unknown and made the state machine reopen the menu
            # forever.  Search a tightly bounded live patch so the local button
            # can align itself; this still compares both known lower states and
            # never treats the full-context winner as confirmation.
            search_left = max(20, int(round(36 * scale)))
            search_right = max(22, int(round(40 * scale)))
            search_up = max(14, int(round(24 * scale)))
            search_down = max(13, int(round(22 * scale)))
            live = frame[
                max(0, py - search_up):min(h, py + search_down),
                max(0, px - search_left):min(w, px + search_right),
            ]
            local_scores = {}
            local_locations = {}
            if live.size:
                live_gray = cv2.cvtColor(live, cv2.COLOR_BGR2GRAY)
                for ref_name, state in (
                    ("聊天發送頻道_目前內容", "目前已選"),
                    ("聊天發送頻道_世界內容", "其他頻道"),
                ):
                    ref_full = TB.data.get(ref_name)
                    if ref_full is None or ref_full.size == 0:
                        continue
                    ref = ref_full[max(0, ref_full.shape[0] - 32):, :min(52, ref_full.shape[1])]
                    ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
                    target_w = max(18, int(round(ref_gray.shape[1] * scale)))
                    target_h = max(12, int(round(ref_gray.shape[0] * scale)))
                    ref_gray = cv2.resize(
                        ref_gray,
                        (target_w, target_h),
                        interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
                    )
                    if live_gray.shape[0] < target_h or live_gray.shape[1] < target_w:
                        continue
                    result = cv2.matchTemplate(live_gray, ref_gray, cv2.TM_CCOEFF_NORMED)
                    _min_score, best_score, _min_loc, best_loc = cv2.minMaxLoc(result)
                    local_scores[state] = float(best_score)
                    local_locations[state] = (int(best_loc[0]), int(best_loc[1]))
            if local_scores:
                ranked = sorted(local_scores.items(), key=lambda item: item[1], reverse=True)
                local_state, local_score = ranked[0]
                local_second = ranked[1][1] if len(ranked) > 1 else -1.0
                local_loc = local_locations.get(local_state, (-1, -1))
                evidence = (
                    f"{name}/{score:.3f}/下排位移搜尋{local_state}={local_score:.3f}"
                    f"@{local_loc[0]},{local_loc[1]}/次高={local_second:.3f}"
                )
                # 「公會↑」與「目前↑」的外框幾乎相同。實機證據顯示
                # 公會會被舊規則以 0.705 誤判為目前，因此「目前已選」
                # 必須有 0.90 以上的文字區局部證據；不足時回到未知，
                # 讓狀態機展開選單並明確點選目前。其他頻道可直接回報，
                # 因為其後續動作同樣只是安全地展開並選目前。
                current_is_strong = local_state != "目前已選" or local_score >= 0.90
                if current_is_strong and local_score >= 0.70 and (
                    local_score >= 0.90 or local_score >= local_second + 0.12
                ):
                    return local_state, point, None, evidence
            # A known live selector point is still safe to open. Unknown means
            # the state machine opens the list and explicitly selects 「目前」.
            return "未知", point, None, f"{name}/{score:.3f}/下排局部狀態不明"

    # Generic fallback: derive the selector from the left edge of the detected
    # input border, then OCR only the tiny selector cell. This supports other
    # channels without one template per channel.
    lines = _chat_horizontal_lines(frame)
    eligible = [
        line for line in lines
        if line[2] <= int(w * 0.70) and line[0] <= int(w * 0.35)
    ]
    if not eligible:
        return "未知", None, None, "找不到下排輸入框邊界"
    input_line = max(eligible, key=lambda line: (line[2] - line[0], line[1]))
    x1, line_y, _x2, _ = input_line
    selector_point = (
        max(2, int(round(x1 - max(15, w * 0.028)))),
        max(2, min(h - 2, int(line_y))),
    )
    if OCR is not None and OCR.enabled:
        pad_x = max(22, int(w * 0.035))
        pad_y = max(12, int(h * 0.025))
        items = _ocr_frame_region(
            frame,
            max(0, selector_point[0] - pad_x),
            max(0, selector_point[1] - pad_y),
            min(w, selector_point[0] + pad_x),
            min(h, selector_point[1] + pad_y),
            priority="flow",
        )
        texts = [normalize_game_text(item.text) for item in items if item.score >= 0.55]
        if any("目前" in text for text in texts):
            return "目前已選", selector_point, None, f"輸入框邊界/OCR:{'|'.join(texts)}"
        if texts:
            return "其他頻道", selector_point, None, f"輸入框邊界/OCR:{'|'.join(texts)}"
    return "未知", selector_point, None, "輸入框邊界/頻道文字未知"


@dataclass
class MapFingerprint:
    """Map-change fingerprint; it identifies change, never a specific map."""

    header_edges: np.ndarray
    scene_gray: np.ndarray
    header_density: float


def build_map_fingerprint(frame: np.ndarray) -> Optional[MapFingerprint]:
    """Build a scale-independent fingerprint from the map label and scene.

    The rightmost coordinate digits are deliberately excluded. Walking changes
    coordinates and the central scene, but it must not be mistaken for a map
    transition unless the map-name bar changes as well.
    """
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    hx0, hx1 = int(w * 0.795), int(w * 0.958)
    hy0, hy1 = 0, max(8, int(h * 0.072))
    header = frame[hy0:hy1, hx0:hx1]
    sx0, sx1 = int(w * 0.10), int(w * 0.73)
    sy0, sy1 = int(h * 0.09), int(h * 0.63)
    scene = frame[sy0:sy1, sx0:sx1]
    if header.size == 0 or scene.size == 0:
        return None
    header_gray = cv2.cvtColor(header, cv2.COLOR_BGR2GRAY)
    header_gray = cv2.resize(header_gray, (160, 36), interpolation=cv2.INTER_AREA)
    header_edges = cv2.Canny(header_gray, 42, 126)
    header_edges = cv2.dilate(header_edges, np.ones((2, 2), np.uint8), iterations=1)
    scene_gray = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY)
    scene_gray = cv2.resize(scene_gray, (64, 40), interpolation=cv2.INTER_AREA)
    scene_gray = cv2.GaussianBlur(scene_gray, (3, 3), 0)
    return MapFingerprint(
        header_edges=header_edges,
        scene_gray=scene_gray,
        header_density=float((header_edges > 0).mean()),
    )


def map_fingerprint_difference(
    before: Optional[MapFingerprint],
    after: Optional[MapFingerprint],
) -> Tuple[float, float]:
    if before is None or after is None:
        return 0.0, 0.0
    header_diff = float(((before.header_edges > 0) != (after.header_edges > 0)).mean())
    scene_diff = float(cv2.absdiff(before.scene_gray, after.scene_gray).mean() / 255.0)
    return header_diff, scene_diff


def map_transition_changed(
    before: Optional[MapFingerprint],
    after: Optional[MapFingerprint],
) -> Tuple[bool, str]:
    """Require both map-label and scene change; coordinate movement alone fails."""
    header_diff, scene_diff = map_fingerprint_difference(before, after)
    header_need = max(0.035, float(CONFIG.get("釣魚轉圖地圖列變化比例", 0.070)))
    scene_need = max(0.055, float(CONFIG.get("釣魚轉圖場景變化比例", 0.115)))
    visible = bool(after is not None and after.header_density >= 0.012)
    changed = bool(visible and header_diff >= header_need and scene_diff >= scene_need)
    return changed, f"地圖列差={header_diff:.3f}/{header_need:.3f} 場景差={scene_diff:.3f}/{scene_need:.3f} 可見={visible}"


def map_fingerprint_stable(
    previous: Optional[MapFingerprint],
    current: Optional[MapFingerprint],
) -> Tuple[bool, str]:
    """The new map label must be visible and stable across consecutive frames."""
    header_diff, scene_diff = map_fingerprint_difference(previous, current)
    stable_need = max(0.010, float(CONFIG.get("釣魚轉圖穩定地圖列差", 0.038)))
    visible = bool(current is not None and current.header_density >= 0.012)
    stable = bool(visible and header_diff <= stable_need)
    return stable, f"穩定地圖列差={header_diff:.3f}/{stable_need:.3f} 場景差={scene_diff:.3f} 可見={visible}"


def _visual_fishing_link_points(frame: np.ndarray, expected_count: int) -> List[Tuple[int, int]]:
    """Fallback for underlined Flash chat links when OCR splits/omits glyphs."""
    h, w = frame.shape[:2]
    # The actual message history ends above the bottom chat tabs/input toolbar.
    # The four small cyan toolbar icons at x≈365..434 live around y≈519 on a
    # 900x572 frame and exactly mimic four regularly spaced link underlines.
    # Exclude that control strip before contour extraction.
    y0, y1 = int(h * 0.48), int(h * 0.885)
    x1 = int(w * 0.72)
    roi = frame[y0:y1, 0:x1]
    if roi.size == 0:
        return []
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, np.array([76, 75, 90]), np.array([112, 255, 255]))
    kernel_w = max(4, int(round(w * 0.006)))
    horizontal = cv2.morphologyEx(cyan, cv2.MORPH_OPEN, np.ones((1, kernel_w), np.uint8))
    contours, _hier = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    segments = []
    for contour in contours:
        x, y, ww, hh = cv2.boundingRect(contour)
        if ww < max(7, int(w * 0.009)) or ww > int(w * 0.09):
            continue
        if hh > max(5, int(h * 0.014)):
            continue
        segments.append((int(x), int(y + y0), int(ww), int(hh)))
    if not segments:
        return []
    row_tol = max(3, int(h * 0.010))
    rows: List[List[Tuple[int, int, int, int]]] = []
    for seg in sorted(segments, key=lambda s: (s[1], s[0])):
        placed = False
        cy = seg[1] + seg[3] // 2
        for row in rows:
            row_y = int(np.mean([v[1] + v[3] // 2 for v in row]))
            if abs(cy - row_y) <= row_tol:
                row.append(seg)
                placed = True
                break
        if not placed:
            rows.append([seg])
    candidates = []
    for row in rows:
        row = sorted(row, key=lambda s: s[0])
        # Merge fragments separated by only one or two pixels, but preserve the
        # visible gap between separate hyperlinks.
        merged: List[List[int]] = []
        for x, y, ww, hh in row:
            if merged and x <= merged[-1][0] + merged[-1][2] + max(2, int(w * 0.003)):
                right = max(merged[-1][0] + merged[-1][2], x + ww)
                merged[-1][1] = min(merged[-1][1], y)
                merged[-1][2] = right - merged[-1][0]
                merged[-1][3] = max(merged[-1][3], hh)
            else:
                merged.append([x, y, ww, hh])
        if len(merged) >= expected_count:
            # Evaluate every consecutive group and require approximately regular
            # spacing. This rejects chat tabs and unrelated cyan UI fragments;
            # a failed match is safer than clicking a guessed coordinate.
            for start in range(0, len(merged) - expected_count + 1):
                use = merged[start:start + expected_count]
                centers = [v[0] + v[2] // 2 for v in use]
                gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
                if gaps:
                    # A configured two-digit Flash hyperlink occupies about
                    # 22 px on a 900 px canvas.  The bottom chat tabs can also
                    # produce exactly N cyan underlines, but their centres are
                    # roughly 100+ px apart.  Keep this as a hard visual
                    # boundary so exact-count underline evidence cannot turn
                    # those tabs into fishing targets.
                    scale = max(0.50, float(w) / 900.0)
                    if min(gaps) < int(round(14.0 * scale)):
                        continue
                    if max(gaps) > int(round(30.0 * scale)):
                        continue
                    if max(gaps) / max(1.0, float(min(gaps))) > 1.45:
                        continue
                widths = [v[2] for v in use]
                if max(widths) / max(1.0, float(min(widths))) > 2.0:
                    continue
                underline_ys = [v[1] for v in use]
                if max(underline_ys) - min(underline_ys) > max(2, int(round(h * 0.004))):
                    continue
                row_y = max(v[1] for v in use)
                points = [(v[0] + v[2] // 2, max(y0, v[1] - max(5, int(h * 0.012)))) for v in use]
                candidates.append((row_y, points))
    return max(candidates, key=lambda value: value[0])[1] if candidates else []


def _visual_yellow_fishing_code_points(frame: np.ndarray, expected_count: int) -> List[Tuple[int, int]]:
    """Locate the live yellow ``[51][52]...`` glyphs by their bracket geometry.

    The current Flash skin renders fishing links as hue≈24 yellow text, not
    cyan underlines.  Each token has two full-height square-bracket strokes;
    on the logical 900 px canvas they are about 16 px apart and consecutive
    left brackets are about 20 px apart.  Requiring every measured bracket
    pair prevents ordinary yellow labels or toolbar icons becoming targets.
    """
    if frame is None or frame.size == 0 or expected_count < 2:
        return []
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.48), int(h * 0.885)
    x1 = int(w * 0.72)
    roi = frame[y0:y1, :x1]
    if roi.size == 0:
        return []
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # Different Flash windows render the same yellow link with noticeably
    # different saturation after PrintWindow/DPI normalization.  The former
    # H=21..32/S>=150/V>=170 range happened to fit 120福, but discarded the
    # anti-aliased bracket strokes in five other live windows.  Keep hue bounded
    # to yellow and let the bracket geometry below provide the hard evidence.
    yellow = cv2.inRange(hsv, np.array([14, 75, 115]), np.array([40, 255, 255]))
    active_rows = np.where(np.count_nonzero(yellow, axis=1) >= expected_count * 2)[0]
    if active_rows.size == 0:
        return []
    row_bands: List[Tuple[int, int]] = []
    start = previous = int(active_rows[0])
    for value in (int(v) for v in active_rows[1:]):
        if value > previous + 1:
            row_bands.append((start, previous + 1))
            start = value
        previous = value
    row_bands.append((start, previous + 1))

    scale = max(0.50, float(w) / 900.0)
    pair_min, pair_max = int(round(14 * scale)), int(round(18 * scale))
    step_min, step_max = int(round(18 * scale)), int(round(24 * scale))
    candidates: List[Tuple[int, List[Tuple[int, int]]]] = []
    for band0, band1 in row_bands:
        band_h = band1 - band0
        if band_h < max(6, int(round(8 * scale))) or band_h > max(18, int(round(17 * scale))):
            continue
        counts = np.count_nonzero(yellow[band0:band1], axis=0)
        # Anti-aliased square brackets are not solid on every scanline.  Requiring
        # almost the complete text height made otherwise identical windows fail.
        # A measured vertical stroke on at least 45% of the row is sufficient;
        # exact pair/step geometry and a right-edge tail check remain mandatory.
        strong = np.where(counts >= max(4, int(round(band_h * 0.45))))[0]
        if strong.size < expected_count * 2:
            continue
        columns: List[int] = []
        group = [int(strong[0])]
        for value in (int(v) for v in strong[1:]):
            if value <= group[-1] + 1:
                group.append(value)
            else:
                columns.append(int(round(float(np.mean(group)))))
                group = [value]
        columns.append(int(round(float(np.mean(group)))))

        for left0 in columns:
            lefts = [left0]
            rights: List[int] = []
            valid = True
            for index in range(expected_count):
                left = lefts[index]
                right_options = [v for v in columns if pair_min <= v - left <= pair_max]
                if not right_options:
                    valid = False
                    break
                rights.append(min(right_options, key=lambda v: abs((v - left) - 16 * scale)))
                if index + 1 < expected_count:
                    next_options = [v for v in columns if step_min <= v - left <= step_max]
                    if not next_options:
                        valid = False
                        break
                    lefts.append(min(next_options, key=lambda v: abs((v - left) - 20 * scale)))
            if not valid or len(set(lefts + rights)) != expected_count * 2:
                continue
            points = [
                (int(round((left + right) / 2.0)), y0 + int(round((band0 + band1) / 2.0)))
                for left, right in zip(lefts, rights)
            ]
            candidates.append((points[0][1], points))
    # Fishing codes are the rightmost regular bracket sequence in a chat row;
    # selecting the rightmost candidate prevents yellow sender-name glyphs from
    # winning when they happen to contain similar vertical strokes.
    return max(candidates, key=lambda value: (value[0], value[1][0][0]))[1] if candidates else []


def locate_fishing_link_points(frame: np.ndarray, links: List[dict]) -> Tuple[List[Tuple[int, int]], str]:
    """Find the newly sent clickable links from left to right in the current frame."""
    expected = len(links)
    if expected <= 0 or frame is None or frame.size == 0:
        return [], "沒有有效連結"
    h, w = frame.shape[:2]
    if OCR is not None and OCR.enabled:
        x1 = int(w * 0.72)
        y0, y1 = int(h * 0.48), int(h * 0.94)
        items = _ocr_frame_region(frame, 0, y0, x1, y1, priority="flow")
        labels = [normalize_fishing_link_text(link.get("label", "")) for link in links]
        final_chars = {label[-1:] for label in labels if label}
        same_label = bool(labels and all(label == labels[0] and label for label in labels))
        numeric_codes = bool(labels and all(re.fullmatch(r"\d{2}", label) for label in labels))
        same_prefix = bool(numeric_codes and len({label[:1] for label in labels}) == 1)
        tail_candidates: List[Tuple[int, List[Tuple[int, int]], str]] = []
        ocr_anchors: List[Tuple[int, int, str, str, float]] = []
        row_tol = max(7, int(h * 0.022))
        rows: List[List[Tuple[int, int, int, int, str, float]]] = []
        for item in items:
            norm = normalize_fishing_link_text(item.text)
            bx0, by0, bx1, by1 = _ocr_box_bounds(item)
            cy = int(round((by0 + by1) / 2.0))
            if norm:
                ocr_anchors.append((cy, bx0, norm, str(item.text), float(item.score)))
            exactish = bool(
                norm and any(
                    (label and (label in norm or (len(norm) >= 2 and norm in label)))
                    or (not numeric_codes and last and last in norm)
                    for label, last in zip(labels, [x[-1:] for x in labels])
                )
            )
            first_char = labels[0][:1] if (same_label or same_prefix) else ""
            tailish = bool(
                first_char
                and len(norm) >= expected
                and norm.count(first_char) >= max(2, expected - 1)
            )
            if not norm or (not exactish and not tailish):
                continue
            if tailish:
                # When small-font OCR preserves the repeated first glyph but
                # corrupts the second glyph, the clickable tokens are still the
                # final N equal-width cells of the newest chat item. Cell width
                # is derived from current frame scale and configured label length.
                # Live 900px Flash evidence shows a bracketed two-digit link
                # occupies about 22px, not 34px. The older 8.5px-per-glyph
                # estimate pushed the first point left onto the sender name and
                # opened the 密語/私聊/資料 player menu.
                cell_w = max(14.0, (len(labels[0]) + 2.0) * 5.5 * w / 900.0)
                tail_points = []
                for idx in range(expected):
                    cx = int(round(bx1 - cell_w * (expected - idx - 0.5)))
                    tail_points.append((cx, cy))
                raw_text = str(item.text or "")
                colon_index = max(raw_text.rfind(":"), raw_text.rfind("："))
                colon_x = bx0
                if colon_index >= 0 and raw_text:
                    colon_x = int(round(bx0 + (bx1 - bx0) * (colon_index + 1) / len(raw_text)))
                sender_safe_x = colon_x + max(6, int(round(w * 0.008)))
                # A mixed Chinese-name/digit OCR box is not truly proportional.
                # If it includes the sender prefix, require inferred links to be
                # in the right-hand 55% as an additional hard boundary.
                if "目前" in raw_text or colon_index >= 0:
                    sender_safe_x = max(
                        sender_safe_x,
                        int(round(bx0 + (bx1 - bx0) * 0.45)),
                    )
                if (
                    tail_points[0][0] >= max(sender_safe_x, bx0)
                    and tail_points[-1][0] <= bx1 + int(cell_w * 0.25)
                ):
                    tail_candidates.append((cy, tail_points, f"OCR尾端等寬:{item.text}/{item.score:.2f}"))
            if not exactish:
                continue
            # OCR may merge the sender prefix plus every repeated hyperlink into
            # one box. Splitting that whole box evenly would click the sender
            # name. Instead map the actual label occurrences within OCR text to
            # their proportional positions inside the returned box.
            occurrence_spans: List[Tuple[int, int]] = []
            for label in sorted({v for v in labels if v}, key=len, reverse=True):
                start = 0
                while True:
                    pos = norm.find(label, start)
                    if pos < 0:
                        break
                    occurrence_spans.append((pos, pos + len(label)))
                    start = pos + max(1, len(label))
            if not occurrence_spans:
                for ch in final_chars:
                    if not ch:
                        continue
                    occurrence_spans.extend((idx, idx + 1) for idx, value in enumerate(norm) if value == ch)
            occurrence_spans = sorted(set(occurrence_spans))[:expected]
            if not occurrence_spans:
                occurrence_spans = [(0, max(1, len(norm)))]
            pieces = []
            text_len = max(1, len(norm))
            raw_text = str(item.text or "")
            has_sender_prefix = bool("目前" in raw_text or ":" in raw_text or "：" in raw_text)
            item_safe_x = int(round(bx0 + (bx1 - bx0) * 0.45)) if has_sender_prefix else bx0
            for start, end in occurrence_spans:
                px0 = int(round(bx0 + (bx1 - bx0) * start / text_len))
                px1 = int(round(bx0 + (bx1 - bx0) * end / text_len))
                if px1 <= px0:
                    px1 = px0 + 1
                if int(round((px0 + px1) / 2.0)) < item_safe_x:
                    continue
                pieces.append((px0, by0, px1, by1, item.text, item.score))
            for piece in pieces:
                placed = False
                pcy = int(round((piece[1] + piece[3]) / 2.0))
                for row in rows:
                    rcy = int(np.mean([(v[1] + v[3]) / 2.0 for v in row]))
                    if abs(pcy - rcy) <= row_tol:
                        row.append(piece)
                        placed = True
                        break
                if not placed:
                    rows.append([piece])
        candidates = []
        for row in rows:
            row = sorted(row, key=lambda value: value[0])
            unique = []
            for value in row:
                cx = int(round((value[0] + value[2]) / 2.0))
                if unique and abs(cx - unique[-1][0]) <= max(8, int(w * 0.010)):
                    continue
                unique.append((cx, int(round((value[1] + value[3]) / 2.0)), value))
            if len(unique) >= expected:
                use = unique[:expected]
                gaps = [use[idx + 1][0] - use[idx][0] for idx in range(len(use) - 1)]
                if gaps and (
                    min(gaps) < max(10, int(w * 0.011))
                    or max(gaps) > max(60, int(w * 0.100))
                    or max(gaps) / max(1.0, float(min(gaps))) > 1.80
                ):
                    continue
                candidates.append((max(v[1] for v in use), [(v[0], v[1]) for v in use], use))
        if candidates:
            chosen = max(candidates, key=lambda value: value[0])
            evidence = ",".join(str(v[2][4]) for v in chosen[2])
            return chosen[1], f"OCR:{evidence}"

        if tail_candidates:
            chosen_tail = max(tail_candidates, key=lambda value: value[0])
            return chosen_tail[1], chosen_tail[2]

        # OCR can lose one digit from small labels such as 11/12/13. In that
        # case use the exact-count cyan underlines, but only when OCR from the
        # same row still anchors at least one complete code or every shared
        # level prefix. This activates the existing visual detector without
        # allowing unrelated cyan tabs to become clickable evidence by itself.
        visual_points = _visual_fishing_link_points(frame, expected)
        visual_kind = "青色底線"
        if len(visual_points) != expected:
            visual_points = _visual_yellow_fishing_code_points(frame, expected)
            visual_kind = "黃色代碼括號"
        if len(visual_points) == expected and numeric_codes and ocr_anchors:
            visual_y = int(round(float(np.median([point[1] for point in visual_points]))))
            anchor_tol = max(12, int(h * 0.035))
            near = [item for item in ocr_anchors if abs(item[0] - visual_y) <= anchor_tol]
            joined = "".join(item[2] for item in sorted(near, key=lambda item: item[1]))
            exact_hits = sum(1 for label in labels if label and label in joined)
            prefix_hits = joined.count(labels[0][0]) if labels and joined else 0
            if exact_hits >= 1 or (same_prefix and prefix_hits >= expected):
                evidence = "|".join(item[3] for item in near[:4])
                return visual_points, f"OCR+{visual_kind}容錯:{evidence or joined}"

        # RapidOCR can omit the entire tiny 11/12/... or 51/52/... row even
        # though Flash renders every clickable underline cleanly.  Exact count,
        # a shared configured level prefix, and the detector's strict 14..36 px
        # regular spacing are independent pixel evidence; these are measured
        # points, not extrapolated coordinates.  The spacing rule above rejects
        # the much wider 全部/世界/目前/... chat-tab row seen in the live failure.
        if len(visual_points) == expected and expected >= 2 and numeric_codes and same_prefix:
            return visual_points, f"{visual_kind}精確數量與等距證據（OCR漏字）"

    # Visual underlines alone are not sufficient: chat tabs can form the same
    # pattern. Never click them without text evidence from the configured row.
    return [], f"未取得 {expected} 個具文字證據的連結"


def detect_fishing_status(frame: np.ndarray, success_text: str = "正在釣魚", priority: str = "flow") -> Tuple[bool, str]:
    """Read only the user-supplied central fishing-status area."""
    if frame is None or frame.size == 0 or OCR is None or not OCR.enabled:
        return False, "OCR不可用"
    h, w = frame.shape[:2]
    x0, x1 = int(w * 0.27), int(w * 0.66)
    y0, y1 = int(h * 0.38), int(h * 0.62)
    items = _ocr_frame_region(frame, x0, y0, x1, y1, priority=priority)
    wanted = normalize_game_text(success_text)
    evidence = []
    for item in items:
        text = normalize_game_text(item.text)
        if text:
            evidence.append(f"{item.text}/{item.score:.2f}")
        if wanted and wanted in text and item.score >= 0.45:
            return True, f"{item.text}/{item.score:.2f}"
        # At smaller captures RapidOCR commonly reads 釣 as 約/钓-like glyphs.
        # The supplied status area is narrow, so accept only the anchored phrase
        # shape "正在 + up to two glyphs + 魚"; do not use a generic fuzzy match.
        if item.score >= 0.58 and re.search(r"正在.{0,2}鱼", text):
            return True, f"{item.text}/{item.score:.2f}/釣魚字形容錯"
    return False, ",".join(evidence[:4]) or "指定區域無文字"


# -----------------------------
# 視窗擷取 / 點擊
# -----------------------------

@dataclass
class FrameGeometry:
    """一次背景擷取對應的幾何資訊。

    raw_* 是 PrintWindow 實際像素；logical_* 是 Flash 目標視窗自己的邏輯客戶區。
    上層辨識永遠只看 logical 畫布，完全不需要知道螢幕 DPI。
    """
    raw_w: int
    raw_h: int
    logical_w: int
    logical_h: int
    phys_origin_x: int
    phys_origin_y: int
    monitor_dpi: int
    root_dpi: int
    captured_at: float


class WindowIO:
    """視窗擷取與輸入。

    V8.0 核心原則：
    1. PrintWindow 取得的是「實際像素」；先正規化成 Flash 自己的邏輯畫布。
    2. 辨識座標永遠是 root Flash 的邏輯 client 座標。
    3. 點擊時先把該邏輯點映射回實際螢幕像素，用實體點找真正 ShockwaveFlash 子視窗。
    4. 再用 Windows 的 PhysicalToLogicalPointForPerMonitorDPI 轉成「接收視窗自己的」訊息座標。
    5. 不再用 monitorDPI/targetDPI 人工乘除整個座標。
    6. 背景訊息輸入經畫面驗證全數無效時，才允許最後的前景實體點擊；
       實體點擊使用獨立的顯示座標映射，絕不與 Flash 訊息座標混用。
    """

    def __init__(self):
        self.user32 = ctypes.windll.user32
        slots = max(1, min(4, int(CONFIG.get("背景擷取並行數", 2))))
        self.capture_lock = threading.BoundedSemaphore(slots)
        self.last_capture_warning: Dict[int, float] = {}
        self.geometry: Dict[int, FrameGeometry] = {}
        # V8.2：保留每個視窗最後一次 PrintWindow 原始客戶區畫面。
        # 辨識預設仍用邏輯畫布；若跨 DPI 縮放後斷線模板失真，才用 raw 畫面做第二路徑。
        self.last_raw_frame: Dict[int, np.ndarray] = {}
        # V8.4：PrintWindow 在「高 DPI 螢幕 + 舊 DPI-unaware Flash」上可能回傳
        # 物理 client 大小的黑底緩衝區，但 Flash 實際只在左上角以自己的邏輯像素 1:1 繪製。
        # 這種畫面不能再整張縮小，否則會把有效遊戲畫面二次縮放。
        self.surface_mode: Dict[int, str] = {}
        self.surface_mode_logged: Dict[int, str] = {}
        self.input_calibration: Dict[int, dict] = {}
        self.uncalibrated_attempt: Dict[int, int] = {}
        # V11.5：每個 Flash 的「輸入基準畫布」。只做座標正規化，絕不拿來改 Windows 視窗大小。
        self.input_base_client: Dict[int, Tuple[int, int]] = {}
        # 多個 worker 不能同時爭用全域滑鼠。
        self.foreground_input_lock = threading.Lock()

        # Windows 8.1+。若個別 API 不存在，下面仍有幾何比例備援。
        try:
            self._p2l = self.user32.PhysicalToLogicalPointForPerMonitorDPI
            self._p2l.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            self._p2l.restype = wintypes.BOOL
        except Exception:
            self._p2l = None
        try:
            self._l2p = self.user32.LogicalToPhysicalPointForPerMonitorDPI
            self._l2p.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
            self._l2p.restype = wintypes.BOOL
        except Exception:
            self._l2p = None

        # V9.0 左側專用：座標映射時暫時採用目標 Flash 自己的 DPI awareness context。
        # 不寫死 144/96，也不修改 Flash、螢幕縮放或前景狀態。
        try:
            self._get_window_dpi_context = self.user32.GetWindowDpiAwarenessContext
            self._get_window_dpi_context.argtypes = [wintypes.HWND]
            self._get_window_dpi_context.restype = ctypes.c_void_p
            self._set_thread_dpi_context = self.user32.SetThreadDpiAwarenessContext
            self._set_thread_dpi_context.argtypes = [ctypes.c_void_p]
            self._set_thread_dpi_context.restype = ctypes.c_void_p
        except Exception:
            self._get_window_dpi_context = None
            self._set_thread_dpi_context = None

    @staticmethod
    def is_window(hwnd: int) -> bool:
        return bool(win32gui.IsWindow(hwnd))

    @staticmethod
    def client_size(hwnd: int) -> Tuple[int, int]:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        return max(0, r - l), max(0, b - t)

    @staticmethod
    def client_to_screen(hwnd: int, x: int, y: int) -> Tuple[int, int]:
        return win32gui.ClientToScreen(hwnd, (int(x), int(y)))

    def _physical_to_logical(self, hwnd: int, sx: int, sy: int) -> Optional[Tuple[int, int]]:
        if self._p2l is None:
            return None
        try:
            pt = wintypes.POINT(int(sx), int(sy))
            if self._p2l(wintypes.HWND(int(hwnd)), ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        return None

    def _logical_to_physical(self, hwnd: int, sx: int, sy: int) -> Optional[Tuple[int, int]]:
        if self._l2p is None:
            return None
        try:
            pt = wintypes.POINT(int(sx), int(sy))
            if self._l2p(wintypes.HWND(int(hwnd)), ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        return None

    def _logical_size_for_raw(self, hwnd: int, ox: int, oy: int, rw: int, rh: int) -> Tuple[int, int]:
        """用目標視窗自己的 DPI 感知把實際像素尺寸轉成它的邏輯 client 尺寸。"""
        p0 = self._physical_to_logical(hwnd, ox, oy)
        p1 = self._physical_to_logical(hwnd, ox + rw, oy + rh)
        if p0 is not None and p1 is not None:
            lw, lh = abs(int(p1[0] - p0[0])), abs(int(p1[1] - p0[1]))
            if 180 <= lw <= max(4000, rw * 3) and 120 <= lh <= max(3000, rh * 3):
                return lw, lh

        # 舊系統備援：只有 API 不可用時才使用 DPI 比率，而且只用來建立「辨識畫布」，
        # 絕不直接拿來乘滑鼠訊息座標。
        mon = max(48, int(get_monitor_dpi(hwnd)))
        target = max(48, int(get_window_dpi(hwnd)))
        lw = max(1, int(round(rw * target / float(mon))))
        lh = max(1, int(round(rh * target / float(mon))))
        return lw, lh

    @staticmethod
    def _padding_is_nearly_black(region: np.ndarray) -> bool:
        """判斷 PrintWindow 緩衝區中的區塊是否只是未繪製黑底。

        門檻刻意很嚴格：只有幾乎全黑才視為 DPI 虛擬化產生的 padding，
        避免戰鬥暗場景或遊戲本身黑色區域被誤裁。
        """
        if region is None or region.size == 0:
            return True
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
        dark_ratio = float(np.mean(gray <= 8))
        return dark_ratio >= 0.985 and float(gray.mean()) <= 6.0

    def _is_logical_surface_padded(self, raw: np.ndarray, lw: int, lh: int) -> bool:
        """偵測舊 Flash 的「邏輯面直接畫在物理大小黑底左上角」模式。

        例：150% 螢幕上 client/PrintWindow 是 1350x852，但 Flash 本身只畫約
        900x568 邏輯像素，剩餘右側/下方是純黑。此時正確處理是裁切
        raw[:568, :900]，而不是把 1350x852 resize 成 900x568。
        """
        if raw is None or raw.size == 0:
            return False
        rh, rw = raw.shape[:2]
        lw, lh = int(lw), int(lh)
        if lw <= 0 or lh <= 0 or lw > rw or lh > rh:
            return False
        # 尺寸幾乎相同時沒有必要判斷 padding。
        if rw <= lw * 1.08 and rh <= lh * 1.08:
            return False
        active = raw[:lh, :lw]
        if not self._valid_capture(active):
            return False
        right = raw[:, lw:] if rw > lw else None
        bottom = raw[lh:, :] if rh > lh else None
        right_dark = self._padding_is_nearly_black(right) if right is not None and right.size else True
        bottom_dark = self._padding_is_nearly_black(bottom) if bottom is not None and bottom.size else True
        # 高 DPI 造成的黑底通常右、下兩邊都成立；要求兩者同時成立，降低誤判。
        return bool(right_dark and bottom_dark)

    def capture(self, hwnd: int) -> Optional[np.ndarray]:
        """背景擷取並建立 Flash 自己的邏輯客戶區。

        V8.4：若 PrintWindow 已經把 DPI-unaware Flash 的邏輯 surface 以 1:1
        像素畫在物理 client 左上角，直接裁切有效邏輯區；只有畫面確實填滿
        物理 client 時才做 resize。
        """
        if not self.is_window(hwnd):
            return None

        with self.capture_lock:
            raw = self._capture_printwindow_raw(hwnd)
        if not self._valid_capture(raw):
            now = time.monotonic()
            last = self.last_capture_warning.get(hwnd, 0.0)
            if now - last >= 10.0:
                state = "最小化" if win32gui.IsIconic(hwnd) else "背景"
                LOG.warning(
                    "視窗 %s 的 PrintWindow 背景擷取目前沒有有效畫面（%s）。"
                    "程式不會切前景或閃爍；Flash 若在最小化時停止繪圖，請保持視窗非最小化，可放在其他視窗後方。",
                    hwnd, state,
                )
                self.last_capture_warning[hwnd] = now
            return None

        rh, rw = raw.shape[:2]
        # 僅保存最近一幀，不做額外擷取；raw 與 logical 來自同一次 PrintWindow。
        self.last_raw_frame[int(hwnd)] = raw
        try:
            ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
        except Exception:
            ox, oy = 0, 0
        lw, lh = self._logical_size_for_raw(hwnd, int(ox), int(oy), int(rw), int(rh))

        # 避免 API 在少數舊系統回傳離譜值。
        if lw < 180 or lh < 120 or lw > rw * 3 or lh > rh * 3:
            lw, lh = rw, rh

        geom = FrameGeometry(
            raw_w=int(rw), raw_h=int(rh), logical_w=int(lw), logical_h=int(lh),
            phys_origin_x=int(ox), phys_origin_y=int(oy),
            monitor_dpi=max(48, int(get_monitor_dpi(hwnd))),
            root_dpi=max(48, int(get_window_dpi(hwnd))),
            captured_at=time.monotonic(),
        )
        self.geometry[int(hwnd)] = geom

        if not CONFIG.get("統一邏輯畫布", True):
            self.surface_mode[int(hwnd)] = "raw"
            return raw
        if abs(lw - rw) <= 1 and abs(lh - rh) <= 1:
            # V11.6：Windows 視窗保持使用者原尺寸；只有「程式內辨識畫布」正規化。
            # 模板本來以約 900px 寬為基準，因此任意 DPI-aware 視窗都先等比縮到
            # 辨識基準寬度。這不呼叫 SetWindowPos，也不改遊戲視窗。
            base_w = max(480, int(CONFIG.get("辨識基準寬度", 900) or 900))
            if abs(int(rw) - int(base_w)) > 2:
                base_h = max(180, int(round(float(rh) * float(base_w) / max(1.0, float(rw)))))
                geom.logical_w = int(base_w)
                geom.logical_h = int(base_h)
                self.surface_mode[int(hwnd)] = "native-normalized"
                prev = self.surface_mode_logged.get(int(hwnd))
                if prev != "native-normalized":
                    LOG.info(
                        "視窗 %s 保留使用者實際尺寸 %sx%s；僅在程式內建立辨識畫布 %sx%s，不改視窗大小/位置。",
                        hwnd, rw, rh, base_w, base_h,
                    )
                    self.surface_mode_logged[int(hwnd)] = "native-normalized"
                interp = cv2.INTER_AREA if base_w < rw else cv2.INTER_CUBIC
                return cv2.resize(raw, (int(base_w), int(base_h)), interpolation=interp)
            self.surface_mode[int(hwnd)] = "native"
            return raw

        # V8.4 核心修正：高 DPI + 舊 Flash 的 PrintWindow 常是「黑底物理緩衝區
        # + 左上角邏輯 surface」。這種情況直接裁切，不得再二次縮小。
        if self._is_logical_surface_padded(raw, int(lw), int(lh)):
            frame = raw[:int(lh), :int(lw)].copy()
            self.surface_mode[int(hwnd)] = "logical-padded-crop"
            prev = self.surface_mode_logged.get(int(hwnd))
            if prev != "logical-padded-crop":
                LOG.info(
                    "視窗 %s 偵測到 DPI 虛擬化黑底：PrintWindow=%sx%s，Flash邏輯=%sx%s；"
                    "改用左上邏輯面裁切，不做二次縮放。",
                    hwnd, rw, rh, lw, lh,
                )
                self.surface_mode_logged[int(hwnd)] = "logical-padded-crop"
            return frame

        self.surface_mode[int(hwnd)] = "scaled-physical"
        prev = self.surface_mode_logged.get(int(hwnd))
        if prev != "scaled-physical":
            LOG.info(
                "視窗 %s PrintWindow 內容填滿物理 client：%sx%s → 邏輯畫布 %sx%s；使用等比例正規化。",
                hwnd, rw, rh, lw, lh,
            )
            self.surface_mode_logged[int(hwnd)] = "scaled-physical"
        interp = cv2.INTER_AREA if lw < rw or lh < rh else cv2.INTER_CUBIC
        return cv2.resize(raw, (int(lw), int(lh)), interpolation=interp)

    def get_last_raw(self, hwnd: int) -> Optional[np.ndarray]:
        return self.last_raw_frame.get(int(hwnd))

    def raw_point_to_logical(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        """把同一次 PrintWindow raw 畫面的點轉回上層邏輯畫布座標。

        V8.4：logical-padded-crop 模式下 raw 左上有效區本來就是 Flash 邏輯像素，
        所以點位必須 1:1，不可再乘 logical/raw 比率。
        """
        g = self.geometry.get(int(hwnd))
        if g is None or g.raw_w <= 0 or g.raw_h <= 0:
            return int(x), int(y)
        if self.surface_mode.get(int(hwnd)) == "logical-padded-crop":
            return (
                max(0, min(int(g.logical_w - 1), int(x))),
                max(0, min(int(g.logical_h - 1), int(y))),
            )
        lx = int(round(float(x) * g.logical_w / float(g.raw_w)))
        ly = int(round(float(y) * g.logical_h / float(g.raw_h)))
        return lx, ly

    @staticmethod
    def _valid_capture(img: Optional[np.ndarray]) -> bool:
        if img is None or img.size == 0:
            return False
        if img.shape[0] < 100 or img.shape[1] < 100:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return float(gray.std()) > 8.0 and 5.0 < float(gray.mean()) < 250.0

    def _capture_printwindow_raw(self, hwnd: int) -> Optional[np.ndarray]:
        hwnd_dc = None
        src_dc = None
        mem_dc = None
        bmp = None
        try:
            wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
            ww, wh = wr - wl, wb - wt
            if ww <= 0 or wh <= 0:
                return None

            hwnd_dc = win32gui.GetWindowDC(hwnd)
            src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            mem_dc = src_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(src_dc, ww, wh)
            mem_dc.SelectObject(bmp)

            ok = self.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
            if not ok:
                ok = self.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), 0)
            if not ok:
                return None

            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            arr = np.frombuffer(bits, dtype=np.uint8).reshape((info["bmHeight"], info["bmWidth"], 4))
            img = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)

            # 保留 7.2 已驗證穩定的 PrintWindow 裁切方式；V8.0 只在裁切後建立邏輯畫布。
            cx0, cy0 = win32gui.ClientToScreen(hwnd, (0, 0))
            cw, ch = self.client_size(hwnd)
            x0 = max(0, int(cx0 - wl))
            y0 = max(0, int(cy0 - wt))
            x1 = min(img.shape[1], x0 + int(cw))
            y1 = min(img.shape[0], y0 + int(ch))
            if x1 <= x0 or y1 <= y0:
                return None
            return img[y0:y1, x0:x1].copy()
        except Exception:
            return None
        finally:
            try:
                if bmp is not None:
                    win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
            try:
                if mem_dc is not None:
                    mem_dc.DeleteDC()
            except Exception:
                pass
            try:
                if src_dc is not None:
                    src_dc.DeleteDC()
            except Exception:
                pass
            try:
                if hwnd_dc is not None:
                    win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass

    def _ensure_geometry(self, hwnd: int) -> Optional[FrameGeometry]:
        g = self.geometry.get(int(hwnd))
        # 點擊永遠緊接在擷取/辨識之後；超過 2 秒就重新抓一幀，避免視窗剛搬螢幕/縮放後沿用舊幾何。
        if g is None or time.monotonic() - g.captured_at > 2.0:
            _ = self.capture(hwnd)
            g = self.geometry.get(int(hwnd))
        return g

    def _logical_root_to_physical(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        g = self._ensure_geometry(hwnd)
        if g is None:
            sx, sy = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
            return int(sx), int(sy)
        # V8.7：logical-padded-crop 代表 PrintWindow 的左上有效區本身就是
        # Flash 的 1:1 邏輯像素。此模式若再乘 raw/logical（例如 1.5），
        # 會把 hit-test 與輸入定位推到錯誤位置。只有 scaled-physical 才反向縮放。
        if self.surface_mode.get(int(hwnd)) == "logical-padded-crop":
            return int(g.phys_origin_x + int(x)), int(g.phys_origin_y + int(y))
        px = g.phys_origin_x + int(round(float(x) * g.raw_w / max(1.0, float(g.logical_w))))
        py = g.phys_origin_y + int(round(float(y) * g.raw_h / max(1.0, float(g.logical_h))))
        return int(px), int(py)

    @staticmethod
    def _logical_to_visible_offset(g: FrameGeometry, x: int, y: int) -> Tuple[int, int]:
        """把辨識畫布座標映射成 DWM 顯示的實體 client 偏移。

        這個映射只能給 SetCursorPos/SendInput 使用。logical-padded-crop 時，
        PrintWindow 的有效影像雖然在左上角以 1:1 邏輯像素繪製，Windows 桌面合成仍會
        把 900x590 的 Flash 放大到實體 client（例如 2521x1653）。因此真實滑鼠
        必須用 raw/logical 比率，而背景訊息 hit-test 仍維持原本的 1:1 路徑。
        """
        if g.logical_w <= 0 or g.logical_h <= 0 or g.raw_w <= 0 or g.raw_h <= 0:
            return int(x), int(y)
        px = int(round(float(x) * float(g.raw_w) / float(g.logical_w)))
        py = int(round(float(y) * float(g.raw_h) / float(g.logical_h)))
        return px, py

    def logical_root_to_visible_physical(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        """只供真實滑鼠備援使用的螢幕實體座標。"""
        g = self._ensure_geometry(hwnd)
        if g is None:
            sx, sy = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
            return int(sx), int(sy)
        ox, oy = self._logical_to_visible_offset(g, int(x), int(y))
        return int(g.phys_origin_x + ox), int(g.phys_origin_y + oy)

    @staticmethod
    def _target_under_physical(hwnd: int, sx: int, sy: int) -> Tuple[int, str]:
        """找真正接收輸入的 ShockwaveFlash 子視窗。

        V8.5：跨 DPI 的舊 Flash 可能由 Windows 在合成階段縮放；此時用螢幕實體點
        判斷 child hit-test 可能失真。先收集所有 ShockwaveFlash 子視窗；若實體點
        能命中就使用命中的，否則退回最大的 ShockwaveFlash，而不是退回頂層視窗。
        """
        hit_candidates = []
        flash_candidates = []

        def cb(child, _):
            try:
                if not win32gui.IsWindowVisible(child) or not win32gui.IsWindowEnabled(child):
                    return
                l, t, r, b = win32gui.GetWindowRect(child)
                cls = win32gui.GetClassName(child) or ""
                area = max(1, (r - l) * (b - t))
                is_flash = "shockwaveflash" in cls.lower()
                if is_flash:
                    flash_candidates.append((area, int(child), cls))
                if l <= sx < r and t <= sy < b:
                    flash_rank = 0 if is_flash else 1
                    hit_candidates.append((flash_rank, area, int(child), cls))
            except Exception:
                pass

        try:
            win32gui.EnumChildWindows(hwnd, cb, None)
        except Exception:
            pass
        if hit_candidates:
            _rank, _area, child, cls = min(hit_candidates, key=lambda z: (z[0], z[1]))
            return child, cls
        if flash_candidates:
            _area, child, cls = max(flash_candidates, key=lambda z: z[0])
            return child, cls
        return int(hwnd), win32gui.GetClassName(hwnd) or ""

    def set_input_base_client(self, hwnd: int, size) -> None:
        try:
            if size and len(size) == 2:
                self.input_base_client[int(hwnd)] = (max(1, int(size[0])), max(1, int(size[1])))
            else:
                self.input_base_client.pop(int(hwnd), None)
        except Exception:
            self.input_base_client.pop(int(hwnd), None)

    def _base_input_point(self, hwnd: int, x: int, y: int) -> Optional[Tuple[int, int]]:
        """把目前任意 client/辨識畫布點正規化回已驗證的 Flash 輸入基準。

        這裡只轉「訊息座標」，不改視窗尺寸。當目前畫布與基準相同時為 1:1。
        """
        base = self.input_base_client.get(int(hwnd))
        g = self.geometry.get(int(hwnd))
        if not base or g is None or g.logical_w <= 0 or g.logical_h <= 0:
            return None
        bw, bh = base
        return (
            max(0, int(round(float(x) * float(bw) / float(g.logical_w)))),
            max(0, int(round(float(y) * float(bh) / float(g.logical_h)))),
        )

    def _message_point_candidates(self, hwnd: int, x: int, y: int, root: bool = False) -> Tuple[int, str, Tuple[int, int], List[Tuple[str, int, int]]]:
        """從邏輯畫布點產生目標 Flash 訊息座標候選。

        候選不是「猜 DPI 倍率」，而是 Windows 對同一個實體點的不同合法座標表示。
        斷線確認會逐一用下一頁畫面驗證，成功後只保存 mode 名稱。
        """
        sx, sy = self._logical_root_to_physical(hwnd, int(x), int(y))
        if root:
            target, cls = int(hwnd), win32gui.GetClassName(hwnd) or ""
        else:
            target, cls = self._target_under_physical(hwnd, sx, sy)
        try:
            tox, toy = win32gui.ClientToScreen(target, (0, 0))
        except Exception:
            tox, toy = sx, sy

        vals: List[Tuple[str, int, int]] = []

        # V11.5：DPI 已統一後，視窗可以維持使用者任意實際尺寸。
        # 辨識仍在目前畫布進行；送給舊 Flash 的訊息座標先正規化回「原生啟動時已驗證的輸入基準」。
        # 例如目前 1358x905 的按鈕中心 681,770，若輸入基準是 900x590，會送約 451,502。
        # 900x590 在此只是座標基準，不是 Windows 視窗尺寸。
        base_pt = self._base_input_point(hwnd, int(x), int(y))
        current_surface = self.surface_mode.get(int(hwnd), "")
        # V11.6：DPI-aware/native 視窗的 Flash 內容會跟著 client 尺寸縮放；
        # 實機已證明應把辨識座標映回「目前實際 client」，不能固定送舊 900x590 基準。
        # Flash輸入基準只保留給非 native 的舊相容路徑。
        if base_pt is not None and current_surface not in ("native", "native-normalized"):
            bx, by = base_pt
            g_now = self.geometry.get(int(hwnd))
            if g_now is not None and (abs(int(g_now.logical_w)-int(self.input_base_client[int(hwnd)][0])) > 2 or abs(int(g_now.logical_h)-int(self.input_base_client[int(hwnd)][1])) > 2):
                vals.append(("Flash輸入基準", int(bx), int(by)))

        # V8.7：logical-padded-crop 的左上有效區已證實是 Flash 1:1 邏輯畫布。
        # 先直接送辨識座標，避免 Windows DPI 虛擬化再次換算。
        if self.surface_mode.get(int(hwnd)) in ("logical-padded-crop", "native", "native-normalized"):
            vals.append(("邏輯畫布直送", int(x), int(y)))

        # V8.5：舊 DPI-unaware Flash 在高 DPI 螢幕上可能出現「視窗實體尺寸」
        # 與「Flash 邏輯畫布尺寸」不同。不要猜 1.25/1.5 倍；直接用接收視窗
        # 自己的實際矩形/客戶區尺寸建立訊息座標候選。斷線確認會用畫面驗證
        # 哪一種真的有效，成功後才保存模式。
        g = self.geometry.get(int(hwnd))
        if g is not None and g.logical_w > 0 and g.logical_h > 0:
            try:
                l, t, r, b = win32gui.GetWindowRect(target)
                tw, th = max(1, int(r - l)), max(1, int(b - t))
                if abs(tw - g.logical_w) > 2 or abs(th - g.logical_h) > 2:
                    vals.append((
                        "接收視窗實際尺寸映射",
                        int(round(float(x) * tw / float(g.logical_w))),
                        int(round(float(y) * th / float(g.logical_h))),
                    ))
            except Exception:
                pass
            try:
                cl, ct, cr, cb = win32gui.GetClientRect(target)
                cw, ch = max(1, int(cr - cl)), max(1, int(cb - ct))
                if abs(cw - g.logical_w) > 2 or abs(ch - g.logical_h) > 2:
                    vals.append((
                        "接收客戶區尺寸映射",
                        int(round(float(x) * cw / float(g.logical_w))),
                        int(round(float(y) * ch / float(g.logical_h))),
                    ))
            except Exception:
                pass

        # 1) Windows 官方「針對這個目標視窗」的實體→邏輯轉換。
        pl = self._physical_to_logical(target, sx, sy)
        ol = self._physical_to_logical(target, int(tox), int(toy))
        if pl is not None and ol is not None:
            vals.append(("目標原生邏輯", int(pl[0] - ol[0]), int(pl[1] - ol[1])))

        # 2) ScreenToClient：在同一個 Per-Monitor-V2 呼叫者下，部分舊 Flash 反而以此座標最穩。
        try:
            cx, cy = win32gui.ScreenToClient(target, (int(sx), int(sy)))
            vals.append(("視窗客戶區", int(cx), int(cy)))
        except Exception:
            pass

        # 3) 純實體相對值。只作相容性候選，不是以 DPI 硬乘。
        vals.append(("實體相對", int(sx - tox), int(sy - toy)))

        # 4) MapWindowPoints：若 root/child 的 DPI 虛擬化規則不同，讓 Windows 自己轉。
        try:
            # x,y 是 root 邏輯 client 座標。MapWindowPoints 由 Windows 依視窗上下文轉換。
            pt = wintypes.POINT(int(x), int(y))
            fn = self.user32.MapWindowPoints
            fn.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
            fn.restype = ctypes.c_int
            fn(wintypes.HWND(int(hwnd)), wintypes.HWND(int(target)), ctypes.byref(pt), 1)
            vals.append(("Windows視窗映射", int(pt.x), int(pt.y)))
        except Exception:
            pass

        # 去重；同一點不重複送。
        unique: List[Tuple[str, int, int]] = []
        for mode, tx, ty in vals:
            if tx < -50 or ty < -50 or tx > 10000 or ty > 10000:
                continue
            if any(abs(tx - ux) <= 1 and abs(ty - uy) <= 1 for _um, ux, uy in unique):
                continue
            unique.append((mode, tx, ty))
        return target, cls, (sx, sy), unique or [("視窗客戶區", int(x), int(y))]

    def _send_message_timeout(self, target: int, msg: int, wparam: int, lparam: int, timeout_ms: int = 180) -> bool:
        result = ctypes.c_size_t()
        try:
            fn = self.user32.SendMessageTimeoutW
            fn.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
            ]
            fn.restype = ctypes.c_size_t
            ok = fn(
                ctypes.c_void_p(int(target)), int(msg), ctypes.c_size_t(int(wparam)), ctypes.c_ssize_t(int(lparam)),
                0x0001 | 0x0002, int(timeout_ms), ctypes.byref(result),
            )
            return bool(ok)
        except Exception:
            return False

    def _post_message(self, target: int, msg: int, wparam: int, lparam: int) -> bool:
        try:
            fn = self.user32.PostMessageW
            fn.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            fn.restype = wintypes.BOOL
            return bool(fn(wintypes.HWND(int(target)), int(msg), wintypes.WPARAM(int(wparam)), wintypes.LPARAM(int(lparam))))
        except Exception:
            try:
                return bool(win32api.PostMessage(int(target), int(msg), int(wparam), int(lparam)))
            except Exception:
                return False

    def _queued_mouse_click_attached_active(self, target: int, tx: int, ty: int, root_hwnd: int) -> bool:
        """不切前景的舊 Flash 排隊輸入路徑。

        AttachThreadInput 後以 SetActiveWindow/SetFocus 建立目標執行緒的真實 active/focus 狀態，
        再用 PostMessage 將滑鼠事件放進 Flash 自己的訊息佇列。完全不呼叫
        SetForegroundWindow / BringWindowToTop / SetCursorPos。
        """
        user32 = self.user32
        kernel32 = ctypes.windll.kernel32
        attached = []
        old_focus = 0
        old_active = 0
        try:
            cur_tid = int(kernel32.GetCurrentThreadId())
            pid = wintypes.DWORD()
            target_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(target)), ctypes.byref(pid)))
            fg = int(user32.GetForegroundWindow() or 0)
            fg_tid = 0
            if fg:
                fpid = wintypes.DWORD()
                fg_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fpid)))
            for tid in (target_tid, fg_tid):
                if tid and tid != cur_tid and tid not in attached:
                    try:
                        if user32.AttachThreadInput(cur_tid, int(tid), True):
                            attached.append(int(tid))
                    except Exception:
                        pass
            try:
                old_focus = int(user32.GetFocus() or 0)
            except Exception:
                old_focus = 0
            try:
                old_active = int(user32.GetActiveWindow() or 0)
            except Exception:
                old_active = 0
            try:
                user32.SetActiveWindow(wintypes.HWND(int(root_hwnd)))
            except Exception:
                pass
            try:
                user32.SetFocus(wintypes.HWND(int(target)))
            except Exception:
                pass
            time.sleep(0.012)

            lp = win32api.MAKELONG(int(tx), int(ty))
            hit_mouse = win32api.MAKELONG(1, 0x0201)
            self._post_message(int(root_hwnd), 0x001C, 1, 0)
            self._post_message(int(root_hwnd), 0x0086, 1, 0)
            self._post_message(int(root_hwnd), 0x0006, 1, int(target))
            self._post_message(int(target), 0x0007, int(root_hwnd), 0)
            self._post_message(int(target), 0x0021, int(root_hwnd), hit_mouse)
            self._post_message(int(target), 0x0020, int(target), hit_mouse)
            ok1 = self._post_message(int(target), win32con.WM_MOUSEMOVE, 0, lp)
            ok2 = self._post_message(int(target), win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
            time.sleep(0.035)
            ok3 = self._post_message(int(target), win32con.WM_LBUTTONUP, 0, lp)
            time.sleep(0.045)
            return bool(ok1 or ok2 or ok3)
        except Exception:
            return False
        finally:
            try:
                if old_focus and old_focus != int(target):
                    user32.SetFocus(wintypes.HWND(int(old_focus)))
            except Exception:
                pass
            try:
                if old_active and old_active != int(root_hwnd):
                    user32.SetActiveWindow(wintypes.HWND(int(old_active)))
            except Exception:
                pass
            try:
                cur_tid = int(kernel32.GetCurrentThreadId())
                for tid in reversed(attached):
                    try:
                        user32.AttachThreadInput(cur_tid, int(tid), False)
                    except Exception:
                        pass
            except Exception:
                pass

    def _queued_enter_attached_active(self, target: int, root_hwnd: int) -> bool:
        user32 = self.user32
        kernel32 = ctypes.windll.kernel32
        attached = []
        old_focus = 0
        old_active = 0
        try:
            cur_tid = int(kernel32.GetCurrentThreadId())
            pid = wintypes.DWORD()
            target_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(target)), ctypes.byref(pid)))
            fg = int(user32.GetForegroundWindow() or 0)
            fg_tid = 0
            if fg:
                fpid = wintypes.DWORD()
                fg_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fpid)))
            for tid in (target_tid, fg_tid):
                if tid and tid != cur_tid and tid not in attached:
                    try:
                        if user32.AttachThreadInput(cur_tid, int(tid), True):
                            attached.append(int(tid))
                    except Exception:
                        pass
            try:
                old_focus = int(user32.GetFocus() or 0)
            except Exception:
                old_focus = 0
            try:
                old_active = int(user32.GetActiveWindow() or 0)
            except Exception:
                old_active = 0
            try:
                user32.SetActiveWindow(wintypes.HWND(int(root_hwnd)))
            except Exception:
                pass
            try:
                user32.SetFocus(wintypes.HWND(int(target)))
            except Exception:
                pass
            time.sleep(0.015)
            vk = 0x0D
            down_lp = 1 | (0x1C << 16)
            up_lp = 1 | (0x1C << 16) | (1 << 30) | (1 << 31)
            ok1 = self._post_message(int(target), 0x0100, vk, down_lp)
            ok2 = self._post_message(int(target), 0x0102, 13, down_lp)
            time.sleep(0.025)
            ok3 = self._post_message(int(target), 0x0101, vk, up_lp)
            time.sleep(0.040)
            return bool(ok1 or ok2 or ok3)
        except Exception:
            return False
        finally:
            try:
                if old_focus and old_focus != int(target):
                    user32.SetFocus(wintypes.HWND(int(old_focus)))
            except Exception:
                pass
            try:
                if old_active and old_active != int(root_hwnd):
                    user32.SetActiveWindow(wintypes.HWND(int(old_active)))
            except Exception:
                pass
            try:
                cur_tid = int(kernel32.GetCurrentThreadId())
                for tid in reversed(attached):
                    try:
                        user32.AttachThreadInput(cur_tid, int(tid), False)
                    except Exception:
                        pass
            except Exception:
                pass

    def _target_context_flash_point(self, hwnd: int, x: int, y: int) -> Optional[Tuple[int, str, int, int, str]]:
        """在目標 Flash 自己的 DPI context 內直接做 root -> ShockwaveFlash client 映射。"""
        if self._get_window_dpi_context is None or self._set_thread_dpi_context is None:
            return None
        root = int(hwnd)
        previous_ctx = None
        try:
            target_ctx = self._get_window_dpi_context(wintypes.HWND(root))
            if not target_ctx:
                return None
            previous_ctx = self._set_thread_dpi_context(ctypes.c_void_p(target_ctx))
            root_pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(wintypes.HWND(root), ctypes.byref(root_pid))
            candidates = []

            def cb(child, _):
                try:
                    child = int(child)
                    cls = win32gui.GetClassName(child) or ""
                    if "shockwaveflash" not in cls.lower():
                        return
                    child_pid = wintypes.DWORD()
                    self.user32.GetWindowThreadProcessId(wintypes.HWND(child), ctypes.byref(child_pid))
                    if int(child_pid.value) != int(root_pid.value):
                        return
                    pt = wintypes.POINT(int(x), int(y))
                    fn = self.user32.MapWindowPoints
                    fn.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
                    fn.restype = ctypes.c_int
                    fn(wintypes.HWND(root), wintypes.HWND(child), ctypes.byref(pt), 1)
                    l, t, r, b = win32gui.GetClientRect(child)
                    cw, ch = int(r-l), int(b-t)
                    if cw > 1 and ch > 1 and 0 <= int(pt.x) < cw and 0 <= int(pt.y) < ch:
                        candidates.append((cw*ch, child, cls, int(pt.x), int(pt.y)))
                except Exception:
                    pass

            try:
                win32gui.EnumChildWindows(root, cb, None)
            except Exception:
                pass
            if candidates:
                area, target, cls, tx, ty = min(candidates, key=lambda z: z[0])
                return int(target), str(cls), int(tx), int(ty), f"child-area={area}"

            root_cls = win32gui.GetClassName(root) or ""
            if "shockwaveflash" in root_cls.lower():
                l, t, r, b = win32gui.GetClientRect(root)
                if 0 <= int(x) < int(r-l) and 0 <= int(y) < int(b-t):
                    return root, root_cls, int(x), int(y), "root-shockwave"
            return None
        except Exception:
            return None
        finally:
            try:
                if previous_ctx:
                    self._set_thread_dpi_context(ctypes.c_void_p(previous_ctx))
            except Exception:
                pass

    def click_target_dpi_context(self, hwnd: int, x: int, y: int, note: str = "") -> bool:
        """左側高 DPI 唯一新輸入路徑；不搶滑鼠、不切前景、不建立真實 focus。"""
        if CONFIG.get("只辨識不點擊", False):
            return True
        resolved = self._target_context_flash_point(hwnd, int(x), int(y))
        if resolved is None:
            LOG.warning("背景點擊[目標DPI上下文]：%s；找不到包含該點的 ShockwaveFlash。", note)
            return False
        target, cls, tx, ty, route = resolved
        tx=max(1,min(16000,int(tx))); ty=max(1,min(16000,int(ty)))
        lp=win32api.MAKELONG(tx,ty)
        hit=win32api.MAKELONG(1, win32con.WM_LBUTTONDOWN)
        # 只建立 legacy mouse message 的 hit/cursor 狀態，不呼叫 SetFocus/SetActiveWindow。
        self._send_message_timeout(target, 0x0021, int(hwnd), hit, 160)  # WM_MOUSEACTIVATE
        self._send_message_timeout(target, 0x0020, int(target), hit, 160)  # WM_SETCURSOR
        m1=self._send_message_timeout(target, win32con.WM_MOUSEMOVE, 0, lp, 180)
        time.sleep(0.050)
        m2=self._send_message_timeout(target, win32con.WM_MOUSEMOVE, 0, lp, 180)
        dn=self._send_message_timeout(target, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp, 220)
        time.sleep(0.085)
        self._send_message_timeout(target, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp, 180)
        up=self._send_message_timeout(target, win32con.WM_LBUTTONUP, 0, lp, 220)
        self._send_message_timeout(target, 0x0000, 0, 0, 120)
        g=self.geometry.get(int(hwnd))
        geo=(f"raw={g.raw_w}x{g.raw_h} logical={g.logical_w}x{g.logical_h} "
             f"surface={self.surface_mode.get(int(hwnd),'未知')} 螢幕DPI={g.monitor_dpi} rootDPI={g.root_dpi}") if g else "幾何=未知"
        LOG.info("背景點擊[目標DPI上下文]：%s；接收視窗=%s 類別=%s；%s；root邏輯=%s,%s → child訊息=%s,%s；%s",
                 note,target,cls or '未知',geo,int(x),int(y),tx,ty,route)
        return bool(m1 or m2 or dn or up)

    def clear_mouse_calibration(self, hwnd: int):
        """清除只對滑鼠點擊有效的舊校準。

        Enter 成功不代表任何滑鼠座標模式成功；高 DPI 舊 Flash 尤其不能把
        前一個動作的滑鼠模式無條件沿用到「強制登入」。
        """
        self.input_calibration.pop(int(hwnd), None)
        self.uncalibrated_attempt[int(hwnd)] = 0

    def _humanized_mouse_click_attached(self, target: int, tx: int, ty: int, root_hwnd: int) -> bool:
        """不切前景的「較像真人」Flash 點擊。

        舊 Flash 的部分自訂按鈕（尤其自動戰鬥）會忽略只排入訊息佇列的瞬時
        mouse down/up。這條路徑在 AttachThreadInput + SetFocus 後，以
        SendMessageTimeout 同步送入 hover -> down -> short hold -> up。
        不使用 SetForegroundWindow、SetCursorPos 或 SendInput，因此不移動實體滑鼠。
        """
        user32 = self.user32
        kernel32 = ctypes.windll.kernel32
        attached = []
        old_focus = 0
        old_active = 0
        try:
            cur_tid = int(kernel32.GetCurrentThreadId())
            pid = wintypes.DWORD()
            target_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(target)), ctypes.byref(pid)))
            fg = int(user32.GetForegroundWindow() or 0)
            fg_tid = 0
            if fg:
                fpid = wintypes.DWORD()
                fg_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(fg), ctypes.byref(fpid)))
            for tid in (target_tid, fg_tid):
                if tid and tid != cur_tid and tid not in attached:
                    try:
                        if user32.AttachThreadInput(cur_tid, int(tid), True):
                            attached.append(int(tid))
                    except Exception:
                        pass
            try:
                old_focus = int(user32.GetFocus() or 0)
            except Exception:
                old_focus = 0
            try:
                old_active = int(user32.GetActiveWindow() or 0)
            except Exception:
                old_active = 0
            try:
                user32.SetActiveWindow(wintypes.HWND(int(root_hwnd)))
            except Exception:
                pass
            try:
                user32.SetFocus(wintypes.HWND(int(target)))
            except Exception:
                pass
            time.sleep(0.020)

            lp = win32api.MAKELONG(int(tx), int(ty))
            hit_mouse = win32api.MAKELONG(1, 0x0201)
            self._send_message_timeout(int(root_hwnd), 0x001C, 1, 0, 120)
            self._send_message_timeout(int(root_hwnd), 0x0086, 1, 0, 120)
            self._send_message_timeout(int(root_hwnd), 0x0006, 1, int(target), 120)
            self._send_message_timeout(int(target), 0x0007, int(root_hwnd), 0, 120)
            self._send_message_timeout(int(target), 0x0021, int(root_hwnd), hit_mouse, 120)
            self._send_message_timeout(int(target), 0x0020, int(target), hit_mouse, 120)
            # 先讓 Flash 建立 hover / hit-test 狀態。
            self._send_message_timeout(int(target), win32con.WM_MOUSEMOVE, 0, lp, 150)
            time.sleep(max(0.025, float(CONFIG.get("互動背景移入等待秒", 0.075))))
            self._send_message_timeout(int(target), win32con.WM_MOUSEMOVE, 0, lp, 150)
            ok_down = self._send_message_timeout(int(target), win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp, 180)
            time.sleep(max(0.035, float(CONFIG.get("互動背景按下秒", 0.090))))
            # 按住期間再送一個相同座標 mouse move，部分 ActionScript 自訂按鈕會依此更新狀態。
            self._send_message_timeout(int(target), win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp, 150)
            ok_up = self._send_message_timeout(int(target), win32con.WM_LBUTTONUP, 0, lp, 180)
            time.sleep(0.060)
            return bool(ok_down or ok_up)
        except Exception:
            return False
        finally:
            try:
                if old_focus and old_focus != int(target):
                    user32.SetFocus(wintypes.HWND(int(old_focus)))
            except Exception:
                pass
            try:
                if old_active and old_active != int(root_hwnd):
                    user32.SetActiveWindow(wintypes.HWND(int(old_active)))
            except Exception:
                pass
            try:
                cur_tid = int(kernel32.GetCurrentThreadId())
                for tid in reversed(attached):
                    try:
                        user32.AttachThreadInput(cur_tid, int(tid), False)
                    except Exception:
                        pass
            except Exception:
                pass

    def _sync_mouse_click(self, target: int, tx: int, ty: int, root_hwnd: int) -> bool:
        lp = win32api.MAKELONG(int(tx), int(ty))
        hit_mouse = win32api.MAKELONG(1, 0x0201)
        # Move the synthetic pointer away after release so ActionScript receives
        # rollOut/releaseOutside instead of leaving the button in its down skin.
        away_x = max(1, int(tx) - 80)
        away_y = max(1, int(ty) - 40)
        away_lp = win32api.MAKELONG(away_x, away_y)

        # 純背景訊息路徑：不切前景、不移動實體滑鼠。
        self._send_message_timeout(int(root_hwnd), 0x001C, 1, 0, 100)               # WM_ACTIVATEAPP
        self._send_message_timeout(int(root_hwnd), 0x0086, 1, 0, 100)               # WM_NCACTIVATE
        self._send_message_timeout(int(root_hwnd), 0x0006, 1, int(target), 100)      # WM_ACTIVATE / WA_ACTIVE
        self._send_message_timeout(int(target), 0x0007, int(root_hwnd), 0, 100)     # WM_SETFOCUS (message only)
        self._send_message_timeout(target, 0x0021, int(root_hwnd), hit_mouse, 100)   # WM_MOUSEACTIVATE
        self._send_message_timeout(target, 0x0020, int(target), hit_mouse, 100)      # WM_SETCURSOR

        # Do not SendMessage(WM_LBUTTONDOWN) synchronously. Old Flash may enter
        # its button tracking loop inside that call and wait for WM_LBUTTONUP;
        # the sender would then be unable to issue UP until the DOWN call times
        # out, leaving the on-screen button visibly held. Queue the complete
        # sequence instead so UP is available to Flash's own modal message loop.
        pre_up = self._post_message(target, win32con.WM_LBUTTONUP, 0, lp)
        hover = self._post_message(target, win32con.WM_MOUSEMOVE, 0, lp)
        down = self._post_message(target, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
        time.sleep(0.070)
        drag = self._post_message(target, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp)
        up1 = self._post_message(target, win32con.WM_LBUTTONUP, 0, lp)
        time.sleep(0.025)
        up2 = self._post_message(target, win32con.WM_LBUTTONUP, 0, lp)
        leave_move = self._post_message(target, win32con.WM_MOUSEMOVE, 0, away_lp)
        mouse_leave = self._post_message(target, 0x02A3, 0, 0)  # WM_MOUSELEAVE
        # The click itself is successful once its queued DOWN and first UP were
        # accepted. Cleanup messages are best-effort and must not become another
        # over-strict gate that prevents the workflow from advancing.
        return bool(down and up1)

    def _sync_mouse_click_attached_focus(self, target: int, tx: int, ty: int, root_hwnd: int) -> bool:
        """V8.7：高 DPI 舊 Flash 使用真實 active/focus + PostMessage 佇列。
        不切到前景、不移動滑鼠。
        """
        ok = self._queued_mouse_click_attached_active(target, tx, ty, root_hwnd)
        if ok:
            return True
        # 最後保留同步 SendMessage 相容路徑。
        return self._sync_mouse_click(target, tx, ty, root_hwnd)

    def _calibration_valid(self, hwnd: int) -> bool:
        hit = self.input_calibration.get(int(hwnd))
        if not hit:
            return False
        try:
            mon = max(48, int(get_monitor_dpi(hwnd)))
            return int(hit.get("monitor_dpi", 0)) == mon
        except Exception:
            return False

    def calibrated_mode(self, hwnd: int) -> Optional[str]:
        if not self._calibration_valid(hwnd):
            return None
        mode = str(self.input_calibration[int(hwnd)].get("mode", "") or "") or None
        # V11.5：如果目前視窗尺寸已不同於輸入基準，舊的「目標原生邏輯」校準不能直接沿用；
        # 必須切到尺寸無關的 Flash輸入基準。
        base = self.input_base_client.get(int(hwnd))
        g = self.geometry.get(int(hwnd))
        surface = self.surface_mode.get(int(hwnd), "")
        if surface not in ("native", "native-normalized") and base and g is not None and (abs(int(g.logical_w)-int(base[0])) > 2 or abs(int(g.logical_h)-int(base[1])) > 2):
            return "Flash輸入基準"
        return mode

    def confirm_click_mode(self, hwnd: int, mode: str, reason: str = "", root: bool = False):
        mon = max(48, int(get_monitor_dpi(hwnd)))
        self.input_calibration[int(hwnd)] = {
            "monitor_dpi": mon, "mode": str(mode), "root": bool(root),
            "reason": str(reason), "at": time.time(),
        }
        self.uncalibrated_attempt[int(hwnd)] = 0
        route = "頂層" if root else "Flash子視窗"
        LOG.info("視窗 %s 背景輸入模式校準完成：螢幕DPI=%s，路徑=%s，模式=%s%s", hwnd, mon, route, mode, f"（{reason}）" if reason else "")

    def calibrated_root(self, hwnd: int) -> bool:
        if not self._calibration_valid(hwnd):
            return False
        return bool(self.input_calibration[int(hwnd)].get("root", False))

    def clear_calibration_if_environment_changed(self, hwnd: int):
        hit = self.input_calibration.get(int(hwnd))
        if not hit:
            return
        mon = max(48, int(get_monitor_dpi(hwnd)))
        if int(hit.get("monitor_dpi", 0)) != mon:
            self.input_calibration.pop(int(hwnd), None)
            self.uncalibrated_attempt[int(hwnd)] = 0

    def candidate_modes(self, hwnd: int, x: int, y: int, root: bool = False) -> List[str]:
        self.clear_calibration_if_environment_changed(hwnd)
        _target, _cls, _phys, vals = self._message_point_candidates(hwnd, x, y, root=root)
        calibrated = self.calibrated_mode(hwnd)
        modes = [m for m, _x, _y in vals]
        if calibrated and calibrated in modes:
            return [calibrated]
        return modes

    def next_uncalibrated_mode(self, hwnd: int, x: int, y: int, root: bool = False) -> str:
        modes = self.candidate_modes(hwnd, x, y, root=root)
        if not modes:
            return "視窗客戶區"
        if len(modes) == 1:
            return modes[0]
        idx = int(self.uncalibrated_attempt.get(int(hwnd), 0)) % len(modes)
        self.uncalibrated_attempt[int(hwnd)] = idx + 1
        return modes[idx]

    def click_mode(self, hwnd: int, x: int, y: int, mode: str, note: str = "", root: bool = False, transport_override: str = "") -> bool:
        if CONFIG.get("只辨識不點擊", False):
            LOG.info("[測試] 應背景點擊：%s  邏輯畫布=(%d,%d) 模式=%s", note, x, y, mode)
            return True
        # Recheck immediately before the actual message send. This closes the
        # small race where the user starts typing/clicking after worker.step()
        # passed its activity guard but before this input call.
        if USER_ACTIVITY_GUARD.blocked(hwnd):
            return False
        try:
            target, cls, phys, vals = self._message_point_candidates(hwnd, int(x), int(y), root=root)
            selected = None
            for m, tx, ty in vals:
                if m == mode:
                    selected = (m, tx, ty)
                    break
            if selected is None:
                selected = vals[0]
            use_mode, tx, ty = selected
            tx = max(1, min(16000, int(tx)))
            ty = max(1, min(16000, int(ty)))
            g = self.geometry.get(int(hwnd))
            surface = self.surface_mode.get(int(hwnd), "未知")
            use_attached_focus = bool(
                g is not None
                and surface == "logical-padded-crop"
                and int(g.monitor_dpi) != int(g.root_dpi)
            )
            if transport_override == "純背景":
                # Fishing and normal game actions must never attach to the user's
                # input queue or activate/focus the Flash window.  Post the mouse
                # sequence directly even on mixed-DPI/logical-padded surfaces.
                ok = self._sync_mouse_click(target, tx, ty, hwnd)
                transport = "純背景訊息"
            elif transport_override == "互動同步":
                ok = self._humanized_mouse_click_attached(target, tx, ty, hwnd)
                transport = "互動同步焦點"
            elif transport_override == "排隊啟用":
                ok = self._queued_mouse_click_attached_active(target, tx, ty, hwnd)
                transport = "排隊啟用焦點"
            elif use_attached_focus:
                ok = self._sync_mouse_click_attached_focus(target, tx, ty, hwnd)
                transport = "排隊啟用焦點"
            else:
                ok = self._sync_mouse_click(target, tx, ty, hwnd)
                transport = "純背景訊息"
            if g:
                geo = f"raw={g.raw_w}x{g.raw_h} logical={g.logical_w}x{g.logical_h} surface={surface} 螢幕DPI={g.monitor_dpi} rootDPI={g.root_dpi}"
            else:
                geo = "幾何=未知"
            LOG.info(
                "背景點擊[%s/%s]：%s；接收視窗=%s 類別=%s；%s；邏輯=%s,%s → 實體=%s,%s → 訊息=%s,%s",
                transport, use_mode, note, target, cls or "未知", geo, int(x), int(y), phys[0], phys[1], tx, ty,
            )
            return ok
        except Exception as e:
            LOG.error("背景點擊失敗（不切前景）：%s；%s", note, e)
            return False

    def click(self, hwnd: int, x: int, y: int, note: str = "") -> bool:
        self.clear_calibration_if_environment_changed(hwnd)
        mode = self.calibrated_mode(hwnd)
        root = self.calibrated_root(hwnd) if mode is not None else False
        if mode is None:
            mode = self.next_uncalibrated_mode(hwnd, x, y, root=False)
        return self.click_mode(hwnd, x, y, mode, note, root=root)

    def click_root(self, hwnd: int, x: int, y: int, note: str = "") -> bool:
        self.clear_calibration_if_environment_changed(hwnd)
        mode = self.calibrated_mode(hwnd)
        if mode is None:
            mode = self.next_uncalibrated_mode(hwnd, x, y, root=True)
        return self.click_mode(hwnd, x, y, mode, note, root=True)

    def click_active(self, hwnd: int, x: int, y: int, note: str = "", root: bool = False) -> bool:
        self.clear_calibration_if_environment_changed(hwnd)
        mode = self.calibrated_mode(hwnd)
        if mode is None:
            mode = self.next_uncalibrated_mode(hwnd, x, y, root=root)
        return self.click_mode(hwnd, x, y, mode, note, root=root, transport_override="排隊啟用")

    def click_interactive(self, hwnd: int, x: int, y: int, note: str = "", root: bool = False, mode: Optional[str] = None) -> bool:
        """自訂 Flash 控制用的純背景點擊；不取得焦點、不碰實體輸入。"""
        self.clear_calibration_if_environment_changed(hwnd)
        if mode is None:
            # 辨識點已位於目前 Flash 邏輯畫布。優先採 Windows 對目標客戶區
            # 的原生轉換，禁止選到包含非客戶區邊框的「實際尺寸映射」；
            # 實機錯例：(863,406) 被錯送為 (878,434)。
            modes = self.candidate_modes(hwnd, x, y, root=root)
            if self.surface_mode.get(int(hwnd)) in ("logical-padded-crop", "native", "native-normalized") and "邏輯畫布直送" in modes:
                mode = "邏輯畫布直送"
            elif "目標原生邏輯" in modes:
                mode = "目標原生邏輯"
            elif "視窗客戶區" in modes:
                mode = "視窗客戶區"
            elif modes:
                mode = modes[0]
            else:
                mode = "視窗客戶區"
        return self.click_mode(hwnd, x, y, str(mode), note, root=root, transport_override="純背景")

    def release_interactive(self, hwnd: int, x: int, y: int, note: str = "", root: bool = False) -> bool:
        """Release a possibly stuck Flash button without ever sending MouseDown."""
        if CONFIG.get("只辨識不點擊", False):
            return True
        try:
            target, cls, _phys, vals = self._message_point_candidates(hwnd, int(x), int(y), root=root)
            modes = [name for name, _tx, _ty in vals]
            if self.surface_mode.get(int(hwnd)) == "logical-padded-crop" and "邏輯畫布直送" in modes:
                wanted = "邏輯畫布直送"
            elif "目標原生邏輯" in modes:
                wanted = "目標原生邏輯"
            elif "視窗客戶區" in modes:
                wanted = "視窗客戶區"
            else:
                wanted = modes[0] if modes else "視窗客戶區"
            selected = next(((name, tx, ty) for name, tx, ty in vals if name == wanted), None)
            if selected is None:
                return False
            use_mode, tx, ty = selected
            tx = max(1, min(16000, int(tx)))
            ty = max(1, min(16000, int(ty)))
            lp = win32api.MAKELONG(tx, ty)
            away_lp = win32api.MAKELONG(max(1, tx - 80), max(1, ty - 40))

            # UP-only recovery. WM_CANCELMODE/WM_CAPTURECHANGED terminate an old
            # Flash tracking loop left behind by a previous process. There is no
            # DOWN here, so this path cannot activate the button accidentally.
            up1 = self._post_message(target, win32con.WM_LBUTTONUP, 0, lp)
            self._post_message(target, 0x001F, 0, 0)  # WM_CANCELMODE
            self._post_message(target, 0x0215, 0, 0)  # WM_CAPTURECHANGED
            up2 = self._post_message(target, win32con.WM_LBUTTONUP, 0, lp)
            self._post_message(target, win32con.WM_MOUSEMOVE, 0, away_lp)
            self._post_message(target, 0x02A3, 0, 0)  # WM_MOUSELEAVE
            if int(root_hwnd := hwnd) != int(target):
                self._post_message(int(root_hwnd), win32con.WM_LBUTTONUP, 0, lp)
                self._post_message(int(root_hwnd), 0x001F, 0, 0)
            LOG.info(
                "背景釋放[無MouseDown/%s]：%s；接收視窗=%s 類別=%s；邏輯=%s,%s → 訊息=%s,%s",
                use_mode, note, target, cls or "未知", int(x), int(y), tx, ty,
            )
            return bool(up1 and up2)
        except Exception as exc:
            LOG.warning("背景釋放失敗：%s；%s", note, exc)
            return False

    def click_foreground_physical(
        self,
        hwnd: int,
        x: int,
        y: int,
        note: str = "",
        *,
        hold_s: Optional[float] = None,
        post_wait_s: Optional[float] = None,
        click_count: int = 1,
        click_interval_s: Optional[float] = None,
    ) -> bool:
        """最後備援：短暫切前景並送真實 Windows 滑鼠輸入。

        只能在背景輸入已由畫面驗證證明無效後呼叫。函式會保存滑鼠與原前景視窗，
        點擊後立即還原。最小化視窗不自動還原，避免偷改使用者視窗狀態。
        """
        use_click_count = max(1, min(2, int(click_count)))
        if CONFIG.get("只辨識不點擊", False):
            LOG.info("[測試] 應前景實體點擊：%s  邏輯畫布=(%d,%d) 次數=%d", note, x, y, use_click_count)
            return True
        if not FOREGROUND_PHYSICAL_FALLBACK or not bool(CONFIG.get("允許前景實體輸入備援", True)):
            LOG.warning("前景實體點擊備援已停用：%s", note)
            return False
        root = int(hwnd)
        if not self.is_window(root) or not win32gui.IsWindowVisible(root):
            LOG.error("前景實體點擊拒絕：遊戲視窗不存在或不可見；%s", note)
            return False
        if win32gui.IsIconic(root):
            LOG.error("前景實體點擊拒絕：遊戲視窗已最小化，程式不會偷偷還原視窗；%s", note)
            return False

        g = self._ensure_geometry(root)
        if g is None:
            LOG.error("前景實體點擊拒絕：沒有可驗證的視窗幾何；%s", note)
            return False
        if not (0 <= int(x) < int(g.logical_w) and 0 <= int(y) < int(g.logical_h)):
            LOG.error(
                "前景實體點擊拒絕：邏輯點 %s,%s 超出畫布 %sx%s；%s",
                x, y, g.logical_w, g.logical_h, note,
            )
            return False
        sx, sy = self.logical_root_to_visible_physical(root, int(x), int(y))
        client_right = int(g.phys_origin_x + g.raw_w)
        client_bottom = int(g.phys_origin_y + g.raw_h)
        if not (g.phys_origin_x <= sx < client_right and g.phys_origin_y <= sy < client_bottom):
            LOG.error(
                "前景實體點擊拒絕：映射後點 %s,%s 超出實體 client (%s,%s)-(%s,%s)；%s",
                sx, sy, g.phys_origin_x, g.phys_origin_y, client_right, client_bottom, note,
            )
            return False

        user32 = self.user32
        kernel32 = ctypes.windll.kernel32
        with self.foreground_input_lock:
            old_fg = int(user32.GetForegroundWindow() or 0)
            try:
                old_cursor = tuple(int(v) for v in win32gui.GetCursorPos())
            except Exception:
                old_cursor = None
            attached: List[int] = []
            cur_tid = int(kernel32.GetCurrentThreadId())
            mouse_down_sent = False
            clicked = False
            try:
                tids: List[int] = []
                for wh in (old_fg, root):
                    if not wh:
                        continue
                    pid = wintypes.DWORD()
                    tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(wh)), ctypes.byref(pid)))
                    if tid and tid != cur_tid and tid not in tids:
                        tids.append(tid)
                for tid in tids:
                    try:
                        if user32.AttachThreadInput(cur_tid, int(tid), True):
                            attached.append(int(tid))
                    except Exception:
                        pass

                target, cls = self._target_under_physical(root, int(sx), int(sy))
                for _ in range(3):
                    try:
                        user32.BringWindowToTop(wintypes.HWND(root))
                    except Exception:
                        pass
                    try:
                        user32.SetForegroundWindow(wintypes.HWND(root))
                    except Exception:
                        pass
                    try:
                        user32.SetActiveWindow(wintypes.HWND(root))
                        user32.SetFocus(wintypes.HWND(int(target)))
                    except Exception:
                        pass
                    time.sleep(0.045)
                    if int(user32.GetForegroundWindow() or 0) == root:
                        break
                if int(user32.GetForegroundWindow() or 0) != root:
                    LOG.error("前景實體點擊拒絕：Windows 不允許遊戲取得前景，為避免點到其他程式已取消；%s", note)
                    return False

                if not bool(user32.SetCursorPos(int(sx), int(sy))):
                    LOG.error("前景實體點擊拒絕：SetCursorPos 失敗；%s", note)
                    return False
                time.sleep(max(0.025, float(CONFIG.get("前景實體點擊移入等待秒", 0.055))))
                actual = tuple(int(v) for v in win32gui.GetCursorPos())
                if abs(actual[0] - int(sx)) > 2 or abs(actual[1] - int(sy)) > 2:
                    LOG.error(
                        "前景實體點擊拒絕：滑鼠未到指定點，目標=%s,%s 實際=%s,%s；%s",
                        sx, sy, actual[0], actual[1], note,
                    )
                    return False

                use_hold = float(CONFIG.get("前景實體點擊按下秒", 0.085)) if hold_s is None else float(hold_s)
                use_interval = float(CONFIG.get("釣魚雙擊間隔秒", 0.12)) if click_interval_s is None else float(click_interval_s)
                for click_no in range(use_click_count):
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    mouse_down_sent = True
                    time.sleep(max(0.040, use_hold))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    mouse_down_sent = False
                    if click_no + 1 < use_click_count:
                        time.sleep(max(0.060, min(0.30, use_interval)))
                use_post = float(CONFIG.get("前景實體點擊後等待秒", 0.120)) if post_wait_s is None else float(post_wait_s)
                time.sleep(max(0.060, use_post))
                clicked = True
                LOG.warning(
                    "前景實體點擊備援：%s；接收視窗=%s 類別=%s；"
                    "raw=%sx%s logical=%sx%s surface=%s DPI=%s/%s；邏輯=%s,%s → 螢幕實體=%s,%s；點擊次數=%d；已排程還原滑鼠與前景。",
                    note, target, cls or "未知", g.raw_w, g.raw_h, g.logical_w, g.logical_h,
                    self.surface_mode.get(root, "未知"), g.monitor_dpi, g.root_dpi,
                    int(x), int(y), int(sx), int(sy), use_click_count,
                )
                return True
            except Exception as e:
                LOG.error("前景實體點擊失敗：%s；%s", note, e)
                return False
            finally:
                # mouse down 成功後無論中間發生什麼例外，都再補一次 up，避免滑鼠卡住。
                if mouse_down_sent:
                    try:
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    except Exception:
                        pass
                if old_cursor is not None:
                    try:
                        user32.SetCursorPos(int(old_cursor[0]), int(old_cursor[1]))
                    except Exception:
                        pass
                if old_fg and old_fg != root and self.is_window(old_fg):
                    try:
                        user32.SetForegroundWindow(wintypes.HWND(int(old_fg)))
                        user32.SetActiveWindow(wintypes.HWND(int(old_fg)))
                    except Exception:
                        pass
                try:
                    for tid in reversed(attached):
                        user32.AttachThreadInput(cur_tid, int(tid), False)
                except Exception:
                    pass

    @staticmethod
    def _send_unicode_text(text: str) -> bool:
        """Send Unicode keystrokes without changing or exposing the clipboard."""
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        for char in str(text or ""):
            code = ord(char)
            # The configured fishing syntax and CJK labels are BMP text. Reject
            # unsupported surrogate input rather than silently sending corruption.
            if code > 0xFFFF:
                return False
            events = (_INPUT * 2)()
            events[0].type = 1
            events[0].ki = _KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
            events[1].type = 1
            events[1].ki = _KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            sent = int(user32.SendInput(2, events, ctypes.sizeof(_INPUT)))
            if sent != 2:
                return False
            # Old 32-bit Flash drops bursts that arrive in the same scheduler tick.
            time.sleep(0.004)
        return True

    def send_chat_message_background(
        self,
        hwnd: int,
        input_point: Tuple[int, int],
        send_point: Tuple[int, int],
        text: str,
        note: str = "",
    ) -> bool:
        """Send chat through the Flash message queue without moving the real mouse."""
        if CONFIG.get("只辨識不點擊", False):
            LOG.info("[測試] 應背景聊天發送：%s；字數=%d", note, len(str(text or "")))
            return True
        resolved = self._target_context_flash_point(hwnd, int(input_point[0]), int(input_point[1]))
        if resolved is None:
            LOG.warning("背景聊天發送失敗：%s；找不到輸入框所屬 ShockwaveFlash。", note)
            return False
        target, cls, _tx, _ty, route = resolved
        if not self.click_interactive(hwnd, int(input_point[0]), int(input_point[1]), note + "／輸入框"):
            return False
        # WM_CHAR is queued directly to Flash.  It does not call SendInput,
        # SetForegroundWindow, SetCursorPos, or touch the clipboard.
        for char in str(text or ""):
            code = ord(char)
            if code > 0xFFFF or not self._post_message(int(target), 0x0102, code, 1):
                LOG.warning("背景聊天輸入失敗：%s；字元=%r。", note, char)
                return False
            time.sleep(0.004)
        if not self.click_interactive(hwnd, int(send_point[0]), int(send_point[1]), note + "／發送"):
            return False
        LOG.info(
            "背景聊天發送：%s；接收視窗=%s 類別=%s；輸入=(%s,%s) 發送=(%s,%s)；字數=%d；%s",
            note, target, cls or "未知", input_point[0], input_point[1],
            send_point[0], send_point[1], len(str(text or "")), route,
        )
        return True

    def send_chat_message_foreground_physical(
        self,
        hwnd: int,
        input_point: Tuple[int, int],
        send_point: Tuple[int, int],
        text: str,
        note: str = "",
    ) -> bool:
        """Focus the detected chat field, type Unicode, and click the detected send button.

        Both points come from the latest captured frame. The function revalidates
        geometry, refuses minimized windows, and restores the user's cursor and
        foreground window after the atomic operation.
        """
        if CONFIG.get("只辨識不點擊", False):
            LOG.info("[測試] 應輸入並發送聊天：%s；輸入=%s 發送=%s 字數=%d", note, input_point, send_point, len(text))
            return True
        if not FOREGROUND_PHYSICAL_FALLBACK or not bool(CONFIG.get("允許前景實體輸入備援", True)):
            LOG.error("聊天發送拒絕：前景實體輸入已停用；%s", note)
            return False
        root = int(hwnd)
        if not self.is_window(root) or not win32gui.IsWindowVisible(root) or win32gui.IsIconic(root):
            LOG.error("聊天發送拒絕：遊戲視窗不可見或已最小化；%s", note)
            return False
        if not text or len(text) > 1500:
            LOG.error("聊天發送拒絕：字串為空或超過安全上限；%s", note)
            return False
        g = self._ensure_geometry(root)
        if g is None:
            LOG.error("聊天發送拒絕：沒有最新視窗幾何；%s", note)
            return False
        points = [tuple(int(v) for v in input_point), tuple(int(v) for v in send_point)]
        if any(not (0 <= x < int(g.logical_w) and 0 <= y < int(g.logical_h)) for x, y in points):
            LOG.error("聊天發送拒絕：辨識點超出目前畫布 %sx%s；輸入=%s 發送=%s；%s", g.logical_w, g.logical_h, input_point, send_point, note)
            return False
        input_screen = self.logical_root_to_visible_physical(root, *points[0])
        send_screen = self.logical_root_to_visible_physical(root, *points[1])
        client_right = int(g.phys_origin_x + g.raw_w)
        client_bottom = int(g.phys_origin_y + g.raw_h)
        for sx, sy in (input_screen, send_screen):
            if not (g.phys_origin_x <= sx < client_right and g.phys_origin_y <= sy < client_bottom):
                LOG.error("聊天發送拒絕：映射點 %s,%s 超出目前實體 client；%s", sx, sy, note)
                return False

        user32 = self.user32
        kernel32 = ctypes.windll.kernel32
        with self.foreground_input_lock:
            old_fg = int(user32.GetForegroundWindow() or 0)
            try:
                old_cursor = tuple(int(v) for v in win32gui.GetCursorPos())
            except Exception:
                old_cursor = None
            attached: List[int] = []
            cur_tid = int(kernel32.GetCurrentThreadId())
            mouse_down_sent = False
            ctrl_down = False
            try:
                tids: List[int] = []
                for wh in (old_fg, root):
                    if not wh:
                        continue
                    pid = wintypes.DWORD()
                    tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(wh)), ctypes.byref(pid)))
                    if tid and tid != cur_tid and tid not in tids:
                        tids.append(tid)
                for tid in tids:
                    try:
                        if user32.AttachThreadInput(cur_tid, int(tid), True):
                            attached.append(int(tid))
                    except Exception:
                        pass

                input_target, input_cls = self._target_under_physical(root, *input_screen)
                for _ in range(3):
                    try:
                        user32.BringWindowToTop(wintypes.HWND(root))
                        user32.SetForegroundWindow(wintypes.HWND(root))
                        user32.SetActiveWindow(wintypes.HWND(root))
                        user32.SetFocus(wintypes.HWND(int(input_target)))
                    except Exception:
                        pass
                    time.sleep(0.045)
                    if int(user32.GetForegroundWindow() or 0) == root:
                        break
                if int(user32.GetForegroundWindow() or 0) != root:
                    LOG.error("聊天發送拒絕：Windows 不允許遊戲取得前景；%s", note)
                    return False

                if not bool(user32.SetCursorPos(*input_screen)):
                    return False
                time.sleep(0.055)
                actual = tuple(int(v) for v in win32gui.GetCursorPos())
                if abs(actual[0] - input_screen[0]) > 2 or abs(actual[1] - input_screen[1]) > 2:
                    LOG.error("聊天發送拒絕：滑鼠未到輸入框；目標=%s 實際=%s；%s", input_screen, actual, note)
                    return False
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                mouse_down_sent = True
                time.sleep(0.070)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                mouse_down_sent = False
                time.sleep(0.090)

                # Clear any prior text. Key-up cleanup is guaranteed in finally.
                win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
                ctrl_down = True
                win32api.keybd_event(ord("A"), 0, 0, 0)
                win32api.keybd_event(ord("A"), 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
                ctrl_down = False
                win32api.keybd_event(win32con.VK_BACK, 0, 0, 0)
                win32api.keybd_event(win32con.VK_BACK, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.050)
                if not self._send_unicode_text(text):
                    LOG.error("聊天發送失敗：Unicode 鍵盤輸入未完整送出；%s", note)
                    return False
                time.sleep(0.090)

                if not bool(user32.SetCursorPos(*send_screen)):
                    return False
                time.sleep(0.055)
                actual = tuple(int(v) for v in win32gui.GetCursorPos())
                if abs(actual[0] - send_screen[0]) > 2 or abs(actual[1] - send_screen[1]) > 2:
                    LOG.error("聊天發送拒絕：滑鼠未到發送按鈕；目標=%s 實際=%s；%s", send_screen, actual, note)
                    return False
                send_target, send_cls = self._target_under_physical(root, *send_screen)
                try:
                    user32.SetFocus(wintypes.HWND(int(send_target)))
                except Exception:
                    pass
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                mouse_down_sent = True
                time.sleep(0.085)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                mouse_down_sent = False
                time.sleep(0.160)
                LOG.warning(
                    "前景實體聊天發送：%s；輸入接收=%s/%s 發送接收=%s/%s；"
                    "frame=%sx%s raw=%sx%s；邏輯輸入=%s 發送=%s → 實體輸入=%s 發送=%s；字數=%d。",
                    note, input_target, input_cls or "未知", send_target, send_cls or "未知",
                    g.logical_w, g.logical_h, g.raw_w, g.raw_h,
                    input_point, send_point, input_screen, send_screen, len(text),
                )
                return True
            except Exception as e:
                LOG.error("聊天發送失敗：%s；%s", note, e)
                return False
            finally:
                if ctrl_down:
                    try:
                        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
                    except Exception:
                        pass
                if mouse_down_sent:
                    try:
                        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                    except Exception:
                        pass
                if old_cursor is not None:
                    try:
                        user32.SetCursorPos(*old_cursor)
                    except Exception:
                        pass
                if old_fg and old_fg != root and self.is_window(old_fg):
                    try:
                        user32.SetForegroundWindow(wintypes.HWND(int(old_fg)))
                        user32.SetActiveWindow(wintypes.HWND(int(old_fg)))
                    except Exception:
                        pass
                for tid in reversed(attached):
                    try:
                        user32.AttachThreadInput(cur_tid, int(tid), False)
                    except Exception:
                        pass

    def press_enter_foreground_physical(self, hwnd: int, note: str = "", post_wait_s: float = 0.35) -> bool:
        """在目標 Flash 真正取得前景後，用 Windows 鍵盤輸入送 Enter。"""
        if CONFIG.get("只辨識不點擊", False):
            LOG.info("[測試] 應前景實體按 Enter：%s", note)
            return True
        if not FOREGROUND_PHYSICAL_FALLBACK or not bool(CONFIG.get("允許前景實體輸入備援", True)):
            return False
        root = int(hwnd)
        if not self.is_window(root) or not win32gui.IsWindowVisible(root) or win32gui.IsIconic(root):
            LOG.error("前景實體 Enter 拒絕：遊戲視窗不可見或已最小化；%s", note)
            return False
        user32 = self.user32
        kernel32 = ctypes.windll.kernel32
        with self.foreground_input_lock:
            old_fg = int(user32.GetForegroundWindow() or 0)
            attached: List[int] = []
            cur_tid = int(kernel32.GetCurrentThreadId())
            key_down_sent = False
            try:
                tids: List[int] = []
                for wh in (old_fg, root):
                    if not wh:
                        continue
                    pid = wintypes.DWORD()
                    tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(wh)), ctypes.byref(pid)))
                    if tid and tid != cur_tid and tid not in tids:
                        tids.append(tid)
                for tid in tids:
                    try:
                        if user32.AttachThreadInput(cur_tid, int(tid), True):
                            attached.append(int(tid))
                    except Exception:
                        pass
                g = self._ensure_geometry(root)
                cx = (g.logical_w // 2) if g else 450
                cy = (g.logical_h // 2) if g else 295
                sx, sy = self.logical_root_to_visible_physical(root, cx, cy)
                target, cls = self._target_under_physical(root, sx, sy)
                for _ in range(3):
                    try:
                        user32.BringWindowToTop(wintypes.HWND(root))
                        user32.SetForegroundWindow(wintypes.HWND(root))
                        user32.SetActiveWindow(wintypes.HWND(root))
                        user32.SetFocus(wintypes.HWND(int(target)))
                    except Exception:
                        pass
                    time.sleep(0.045)
                    if int(user32.GetForegroundWindow() or 0) == root:
                        break
                if int(user32.GetForegroundWindow() or 0) != root:
                    LOG.error("前景實體 Enter 拒絕：Windows 不允許遊戲取得前景；%s", note)
                    return False
                vk = 0x0D
                scan = int(user32.MapVirtualKeyW(vk, 0) or 0x1C)
                win32api.keybd_event(vk, scan, 0, 0)
                key_down_sent = True
                time.sleep(0.085)
                win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)
                key_down_sent = False
                time.sleep(max(0.12, float(post_wait_s)))
                LOG.warning("前景實體按鍵備援：%s；接收視窗=%s 類別=%s；Enter 已送出並還原原前景。", note, target, cls or "未知")
                return True
            except Exception as e:
                LOG.error("前景實體 Enter 失敗：%s；%s", note, e)
                return False
            finally:
                if key_down_sent:
                    try:
                        win32api.keybd_event(0x0D, 0x1C, win32con.KEYEVENTF_KEYUP, 0)
                    except Exception:
                        pass
                if old_fg and old_fg != root and self.is_window(old_fg):
                    try:
                        user32.SetForegroundWindow(wintypes.HWND(int(old_fg)))
                        user32.SetActiveWindow(wintypes.HWND(int(old_fg)))
                    except Exception:
                        pass
                for tid in reversed(attached):
                    try:
                        user32.AttachThreadInput(cur_tid, int(tid), False)
                    except Exception:
                        pass

    def press_enter(self, hwnd: int, note: str = "") -> bool:
        if CONFIG.get("只辨識不點擊", False):
            return True
        if USER_ACTIVITY_GUARD.blocked(hwnd):
            return False
        try:
            g = self._ensure_geometry(hwnd)
            cx = (g.logical_w // 2) if g else 450
            cy = (g.logical_h // 2) if g else 284
            target, cls, _phys, _vals = self._message_point_candidates(hwnd, cx, cy, root=False)
            vk = 0x0D
            down_lp = 1 | (0x1C << 16)
            up_lp = 1 | (0x1C << 16) | (1 << 30) | (1 << 31)
            g = self.geometry.get(int(hwnd))
            if g is not None and self.surface_mode.get(int(hwnd)) == "logical-padded-crop" and int(g.monitor_dpi) != int(g.root_dpi):
                ok = self._queued_enter_attached_active(target, hwnd)
                LOG.info("背景按鍵[排隊啟用/Enter]：%s；接收視窗=%s 類別=%s", note, target, cls or "未知")
                return bool(ok)
            ok1 = self._send_message_timeout(target, 0x0100, vk, down_lp)
            ok2 = self._send_message_timeout(target, 0x0102, 13, down_lp)
            ok3 = self._send_message_timeout(target, 0x0101, vk, up_lp)
            LOG.info("背景按鍵[Enter]：%s；接收視窗=%s 類別=%s", note, target, cls or "未知")
            return bool(ok1 or ok2 or ok3)
        except Exception as e:
            LOG.error("背景 Enter 失敗：%s；%s", note, e)
            return False


WIO = WindowIO()


# -----------------------------
# 畫面模板
# -----------------------------

@dataclass
class Match:
    score: float
    x: int
    y: int
    w: int
    h: int
    scale: float

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


class TemplateBank:
    def __init__(self):
        self.data: Dict[str, np.ndarray] = {}
        self.gray: Dict[str, np.ndarray] = {}
        self.base_width: Dict[str, float] = {}
        self.scaled_cache: Dict[Tuple[str, int, int], np.ndarray] = {}
        self.local = threading.local()
        self.missing: List[str] = []
        self._load("斷線_帳號異地", "disconnect_other_login.png", 895)
        self._load("斷線_伺服器中斷", "disconnect_server.png", 900)
        self._load("斷線_連線逾時", "disconnect_timeout.png", 900)
        # V6.2：斷線主按鈕本身比半透明整框穩定，作為低成本快篩第二路徑。
        self._load_optional("斷線_確定按鈕", "disconnect_confirm.png", 900)
        self._load("強制登入", "force_login.png", 900)
        # V5.2：由使用者提供的完整登入畫面直接裁出較大的視覺模板。
        # 不再用「開始遊戲」的位置去推算「強制登入」；匹配到的模板中心就是實際按鈕中心。
        self._load_optional("強制登入_畫面區塊", "force_login_context.png", 900)
        self._load_optional("登入頁_雙按鈕面板", "login_buttons_panel.png", 900)
        self._load("開始遊戲", "start_game.png", 900)
        self._load("最近一次登錄資訊_標題", "last_login_title.png", 900)
        # V5.9：線路按鈕直接使用使用者提供的實際畫面模板辨識，不再用標題位置推算點擊座標。
        for _line_no in range(1, 9):
            self._load_optional(f"線路按鈕_{_line_no}", f"line_{_line_no}.png", 900)
        self._load("角色選擇_標題", "role_select_title.png", 898)
        self._load("進入遊戲", "enter_game.png", 898)
        self._load("彈窗關閉X_自動副本", "close_auto_instance.png", 895)
        self._load("彈窗關閉X_日常活動", "close_daily_activity.png", 896)
        # 莊園與寵物／人物／背包等一般面板共用較窄的紅色圓形 X。
        # 這張既有莊園實圖只作通用面板關閉證據；莊園執行期間由協調鎖排除。
        self._load_path("彈窗關閉X_通用面板", RESOURCE_DIR / "manor_assets" / "manor_close.png", 900)
        # 通用清面板不得關閉莊園。操作列是比單一 X 更完整的莊園畫面證據，
        # 即使莊園工作剛失敗並釋放協調鎖，仍必須保護該介面。
        self._load_path("莊園_操作列", RESOURCE_DIR / "manor_assets" / "action_bar.png", 897)
        # V5.7：聊天視窗可能蓋住斷線彈窗；只在已看到「部分斷線文字」時才關閉，不會平常亂關。
        self._load_optional("遮擋_聊天標題", "chat_header.png", 810)
        self._load("自動戰鬥_目標", "auto_battle_target.png", 895)
        self._load_optional("自動戰鬥_目標_使用者裁切", "auto_battle_target_user_crop.png", 895)
        self._load_optional("自動戰鬥_目標_完整樣本", "auto_battle_target_full_sample.png", 895)
        self._load("自動戰鬥_另一狀態", "auto_battle_other.png", 895)
        self._load_optional("自動戰鬥_另一狀態_最新", "auto_battle_other_latest.png", 895)
        # V11.8.2：名稱直接描述使用者指定的視覺結果，避免再把「開啟／關閉」
        # 的遊戲語意與按鈕外觀搞反。正確狀態必須同時命中無 X，且未命中有 X。
        self._load_optional("自動戰鬥_無X正確_使用者", "auto_battle_no_x_correct_v1182.png", 895)
        self._load_optional("自動戰鬥_有X錯誤_使用者", "auto_battle_x_wrong_v1182.png", 895)
        self._load_optional("聊天目前分頁_未選內容", "chat_current_inactive_context_v1182.png", 896)
        self._load_optional("聊天目前分頁_紅色內容", "chat_current_active_context_v1182.png", 896)
        # V11.8.4：上排「目前」只決定顯示哪一頁；下排發送頻道是另一個
        # 獨立控制。三個使用者實圖用來定位下排按鈕及展開後的「目前」。
        self._load_optional("聊天發送頻道_世界內容", "chat_sender_world_context_v1183.png", 896)
        self._load_optional("聊天發送頻道_目前內容", "chat_sender_current_context_v1183.png", 896)
        self._load_optional("聊天發送頻道_展開選單", "chat_sender_channel_menu_v1183.png", 896)
        # 使用者指定的實際畫面裁圖；只作釣魚前置步驟的視覺證據。
        self._load_optional("釣魚前置_飛行", "fishing_flight_button.png", 899)
        self._load_optional("釣魚前置_飛行_目前實圖", "fishing_flight_button_current.png", 900)
        self._load_optional("釣魚前置_降落", "fishing_land_button.png", 893)
        self._load_optional("釣魚前置_降落_120福實圖", "fishing_land_button_120fu.png", 895)
        self._load_optional("釣魚前置_收回系統列", "fishing_menu_collapse_button.png", 899)
        self._load_optional("釣魚前置_收回系統列_目前實圖", "fishing_menu_collapse_button_live.png", 895)
        self._load_optional("釣魚前置_展開系統列_120福實圖", "fishing_menu_expand_button_120fu.png", 895)
        self._load_optional("釣魚前置_聊天小按鈕", "fishing_chat_prepare_button.png", 899)
        self._load("戰鬥場景_自動戰鬥標題", "battle_auto_title.png", 1340)

    @staticmethod
    def _read_image(p: Path) -> Optional[np.ndarray]:
        """Windows/OpenCV 對非 ASCII 路徑偶有 imread 失敗；先用 imdecode 讀原始位元組。"""
        try:
            raw = np.fromfile(str(p), dtype=np.uint8)
            if raw.size:
                img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                if img is not None:
                    return img
        except Exception:
            pass
        try:
            return cv2.imread(str(p), cv2.IMREAD_COLOR)
        except Exception:
            return None

    def _load_path(self, name: str, path: Path, base_width: float):
        # V5.4：任何單一模板損壞/遺漏都不能拖垮整個背景監測。
        p = Path(path)
        img = self._read_image(p)
        if img is None:
            self.missing.append(p.name)
            LOG.warning("畫面模板缺少或無法讀取：%s；略過此模板，其他辨識與 OCR 繼續運作。", p)
            return
        self.data[name] = img
        self.gray[name] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        self.base_width[name] = base_width

    def _load(self, name: str, filename: str, base_width: float):
        self._load_path(name, TEMPLATE_DIR / filename, base_width)

    def _load_optional(self, name: str, filename: str, base_width: float):
        self._load(name, filename, base_width)

    def _frame_gray(self, frame: np.ndarray) -> np.ndarray:
        # 同一個 step 裡會比對數個模板；每張畫面只轉灰階一次。thread-local 避免多視窗互相覆寫。
        if getattr(self.local, "frame_obj", None) is frame:
            return self.local.frame_gray
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.local.frame_obj = frame
        self.local.frame_gray = gray
        return gray

    def _scaled_gray(self, name: str, frame_width: int, mul: float) -> np.ndarray:
        key = (name, int(frame_width), int(round(mul * 1000)))
        hit = self.scaled_cache.get(key)
        if hit is not None:
            return hit
        tgray0 = self.gray[name]
        s = frame_width / float(self.base_width[name]) * mul
        tw = max(6, int(round(tgray0.shape[1] * s)))
        th = max(6, int(round(tgray0.shape[0] * s)))
        tgray = cv2.resize(tgray0, (tw, th), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC)
        self.scaled_cache[key] = tgray
        return tgray

    def match(
        self,
        frame: np.ndarray,
        name: str,
        threshold: float,
        roi: Optional[Tuple[float, float, float, float]] = None,
        scale_spread: Tuple[float, ...] = (0.94, 0.97, 1.00, 1.03, 1.06),
    ) -> Optional[Match]:
        if name not in self.data:
            return None
        templ = self.data[name]
        fh, fw = frame.shape[:2]
        if roi is None:
            x0, y0, x1, y1 = 0, 0, fw, fh
        else:
            rx0, ry0, rx1, ry1 = roi
            x0, y0 = int(fw * rx0), int(fh * ry0)
            x1, y1 = int(fw * rx1), int(fh * ry1)
        gray_full = self._frame_gray(frame)
        gray = gray_full[y0:y1, x0:x1]
        if gray.size == 0:
            return None

        nominal = fw / float(self.base_width[name])
        best: Optional[Match] = None

        for mul in scale_spread:
            s = nominal * mul
            tgray = self._scaled_gray(name, fw, mul)
            th, tw = tgray.shape[:2]
            if tw >= gray.shape[1] or th >= gray.shape[0]:
                continue
            res = cv2.matchTemplate(gray, tgray, cv2.TM_CCOEFF_NORMED)
            _minv, maxv, _minl, maxl = cv2.minMaxLoc(res)
            if best is None or maxv > best.score:
                best = Match(float(maxv), x0 + maxl[0], y0 + maxl[1], tw, th, s)

        if best is not None and best.score >= threshold:
            return best
        return None

    def match_absolute(
        self,
        frame: np.ndarray,
        name: str,
        threshold: float,
        roi: Optional[Tuple[float, float, float, float]] = None,
        scales: Tuple[float, ...] = (0.78, 0.86, 0.94, 1.00, 1.06, 1.14, 1.22, 1.30),
    ) -> Optional[Match]:
        """以模板原始像素尺寸為基準做多尺度比對。

        Flash 的 UI 在不同視窗大小／不同螢幕 DPI 下常維持近似固定像素大小，
        不能只用「目前視窗寬度 ÷ 範本寬度」推算縮放。V6.2 對斷線按鈕、
        登入按鈕與線路面板加入這條獨立尺度路徑。
        """
        if name not in self.data:
            return None
        fh, fw = frame.shape[:2]
        if roi is None:
            x0, y0, x1, y1 = 0, 0, fw, fh
        else:
            rx0, ry0, rx1, ry1 = roi
            x0, y0 = int(fw * rx0), int(fh * ry0)
            x1, y1 = int(fw * rx1), int(fh * ry1)
        gray = self._frame_gray(frame)[y0:y1, x0:x1]
        if gray.size == 0:
            return None
        tgray0 = self.gray[name]
        best: Optional[Match] = None
        for s in scales:
            tw = max(6, int(round(tgray0.shape[1] * s)))
            th = max(6, int(round(tgray0.shape[0] * s)))
            if tw >= gray.shape[1] or th >= gray.shape[0]:
                continue
            tgray = cv2.resize(tgray0, (tw, th), interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC)
            res = cv2.matchTemplate(gray, tgray, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _ml, loc = cv2.minMaxLoc(res)
            cand = Match(float(mx), x0 + loc[0], y0 + loc[1], tw, th, float(s))
            if best is None or cand.score > best.score:
                best = cand
        return best if best is not None and best.score >= threshold else None

    @staticmethod
    def match_external(frame: np.ndarray, path: Path, threshold: float = 0.72) -> Optional[Match]:
        templ = TemplateBank._read_image(path)
        if templ is None:
            return None
        fh, fw = frame.shape[:2]
        # 角色模板目前是 898 寬畫面截取，因此以 898 為基準縮放。
        nominal = fw / 898.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tgray0 = cv2.cvtColor(templ, cv2.COLOR_BGR2GRAY)
        best = None
        for mul in (0.94, 0.97, 1.0, 1.03, 1.06):
            s = nominal * mul
            tw = max(6, int(round(tgray0.shape[1] * s)))
            th = max(6, int(round(tgray0.shape[0] * s)))
            if tw >= fw or th >= fh:
                continue
            tgray = cv2.resize(tgray0, (tw, th), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
            res = cv2.matchTemplate(gray, tgray, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _ml, loc = cv2.minMaxLoc(res)
            if best is None or mx > best.score:
                best = Match(float(mx), loc[0], loc[1], tw, th, s)
        if best and best.score >= threshold:
            return best
        return None


TB: Optional[TemplateBank] = None  # V5.4：延後到 main() 初始化。


# -----------------------------
# 畫面辨識
# -----------------------------

ANOMALY_KEYWORDS = (
    "連線異常", "连线异常",
    "連線超時", "连线超时",
    "連線逾時", "连线逾时",
    "連線已中斷", "连线已中断",
    "連線中斷", "连线中断",
    "伺服器的連線已中斷", "服务器的连线已中断",
    "帳號在其他地方", "账号在其他地方", "賬號在其他地方",
    "被迫下線", "被迫下线", "下線請注意", "下线请注意",
    "強制登入", "强制登录",
    "返回登入", "返回登录", "返回登錄",
)
CONFIRM_WORDS = ("確定", "确定", "確認", "确认", "是")

# V5.7：當遊戲內的聊天／自動副本等視窗蓋住斷線框時，完整模板可能看不到。
# 這些詞只用來判斷「可能有被遮住的斷線訊息」，不會直接執行登入或點擊確定。
PARTIAL_ANOMALY_KEYWORDS = (
    "您的帳號", "您的账号", "您的賬號", "您的帐号",
    "帳號在其他地方", "账号在其他地方", "賬號在其他地方",
    "被迫下線", "被迫下线", "下線請注意", "下线请注意",
    "返回登入", "返回登录", "返回登錄",
    "伺服器的連線", "服务器的连线", "連線已中斷", "连线已中断",
    "連線超時", "连线超时", "連線逾時", "连线逾时",
)


def norm_text(s: str) -> str:
    return re.sub(r"[\s\u3000,，。．.：:;；!！?？]", "", s or "")


def has_central_dialog(frame: np.ndarray) -> bool:
    """
    低成本斷線候選快篩。

    V6.0 的上限 hr<=0.24 太嚴：半透明斷線框很容易和後方遊戲 UI 的青色元件
    在形態學閉運算後黏成一個較高的輪廓，導致「肉眼明明看到斷線框」卻在快篩
    這一層直接被丟掉。V6.1 放寬高度，但這裡仍只代表「值得做後續模板/OCR」，
    不會因快篩命中就直接點擊，因此放寬不會造成誤按。
    """
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([78, 55, 105]), np.array([112, 255, 255]))
    gate = np.zeros_like(mask)
    gate[int(h * 0.15):int(h * 0.82), int(w * 0.10):int(w * 0.90)] = 255
    mask = cv2.bitwise_and(mask, gate)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        wr, hr = ww / w, hh / h
        cy = (y + hh / 2) / h
        if 0.25 <= wr <= 0.70 and 0.07 <= hr <= 0.62 and 0.20 <= cy <= 0.80:
            return True
    return False


def ocr_disconnect_click(frame: np.ndarray) -> Optional[Tuple[int, int, str]]:
    if OCR is None or not OCR.enabled:
        return None
    h, w = frame.shape[:2]
    # 只讀中央；標題列與右側工具列都不送 OCR。
    x0, y0, x1, y1 = int(w * 0.10), int(h * 0.18), int(w * 0.90), int(h * 0.78)
    items = OCR.read(frame[y0:y1, x0:x1], offset=(x0, y0), priority="low")
    min_score = float(CONFIG.get("OCR最低信心", 0.50))
    good = [it for it in items if it.score >= min_score]
    if not good:
        return None
    joined = "".join(norm_text(it.text) for it in good)
    if not any(norm_text(k) in joined for k in ANOMALY_KEYWORDS):
        return None

    # 優先直接點 OCR 讀到的「確認 / 確定 / 是」。
    candidates = []
    for it in good:
        t = norm_text(it.text)
        if any(norm_text(k) == t or norm_text(k) in t for k in CONFIRM_WORDS):
            cx, cy = it.center
            # 斷線確認鈕通常在中央偏下；排除其他地方同名文字。
            if w * 0.25 <= cx <= w * 0.75 and h * 0.28 <= cy <= h * 0.72:
                candidates.append((cy, cx, it.text))
    if candidates:
        # 取較下方的按鈕文字。
        cy, cx, text = sorted(candidates, reverse=True)[0]
        return int(cx), int(cy), f"異常確認按鈕「{text}」"

    # OCR 有讀到異常文字但沒讀到按鈕：找文字下方的青色小矩形。
    # 這是第二層備援，避免「確認」字因黃字描邊而辨識失敗。
    text_bottom = int(max(float(np.max(it.box[:, 1])) for it in good))
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([75, 60, 95]), np.array([112, 255, 255]))
    cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rects = []
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        if not (w * 0.03 <= ww <= w * 0.25 and h * 0.02 <= hh <= h * 0.10):
            continue
        cx, cy = x + ww / 2, y + hh / 2
        if not (w * 0.30 <= cx <= w * 0.70 and text_bottom - h * 0.03 <= cy <= h * 0.74):
            continue
        area = cv2.contourArea(c)
        rectangularity = area / max(1.0, ww * hh)
        rects.append((abs(cx - w / 2), -rectangularity, int(cx), int(cy)))
    if rects:
        _d, _r, cx, cy = sorted(rects)[0]
        return cx, cy, "異常確認按鈕（畫面矩形備援）"
    return None


def confirm_button_present_near(frame: np.ndarray, x: int, y: int) -> bool:
    """檢查剛才那個「確定/確認/是」按鈕是否還留在原位置，用來驗證背景點擊是否真的生效。"""
    h, w = frame.shape[:2]
    scale = max(0.70, w / 900.0)
    hw = max(45, int(72 * scale))
    hh = max(20, int(28 * scale))
    x0, x1 = max(0, int(x) - hw), min(w, int(x) + hw)
    y0, y1 = max(0, int(y) - hh), min(h, int(y) + hh)
    if x1 - x0 < 20 or y1 - y0 < 12:
        return False
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, np.array([70, 65, 80]), np.array([115, 255, 255]))
    yellow = cv2.inRange(hsv, np.array([15, 85, 135]), np.array([45, 255, 255]))
    cyan = cv2.morphologyEx(cyan, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    cnts, _ = cv2.findContours(cyan, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    roi_area = float(max(1, roi.shape[0] * roi.shape[1]))
    rect_score = 0.0
    for c in cnts:
        xx, yy, ww, hh2 = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        rectangularity = area / max(1.0, ww * hh2)
        if ww >= roi.shape[1] * 0.42 and hh2 >= roi.shape[0] * 0.28 and rectangularity >= 0.40:
            rect_score = max(rect_score, area / roi_area)
    yellow_ratio = float(np.count_nonzero(yellow)) / roi_area
    return rect_score >= 0.12 and yellow_ratio >= 0.002


def has_disconnect_dialog_shape(frame: np.ndarray) -> bool:
    """比 has_central_dialog 更精準的斷線框幾何快篩。

    排除「自動副本」等大型遊戲內視窗，只接受中央偏下、寬約 1/4~3/5 畫面、
    高度約 7%~28% 的青色半透明對話框。
    """
    h, w = frame.shape[:2]
    if h < 120 or w < 180:
        return False
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([76, 48, 80]), np.array([116, 255, 255]))
    gate = np.zeros_like(mask)
    gate[int(h * 0.20):int(h * 0.78), int(w * 0.08):int(w * 0.92)] = 255
    mask = cv2.bitwise_and(mask, gate)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8), iterations=1)
    cnts, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        wr, hr = ww / float(w), hh / float(h)
        cx, cy = (x + ww / 2.0) / w, (y + hh / 2.0) / h
        if 0.24 <= wr <= 0.62 and 0.065 <= hr <= 0.30 and 0.28 <= cx <= 0.72 and 0.32 <= cy <= 0.68:
            return True
    return False


def find_disconnect_confirm_visual(frame: np.ndarray) -> Optional[Match]:
    """低成本找中央斷線確認鈕，不依賴模板縮放。

    斷線「確定/確認/是」共同特徵是：中央偏下、小型青色按鈕、內含明顯黃字。
    這個幾何快篩在 100%/125%/150% DPI 正規化後仍保留，並能排除登入頁的青色按鈕
    （登入按鈕通常不是黃字）。只代表「值得做斷線分類」，不會單獨觸發點擊。
    """
    h, w = frame.shape[:2]
    if h < 120 or w < 180:
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, np.array([75, 70, 90]), np.array([115, 255, 255]))
    yellow = cv2.inRange(hsv, np.array([15, 70, 110]), np.array([45, 255, 255]))
    gate = np.zeros_like(cyan)
    gate[int(h * 0.26):int(h * 0.72), int(w * 0.20):int(w * 0.80)] = 255
    cyan = cv2.bitwise_and(cyan, gate)
    cyan = cv2.morphologyEx(cyan, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    cnts, _ = cv2.findContours(cyan, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_yellow = max(0.003, min(0.020, float(CONFIG.get("斷線視覺確認鈕最低黃字比例", 0.004))))
    best = None
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c)
        wr, hr = ww / float(w), hh / float(h)
        cx, cy = x + ww / 2.0, y + hh / 2.0
        if not (0.045 <= wr <= 0.16 and 0.030 <= hr <= 0.090):
            continue
        if not (0.28 <= cx / w <= 0.72 and 0.36 <= cy / h <= 0.68):
            continue
        aspect = ww / max(1.0, float(hh))
        if not (1.7 <= aspect <= 6.8):
            continue
        area = float(cv2.contourArea(c))
        rectangularity = area / max(1.0, float(ww * hh))
        if rectangularity < 0.30:
            continue
        pad_x = max(1, int(round(ww * 0.08)))
        pad_y = max(1, int(round(hh * 0.08)))
        xx0, yy0 = max(0, x - pad_x), max(0, y - pad_y)
        xx1, yy1 = min(w, x + ww + pad_x), min(h, y + hh + pad_y)
        yr = float(np.count_nonzero(yellow[yy0:yy1, xx0:xx1])) / max(1.0, float((xx1-xx0)*(yy1-yy0)))
        if yr < min_yellow:
            continue
        center_penalty = abs(cx / w - 0.5) * 0.15 + abs(cy / h - 0.53) * 0.10
        score = min(0.999, 0.72 + min(0.20, yr * 2.0) + min(0.08, rectangularity * 0.08) - center_penalty)
        m = Match(float(score), int(x), int(y), int(ww), int(hh), 1.0)
        if best is None or m.score > best.score:
            best = m
    return best


def disconnect_visual_gate(frame: np.ndarray) -> bool:
    return bool(find_disconnect_confirm_visual(frame) is not None or has_central_dialog(frame))


def detect_chat_overlay(frame: np.ndarray) -> Optional[Match]:
    """找遊戲內「聊天」視窗標題。只作遮擋處理，不是斷線判定本身。"""
    return TB.match(
        frame, "遮擋_聊天標題", 0.70,
        roi=(0.18, 0.10, 0.82, 0.55),
        scale_spread=(0.86, 0.90, 0.94, 0.98, 1.00, 1.04, 1.08, 1.12, 1.16),
    )


def partial_disconnect_evidence(frame: np.ndarray) -> str:
    """
    只在完整斷線辨識失敗時使用。
    若其他遊戲內視窗蓋住斷線框，OCR 仍可能讀到「您的賬號」「被迫下線」等殘留文字。
    命中只代表可以先移除已知遮擋視窗；絕不直接當作斷線成功。
    """
    if OCR is None or not OCR.enabled:
        return ""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = int(w * 0.08), int(h * 0.18), int(w * 0.92), int(h * 0.78)
    try:
        items = OCR.read(frame[y0:y1, x0:x1], offset=(x0, y0), priority="low")
    except Exception:
        return ""
    min_score = max(0.34, float(CONFIG.get("OCR最低信心", 0.50)) - 0.12)
    joined = "".join(norm_text(it.text) for it in items if it.score >= min_score)
    for kw in PARTIAL_ANOMALY_KEYWORDS:
        k = norm_text(kw)
        if k and k in joined:
            return kw
    return ""


def close_chat_overlay(hwnd: int, m: Match, note: str) -> bool:
    """點「聊天」標題列自己的紅 X。座標來自當下模板命中位置，不使用桌面固定座標。"""
    # chat_header.png 裁圖尺寸 280x38；紅 X 中心約在 (257, 18)。
    x = m.x + int(round(m.w * (257.0 / 280.0)))
    y = m.y + int(round(m.h * (18.0 / 38.0)))
    return WIO.click(hwnd, x, y, note)


def detect_disconnect(frame: np.ndarray, allow_ocr: bool = True) -> Optional[Tuple[int, int, str]]:
    """V8.1 斷線辨識：統一畫布後以「確認鈕幾何＋整框/文字」雙證據判斷。

    150% DPI 的 raw 畫面縮回邏輯畫布時，半透明整框模板可能因重採樣而掉分；
    但中央青色黃字確認鈕的幾何/色彩仍很穩。它只做候選，必須再有整框模板或斷線文字證據才會點。
    """
    central = has_disconnect_dialog_shape(frame) or has_central_dialog(frame)
    visual_confirm = find_disconnect_confirm_visual(frame)
    confirm_hint = None
    if TB is not None:
        # 統一畫布後只需靠近 1.0 的少量尺度，避免每幀做大量模板縮放。
        confirm_hint = TB.match_absolute(
            frame, "斷線_確定按鈕",
            min(0.80, max(0.74, float(CONFIG.get("斷線確認按鈕快篩最低相似度", 0.78)))),
            roi=(0.14, 0.16, 0.86, 0.80),
            # V8.2：小按鈕只多掃幾個便宜尺度。跨 DPI raw/logical 兩種表示下
            # 實際尺寸可能落在約 0.8 倍；舊版 0.90~1.10 會直接在 OCR 前提早 return。
            scales=(0.72, 0.80, 0.88, 0.96, 1.04, 1.12),
        )
    if not central and visual_confirm is None and confirm_hint is None:
        return None

    known = (
        ("斷線_帳號異地", 0.60, "帳號在其他地方登入"),
        ("斷線_伺服器中斷", 0.59, "與伺服器連線中斷"),
        ("斷線_連線逾時", 0.57, "連線逾時"),
    )
    weak_best = None
    weak_threshold = max(0.30, float(CONFIG.get("斷線弱整框最低相似度", 0.36)))
    for name, threshold, label in known:
        # 統一邏輯畫布下，relative 尺度已由 frame_width/base_width 自動補償視窗大小。
        m = TB.match(frame, name, threshold, roi=(0.08, 0.14, 0.92, 0.82), scale_spread=(0.94, 1.00, 1.06))
        if m:
            click = visual_confirm or confirm_hint
            if click:
                cx, cy = click.center
                return cx, cy, label
            return m.x + m.w // 2, m.y + int(m.h * 0.72), label
        if visual_confirm is not None or confirm_hint is not None:
            weak = TB.match(frame, name, weak_threshold, roi=(0.08, 0.14, 0.92, 0.82), scale_spread=(0.92, 1.00, 1.08))
            if weak and (weak_best is None or weak.score > weak_best[0]):
                weak_best = (weak.score, label)

    if allow_ocr:
        hit = ocr_disconnect_click(frame)
        if hit:
            return hit

    # 確認鈕本身非常明顯 + 整框仍有弱證據時，直接用「實際確認鈕中心」；
    # 不再讓 150% DPI 的重採樣把整框分數卡死在舊門檻。
    click = visual_confirm or confirm_hint
    if click is not None and weak_best is not None:
        if visual_confirm is not None or float(getattr(confirm_hint, "score", 0.0)) >= 0.90:
            cx, cy = click.center
            return cx, cy, f"{weak_best[1]}（確認鈕＋弱整框）"
    return None

def detect_login_force(frame: np.ndarray, allow_ocr: bool = True, fast: bool = False) -> Optional[Match]:
    """直接辨識「強制登入」本體。fast=True 時先用少量尺度快速掃描，失敗再由狀態機定期做完整辨識。"""
    roi = (0.12, 0.52, 0.88, 1.00)
    # 先找較大的上下文，再找按鈕本體。快速路徑只掃常見尺度；完整路徑保留舊版廣尺度相容性。
    if CONFIG.get("統一邏輯畫布", True):
        ctx_rel = (0.96, 1.00, 1.04) if fast else (0.92, 0.96, 1.00, 1.04, 1.08)
        ctx_abs = (0.96, 1.00, 1.04) if fast else (0.90, 0.96, 1.00, 1.04, 1.10)
    else:
        ctx_rel = (0.90, 1.00, 1.10) if fast else (0.78,0.86,0.92,0.98,1.00,1.06,1.14,1.22)
        ctx_abs = (0.88, 1.00, 1.12) if fast else (0.72,0.80,0.88,0.94,1.00,1.06,1.14,1.22,1.30)
    cands = []
    for m in (
        TB.match(frame, "強制登入_畫面區塊", 0.64, roi=roi, scale_spread=ctx_rel),
        TB.match_absolute(frame, "強制登入_畫面區塊", 0.62, roi=roi, scales=ctx_abs),
    ):
        if m:
            cands.append(m)
    if cands:
        return max(cands, key=lambda m: m.score)

    if CONFIG.get("統一邏輯畫布", True):
        force_rel = (0.96, 1.00, 1.04) if fast else (0.92, 0.96, 1.00, 1.04, 1.08)
        force_abs = (0.96, 1.00, 1.04) if fast else (0.90, 0.96, 1.00, 1.04, 1.10)
    else:
        force_rel = (0.90, 1.00, 1.10) if fast else (0.74,0.80,0.86,0.92,0.98,1.00,1.04,1.10,1.18,1.26)
        force_abs = (0.88, 1.00, 1.12) if fast else (0.72,0.80,0.88,0.94,1.00,1.06,1.14,1.22,1.30)
    force_cands = []
    for m in (
        TB.match(frame, "強制登入", 0.56, roi=roi, scale_spread=force_rel),
        TB.match_absolute(frame, "強制登入", 0.54, roi=roi, scales=force_abs),
    ):
        if m:
            force_cands.append(m)
    if force_cands:
        force = max(force_cands, key=lambda m: m.score)
        if CONFIG.get("統一邏輯畫布", True):
            panel_rel = (0.96, 1.00, 1.04) if fast else (0.92, 0.96, 1.00, 1.04, 1.08)
            panel_abs = (0.96, 1.00, 1.04) if fast else (0.90, 0.96, 1.00, 1.04, 1.10)
        else:
            panel_rel = (0.90, 1.00, 1.10) if fast else (0.76,0.84,0.92,1.00,1.08,1.16,1.24)
            panel_abs = (0.88, 1.00, 1.12) if fast else (0.72,0.80,0.88,0.94,1.00,1.06,1.14,1.22,1.30)
        panel = TB.match(frame, "登入頁_雙按鈕面板", 0.52, roi=(0.10,0.48,0.90,1.00), scale_spread=panel_rel)
        panel = panel or TB.match_absolute(frame, "登入頁_雙按鈕面板", 0.50, roi=(0.10,0.48,0.90,1.00), scales=panel_abs)
        if panel or "登入頁_雙按鈕面板" not in TB.data:
            return force

    # 最後只在「流程已預期登入頁」時才會呼叫本函式；模板失敗則用 OCR 直接找實際文字框。
    if allow_ocr and (not fast) and OCR is not None and OCR.enabled:
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = int(w*0.15), int(h*0.52), int(w*0.85), h
        crop = frame[y0:y1, x0:x1]
        if crop.size:
            zoom = 1.5
            enlarged = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
            try:
                items = OCR.read(enlarged, priority="flow")
            except Exception:
                items = []
            best = None
            for it in items:
                txt = norm_text(it.text)
                if ("強制登入" not in txt and "强制登录" not in txt) or it.score < 0.36:
                    continue
                cx, cy = it.center
                cx = int(round(x0 + cx/zoom)); cy = int(round(y0 + cy/zoom))
                cand = Match(float(it.score), cx-30, cy-12, 60, 24, 1.0)
                if best is None or cand.score > best.score:
                    best = cand
            if best:
                return best
    return None

def detect_line_header(frame: np.ndarray, fast: bool = False) -> Optional[Match]:
    """辨識線路面板標題；fast=True 先用常見尺度快速掃描，完整模式保留不同 DPI 相容性。"""
    threshold = max(0.50, min(0.90, float(CONFIG.get("線路畫面標題最低相似度", 0.58))))
    roi = (0.00, 0.00, 1.00, 0.86)
    if CONFIG.get("統一邏輯畫布", True):
        rel = (0.96, 1.00, 1.04) if fast else (0.92, 0.96, 1.00, 1.04, 1.08)
        abss = (0.96, 1.00, 1.04) if fast else (0.90, 0.96, 1.00, 1.04, 1.10)
    else:
        rel = (0.90, 1.00, 1.10) if fast else (0.74, 0.80, 0.86, 0.92, 0.98, 1.00, 1.04, 1.10, 1.18, 1.26)
        abss = (0.88, 1.00, 1.12) if fast else (0.72, 0.80, 0.88, 0.94, 1.00, 1.06, 1.14, 1.22, 1.30, 1.38)
    m1 = TB.match(
        frame, "最近一次登錄資訊_標題", threshold, roi=roi, scale_spread=rel,
    )
    m2 = TB.match_absolute(
        frame, "最近一次登錄資訊_標題", max(0.50, threshold - 0.03), roi=roi, scales=abss,
    )
    if m1 and m2:
        return m1 if m1.score >= m2.score else m2
    return m1 or m2

def line_panel_roi(frame: np.ndarray, header: Match) -> Tuple[float, float, float, float]:
    """由已實際辨識到的標題限定「線路面板」搜尋區域。

    這裡只拿來縮小後續圖片/OCR搜尋範圍；實際點擊仍必須命中按鈕圖片或 OCR 文字框，
    不會把推算位置直接當點擊位置。
    """
    h, w = frame.shape[:2]
    s = float(header.scale or 1.0)
    x0 = max(0, int(round(header.x - 58 * s)))
    y0 = max(0, int(round(header.y - 8 * s)))
    x1 = min(w, int(round(header.x + header.w + 58 * s)))
    y1 = min(h, int(round(header.y + 330 * s)))
    if x1 <= x0 or y1 <= y0:
        return (0.0, 0.0, 1.0, 1.0)
    return (x0 / w, y0 / h, x1 / w, y1 / h)


def read_recent_line_detail(frame: np.ndarray, header: Match) -> Tuple[Optional[int], float, str]:
    """回傳（線路, OCR信心, 原始文字）。高信心可在 V6.3 單次通過，低信心仍多次投票。"""
    if OCR is None or not OCR.enabled:
        return None, 0.0, ""
    s = header.scale
    panel_left = int(round(header.x - 43 * s))
    panel_top = int(round(header.y - 1 * s))
    x0 = max(0, int(panel_left + 15 * s))
    y0 = max(0, int(panel_top + 15 * s))
    x1 = min(frame.shape[1], int(panel_left + 215 * s))
    y1 = min(frame.shape[0], int(panel_top + 48 * s))
    if x1 <= x0 or y1 <= y0:
        return None, 0.0, ""
    items = OCR.read(frame[y0:y1, x0:x1], offset=(x0, y0), priority="flow")
    min_score = float(CONFIG.get("OCR最低信心", 0.50))
    valid = [it for it in items if it.score >= min_score]
    text = "".join(it.text for it in valid)
    compact = text.replace(" ", "")
    m = re.search(r"(?:線路|线路)\s*[:：]?\s*([1-8])", compact)
    if m:
        scores = [float(it.score) for it in valid if m.group(1) in norm_text(it.text)]
        conf = max(scores) if scores else (min((float(it.score) for it in valid), default=0.0))
        return int(m.group(1)), conf, compact
    nums = []
    for it in valid:
        t = norm_text(it.text).replace(" ", "")
        found = re.findall(r"(?<!\d)([1-8])(?!\d)", t)
        for n in found:
            nums.append((int(n), float(it.score)))
    unique = {n for n, _ in nums}
    if len(unique) == 1:
        n = next(iter(unique))
        conf = max(score for num, score in nums if num == n)
        return n, conf, compact
    return None, 0.0, compact


def read_recent_line(frame: np.ndarray, header: Match) -> Optional[int]:
    return read_recent_line_detail(frame, header)[0]


def find_line_button(frame: np.ndarray, line_no: int, header: Optional[Match] = None) -> Optional[Match]:
    """直接找指定線路按鈕；V6.2 同時使用視窗比例尺度與固定像素尺度。"""
    if line_no not in range(1, 9):
        return None
    name = f"線路按鈕_{line_no}"
    if name not in TB.data:
        return None
    threshold = max(0.50, min(0.95, float(CONFIG.get("線路按鈕模板最低相似度", 0.54))))
    roi = line_panel_roi(frame, header) if header is not None else (0.00, 0.00, 1.00, 1.00)
    m1 = TB.match(
        frame, name, threshold, roi=roi,
        scale_spread=((0.92, 0.96, 1.00, 1.04, 1.08) if CONFIG.get("統一邏輯畫布", True) else (0.74, 0.80, 0.86, 0.92, 0.98, 1.00, 1.04, 1.10, 1.18, 1.26)),
    )
    m2 = TB.match_absolute(
        frame, name, max(0.50, threshold - 0.02), roi=roi,
        scales=((0.90, 0.96, 1.00, 1.04, 1.10) if CONFIG.get("統一邏輯畫布", True) else (0.72, 0.80, 0.88, 0.94, 1.00, 1.06, 1.14, 1.22, 1.30)),
    )
    if m1 and m2:
        return m1 if m1.score >= m2.score else m2
    return m1 or m2

def find_line_button_ocr(frame: np.ndarray, header: Match, line_no: int) -> Optional[Tuple[int, int, str]]:
    """圖片模板沒命中時，直接在已確認的線路面板內讀按鈕文字。

    這是 V6.0 的第二條路徑，目的是避免不同縮放/反鋸齒讓小型按鈕模板失敗。
    OCR 找到的是實際文字框，點擊也是文字框中心，不使用固定螢幕座標。
    """
    if OCR is None or not OCR.enabled or line_no not in range(1, 9):
        return None
    h, w = frame.shape[:2]
    rx0, ry0, rx1, ry1 = line_panel_roi(frame, header)
    x0, y0 = int(rx0 * w), int(ry0 * h)
    x1, y1 = int(rx1 * w), int(ry1 * h)
    if x1 <= x0 or y1 <= y0:
        return None
    crop = frame[y0:y1, x0:x1]
    # 小字放大後再讀，只有在選線階段才執行。
    zoom = 1.7
    enlarged = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
    items = OCR.read(enlarged, priority="flow")
    min_score = max(0.35, float(CONFIG.get("OCR最低信心", 0.50)) - 0.10)
    wanted = {
        1: ("麻布老虎1線", "麻布老虎1", "1線"),
        2: ("公會專線", "公会专线", "公會", "公会"),
        3: ("麻布老虎3線", "麻布老虎3", "3線"),
        4: ("麻布老虎4線", "麻布老虎4", "4線"),
        5: ("麻布老虎5線", "麻布老虎5", "5線"),
        6: ("麻布老虎6線", "麻布老虎6", "6線"),
        7: ("麻布老虎7線", "麻布老虎7", "7線"),
        8: ("郵寄拍賣專線", "邮寄拍卖专线", "郵寄", "拍賣", "邮寄", "拍卖"),
    }[line_no]
    best = None
    for it in items:
        if it.score < min_score:
            continue
        txt = norm_text(it.text)
        compact = re.sub(r"\s+", "", txt)
        if not any(k in compact for k in wanted):
            continue
        cx2, cy2 = it.center
        cx = int(round(x0 + cx2 / zoom))
        cy = int(round(y0 + cy2 / zoom))
        cand = (float(it.score), cx, cy, compact)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return None
    return int(best[1]), int(best[2]), str(best[3])


def line_click_point(header: Match, line_no: int) -> Tuple[int, int]:
    """舊版相對座標函式保留給相容性檢查，但 V5.9 主流程不再使用。"""
    s = header.scale
    panel_left = header.x - 43 * s
    panel_top = header.y - 1 * s
    x = int(round(panel_left + 109 * s))
    y = int(round(panel_top + (54 + 34 * (line_no - 1)) * s))
    return x, y


def detect_character_screen(frame: np.ndarray) -> Optional[Match]:
    roi = (0.12, 0.38, 0.88, 0.82)
    if CONFIG.get("統一邏輯畫布", True):
        rel = (0.94, 1.00, 1.06)
        abss = (0.92, 1.00, 1.08)
    else:
        rel = (0.78,0.86,0.92,0.98,1.00,1.06,1.14,1.22)
        abss = (0.68,0.76,0.84,0.92,1.00,1.08,1.18,1.30,1.42,1.55)
    a = TB.match(frame, "角色選擇_標題", 0.68, roi=roi, scale_spread=rel)
    if a:
        return a
    return TB.match_absolute(frame, "角色選擇_標題", 0.66, roi=roi, scales=abss)

def first_meaningful_char(text: str) -> str:
    """取得角色名稱的第一個有效字元。

    角色選擇畫面會把長名字截成「嘻の...」之類，因此 V5.6 不再要求完整名稱。
    設定仍保存完整角色名，但實際選角以第一個有效字元為主。
    """
    t = norm_text(text)
    for ch in t:
        # 忽略 OCR 偶爾加在前面的裝飾符號／括號，只取第一個字母、數字或中日韓文字。
        cat = unicodedata.category(ch)
        if cat and cat[0] in ("L", "N"):
            return ch
    return ""


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def find_role(frame: np.ndarray, role_name: str, title: Match) -> Tuple[Optional[Tuple[int, int, str]], str]:
    """在角色卡名稱列用 OCR 找角色。

    規則：
    1. 完整設定名稱只用來保存與輔助消歧。
    2. 第一個有效字元是必要主條件，例如「嘻の百二補師」只要求畫面名稱第一字為「嘻」。
    3. 若同時有兩張卡第一字相同，才用畫面上仍可見的後續字元做次要消歧。
    4. 找不到或無法唯一判定時不點任何角色，絕不再選第一格。
    """
    target = norm_text(role_name)
    key = first_meaningful_char(role_name)
    if not key:
        return None, "未設定優先角色"
    if OCR is None or not OCR.enabled:
        return None, f"OCR未啟用，無法辨識角色首字「{key}」"

    h, w = frame.shape[:2]
    # 以「遊戲角色選擇」標題為基準，只讀角色卡最上方的名字列。
    # 這不是猜按鈕座標：title 本身是當下畫面模板匹配出的實際位置。
    s = float(title.scale or 1.0)
    x0 = max(0, int(round(title.x - 115 * s)))
    x1 = min(w, int(round(title.x + title.w + 115 * s)))
    y0 = max(0, int(round(title.y + 29 * s)))
    y1 = min(h, int(round(title.y + 73 * s)))
    if x1 <= x0 or y1 <= y0:
        return None, f"角色名稱區域無效（首字「{key}」）"

    # 名字字體很小，只在進入角色畫面時把這一小條放大後 OCR；正常監看完全不跑這段。
    crop = frame[y0:y1, x0:x1]
    zoom = 2.0
    enlarged = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
    items = OCR.read(enlarged, priority="flow")
    min_score = float(CONFIG.get("OCR最低信心", 0.50))

    candidates = []
    for it in items:
        if it.score < min_score:
            continue
        shown = norm_text(it.text).replace("…", "").replace("...", "")
        if first_meaningful_char(shown) != key:
            continue
        cx2, cy2 = it.center
        cx = int(round(x0 + cx2 / zoom))
        cy = int(round(y0 + cy2 / zoom))
        # 點在同一張角色卡的中央高度，而不是文字本身；X 仍由實際 OCR 名稱位置決定。
        click_y = int(round(title.y + 65 * s))
        click_y = max(1, min(h - 2, click_y))
        click_x = max(1, min(w - 2, cx))
        prefix = common_prefix_len(target, shown) if target and shown else 0
        candidates.append((click_x, click_y, it.score, prefix, shown, cy))

    if not candidates:
        return None, f"找不到角色首字「{key}」"

    # 同一個 OCR 文字偶爾會被拆成重疊結果，先依 X 距離合併成角色卡候選。
    candidates.sort(key=lambda c: c[0])
    merged = []
    merge_gap = max(24, int(round(55 * s)))
    for c in candidates:
        if merged and abs(c[0] - merged[-1][0]) <= merge_gap:
            # 同一卡保留完整名稱前綴較長者，其次保留 OCR 信心較高者。
            if (c[3], c[2]) > (merged[-1][3], merged[-1][2]):
                merged[-1] = c
        else:
            merged.append(c)

    if len(merged) == 1:
        c = merged[0]
        return (c[0], c[1], f"首字辨識「{key}」：{c[4]}"), ""

    # 第一字相同時，以畫面仍看得到的共同前綴做次要消歧；沒有唯一優勝者就停止，不亂點。
    ranked = sorted(merged, key=lambda c: (c[3], c[2]), reverse=True)
    if ranked[0][3] >= 2 and ranked[0][3] > ranked[1][3]:
        c = ranked[0]
        return (c[0], c[1], f"首字「{key}」＋可見前綴：{c[4]}"), ""

    return None, f"角色首字「{key}」有 {len(merged)} 個候選，無法唯一判定"


def detect_enter_game(frame: np.ndarray) -> Optional[Match]:
    roi = (0.12, 0.62, 0.62, 1.00)
    if CONFIG.get("統一邏輯畫布", True):
        rel = (0.94, 1.00, 1.06)
        abss = (0.92, 1.00, 1.08)
    else:
        rel = (0.78,0.86,0.92,0.98,1.00,1.06,1.14,1.22)
        abss = (0.68,0.76,0.84,0.92,1.00,1.08,1.18,1.30,1.42,1.55)
    a = TB.match(frame, "進入遊戲", 0.69, roi=roi, scale_spread=rel)
    if a:
        return a
    return TB.match_absolute(frame, "進入遊戲", 0.67, roi=roi, scales=abss)

def find_popup_close(frame: np.ndarray) -> Optional[Tuple[int, int, str]]:
    # 視窗縮放/DPI 下同時走相對尺度與固定像素尺度。
    roi = (0.52, 0.00, 0.98, 0.28)
    best = None
    labels = {
        "彈窗關閉X_自動副本": "關閉自動副本彈窗",
        "彈窗關閉X_日常活動": "關閉日常活動彈窗",
    }
    for name in ("彈窗關閉X_通用面板", "彈窗關閉X_自動副本", "彈窗關閉X_日常活動"):
        cands = [
            TB.match(frame, name, 0.70, roi=roi, scale_spread=(0.78,0.86,0.92,0.98,1.00,1.06,1.14,1.22)),
            TB.match_absolute(frame, name, 0.68, roi=roi, scales=(0.68,0.76,0.84,0.92,1.00,1.08,1.18,1.30,1.42,1.55)),
        ]
        for m in cands:
            if m and (best is None or m.score > best[0].score):
                best = (m, name)
    if best:
        m, matched_name = best
        return m.center[0], m.center[1], labels.get(matched_name, "關閉遊戲彈窗")
    return None


def find_unexpected_panel_close(frame: np.ndarray) -> Optional[Tuple[int, int, str]]:
    """Find the standard red circular X used by ordinary in-game panels.

    This deliberately searches only inside the Flash client's upper panel area.
    It cannot reach the Windows title-bar close button and it excludes the
    bottom-right auto-battle X state.  Existing user-supplied close-X images,
    including the narrower manor/pet-panel control, are reused as evidence.
    """
    if frame is None or frame.size == 0 or TB is None:
        return None
    h, w = frame.shape[:2]
    roi = (0.08, 0.01, 0.96, 0.62)
    best: Optional[Match] = None
    for name in ("彈窗關閉X_通用面板", "彈窗關閉X_自動副本", "彈窗關閉X_日常活動"):
        for match in (
            TB.match(frame, name, 0.66, roi=roi, scale_spread=(0.78, 0.86, 0.92, 0.98, 1.00, 1.06, 1.14, 1.22)),
            TB.match_absolute(frame, name, 0.64, roi=roi, scales=(0.68, 0.76, 0.84, 0.92, 1.00, 1.08, 1.18, 1.30, 1.42, 1.55)),
        ):
            if match and (best is None or match.score > best.score):
                best = match
    if best is None:
        return None

    # Template matching is grayscale; require the matched live patch to still
    # contain the red fill of the actual close control before allowing a click.
    x0, y0 = max(0, best.x), max(0, best.y)
    x1, y1 = min(w, best.x + best.w), min(h, best.y + best.h)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    blue, green, red = cv2.split(patch)
    red_ratio = float(((red > 95) & (red > green * 1.18) & (red > blue * 1.12)).mean())
    if red_ratio < 0.10:
        return None
    return best.center[0], best.center[1], f"關閉非預期遊戲面板（X={best.score:.3f}/紅={red_ratio:.2f}）"


def find_manor_action_bar(frame: np.ndarray) -> Optional[Match]:
    """Positive evidence that the visible panel is the manor, not a generic panel."""
    if frame is None or frame.size == 0 or TB is None:
        return None
    roi = (0.05, 0.66, 0.96, 0.99)
    matches = (
        TB.match(
            frame,
            "莊園_操作列",
            0.72,
            roi=roi,
            scale_spread=(0.78, 0.86, 0.92, 0.98, 1.0, 1.06, 1.14, 1.22),
        ),
        TB.match_absolute(
            frame,
            "莊園_操作列",
            0.70,
            roi=roi,
            scales=(0.68, 0.76, 0.84, 0.92, 1.0, 1.08, 1.18, 1.30, 1.42, 1.55),
        ),
    )
    return max((match for match in matches if match), key=lambda match: match.score, default=None)


def find_fishing_state_button(frame: np.ndarray, state: str) -> Optional[Match]:
    """Locate only an evidenced 飛行/降落 button in the right-side action column."""
    if frame is None or frame.size == 0 or TB is None:
        return None
    roi = (0.82, 0.55, 1.0, 0.90)
    if state == "飛行":
        # The current live crop is primary evidence. The older supplied crop is
        # retained at the measured-compatible threshold for other render paths.
        specifications = (("釣魚前置_飛行_目前實圖", 0.78), ("釣魚前置_飛行", 0.62))
    elif state == "降落":
        specifications = (
            ("釣魚前置_降落_120福實圖", 0.78),
            ("釣魚前置_降落", 0.62),
        )
    else:
        return None
    found: list[Match] = []
    for name, threshold in specifications:
        for match in (
            TB.match(
                frame,
                name,
                threshold,
                roi=roi,
                scale_spread=(0.78, 0.86, 0.92, 0.98, 1.0, 1.06, 1.14, 1.22),
            ),
            TB.match_absolute(
                frame,
                name,
                threshold,
                roi=roi,
                scales=(0.78, 0.86, 0.92, 0.98, 1.0, 1.06, 1.14, 1.22),
            ),
        ):
            if match is not None:
                found.append(match)
    return max(found, key=lambda match: match.score, default=None)


def find_fishing_state_buttons_ocr(frame: np.ndarray) -> Dict[str, Match]:
    """Read 飛行/降落 text only inside the evidenced right action column."""
    if frame is None or frame.size == 0:
        return {}
    h, w = frame.shape[:2]
    items = _ocr_frame_region(
        frame,
        int(round(w * 0.82)),
        int(round(h * 0.52)),
        w,
        int(round(h * 0.92)),
        priority="low",
    )
    found: Dict[str, Match] = {}
    for item in items:
        if float(item.score) < 0.45:
            continue
        text = normalize_game_text(item.text).replace("飛", "飞")
        state = "飛行" if "飞行" in text else ("降落" if "降落" in text else "")
        if not state:
            continue
        x0, y0, x1, y1 = _ocr_box_bounds(item)
        match = Match(float(item.score), x0, y0, max(2, x1 - x0), max(2, y1 - y0), 1.0)
        previous = found.get(state)
        if previous is None or match.score > previous.score:
            found[state] = match
    return found


def find_fishing_menu_collapse(frame: np.ndarray) -> Optional[Match]:
    """Locate the evidenced arrow beside the expanded right action column."""
    if frame is None or frame.size == 0 or TB is None:
        return None
    # Expanded menu toggle is around x=823 on a 900px canvas.  Never include
    # the collapsed left-arrow at x≈890 or it will immediately reopen the menu.
    roi = (0.84, 0.30, 0.94, 0.78)
    return max(
        (
            TB.match(frame, "釣魚前置_收回系統列_目前實圖", 0.80, roi=roi),
            TB.match(frame, "釣魚前置_收回系統列", 0.74, roi=roi),
        ),
        key=lambda value: value.score if value else -1.0,
    )


def find_fishing_menu_still_expanded(frame: np.ndarray) -> Optional[Match]:
    """Verify expansion by the right-arrow position, for every game window."""
    return find_fishing_menu_collapse(frame)


def find_fishing_menu_expand(frame: np.ndarray) -> Optional[Match]:
    """Locate the evidenced left arrow shown only while the right menu is collapsed."""
    if frame is None or frame.size == 0 or TB is None:
        return None
    return TB.match(
        frame,
        "釣魚前置_展開系統列_120福實圖",
        0.82,
        roi=(0.94, 0.30, 1.0, 0.62),
    )


def auto_battle_state(frame: np.ndarray, allow_ocr: bool = False) -> Tuple[str, Optional[Match]]:
    """Return the user-defined visual state in the bottom-right button ROI.

    Correct means a positive match for the red 「自動戰鬥」 text button and a
    simultaneous negative match for the crossed-weapons X icon.  If both look
    plausible, X wins conservatively; fishing must never start from ambiguity.
    """
    if frame is None or frame.size == 0:
        return "未知", None
    h, w = frame.shape[:2]
    # 真正按鈕長期位於最右側、畫面下半部。此 ROI 可涵蓋 900x568 / 900x590
    # 與一般視窗縮放，同時排除技能列、背包與中央圖示。
    roi = (0.875, 0.66, 1.00, 0.98)
    no_x_threshold = max(0.62, float(CONFIG.get("自動戰鬥無X最低相似度", 0.68)))
    x_threshold = max(0.62, float(CONFIG.get("自動戰鬥有X最低相似度", 0.68)))
    rel_scales = (0.90, 0.95, 1.00, 1.05, 1.10)
    abs_scales = (0.82, 0.88, 0.94, 1.00, 1.06, 1.12, 1.20)

    def best_of(names: Tuple[str, ...]) -> Optional[Match]:
        best: Optional[Match] = None
        for name in names:
            if name not in TB.data:
                continue
            for m in (
                TB.match(frame, name, 0.0, roi=roi, scale_spread=rel_scales),
                TB.match_absolute(frame, name, 0.0, roi=roi, scales=abs_scales),
            ):
                if m and (best is None or m.score > best.score):
                    best = m
        return best

    no_x = best_of((
        "自動戰鬥_無X正確_使用者",
        "自動戰鬥_目標_使用者裁切",
        "自動戰鬥_目標_完整樣本",
        "自動戰鬥_目標",
    ))
    has_x = best_of((
        "自動戰鬥_有X錯誤_使用者",
        "自動戰鬥_另一狀態_最新",
        "自動戰鬥_另一狀態",
    ))
    no_x_score = no_x.score if no_x else -1.0
    x_score = has_x.score if has_x else -1.0

    # Both buttons share the same cyan frame, so even the wrong template can
    # score around 0.7 from the border alone.  Require the winning interior to
    # beat the competing state by a clear margin; a close result is unknown and
    # blocks fishing instead of guessing.
    margin = 0.10
    if has_x and x_score >= x_threshold and (x_score >= 0.90 or x_score >= no_x_score + margin):
        return "有X錯誤", has_x
    if no_x and no_x_score >= no_x_threshold and (no_x_score >= 0.90 or no_x_score >= x_score + margin):
        return "無X正確", no_x

    # The supplied whole-game screenshot has a different Flash scale where the
    # shared frame makes both visual scores close.  Only in that ambiguous case,
    # and only when the caller's throttle permits, use the text inside this tiny
    # bottom-right ROI as positive no-X evidence.  A decisive X returned above
    # can never be overridden by OCR.
    if allow_ocr and OCR is not None and OCR.enabled:
        x0, y0 = int(w * 0.82), int(h * 0.72)
        items = _ocr_frame_region(frame, x0, y0, w, int(h * 0.98), priority="flow")
        for item in items:
            text = normalize_game_text(item.text)
            if not ("自動" in text or "自动" in text) or item.score < 0.72:
                continue
            bx0, by0, bx1, by1 = _ocr_box_bounds(item)
            match = Match(
                float(item.score),
                int(bx0),
                int(by0),
                max(1, int(bx1 - bx0)),
                max(1, int(by1 - by0)),
                1.0,
            )
            return "無X正確", match
    return "未知", None


def is_battle_scene(frame: np.ndarray) -> bool:
    """
    戰鬥場景只在「已偵測到斷線」時才判斷，正常監看完全不增加固定負擔。
    目前依使用者提供的戰鬥畫面，右上角會固定存在「自動戰鬥」標題列。
    只比對標題列，不包含動態回合數，因此穩定且成本低。
    """
    threshold = float(CONFIG.get("戰鬥場景模板最低相似度", 0.82))
    m = TB.match(
        frame,
        "戰鬥場景_自動戰鬥標題",
        threshold,
        roi=(0.66, 0.00, 1.00, 0.26),
        scale_spread=(0.88, 0.94, 1.00, 1.06, 1.12),
    )
    return m is not None


# -----------------------------
# 單一遊戲視窗狀態機
# -----------------------------

LINE_NAMES = {
    1: "一線",
    2: "公會專線（二線）",
    3: "三線",
    4: "四線",
    5: "五線",
    6: "六線",
    7: "七線",
    8: "郵寄拍賣專線（八線）",
}
FIXED_LINE_FALLBACK_NO = 1
FIXED_LINE_RETRY_BACKOFF_SECONDS = 1.0

class FlowCoordinator:
    """只負責排程提示：重連中的視窗優先取得掃描機會。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.active: set[int] = set()
        self.last_started = 0.0

    def activate(self, token: int):
        with self.lock:
            self.active.add(int(token))
            self.last_started = time.monotonic()

    def deactivate(self, token: int):
        with self.lock:
            self.active.discard(int(token))

    def is_active(self, token: int) -> bool:
        with self.lock:
            return int(token) in self.active

    def any_active(self) -> bool:
        with self.lock:
            return bool(self.active)

    def in_discovery_burst(self) -> bool:
        burst = max(0.5, float(CONFIG.get("同時斷線發現加速秒", 2.8)))
        with self.lock:
            return bool(self.active) and (time.monotonic() - self.last_started) < burst


FLOW_COORDINATOR = FlowCoordinator()


# 戰鬥斷線重新啟動時，序列化捷徑啟動，避免同時新開多個同標題視窗而綁錯。
RELAUNCH_LOCK = threading.Lock()
RELAUNCH_GATE = threading.Event()
IGNORED_HWND_LOCK = threading.Lock()
IGNORED_HWND: set[int] = set()


def is_ignored_hwnd(hwnd: int) -> bool:
    with IGNORED_HWND_LOCK:
        return hwnd in IGNORED_HWND


def mark_ignored_hwnd(hwnd: int):
    with IGNORED_HWND_LOCK:
        IGNORED_HWND.add(hwnd)


def launch_shortcut_no_activate(path: Path) -> bool:
    """
    用 Windows ShellExecute 直接啟動捷徑，不需要真的切到桌面點圖示。
    SW_SHOWNOACTIVATE=4：要求新視窗顯示但不要搶目前焦點。
    部分舊版 Flash 本身可能忽略此提示，但監測程式不會主動切前景。
    """
    try:
        # V11：每次由智慧重連啟動捷徑前，先對捷徑目標套用「Application High DPI」相容層。
        # 這是程序建立前的修正；不再等視窗出現後用座標補丁硬救。
        try:
            target_exe = dpi_policy.resolve_shortcut_target(str(path))
            if target_exe:
                # V11.7：戰鬥重開也遵守 V11.6 原則，只在未設定 DPI 相容層時補上；
                # 使用者已有的 DPI 設定一律保留。
                dpi_policy.ensure_high_dpi_aware_if_unset(target_exe)
        except Exception as e:
            LOG.warning("捷徑 DPI 統一準備失敗：%s；%s", path, e)
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        shell_execute.restype = ctypes.c_void_p
        rc = shell_execute(
            None,
            "open",
            str(path),
            None,
            str(path.parent),
            4,  # SW_SHOWNOACTIVATE
        )
        code = int(rc or 0)
        if code <= 32:
            raise OSError(f"ShellExecute 回傳 {code}")
        return True
    except Exception as e:
        LOG.error("背景啟動捷徑失敗：%s；%s", path, e)
        return False


_MONITOR_CAPTURE_PACE_LOCK = threading.Lock()
_MONITOR_CAPTURE_NEXT_AT = 0.0

def wait_monitor_capture_slot(stop_event: threading.Event) -> None:
    """只限制「正常監看」的 PrintWindow 總頻率。

    多視窗時舊版雖然一次只擷取一個，但 10 個 worker 會無縫接力，CPU 幾乎沒有空檔。
    這裡在所有正常監看 worker 之間留出全域間隔；一旦進入重連流程就完全不套此限制，
    所以固定 20 秒、選線、選角、自動戰鬥等既有高速流程不降速。
    """
    global _MONITOR_CAPTURE_NEXT_AT
    gap = max(0.08, float(CONFIG.get("全域監看擷取最短間隔秒", 0.20)))
    with _MONITOR_CAPTURE_PACE_LOCK:
        now = time.monotonic()
        reserved = max(now, _MONITOR_CAPTURE_NEXT_AT)
        _MONITOR_CAPTURE_NEXT_AT = reserved + gap
        delay = max(0.0, reserved - now)
    if delay > 0.0 and not stop_event.is_set():
        stop_event.wait(delay)


class GameWorker(threading.Thread):
    def __init__(self, hwnd: int, profile: dict, stop_event: threading.Event, pause_event: threading.Event):
        super().__init__(daemon=True)
        self.hwnd = hwnd
        self.base_profile = dict(profile)
        self.profile = dict(profile)
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.state = "監看"
        self.state_since = time.monotonic()
        self.force_clicked_at = 0.0
        self.force_retry_count = 0
        self.force_lock_until = 0.0
        self.force_wait_until = 0.0
        self.force_post_wait_until = 0.0
        self.force_login_seen_frames = 0
        self.pending_force_mode: Optional[str] = None
        self.pending_force_root: bool = False
        # V8.8：強制登入的背景輸入模式必須按「這個動作」獨立驗證，
        # 不可直接沿用斷線確認或其他按鈕的舊校準結果。
        self.force_tried_routes: List[Tuple[str, bool]] = []
        self.force_transport: str = ""
        self.line_seen_frames = 0
        self.role_seen_frames = 0
        self.line_ocr_attempts = 0
        self.line_ocr_votes: List[int] = []
        self.line_ocr_next_at = 0.0
        self.selected_line_no = 0
        # 0=沿用舊版「讀最近登入線路」；1..8=此角色固定線路。
        self.preferred_line_no = 0
        # 三項功能彼此獨立。舊綁定沒有欄位時維持既有行為：
        # 自動重連預設開啟、莊園預設關閉、釣魚依是否已有魚級設定決定。
        self.reconnect_enabled = True
        self.manor_enabled = False
        self.fishing_enabled = False
        self.line_click_at = 0.0
        self.line_click_attempts = 0
        self.role_wait_since = 0.0
        self.role_ocr_next_at = 0.0
        self.role_click_at = 0.0
        self.role_click_point: Optional[Tuple[int, int]] = None
        self.enter_click_at = 0.0
        self.enter_click_attempts = 0
        self.enter_input_exhausted = False
        self.last_enter_verify_at = 0.0
        self.enter_clear_frames = 0
        self.enter_transition_started_at = 0.0
        self.last_in_game_evidence_probe = 0.0
        self.entered_at = 0.0
        self.flow_started_at = 0.0
        self.flow_stage_durations: Dict[str, float] = {}
        self.last_flow_duration = 0.0
        self.last_flow_result = ""
        self.last_flow_finished_wall = 0.0
        # 僅供控制台顯示「上次斷線幾點幾分」；不參與重連判斷與任何輸入行為。
        self.last_disconnect_wall = 0.0
        self.last_disconnect_ocr_at = 0.0
        self.warmup_until = time.monotonic() + max(0.5, float(CONFIG.get("啟動自我檢查秒", 2.0)))
        self.warmup_good_frames = 0
        self.warmup_done = False
        self.last_debug_save_at = 0.0
        # 啟動後短時間允許從已停在登入頁的視窗接手；之後「監看」狀態只跑低成本斷線檢查。
        self.startup_login_probe_until = time.monotonic() + 8.0
        self.last_disconnect_click = 0.0
        self.disconnect_attempt = 0
        self.last_popup_click = 0.0
        self.last_unexpected_panel_click = 0.0
        self.last_auto_toggle = 0.0
        self.auto_toggle_attempts = 0
        self.auto_settle_until = 0.0
        self.post_popup_clear_frames = 0
        self.post_popup_quiet_since = 0.0
        self.post_popup_seen: set[str] = set()
        self.auto_target_seen_frames = 0
        self.auto_x_seen_frames = 0
        self.auto_correction_pending = False
        self.auto_correction_started_at = 0.0
        self.last_auto_guard_warning_at = 0.0
        self.last_auto_ocr_at = 0.0
        self.last_auto_no_x_at = 0.0
        self.last_post_entry_warning_at = 0.0
        self.fishing_prerequisites_ready = False
        self.last_occlusion_probe = 0.0
        self.last_flow_disconnect_probe = 0.0
        # V11.7：只保留「上一個沒有斷線彈窗的監看幀」引用。
        # 斷線當下才做戰鬥模板判定，因此正常城鎮監看不增加任何戰鬥模板成本。
        self.last_clean_monitor_frame: Optional[np.ndarray] = None
        self.last_clean_monitor_frame_at = 0.0
        self.last_login_deep_probe = 0.0
        self.last_line_deep_probe = 0.0
        self.cached_line_header: Optional[Match] = None
        self.cached_role_title: Optional[Match] = None
        self.flow_capture_seconds = 0.0
        self.flow_step_seconds = 0.0
        self.flow_frame_count = 0
        self.last_perf_summary = ""
        self.dpi_block_logged = False
        self.dpi_block_exe = ""
        self.dpi_virtualized = False
        # 只有真實畫面驗證證明背景輸入無效、前景實體點擊成功後才會設為 True。
        # 一旦設定，同一個 Flash 視窗後續選線／選角／進入遊戲也必須使用同一有效輸入方式。
        self.foreground_input_required = False
        self.next_minimized_probe_at = 0.0
        self.last_minimized_probe_wall = 0.0
        self.restore_minimized_after_flow = False
        self.minimized_probe_logged = False
        self.last_minimized_log_at = 0.0
        self.minimized_paused_since = 0.0
        self.minimized_paused = False
        self.fishing_profile_id = ""
        self.fishing_progress_key = ""
        self.fishing_profile: Optional[dict] = None
        self.fishing_phase = "停用"
        self.fishing_prepare_deadline = 0.0
        self.fishing_chat_then_select = False
        self.fishing_chat_clear_attempts = 0
        self.fishing_menu_collapse_attempts = 0
        self.fishing_menu_expand_attempts = 0
        self.fishing_menu_expand_next_at = 0.0
        self.fishing_message_group_index = 0
        self.fishing_message_groups: List[str] = []
        self.fishing_link_index = 0
        self.fishing_links: List[dict] = []
        self.fishing_link_points: List[Tuple[int, int]] = []
        self.fishing_send_at = 0.0
        self.fishing_locate_deadline = 0.0
        self.fishing_next_locate_at = 0.0
        self.fishing_link_locate_timeouts = 0
        self.fishing_deadline = 0.0
        self.fishing_next_status_check = 0.0
        self.fishing_missing_checks = 0
        self.fishing_round = 0
        self.fishing_last_route = ""
        self.fishing_current_tab_deadline = 0.0
        self.fishing_current_tab_click_at = 0.0
        self.fishing_current_tab_attempts = 0
        self.fishing_last_tab_warning_at = 0.0
        self.fishing_channel_intent = "send"
        self.fishing_sender_channel_deadline = 0.0
        self.fishing_sender_channel_click_at = 0.0
        self.fishing_sender_channel_attempts = 0
        self.fishing_last_sender_warning_at = 0.0
        self.fishing_map_baseline: Optional[MapFingerprint] = None
        self.fishing_map_candidate_frames = 0
        self.fishing_map_last: Optional[MapFingerprint] = None
        self.fishing_map_stable_frames = 0
        self.fishing_map_wait_deadline = 0.0
        self.fishing_map_settle_until = 0.0
        self.fishing_map_transition_count = 0
        self.fishing_map_last_evidence = ""
        self.fishing_reclick_locate_deadline = 0.0
        self.fishing_prepare_warning_at = 0.0
        self.fishing_release_at = 0.0
        self.fishing_state_ocr_at = 0.0
        self.user_activity_paused = False
        self.user_activity_log_at = 0.0
        self.user_activity_pause_started_at = 0.0
        self.fishing_resume_checks = 0
        self.shortcut_name = ""
        self.shortcut_path = ""
        self.binding_signature = None
        self.binding_missing_streak = 0
        self.name = f"未綁定_{hwnd}"
        self.capture_ok = False
        self.last_capture_wall = 0.0
        self.last_event = "已加入監測"
        self.last_event_wall = time.time()
        # V11.5：DPI 修復後只保存 Flash 輸入基準；不再為自動化改 Windows 視窗尺寸。
        self.geometry_safe_client = None  # 相容舊欄位名稱，語意=輸入基準 client
        self.geometry_restore_rect = None  # V11.5 不再用於 runtime resize
        self.geometry_profile_shortcut = ""
        self.apply_binding(load_window_bindings().get(int(hwnd)), announce=False, initial=True)

    def apply_binding(self, binding: Optional[dict], announce: bool = True, initial: bool = False):
        """即時套用控制台寫入的「視窗 ↔ 捷徑」綁定。

        背景程序每 0.5 秒刷新一次。單次讀檔/鎖競爭取得 None 時，不能立刻把已知捷徑與角色
        清成未綁定；只有連續多次確實找不到才解除。
        """
        if not binding and not initial and self.shortcut_path:
            self.binding_missing_streak += 1
            if self.binding_missing_streak < 20:
                return
        elif binding:
            self.binding_missing_streak = 0
        p = dict(self.base_profile)
        if binding:
            raw = str(binding.get("shortcut_path", "") or "").strip()
            name = str(binding.get("shortcut_name", "") or "").strip()
            if raw:
                p["捷徑路徑"] = raw
            if name:
                p["名稱"] = name
            # 手動綁定後，角色設定由該綁定自己管理。尚未指定角色時留空，
            # 避免所有視窗誤套預設的「心悅君兮」。
            if "preferred_role" in binding:
                p["優先角色"] = str(binding.get("preferred_role", "") or "").strip()
                p["角色模板"] = ""

        raw = str(p.get("捷徑路徑", "") or "").strip()
        display = ""
        if binding:
            display = str(binding.get("shortcut_name", "") or "").strip()
        if not display:
            display = shortcut_display_name(raw, str(p.get("名稱", "") or "").strip()) if raw else ""

        if not binding:
            self.binding_missing_streak = 0
        assigned_fishing_profile_id = str((binding or {}).get("fishing_profile_id", "") or "").strip()
        reconnect_enabled = bool((binding or {}).get("reconnect_enabled", True))
        manor_enabled = bool((binding or {}).get("manor_enabled", False))
        fishing_enabled = bool((binding or {}).get("fishing_enabled", bool(assigned_fishing_profile_id)))
        # 保留魚級設定但只在釣魚開關開啟時交給既有釣魚流程，避免關閉後
        # _refresh_fishing_profile 又把它自動啟用。
        fishing_profile_id = assigned_fishing_profile_id if fishing_enabled else ""
        try:
            preferred_line_no = int((binding or {}).get("preferred_line_no", 0) or 0)
        except Exception:
            preferred_line_no = 0
        if preferred_line_no not in range(0, 9):
            preferred_line_no = 0
        sig = (
            raw, display, str(p.get("優先角色", "") or ""),
            assigned_fishing_profile_id, preferred_line_no,
            reconnect_enabled, manor_enabled, fishing_enabled,
        )
        if sig == self.binding_signature:
            return
        previous_fishing_id = self.fishing_profile_id
        self.binding_signature = sig
        self.profile = p
        self.shortcut_path = raw
        self.shortcut_name = display or "未綁定"
        self.name = self.shortcut_name if display else f"未綁定_{self.hwnd}"
        self.reconnect_enabled = reconnect_enabled
        self.manor_enabled = manor_enabled
        self.fishing_enabled = fishing_enabled
        self.fishing_profile_id = fishing_profile_id
        previous_line_no = self.preferred_line_no
        self.preferred_line_no = preferred_line_no
        if previous_line_no != preferred_line_no:
            self.selected_line_no = 0
            self.line_ocr_attempts = 0
            self.line_ocr_votes = []
        stable_identity = str((binding or {}).get("profile_key", "") or raw).strip().casefold()
        self.fishing_progress_key = f"{stable_identity}|{fishing_profile_id}" if stable_identity and fishing_profile_id else ""
        self.fishing_profile = fishing_profiles.profile_by_id(fishing_profile_id)
        if self.fishing_profile is None:
            self.fishing_profile_id = ""
        if self.fishing_profile_id != previous_fishing_id:
            # 勾選／切換魚級後必須重新走「清彈窗 → 驗證右下角無 X」；
            # 禁止沿用上一個設定的完成狀態直接發送聊天字串。
            self.fishing_prerequisites_ready = False
            self._reset_fishing_runtime("勾選變更")
        self._refresh_geometry_profile()
        if announce:
            if display:
                self.set_event(f"已綁定捷徑：{display}")
                LOG.info("[%s] 即時套用捷徑綁定：%s", self.name, raw)
            else:
                self.set_event("目前尚未綁定捷徑")
            if self.fishing_profile:
                LOG.info("[%s] 釣魚設定已啟用：%s；勾選後立即常駐。", self.name, self.fishing_profile.get("name", ""))
                self.set_event(f"釣魚待執行：{self.fishing_profile.get('name', '')}")
            elif previous_fishing_id:
                LOG.info("[%s] 釣魚設定已取消；斷線監測保持常駐。", self.name)
                self.set_event("釣魚已取消，斷線監測中")

    def _refresh_geometry_profile(self):
        if not self.shortcut_path:
            self.geometry_safe_client = None
            self.geometry_restore_rect = None
            self.geometry_profile_shortcut = ""
            return
        if self.geometry_profile_shortcut == self.shortcut_path and self.geometry_safe_client:
            return
        item = window_geometry.load_profile(self.shortcut_path)
        safe = (item.get("input_base_client") or item.get("safe_client")) if isinstance(item, dict) else None
        rect = (item.get("user_visible_rect") or item.get("user_rect")) if isinstance(item, dict) else None
        self.geometry_profile_shortcut = self.shortcut_path
        self.geometry_safe_client = None
        if isinstance(safe, (list, tuple)) and len(safe) == 2:
            try:
                self.geometry_safe_client = (max(1, int(safe[0])), max(1, int(safe[1])))
            except Exception:
                self.geometry_safe_client = None
        # V11.5：把基準交給 WindowIO 做訊息座標正規化；不論目前視窗多大都不 resize。
        WIO.set_input_base_client(self.hwnd, self.geometry_safe_client)
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            try:
                self.geometry_restore_rect = tuple(int(v) for v in rect)
            except Exception:
                self.geometry_restore_rect = None

    def _reset_fishing_runtime(self, reason: str = ""):
        self.fishing_phase = "準備飛行" if self.fishing_profile else "停用"
        self.fishing_prepare_deadline = 0.0
        self.fishing_chat_then_select = False
        self.fishing_chat_clear_attempts = 0
        self.fishing_menu_collapse_attempts = 0
        self.fishing_menu_expand_attempts = 0
        self.fishing_menu_expand_next_at = 0.0
        self.fishing_message_group_index = 0
        self.fishing_message_groups = fishing_profiles.message_groups(self.fishing_profile or {})
        self.fishing_link_index = 0
        first_message = self.fishing_message_groups[0] if self.fishing_message_groups else ""
        self.fishing_links = fishing_profiles.parse_message_links(first_message)
        self.fishing_link_points = []
        self.fishing_send_at = 0.0
        self.fishing_locate_deadline = 0.0
        self.fishing_next_locate_at = 0.0
        self.fishing_link_locate_timeouts = 0
        self.fishing_deadline = 0.0
        self.fishing_next_status_check = 0.0
        self.fishing_missing_checks = 0
        self.fishing_round = 0
        self.fishing_last_route = ""
        self.fishing_current_tab_deadline = 0.0
        self.fishing_current_tab_click_at = 0.0
        self.fishing_current_tab_attempts = 0
        self.fishing_last_tab_warning_at = 0.0
        self.fishing_channel_intent = "send"
        self.fishing_sender_channel_deadline = 0.0
        self.fishing_sender_channel_click_at = 0.0
        self.fishing_sender_channel_attempts = 0
        self.fishing_last_sender_warning_at = 0.0
        self.fishing_map_baseline = None
        self.fishing_map_candidate_frames = 0
        self.fishing_map_last = None
        self.fishing_map_stable_frames = 0
        self.fishing_map_wait_deadline = 0.0
        self.fishing_map_settle_until = 0.0
        self.fishing_map_transition_count = 0
        self.fishing_map_last_evidence = ""
        self.fishing_reclick_locate_deadline = 0.0
        self.fishing_prepare_warning_at = 0.0
        self.fishing_release_at = 0.0
        self.fishing_state_ocr_at = 0.0
        self.fishing_resume_checks = 0
        resumed = False
        if self.fishing_profile and self.fishing_progress_key:
            saved = _read_fishing_progress().get(self.fishing_progress_key, {})
            if isinstance(saved, dict) and str(saved.get("profile_id", "")) == self.fishing_profile_id:
                try:
                    if self.fishing_message_groups:
                        self.fishing_message_group_index = int(saved.get("message_group_index", 0)) % len(self.fishing_message_groups)
                    message = self._current_fishing_message()
                    self.fishing_links = fishing_profiles.parse_message_links(message)
                    if self.fishing_links:
                        self.fishing_link_index = int(saved.get("link_index", 0)) % len(self.fishing_links)
                    self.fishing_round = max(0, int(saved.get("round", 0)))
                    resumed = True
                except (TypeError, ValueError):
                    resumed = False
        if resumed:
            self.fishing_phase = "恢復檢查"
        if reason and self.fishing_profile:
            LOG.info(
                "[%s] 釣魚流程重設：%s；設定=%s，訊息組數=%d。",
                self.name, reason, self.fishing_profile.get("name", ""), len(self.fishing_message_groups),
            )

    def _save_fishing_progress(self, status: str) -> None:
        if not self.fishing_profile_id or not self.fishing_progress_key:
            return
        _write_fishing_progress(
            self.fishing_progress_key,
            {
                "profile_id": self.fishing_profile_id,
                "message_group_index": int(self.fishing_message_group_index),
                "link_index": int(self.fishing_link_index),
                "round": int(self.fishing_round),
                "status": str(status),
                "updated_at": time.time(),
            },
        )

    def _refresh_fishing_profile(self):
        if not self.fishing_profile_id:
            if self.fishing_profile is not None:
                self.fishing_profile = None
                self._reset_fishing_runtime("設定取消")
            return
        latest = fishing_profiles.profile_by_id(self.fishing_profile_id)
        if latest is None:
            LOG.warning("[%s] 原勾選的釣魚設定已不存在，停止釣魚；斷線監測不受影響。", self.name)
            self.fishing_profile_id = ""
            self.fishing_profile = None
            self._reset_fishing_runtime("設定不存在")
            return
        old_sig = (
            str((self.fishing_profile or {}).get("name", "")),
            str((self.fishing_profile or {}).get("message", "")),
        )
        new_sig = (str(latest.get("name", "")), str(latest.get("message", "")))
        if self.fishing_profile is None or old_sig != new_sig:
            self.fishing_profile = latest
            self._reset_fishing_runtime("設定內容更新")

    def _current_fishing_message(self) -> str:
        groups = self.fishing_message_groups
        if not groups:
            groups = fishing_profiles.message_groups(self.fishing_profile or {})
            self.fishing_message_groups = groups
        if not groups:
            return ""
        self.fishing_message_group_index %= len(groups)
        return str(groups[self.fishing_message_group_index])

    def _advance_fishing_target(self) -> Tuple[int, int, int, int]:
        """Advance link, then message group; return 1-based group/link status."""
        group_count = max(1, len(self.fishing_message_groups))
        link_count = max(1, len(self.fishing_links))
        self.fishing_link_index += 1
        if self.fishing_link_index >= link_count:
            self.fishing_link_index = 0
            self.fishing_message_group_index = (self.fishing_message_group_index + 1) % group_count
            if self.fishing_message_group_index == 0:
                self.fishing_round += 1
        message = self._current_fishing_message()
        self.fishing_links = fishing_profiles.parse_message_links(message)
        self._save_fishing_progress("待嘗試")
        return (
            self.fishing_message_group_index + 1,
            group_count,
            self.fishing_link_index + 1,
            max(1, len(self.fishing_links)),
        )

    def _begin_fishing_channel_selection(self, intent: str, event: str = "") -> None:
        self.fishing_channel_intent = "reclick" if intent == "reclick" else "send"
        # Every fresh send, including a retry after map change or failed link,
        # must clear the chat first. The initial preparation has already just
        # completed this step, so it may proceed directly.
        if intent != "reclick" and self.fishing_phase != "前置完成":
            self.fishing_chat_then_select = True
            self.fishing_chat_clear_attempts = 0
            self.fishing_phase = "準備聊天"
            self.fishing_prepare_deadline = 0.0
            if event:
                self.set_event(f"{event}；先重新清除聊天框")
            return
        self.fishing_phase = "待選目前分頁"
        self.fishing_current_tab_deadline = 0.0
        self.fishing_sender_channel_deadline = 0.0
        if event:
            self.set_event(event)

    def _fishing_prepare_step(self, frame: np.ndarray, now: float) -> bool:
        """Run every user-requested preparation step using background input only."""
        if self.fishing_phase == "準備飛行":
            landed = find_fishing_state_button(frame, "飛行")
            flying = find_fishing_state_button(frame, "降落")
            if landed is None and flying is None and now >= self.fishing_state_ocr_at:
                ocr_states = find_fishing_state_buttons_ocr(frame)
                landed = ocr_states.get("飛行")
                flying = ocr_states.get("降落")
                self.fishing_state_ocr_at = now + 3.0
            if flying:
                self.fishing_phase = "準備收回系統列"
                self.set_event("釣魚前置：已由『降落』按鈕確認目前正在飛行")
                return False
            if landed:
                if not WIO.click_interactive(self.hwnd, *landed.center, f"[{self.name}] 釣魚前置：飛行"):
                    self.set_event("釣魚前置：『飛行』背景點擊未送出，留在本步重試")
                    return False
                self.fishing_phase = "確認飛行"
                self.fishing_prepare_deadline = now + 3.0
                self.set_event("釣魚前置：已點『飛行』，下一幀確認『降落』")
                return False
            collapsed = find_fishing_menu_expand(frame)
            if collapsed and now >= self.fishing_menu_expand_next_at:
                if WIO.click_interactive(
                    self.hwnd,
                    *collapsed.center,
                    f"[{self.name}] 釣魚前置：展開系統列以確認飛行／降落",
                ):
                    self.fishing_menu_expand_attempts += 1
                    self.fishing_menu_expand_next_at = now + 3.0
                    self.fishing_phase = "確認系統列展開"
                    self.fishing_prepare_deadline = now + 1.0
                    self.set_event("釣魚前置：已背景展開系統列，下一幀重新辨識飛行／降落")
                    return False
            if now - self.fishing_prepare_warning_at >= 5.0:
                self.fishing_prepare_warning_at = now
                LOG.warning("[%s] 釣魚前置找不到『飛行』或『降落』圖片／文字證據；不發送輸入，稍後重試。", self.name)
            self.set_event("釣魚前置：等待飛行／降落圖片或文字證據；目前不發送輸入")
            return False

        if self.fishing_phase == "確認系統列展開":
            if now >= self.fishing_prepare_deadline:
                self.fishing_phase = "準備飛行"
                self.set_event("釣魚前置：系統列展開等待完成，重新辨識飛行／降落")
            return False

        if self.fishing_phase == "確認飛行":
            flying = find_fishing_state_button(frame, "降落")
            if flying is None and now >= self.fishing_state_ocr_at:
                flying = find_fishing_state_buttons_ocr(frame).get("降落")
                self.fishing_state_ocr_at = now + 1.0
            if flying:
                self.fishing_phase = "準備收回系統列"
                self.set_event("釣魚前置：已看到『降落』，飛行狀態確認完成")
            elif now >= self.fishing_prepare_deadline:
                self.fishing_phase = "準備飛行"
                self.set_event("釣魚前置：未確認『降落』，重新辨識飛行狀態")
            return False

        if self.fishing_phase == "準備收回系統列":
            # The supplied collapse arrow is beside the middle of the expanded
            # right action column, not in the bottom-right corner.
            collapse = find_fishing_menu_collapse(frame)
            if collapse:
                if not WIO.click_interactive(self.hwnd, *collapse.center, f"[{self.name}] 釣魚前置：收回系統列"):
                    self.set_event("釣魚前置：收回系統列背景點擊未送出，留在本步重試")
                    return False
                self.fishing_phase = "確認系統列收回"
                self.fishing_menu_collapse_attempts += 1
                self.fishing_prepare_deadline = now + 1.0
                self.set_event("釣魚前置：已點收回系統列，下一幀確認")
                return False
            self.fishing_phase = "準備聊天"
            self.fishing_menu_collapse_attempts = 0
            self.set_event("釣魚前置：未見展開狀態證據，視為已收回")
            return False

        if self.fishing_phase == "確認系統列收回":
            if now >= self.fishing_prepare_deadline:
                still_expanded = find_fishing_menu_still_expanded(frame)
                if still_expanded and self.fishing_menu_collapse_attempts < 2:
                    self.fishing_phase = "準備收回系統列"
                    self.set_event("釣魚前置：系統列仍為展開狀態，背景重按一次")
                    return False
                self.fishing_phase = "準備聊天"
                if still_expanded:
                    self.set_event("釣魚前置：背景收回已嘗試兩次，先繼續清除聊天框避免卡死")
                else:
                    self.fishing_menu_collapse_attempts = 0
                    self.set_event("釣魚前置：右側系統列收回已確認，開始清除聊天框")
            return False

        if self.fishing_phase == "準備聊天":
            chat_button = TB.match(frame, "釣魚前置_聊天小按鈕", 0.76, roi=(0.25, 0.78, 0.55, 1.0))
            if chat_button:
                sent = WIO.click_interactive(
                    self.hwnd, *chat_button.center, f"[{self.name}] 釣魚前置：清除聊天框小按鈕"
                )
                if not sent:
                    self.set_event("釣魚前置：清除聊天框背景點擊未送出，留在本步重試")
                    return False
                self.fishing_chat_clear_attempts += 1
                self.fishing_phase = "確認聊天清除"
                self.fishing_prepare_deadline = now + 0.28
                self.set_event(
                    f"釣魚前置：已背景點擊聊天框『－』清除鈕 "
                    f"{self.fishing_chat_clear_attempts}/4 次"
                )
            else:
                if now - self.fishing_prepare_warning_at >= 5.0:
                    self.fishing_prepare_warning_at = now
                    LOG.warning("[%s] 釣魚前置找不到清除聊天框小按鈕；留在本步，不略過。", self.name)
                self.set_event("釣魚前置：等待清除聊天框小按鈕證據")
            return False

        if self.fishing_phase == "確認聊天清除":
            if now >= self.fishing_prepare_deadline:
                if self.fishing_chat_clear_attempts < 4:
                    self.fishing_phase = "準備聊天"
                    self.set_event(
                        f"釣魚前置：聊天框『－』已按 {self.fishing_chat_clear_attempts}/4 次，繼續清除"
                    )
                elif self.fishing_chat_then_select:
                    self.fishing_chat_then_select = False
                    self.fishing_phase = "待選目前分頁"
                    self.fishing_current_tab_deadline = 0.0
                    self.fishing_sender_channel_deadline = 0.0
                    self.set_event("釣魚發送前已連按聊天框『－』4 次；下一步選擇上排目前")
                else:
                    self.fishing_phase = "前置完成"
                    self.set_event("釣魚前置：聊天框『－』已背景連按 4 次")
            return False

        return self.fishing_phase == "前置完成"

    def _detect_in_game_evidence(self, frame: np.ndarray, allow_chat: bool = False) -> str:
        """Return positive evidence that the current capture is already in game.

        The role page disappearing is only negative evidence and was the cause of
        the V11.8 regression when Flash PrintWindow kept an old role-page frame.
        Require an element that belongs to the live game, and reject a frame that
        still contains the real Enter Game button.
        """
        def inspect(candidate: Optional[np.ndarray], route: str) -> str:
            if candidate is None or candidate.size == 0:
                return ""
            if detect_enter_game(candidate) is not None:
                return ""
            close = find_popup_close(candidate)
            if close:
                return f"{route}/{close[2]}"
            auto_state, auto_match = auto_battle_state(candidate)
            if auto_state != "未知" and auto_match is not None:
                return f"{route}/自動戰鬥={auto_state}/{auto_match.score:.3f}"
            if is_battle_scene(candidate):
                return f"{route}/戰鬥HUD"
            if allow_chat:
                controls = detect_chat_controls(candidate)
                if controls:
                    return f"{route}/遊戲聊天列/{controls[2]}"
            return ""

        evidence = inspect(frame, "logical")
        if evidence:
            return evidence
        raw = WIO.get_last_raw(self.hwnd)
        if raw is not None and raw is not frame:
            try:
                if raw.shape[:2] != frame.shape[:2]:
                    evidence = inspect(raw, "raw")
            except Exception:
                evidence = ""
        return evidence

    def _begin_post_entry_cleanup(self, now: float, evidence: str) -> None:
        """Enter the mandatory popup/auto-battle gate before fishing."""
        self.entered_at = now
        self.cached_role_title = None
        self.enter_clear_frames = 0
        self.enter_click_attempts = 0
        self.enter_input_exhausted = False
        self.post_popup_clear_frames = 0
        self.post_popup_quiet_since = 0.0
        self.post_popup_seen = set()
        self.auto_target_seen_frames = 0
        self.auto_x_seen_frames = 0
        self.auto_correction_pending = False
        self.auto_correction_started_at = 0.0
        self.last_auto_guard_warning_at = 0.0
        self.last_auto_ocr_at = 0.0
        self.last_auto_no_x_at = 0.0
        self.auto_toggle_attempts = 0
        self.last_auto_toggle = 0.0
        self.auto_settle_until = 0.0
        self.last_post_entry_warning_at = 0.0
        self.fishing_prerequisites_ready = False
        if self.fishing_profile:
            self.fishing_phase = "等待遊戲整理"
        self.set_state("進入遊戲後整理")
        self.set_event(f"已確認進入遊戲，先關閉彈窗：{evidence}")
        LOG.info("[%s] 已取得遊戲內正向證據：%s；開始依序清彈窗並驗證右下角為無 X 正確狀態。", self.name, evidence)

    def _auto_battle_state_dual(self, frame: np.ndarray, allow_ocr: bool = False) -> Tuple[str, Optional[Tuple[int, int]], str]:
        """Inspect logical and already-captured raw frames without another PrintWindow."""
        results = []
        state, match = auto_battle_state(frame, allow_ocr=allow_ocr)
        if match is not None:
            results.append((state, match.center, f"logical/{state}/{match.score:.3f}"))
        raw = WIO.get_last_raw(self.hwnd)
        if raw is not None and raw is not frame:
            try:
                if raw.shape[:2] != frame.shape[:2]:
                    raw_state, raw_match = auto_battle_state(raw, allow_ocr=allow_ocr and state == "未知")
                    if raw_match is not None:
                        logical_point = WIO.raw_point_to_logical(self.hwnd, *raw_match.center)
                        results.append((raw_state, logical_point, f"raw/{raw_state}/{raw_match.score:.3f}→logical"))
            except Exception:
                pass
        # Any positive X evidence blocks the flow.  Only if neither route sees X
        # may a positive no-X sample release fishing.
        for item in results:
            if item[0] == "有X錯誤":
                return item
        for item in results:
            if item[0] == "無X正確":
                return item
        return "未知", None, "logical/raw 都未明確辨識無X或有X"

    def _interrupt_fishing_for_auto_x(self) -> None:
        if not self.fishing_profile or self.state != "監看":
            return
        if self.fishing_phase == "釣魚中":
            self.fishing_phase = "釣魚中待無X"
        elif self.fishing_phase not in ("等待遊戲整理", "等待重連", "等待遊戲穩定", "停用"):
            # The previous send/link attempt may have been invalidated.  Keep the
            # current link index but restart the exact chat sequence after repair.
            self.fishing_phase = "待無X後重送"
            self.fishing_deadline = 0.0
            self.fishing_locate_deadline = 0.0

    def _maintain_no_x_auto_battle(self, frame: np.ndarray, now: float) -> bool:
        """Keep the user-required no-X button state and return True when verified."""
        ocr_gap = max(1.5, float(CONFIG.get("自動戰鬥常駐OCR最短間隔秒", 3.0)))
        allow_ocr = now - self.last_auto_ocr_at >= ocr_gap
        if allow_ocr:
            self.last_auto_ocr_at = now
        state, point, evidence = self._auto_battle_state_dual(frame, allow_ocr=allow_ocr)
        need = max(2, int(CONFIG.get("自動戰鬥無X確認幀", 2)))
        if state == "無X正確":
            self.auto_x_seen_frames = 0
            self.auto_target_seen_frames += 1
            self.last_auto_no_x_at = now
            if self.auto_target_seen_frames < need:
                self.set_event(f"無 X 自動戰鬥確認 {self.auto_target_seen_frames}/{need}")
                return False
            was_pending = self.auto_correction_pending
            attempts = self.auto_toggle_attempts
            self.auto_correction_pending = False
            self.auto_correction_started_at = 0.0
            self.auto_toggle_attempts = 0
            if was_pending:
                LOG.info("[%s] 有 X 狀態已修正；%s；修正點擊=%d。", self.name, evidence, attempts)
            return True

        if state == "有X錯誤" and point is not None:
            self.auto_target_seen_frames = 0
            self.auto_x_seen_frames += 1
            self.last_auto_no_x_at = 0.0
            retry = max(4.0, float(CONFIG.get("自動戰鬥X修正重試秒", 12.0)))
            may_click = (not self.auto_correction_pending) or (now - self.auto_correction_started_at >= retry)
            if may_click:
                sent = WIO.click_interactive(
                    self.hwnd, int(point[0]), int(point[1]), f"[{self.name}] 背景修正有X自動戰鬥按鈕"
                )
                if sent:
                    self.auto_correction_pending = True
                    self.auto_correction_started_at = time.monotonic()
                    self.auto_toggle_attempts += 1
                    self._interrupt_fishing_for_auto_x()
                    self.set_event("偵測到 X，已點一次右下角按鈕；等待無 X 新畫面確認")
                    LOG.warning(
                        "[%s] 常駐監測偵測到有 X 錯誤狀態：%s；已做第 %d 次單擊，至少等待 %.1f 秒才允許再次修正。",
                        self.name, evidence, self.auto_toggle_attempts, retry,
                    )
                else:
                    self.set_event("偵測到 X，但本次背景點擊未送出；禁止開始釣魚")
                return False
            self.set_event("仍看到 X，等待前次修正的新畫面；禁止釣魚動作")
            return False

        self.auto_x_seen_frames = 0
        # Ambiguous shared-border frames occur between throttled OCR probes.  A
        # recent positive text confirmation remains valid only inside one OCR
        # interval, while the fast X template is still checked on every frame
        # above and immediately cancels this cache.
        recent_no_x = bool(
            self.last_auto_no_x_at > 0.0
            and now - self.last_auto_no_x_at <= ocr_gap + 0.75
            and self.auto_target_seen_frames >= need
        )
        if recent_no_x:
            return True
        if self.last_auto_no_x_at <= 0.0 or now - self.last_auto_no_x_at > ocr_gap + 0.75:
            self.auto_target_seen_frames = 0
        repeat = max(5.0, float(CONFIG.get("自動戰鬥常駐未知警告秒", 10.0)))
        if now - self.last_auto_guard_warning_at >= repeat:
            self.last_auto_guard_warning_at = now
            LOG.warning("[%s] 無法確定右下角為無 X 正確狀態：%s；不盲點、不開始新的釣魚動作。", self.name, evidence)
        self.set_event("右下角無 X 狀態尚未確認；持續監測但不執行釣魚動作")
        return False

    def _detect_current_chat_tab_dual(self, frame: np.ndarray) -> Tuple[str, Optional[Tuple[int, int]], str]:
        state, point, evidence = detect_current_chat_tab(frame)
        if point is not None:
            return state, point, f"logical/{evidence}"
        raw = WIO.get_last_raw(self.hwnd)
        if raw is None or raw is frame:
            return state, point, evidence
        try:
            if raw.shape[:2] == frame.shape[:2]:
                return state, point, evidence
        except Exception:
            return state, point, evidence
        raw_state, raw_point, raw_evidence = detect_current_chat_tab(raw)
        if raw_point is None:
            return state, point, f"logical={evidence}; raw={raw_evidence}"
        return raw_state, WIO.raw_point_to_logical(self.hwnd, *raw_point), f"raw/{raw_evidence}→logical"

    def _detect_sender_chat_channel_dual(
        self,
        frame: np.ndarray,
    ) -> Tuple[str, Optional[Tuple[int, int]], Optional[Tuple[int, int]], str]:
        state, selector, current, evidence = detect_sender_chat_channel(frame)
        if selector is not None or current is not None:
            return state, selector, current, f"logical/{evidence}"
        raw = WIO.get_last_raw(self.hwnd)
        if raw is None or raw is frame:
            return state, selector, current, evidence
        try:
            if raw.shape[:2] == frame.shape[:2]:
                return state, selector, current, evidence
        except Exception:
            return state, selector, current, evidence
        raw_state, raw_selector, raw_current, raw_evidence = detect_sender_chat_channel(raw)
        if raw_selector is not None:
            raw_selector = WIO.raw_point_to_logical(self.hwnd, *raw_selector)
        if raw_current is not None:
            raw_current = WIO.raw_point_to_logical(self.hwnd, *raw_current)
        return raw_state, raw_selector, raw_current, f"raw/{raw_evidence}→logical"

    def _finish_fishing_channel_selection(self, now: float, evidence: str) -> None:
        if self.fishing_channel_intent == "reclick":
            self.fishing_phase = "轉圖後等待連結"
            self.fishing_reclick_locate_deadline = now + max(
                3.0, float(CONFIG.get("釣魚連結出現等待秒", 6.0))
            )
            self.set_event("轉圖後上下兩個『目前』已確認；重新定位同一釣點")
            LOG.info("[%s] 轉圖後發送頻道『目前』確認完成：%s；不沿用舊座標。", self.name, evidence)
        else:
            self.fishing_phase = "待發送"
            self.set_event("上下兩個『目前』均已確認，下一幀才允許發送")
            LOG.info("[%s] 下排發送頻道『目前』確認完成：%s。", self.name, evidence)

    def _select_sender_current_channel_step(self, frame: np.ndarray, now: float) -> None:
        state, selector, current, evidence = self._detect_sender_chat_channel_dual(frame)
        timeout = max(1.5, float(CONFIG.get("釣魚發送頻道確認秒", 3.0)))

        if self.fishing_phase == "待確認發送目前頻道":
            if state == "目前已選":
                self._finish_fishing_channel_selection(now, evidence)
                return
            if state == "選單已開" and current is not None:
                self.fishing_phase = "等待發送頻道選單"
            elif selector is not None:
                sent = WIO.click_interactive(
                    self.hwnd, int(selector[0]), int(selector[1]), f"[{self.name}] 背景開啟下排發送頻道選單"
                )
                if not sent:
                    self.set_event("釣魚：下排發送頻道選單未能開啟，稍後重試")
                    return
                clicked_at = time.monotonic()
                self.fishing_sender_channel_click_at = clicked_at
                self.fishing_sender_channel_deadline = clicked_at + timeout
                self.fishing_sender_channel_attempts += 1
                self.fishing_phase = "等待發送頻道選單"
                self.set_event("釣魚：已開啟下排發送頻道，等待選擇『目前』")
                LOG.info("[%s] 已點下排發送頻道；原狀態=%s，辨識=%s。", self.name, state, evidence)
                return
            else:
                if now - self.fishing_last_sender_warning_at >= 5.0:
                    self.fishing_last_sender_warning_at = now
                    LOG.warning("[%s] 找不到下排發送頻道控制：%s；不使用固定座標。", self.name, evidence)
                self.set_event("釣魚：找不到下排發送頻道，持續辨識但不亂點")
                return

        if self.fishing_phase == "等待發送頻道選單":
            if state == "目前已選" and now > self.fishing_sender_channel_click_at:
                self._finish_fishing_channel_selection(now, evidence)
                return
            if state == "選單已開" and current is not None:
                sent = WIO.click_interactive(
                    self.hwnd, int(current[0]), int(current[1]), f"[{self.name}] 背景選擇下排發送頻道目前"
                )
                if not sent:
                    self.set_event("釣魚：下排『目前』選項點擊未送出，稍後重試")
                    return
                clicked_at = time.monotonic()
                self.fishing_sender_channel_click_at = clicked_at
                self.fishing_sender_channel_deadline = clicked_at + timeout
                self.fishing_phase = "等待發送頻道目前確認"
                self.set_event("釣魚：已選擇下排『目前』，等待確認為目前↑")
                LOG.info("[%s] 下排選單已點『目前』；辨識=%s。", self.name, evidence)
                return
            if now >= self.fishing_sender_channel_deadline > 0.0:
                self.fishing_phase = "待確認發送目前頻道"
                self.set_event("釣魚：下排頻道選單未出現，重新定位後再開啟")
                LOG.warning("[%s] 下排發送頻道選單確認逾時：%s。", self.name, evidence)
            return

        if self.fishing_phase == "等待發送頻道目前確認":
            if state == "目前已選" and now > self.fishing_sender_channel_click_at:
                self._finish_fishing_channel_selection(now, evidence)
                return
            if now >= self.fishing_sender_channel_deadline:
                self.fishing_phase = "待確認發送目前頻道"
                self.set_event("釣魚：下排頻道尚未成為目前↑，重新選擇")
                LOG.warning("[%s] 下排發送頻道未確認為『目前』：%s；禁止發送。", self.name, evidence)

    def _select_current_chat_tab_step(self, frame: np.ndarray, now: float) -> None:
        state, point, evidence = self._detect_current_chat_tab_dual(frame)
        if self.fishing_phase == "待選目前分頁":
            if state == "紅色已選":
                self.fishing_phase = "待確認發送目前頻道"
                self.set_event("釣魚：上排『目前』原本已呈紅色；不重複點擊")
                LOG.info("[%s] 上排『目前』原本已呈紅色：%s；不重複點擊，直接確認下排發送頻道。", self.name, evidence)
                return
            if point is None:
                if now - self.fishing_last_tab_warning_at >= 5.0:
                    self.fishing_last_tab_warning_at = now
                    LOG.warning("[%s] 釣魚前找不到上排『目前』分頁：%s；不使用固定座標。", self.name, evidence)
                self.set_event("釣魚：找不到上排『目前』分頁，持續辨識但不亂點")
                return
            if state != "未選":
                if now - self.fishing_last_tab_warning_at >= 5.0:
                    self.fishing_last_tab_warning_at = now
                    LOG.warning("[%s] 上排『目前』位置存在但狀態不明：%s；本幀不點擊。", self.name, evidence)
                self.set_event("釣魚：上排『目前』狀態不明，本幀不點擊")
                return
            sent = WIO.click_interactive(
                self.hwnd, int(point[0]), int(point[1]), f"[{self.name}] 背景選擇上排目前分頁"
            )
            if not sent:
                self.set_event("釣魚：上排『目前』分頁點擊未送出，稍後重試")
                return
            clicked_at = time.monotonic()
            self.fishing_current_tab_click_at = clicked_at
            self.fishing_current_tab_deadline = clicked_at + max(1.5, float(CONFIG.get("釣魚目前分頁確認秒", 3.0)))
            self.fishing_current_tab_attempts += 1
            self.fishing_phase = "等待目前分頁變紅"
            self.set_event("釣魚：已點上排『目前』，等待紅色選取狀態")
            LOG.info("[%s] 釣魚發送前已點上排『目前』一次；原狀態=%s，辨識=%s。", self.name, state, evidence)
            return

        if self.fishing_phase != "等待目前分頁變紅":
            return
        if state == "紅色已選" and now > self.fishing_current_tab_click_at:
            self.fishing_phase = "待確認發送目前頻道"
            self.set_event("釣魚：上排『目前』已呈紅色；繼續確認下排發送頻道")
            LOG.info("[%s] 上排『目前』紅色狀態確認完成：%s。", self.name, evidence)
            return
        if now >= self.fishing_current_tab_deadline:
            self.fishing_phase = "待選目前分頁"
            self.set_event("釣魚：『目前』未變紅，重新辨識後再點一次")
            LOG.warning("[%s] 上排『目前』在確認時間內未呈紅色：%s；不發送聊天，重新定位。", self.name, evidence)

    def _close_unexpected_panel_step(self, frame: np.ndarray, now: float) -> bool:
        """Close an ordinary in-game panel before any normal-monitor action."""
        if manor_runtime.is_hwnd_active(self.hwnd):
            return False
        if find_manor_action_bar(frame) is not None:
            return False
        close = find_unexpected_panel_close(frame)
        if close is None:
            return False
        retry = max(0.45, float(CONFIG.get("高速彈窗關閉後等待秒", 0.08)))
        if now - self.last_unexpected_panel_click < retry:
            self.set_event("偵測到非預期遊戲面板，等待右上角 X 關閉完成")
            return True
        x, y, label = close
        sent = self.click_flow_action(x, y, f"[{self.name}] {label}")
        self.last_unexpected_panel_click = time.monotonic()
        if sent:
            self.set_event("已按非預期遊戲面板右上角 X；下一幀重新辨識")
            LOG.info("[%s] 已優先按遊戲面板右上角 X；在面板消失前不執行其他操作；辨識=%s。", self.name, label)
        else:
            self.set_event("非預期遊戲面板仍存在；X 點擊未送出，稍後重試")
            LOG.warning("[%s] 遊戲面板 X 點擊未送出；本幀停止其他操作；辨識=%s。", self.name, label)
        return True

    def _clear_window_geometry_cache(self):
        for bag in (WIO.geometry, WIO.surface_mode, WIO.last_raw_frame):
            try:
                bag.pop(int(self.hwnd), None)
            except Exception:
                pass

    def _enter_automation_geometry(self, reason: str) -> bool:
        """V11.5：只準備尺寸無關輸入基準，永遠不改 Windows 視窗大小。

        回傳 False，呼叫端不需要因 resize 丟棄 frame；因為已經沒有 resize。
        """
        self._refresh_geometry_profile()
        WIO.set_input_base_client(self.hwnd, self.geometry_safe_client)
        return False

    def _restore_user_geometry(self, reason: str):
        # V11.5：自動化過程從未改變視窗大小，因此沒有 runtime restore。
        if self.restore_minimized_after_flow:
            self._return_to_minimized(f"{reason}後恢復最小化")

    def _minimized_monitoring_enabled(self) -> bool:
        return bool(
            MINIMIZED_WINDOW_MONITORING
            and CONFIG.get("最小化視窗持續監測", False)
        )

    def _pause_for_minimized(self) -> None:
        now = time.monotonic()
        if self.minimized_paused_since <= 0.0:
            self.minimized_paused_since = now
            self.minimized_paused = True
            self.set_event("視窗已最小化，監測與釣魚暫停")
            LOG.warning("[%s] 偵測到遊戲最小化；依使用者設定不自動還原，斷線監測與釣魚暫停。", self.name)

    def _resume_from_minimized(self) -> None:
        if self.minimized_paused_since <= 0.0:
            self.minimized_paused = False
            return
        now = time.monotonic()
        paused_for = max(0.0, now - self.minimized_paused_since)
        for attr in (
            "fishing_locate_deadline",
            "fishing_deadline",
            "fishing_next_status_check",
            "fishing_current_tab_deadline",
            "fishing_sender_channel_deadline",
            "fishing_map_wait_deadline",
            "fishing_map_settle_until",
            "fishing_reclick_locate_deadline",
        ):
            value = float(getattr(self, attr, 0.0) or 0.0)
            if value > 0.0:
                setattr(self, attr, value + paused_for)
        self.minimized_paused_since = 0.0
        self.minimized_paused = False
        self._clear_window_geometry_cache()
        self.set_event("視窗已恢復，重新讀取尺寸後繼續監測")
        LOG.info("[%s] 遊戲視窗已從最小化恢復；暫停 %.1f 秒，已丟棄舊幾何並重新讀取尺寸。", self.name, paused_for)

    def _return_to_minimized(self, reason: str = "") -> None:
        if not WIO.is_window(self.hwnd):
            self.restore_minimized_after_flow = False
            return
        try:
            fn = ctypes.windll.user32.ShowWindowAsync
            fn.argtypes = [wintypes.HWND, ctypes.c_int]
            fn.restype = wintypes.BOOL
            fn(wintypes.HWND(int(self.hwnd)), int(win32con.SW_MINIMIZE))
        except Exception:
            try:
                win32gui.ShowWindow(int(self.hwnd), win32con.SW_MINIMIZE)
            except Exception:
                return
        self.restore_minimized_after_flow = False
        now = time.monotonic()
        if now - self.last_minimized_log_at >= 30.0:
            LOG.info("[%s] %s；視窗已恢復最小化，將持續週期探測。", self.name, reason or "最小化監測探測完成")
            self.last_minimized_log_at = now

    def capture_worker_frame(self) -> Tuple[Optional[np.ndarray], bool]:
        """取得 worker 幀；第二個回傳值表示本次為最小化短暫還原探測。

        Flash 11 最小化後會停止繪圖，PrintWindow 只會給黑畫面或過期幀，
        因此無法在「始終保持最小化」的同時監測新斷線。這裡使用
        SW_SHOWNOACTIVATE 短暫恢復繪圖，不搶前景；無事就立即再最小化。
        """
        if not WIO.is_window(self.hwnd):
            return None, False
        try:
            iconic = bool(win32gui.IsIconic(self.hwnd))
        except Exception:
            iconic = False
        if not iconic:
            self._resume_from_minimized()
            return WIO.capture(self.hwnd), False
        if not self._minimized_monitoring_enabled():
            self._pause_for_minimized()
            return None, False

        now = time.monotonic()
        if now < self.next_minimized_probe_at:
            return None, False
        gap = max(1.0, float(CONFIG.get("最小化監測探測間隔秒", 3.0)))
        self.next_minimized_probe_at = now + gap
        if not self.minimized_probe_logged:
            LOG.warning(
                "[%s] 偵測到遊戲已最小化；啟用最小化持續監測："
                "每 %.1f 秒無前景還原繪圖一次，無事後恢復最小化。",
                self.name, gap,
            )
            self.minimized_probe_logged = True
        try:
            fn = ctypes.windll.user32.ShowWindowAsync
            fn.argtypes = [wintypes.HWND, ctypes.c_int]
            fn.restype = wintypes.BOOL
            fn(wintypes.HWND(int(self.hwnd)), int(win32con.SW_SHOWNOACTIVATE))
        except Exception:
            try:
                win32gui.ShowWindow(int(self.hwnd), win32con.SW_SHOWNOACTIVATE)
            except Exception as e:
                LOG.error("[%s] 最小化監測無法短暫還原視窗：%s", self.name, e)
                return None, False

        redraw_wait = max(0.20, float(CONFIG.get("最小化還原繪圖等待秒", 0.60)))
        deadline = time.monotonic() + redraw_wait
        while time.monotonic() < deadline and WIO.is_window(self.hwnd):
            try:
                if not win32gui.IsIconic(self.hwnd):
                    break
            except Exception:
                break
            time.sleep(0.03)
        # 即使 show state 已恢復，仍要留給 Flash 至少 redraw_wait 時間處理繪圖。
        remain = deadline - time.monotonic()
        if remain > 0:
            time.sleep(remain)
        self._clear_window_geometry_cache()
        tries = max(1, min(5, int(CONFIG.get("最小化探測擷取重試次數", 3))))
        frame = None
        for idx in range(tries):
            frame = WIO.capture(self.hwnd)
            if frame is not None:
                self.last_minimized_probe_wall = time.time()
                break
            if idx + 1 < tries:
                time.sleep(0.16)
        return frame, True

    def finish_minimized_probe(self, was_probe: bool, frame: Optional[np.ndarray]) -> None:
        if not was_probe:
            return
        active = bool(self.state != "監看" or self.flow_started_at > 0.0)
        if frame is not None and active:
            self.restore_minimized_after_flow = True
            LOG.warning("[%s] 最小化探測發現需要處理的流程；視窗暫時保持還原，完成後會再最小化。", self.name)
            return
        self._return_to_minimized("最小化監測探測完成")

    def set_event(self, text: str):
        self.last_event = str(text)
        self.last_event_wall = time.time()

    def _accumulate_state_time(self, now: float):
        if self.flow_started_at <= 0.0:
            return
        start = max(float(self.state_since), float(self.flow_started_at))
        if now > start:
            self.flow_stage_durations[self.state] = self.flow_stage_durations.get(self.state, 0.0) + (now - start)

    def start_flow(self, now: float, reason: str = ""):
        if self.flow_started_at > 0.0:
            return
        self.fishing_prerequisites_ready = False
        if self.fishing_profile:
            self.fishing_phase = "等待重連"
            self.fishing_link_points = []
            self.fishing_send_at = 0.0
            self.fishing_locate_deadline = 0.0
            self.fishing_deadline = 0.0
            self.fishing_next_status_check = 0.0
            self.fishing_missing_checks = 0
        self.flow_started_at = now
        self.flow_stage_durations = {}
        self.flow_capture_seconds = 0.0
        self.flow_step_seconds = 0.0
        self.flow_frame_count = 0
        self.role_click_point = None
        self.enter_click_at = 0.0
        self.enter_click_attempts = 0
        self.enter_input_exhausted = False
        self.last_enter_verify_at = 0.0
        self.enter_clear_frames = 0
        self.enter_transition_started_at = 0.0
        self.post_popup_clear_frames = 0
        self.post_popup_quiet_since = 0.0
        self.post_popup_seen = set()
        self.auto_target_seen_frames = 0
        self.auto_x_seen_frames = 0
        self.auto_correction_pending = False
        self.auto_correction_started_at = 0.0
        self.last_auto_guard_warning_at = 0.0
        self.last_auto_ocr_at = 0.0
        self.last_auto_no_x_at = 0.0
        self.last_post_entry_warning_at = 0.0
        FLOW_COORDINATOR.activate(id(self))
        if OCR is not None and OCR.enabled:
            OCR.reset_thread_stats()
        if reason:
            LOG.info("[%s] 智慧重連計時開始：%s", self.name, reason)

    def finish_flow(self, now: float, result: str) -> Tuple[float, str]:
        if self.flow_started_at <= 0.0:
            return 0.0, ""
        self._accumulate_state_time(now)
        elapsed = max(0.0, now - self.flow_started_at)
        self.last_flow_duration = elapsed
        self.last_flow_result = str(result)
        self.last_flow_finished_wall = time.time()
        parts = [
            f"{name} {seconds:.1f}s"
            for name, seconds in sorted(self.flow_stage_durations.items(), key=lambda kv: kv[1], reverse=True)
            if seconds >= 0.05
        ]
        breakdown = "｜".join(parts[:8])
        self.last_perf_summary = f"取樣 {self.flow_frame_count} 幀｜擷取/排隊 {self.flow_capture_seconds:.1f}s｜辨識 {self.flow_step_seconds:.1f}s"
        if OCR is not None and OCR.enabled:
            ocr_wait, ocr_run, ocr_calls = OCR.thread_stats()
            self.last_perf_summary += f"｜OCR排隊 {ocr_wait:.1f}s｜OCR執行 {ocr_run:.1f}s/{ocr_calls}次"
        self.flow_started_at = 0.0
        self.flow_stage_durations = {}
        FLOW_COORDINATOR.deactivate(id(self))
        return elapsed, breakdown

    def set_state(self, state: str):
        if self.state != state:
            now = time.monotonic()
            self._accumulate_state_time(now)
            LOG.info("[%s] 狀態：%s → %s", self.name, self.state, state)
            self.state = state
            self.state_since = now
            self.set_event(f"流程：{state}")

    def save_debug_frame(self, frame: np.ndarray, tag: str):
        if not CONFIG.get("自動保存診斷截圖", True):
            return
        now = time.monotonic()
        cooldown = max(1.0, float(CONFIG.get("診斷截圖冷卻秒", 4.0)))
        if now - self.last_debug_save_at < cooldown:
            return
        self.last_debug_save_at = now
        try:
            debug_dir = LOG_DIR / "debug"
            debug_dir.mkdir(exist_ok=True)
            safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", f"{self.name}_{tag}")[:80]
            p = debug_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{safe}.png"
            ok, buf = cv2.imencode(".png", frame)
            if ok:
                buf.tofile(str(p))
                LOG.info("[%s] 已保存診斷畫面：%s", self.name, p.name)
        except Exception as e:
            LOG.debug("[%s] 保存診斷畫面失敗：%r", self.name, e)

    def relaunch_for_battle_disconnect(self) -> bool:
        """
        戰鬥場景斷線專用：
        1. 目前 worker 已知道自己綁定哪一份捷徑設定。
        2. 直接背景啟動該捷徑，不切桌面、不移動滑鼠。
        3. 等待新的 Flash 視窗出現後，把同一個 worker 改綁到新視窗。
        4. 成功後可背景關閉舊視窗，避免留下重複客戶端。
        """
        if not CONFIG.get("戰鬥斷線重新啟動", True):
            return False
        if not self.profile.get("戰鬥斷線允許重開", True):
            return False

        raw = str(self.profile.get("捷徑路徑", "")).strip()
        if not raw:
            LOG.warning("[%s] 戰鬥斷線需要重開，但此視窗尚未設定「捷徑路徑」；改走原視窗登入流程。", self.name)
            return False

        shortcut = Path(os.path.expandvars(raw)).expanduser()
        if not shortcut.exists():
            LOG.warning("[%s] 戰鬥斷線需要重開，但捷徑不存在：%s；改走原視窗登入流程。", self.name, shortcut)
            return False

        old_hwnd = self.hwnd
        # V11.7：重開不是重新設定視窗；精確保存使用者此刻看到的 DWM 實體外框，
        # 新視窗只恢復到同一位置/尺寸，不套 900x590、不改捷徑預設。
        old_visible_rect = window_geometry.visible_rect(old_hwnd)
        old_outer_rect = window_geometry.outer_rect(old_hwnd)
        wait_limit = max(5.0, float(CONFIG.get("戰鬥重開等待新視窗秒", 35.0)))

        with RELAUNCH_LOCK:
            RELAUNCH_GATE.set()
            try:
                before = set(enum_game_windows())
                LOG.info("[%s] 戰鬥場景斷線 → 背景重新啟動對應捷徑：%s", self.name, shortcut)
                if not launch_shortcut_no_activate(shortcut):
                    return False

                deadline = time.monotonic() + wait_limit
                new_hwnd: Optional[int] = None
                while time.monotonic() < deadline and not self.stop_event.is_set():
                    time.sleep(0.15)
                    after = enum_game_windows()
                    candidates = [
                        h for h in after
                        if h not in before and h != old_hwnd and not is_ignored_hwnd(h)
                    ]
                    if candidates:
                        new_hwnd = candidates[0]
                        break

                if new_hwnd is None:
                    LOG.warning("[%s] 重開捷徑後 %s 秒內沒有偵測到新的 Flash 視窗；改走原視窗登入流程。", self.name, int(wait_limit))
                    return False

                # 新 Flash 一出現就恢復到舊視窗的實體可見位置/尺寸；不啟用視窗、不改使用者設定。
                restore_ok = False
                if old_visible_rect:
                    restore_ok = window_geometry.restore_visible_rect_noactivate(new_hwnd, old_visible_rect)
                elif old_outer_rect:
                    restore_ok = window_geometry.restore_outer_noactivate(new_hwnd, old_outer_rect)
                LOG.info("[%s] 戰鬥重開新視窗位置/尺寸恢復：%s；目標=%s。", self.name, "成功" if restore_ok else "未確認", old_visible_rect or old_outer_rect or "未知")

                self.hwnd = new_hwnd
                transfer_window_binding(old_hwnd, new_hwnd)
                # HWND 已換，強制刷新同一份綁定與輸入幾何到新視窗；不能因 signature 相同就略過。
                self.binding_signature = None
                self.geometry_profile_shortcut = ""
                self._clear_window_geometry_cache()
                self.apply_binding(load_window_bindings().get(int(new_hwnd)), announce=False)
                LOG.info("[%s] 已由舊視窗 %s 重新綁定到新視窗 %s。", self.name, old_hwnd, new_hwnd)

                mark_ignored_hwnd(old_hwnd)
                if CONFIG.get("戰鬥重開後關閉舊視窗", True) and WIO.is_window(old_hwnd):
                    try:
                        win32api.PostMessage(old_hwnd, win32con.WM_CLOSE, 0, 0)
                        LOG.info("[%s] 已背景要求關閉舊戰鬥視窗 %s。", self.name, old_hwnd)
                    except Exception as e:
                        LOG.warning("[%s] 舊視窗 %s 關閉訊息失敗：%s", self.name, old_hwnd, e)

                self.force_clicked_at = 0.0
                self.force_retry_count = 0
                self.force_lock_until = 0.0
                self.force_post_wait_until = 0.0
                self.force_login_seen_frames = 0
                self.line_seen_frames = 0
                self.role_seen_frames = 0
                self.line_ocr_attempts = 0
                self.line_ocr_votes = []
                self.line_ocr_next_at = 0.0
                self.selected_line_no = 0
                self.line_click_at = 0.0
                self.line_click_attempts = 0
                self.role_wait_since = 0.0
                self.role_ocr_next_at = 0.0
                self.role_click_at = 0.0
                self.enter_click_at = 0.0
                self.entered_at = 0.0
                self.set_state("等待登入畫面")
                return True
            finally:
                RELAUNCH_GATE.clear()

    @staticmethod
    def _confirm_patch(frame: Optional[np.ndarray], x: int, y: int) -> Optional[np.ndarray]:
        if frame is None or frame.size == 0:
            return None
        h, w = frame.shape[:2]
        half_w = max(44, int(round(w * 0.075)))
        half_h = max(24, int(round(h * 0.050)))
        x0, x1 = max(0, int(x) - half_w), min(w, int(x) + half_w)
        y0, y1 = max(0, int(y) - half_h), min(h, int(y) + half_h)
        if x1 - x0 < 12 or y1 - y0 < 12:
            return None
        patch = frame[y0:y1, x0:x1]
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (96, 48), interpolation=cv2.INTER_AREA)

    def _disconnect_button_cleared(self, x: int, y: int, wait_s: float, baseline: Optional[np.ndarray] = None) -> Optional[bool]:
        """確認斷線「確定」是否真的生效。

        V8.3 不再要求「立刻辨識到完整登入頁」才算成功。舊 Flash 跨螢幕切頁時常先出現
        過渡幀，導致確定明明已按掉，狀態機卻仍判定失敗。現在接受兩種成功證據：
        1) 明確看到登入／線路／角色頁；
        2) 原斷線彈窗與確認鈕連續消失，且確認鈕附近畫面確實發生變化。
        """
        deadline = time.monotonic() + max(0.8, float(CONFIG.get("斷線確認轉頁驗證秒", 2.20)))
        saw_valid = False
        clear_streak = 0
        need_clear = max(2, int(CONFIG.get("斷線確認消失連續幀", 2)))
        min_change = max(1.0, float(CONFIG.get("斷線確認消失最低變化", 5.0)))
        base_patch = self._confirm_patch(baseline, x, y)

        while time.monotonic() < deadline:
            time.sleep(max(0.05, min(0.12, wait_s)))
            fresh = WIO.capture(self.hwnd)
            if fresh is None:
                continue
            saw_valid = True

            if detect_login_force(fresh, allow_ocr=False, fast=True) or detect_line_header(fresh, fast=True) or detect_character_screen(fresh):
                return True

            # 同時看 logical + raw，避免 150% DPI 正規化後小按鈕掉分。
            still_disc = bool(self.detect_disconnect_dual(fresh, allow_ocr=False))
            still_confirm = confirm_button_present_near(fresh, x, y)
            if still_disc or still_confirm:
                clear_streak = 0
                continue

            changed = False
            fresh_patch = self._confirm_patch(fresh, x, y)
            if base_patch is not None and fresh_patch is not None:
                try:
                    delta = float(cv2.absdiff(base_patch, fresh_patch).mean())
                    changed = delta >= min_change
                except Exception:
                    changed = False
            else:
                # 沒有基準圖時，連續兩個有效幀都看不到原斷線 UI 仍可視為轉頁。
                changed = True

            if changed:
                clear_streak += 1
                if clear_streak >= need_clear:
                    LOG.info("[%s] 斷線確認後原彈窗已連續消失 %d 幀；接受為轉頁成功，立即等待登入頁。", self.name, clear_streak)
                    return True
            else:
                clear_streak = 0

        if not saw_valid:
            return None
        return False

    def click_flow_action(
        self,
        x: int,
        y: int,
        note: str,
        *,
        interactive: bool = False,
        root: bool = False,
        hold_s: Optional[float] = None,
        post_wait_s: Optional[float] = None,
    ) -> bool:
        """點流程按鈕；已證實 Flash 拒絕背景訊息時，沿用前景實體模式。"""
        if self.foreground_input_required:
            return WIO.click_foreground_physical(
                self.hwnd,
                int(x),
                int(y),
                note,
                hold_s=hold_s,
                post_wait_s=post_wait_s,
            )
        if interactive:
            return WIO.click_interactive(self.hwnd, int(x), int(y), note, root=bool(root))
        if root:
            return WIO.click_root(self.hwnd, int(x), int(y), note)
        return WIO.click(self.hwnd, int(x), int(y), note)

    def _start_fishing_map_watch(self, frame: np.ndarray) -> None:
        self.fishing_map_baseline = build_map_fingerprint(frame)
        self.fishing_map_candidate_frames = 0
        self.fishing_map_last = None
        self.fishing_map_stable_frames = 0
        self.fishing_map_wait_deadline = 0.0
        self.fishing_map_settle_until = 0.0
        self.fishing_map_last_evidence = ""

    def _fishing_map_transition_step(self, frame: np.ndarray, now: float) -> bool:
        """Observe map change on existing monitor frames; return True when phase owns the step."""
        if self.fishing_phase == "等待結果":
            current = build_map_fingerprint(frame)
            changed, evidence = map_transition_changed(self.fishing_map_baseline, current)
            self.fishing_map_last_evidence = evidence
            if changed:
                self.fishing_map_candidate_frames += 1
            else:
                self.fishing_map_candidate_frames = 0
            need = max(2, min(5, int(CONFIG.get("釣魚轉圖連續確認幀", 2))))
            if self.fishing_map_candidate_frames < need:
                return False
            self.fishing_map_transition_count += 1
            self.fishing_map_last = current
            self.fishing_map_stable_frames = 0
            self.fishing_map_wait_deadline = now + max(
                12.0, float(CONFIG.get("釣魚轉圖最長等待秒", 30.0))
            )
            # Pause the 10-second fishing result timer. It will restart only
            # after the new map is stable, the 2-second buffer passes, and the
            # exact same link has been found again and double-clicked.
            self.fishing_deadline = 0.0
            self.fishing_phase = "等待轉圖穩定"
            self.set_event(f"釣魚：偵測到第 {self.fishing_map_transition_count} 次轉圖，等待新地圖穩定")
            LOG.info(
                "[%s] 釣魚途中偵測到轉圖（連續 %d 幀）：%s；暫停 10 秒驗證。",
                self.name, need, evidence,
            )
            return True

        if self.fishing_phase == "等待轉圖穩定":
            current = build_map_fingerprint(frame)
            stable, evidence = map_fingerprint_stable(self.fishing_map_last, current)
            self.fishing_map_last_evidence = evidence
            if stable:
                self.fishing_map_stable_frames += 1
            else:
                self.fishing_map_stable_frames = 0
            self.fishing_map_last = current
            need = max(2, min(5, int(CONFIG.get("釣魚轉圖穩定確認幀", 2))))
            if self.fishing_map_stable_frames >= need:
                buffer_s = max(2.0, float(CONFIG.get("釣魚轉圖穩定後等待秒", 2.0)))
                self.fishing_map_settle_until = now + buffer_s
                self.fishing_phase = "轉圖後等待2秒"
                self.set_event(f"釣魚：新地圖已穩定，額外等待 {buffer_s:.0f} 秒")
                LOG.info("[%s] 新地圖連續 %d 幀穩定：%s；再等 %.1f 秒。", self.name, need, evidence, buffer_s)
                return True
            if now >= self.fishing_map_wait_deadline:
                # Never click while the new map is uncertain. Extend the safe
                # observation window and keep disconnect/no-X monitoring alive.
                wait_s = max(12.0, float(CONFIG.get("釣魚轉圖最長等待秒", 30.0)))
                self.fishing_map_wait_deadline = now + wait_s
                self.set_event("釣魚：轉圖後畫面仍不穩定；不點擊，持續等待")
                LOG.warning("[%s] 轉圖後等待 %.0f 秒仍未穩定：%s；禁止沿用座標或盲點。", self.name, wait_s, evidence)
            return True

        if self.fishing_phase == "轉圖後等待2秒":
            if now >= self.fishing_map_settle_until:
                self._begin_fishing_channel_selection(
                    "reclick",
                    "轉圖緩衝完成；重新確認上下『目前』後雙擊原釣點",
                )
            return True
        return False

    def _fishing_send(self, frame: np.ndarray, now: float) -> None:
        profile = self.fishing_profile or {}
        message = self._current_fishing_message()
        links = fishing_profiles.parse_message_links(message)
        if not links:
            self.fishing_phase = "設定錯誤"
            self.set_event("釣魚設定沒有有效座標，已停止釣魚")
            LOG.error("[%s] 釣魚設定 '%s' 沒有有效座標；不發送任何聊天文字。", self.name, profile.get("name", ""))
            return
        controls = detect_chat_controls(frame)
        if controls is None:
            self.fishing_phase = "發送重試"
            self.fishing_send_at = now
            self.set_event("釣魚：找不到目前聊天輸入框／發送按鈕，稍後重試")
            LOG.warning("[%s] 釣魚發送前未能同時定位聊天輸入框與發送按鈕；不使用固定座標。", self.name)
            return
        input_point, send_point, route = controls
        sent = WIO.send_chat_message_background(
            self.hwnd,
            input_point,
            send_point,
            message,
            (
                f"[{self.name}] 釣魚/{profile.get('name', '')}/"
                f"訊息組{self.fishing_message_group_index + 1}/{max(1, len(self.fishing_message_groups))}"
            ),
        )
        self.fishing_send_at = time.monotonic()
        self.fishing_last_route = route
        if not sent:
            self.fishing_phase = "發送重試"
            self.set_event("釣魚：聊天輸入未成功，稍後重新辨識再試")
            return
        self.fishing_links = links
        self.fishing_link_points = []
        self.fishing_locate_deadline = self.fishing_send_at + max(3.0, float(CONFIG.get("釣魚連結出現等待秒", 6.0)))
        self.fishing_next_locate_at = self.fishing_send_at + 0.35
        self.fishing_link_locate_timeouts = 0
        self.fishing_phase = "等待連結"
        self.set_event(
            f"釣魚：已發送 {profile.get('name', '')} 第 {self.fishing_message_group_index + 1}/"
            f"{max(1, len(self.fishing_message_groups))} 組，正在辨識 {len(links)} 個座標"
        )
        LOG.info(
            "[%s] 釣魚字串已送出；設定=%s，訊息組=%d/%d，座標數=%d，畫布=%sx%s，控制定位=%s。",
            self.name, profile.get("name", ""), self.fishing_message_group_index + 1,
            max(1, len(self.fishing_message_groups)), len(links), frame.shape[1], frame.shape[0], route,
        )

    def _fishing_click_current_link(
        self,
        frame: np.ndarray,
        points: List[Tuple[int, int]],
        route: str,
        now: float,
        *,
        map_reclick: bool = False,
    ) -> None:
        count = len(points)
        if count <= 0:
            return
        self.fishing_link_index %= count
        point = points[self.fishing_link_index]
        note = f"[{self.name}] 背景釣魚座標 {self.fishing_link_index + 1}/{count}（同點雙擊）"
        sent = WIO.click_interactive(self.hwnd, int(point[0]), int(point[1]), note)
        if sent:
            time.sleep(max(0.06, min(0.30, float(CONFIG.get("釣魚雙擊間隔秒", 0.12)))))
            sent = WIO.click_interactive(self.hwnd, int(point[0]), int(point[1]), note)
        if not sent:
            self.fishing_phase = "發送重試"
            self.fishing_send_at = time.monotonic()
            self.set_event(f"釣魚：第 {self.fishing_link_index + 1}/{count} 個座標未成功送出，稍後重試")
            return
        clicked_at = time.monotonic()
        self._save_fishing_progress("已雙擊，等待結果")
        # New key intentionally avoids an old LocalAppData config retaining the
        # former 20-second value.  The confirmed V11.8.4 sequence is 10 seconds.
        wait_s = max(10.0, float(CONFIG.get("釣魚雙擊後驗證秒", 10.0)))
        self.fishing_deadline = clicked_at + wait_s
        self.fishing_link_points = list(points)
        self.fishing_phase = "等待結果"
        self._start_fishing_map_watch(frame)
        prefix = "轉圖後原釣點已重新雙擊" if map_reclick else "釣點已雙擊"
        self.set_event(f"釣魚：{prefix}（{self.fishing_link_index + 1}/{count}），{wait_s:.0f} 秒後驗證一次")
        LOG.info(
            "[%s] %s座標 %d/%d 已在同一點連點兩下；訊息組=%d/%d；辨識=%s；邏輯點=%s,%s；"
            "等待 %.0f 秒，期間斷線、X 與轉圖監測不中斷。",
            self.name, "轉圖後重新" if map_reclick else "釣魚", self.fishing_link_index + 1, count,
            self.fishing_message_group_index + 1, max(1, len(self.fishing_message_groups)),
            route, point[0], point[1], wait_s,
        )

    def _locate_fishing_links_dual(self, frame: np.ndarray) -> Tuple[List[Tuple[int, int]], str]:
        points, route = locate_fishing_link_points(frame, self.fishing_links)
        if len(points) == len(self.fishing_links):
            return points, f"logical/{route}"
        raw = WIO.get_last_raw(self.hwnd)
        if raw is None or raw is frame:
            return points, route
        try:
            if raw.shape[:2] == frame.shape[:2]:
                return points, route
        except Exception:
            return points, route
        raw_points, raw_route = locate_fishing_link_points(raw, self.fishing_links)
        if len(raw_points) != len(self.fishing_links):
            return points, f"logical={route}; raw={raw_route}"
        logical_points = [WIO.raw_point_to_logical(self.hwnd, x, y) for x, y in raw_points]
        return logical_points, f"raw/{raw_route}→logical"

    def _detect_fishing_status_dual(self, frame: np.ndarray, priority: str) -> Tuple[bool, str]:
        wanted = str((self.fishing_profile or {}).get("success_text", "正在釣魚") or "正在釣魚")
        found, evidence = detect_fishing_status(frame, wanted, priority=priority)
        if found:
            return True, f"logical/{evidence}"
        raw = WIO.get_last_raw(self.hwnd)
        if raw is None or raw is frame:
            return False, evidence
        try:
            if raw.shape[:2] == frame.shape[:2]:
                return False, evidence
        except Exception:
            return False, evidence
        raw_found, raw_evidence = detect_fishing_status(raw, wanted, priority=priority)
        return raw_found, f"logical={evidence}; raw={raw_evidence}"

    def _fishing_step(self, frame: np.ndarray, now: float) -> None:
        """Run fishing as a side state machine; disconnect state always wins."""
        self._refresh_fishing_profile()
        if not self.fishing_profile:
            return
        if manor_runtime.is_hwnd_active(self.hwnd):
            self.fishing_phase = "等待莊園"
            self.set_event("莊園執行中；同一角色暫停釣魚，斷線監測持續")
            return
        if self.fishing_phase == "等待莊園":
            self._reset_fishing_runtime("莊園流程完成")
        if self.state != "監看" or self.flow_started_at > 0.0:
            self.fishing_phase = "等待重連"
            return
        # Startup still probes for login/line/role pages. Fishing cannot safely
        # type until that hand-off window proves the client is already in game.
        if now <= self.startup_login_probe_until:
            self.fishing_phase = "等待遊戲穩定"
            self.set_event("釣魚已勾選，等待登入頁探測完成")
            return
        if not self.fishing_prerequisites_ready:
            self.fishing_phase = "等待遊戲整理"
            allow_chat = now - self.last_in_game_evidence_probe >= 0.90
            if allow_chat:
                self.last_in_game_evidence_probe = now
            evidence = self._detect_in_game_evidence(frame, allow_chat=allow_chat)
            if evidence:
                self._begin_post_entry_cleanup(now, evidence)
            else:
                self.set_event("釣魚等待：先確認遊戲畫面、清除彈窗並驗證右下角無 X")
            return
        if self.fishing_phase in ("等待重連", "等待遊戲穩定"):
            self._reset_fishing_runtime("遊戲已恢復監看")

        if self.fishing_phase == "恢復檢查":
            found, evidence = self._detect_fishing_status_dual(frame, priority="flow")
            self.fishing_resume_checks += 1
            if found:
                self.fishing_phase = "釣魚中"
                self.fishing_missing_checks = 0
                self.fishing_next_status_check = now + max(6.0, float(CONFIG.get("釣魚成功後複查秒", 10.0)))
                self._save_fishing_progress("正在釣魚")
                self.set_event("已恢復中斷前釣魚進度；目前仍在釣魚")
                LOG.info("[%s] 啟動後續跑原釣點，已確認仍在釣魚：%s。", self.name, evidence)
            elif self.fishing_resume_checks >= 2:
                self.fishing_phase = "準備飛行"
                self.set_event(
                    f"恢復中斷前進度：訊息組 {self.fishing_message_group_index + 1}、座標 {self.fishing_link_index + 1}"
                )
                LOG.info(
                    "[%s] 啟動後未見正在釣魚；保留訊息組 %d、座標 %d，重新走前置後續跑。",
                    self.name, self.fishing_message_group_index + 1, self.fishing_link_index + 1,
                )
            return

        if self.fishing_phase in (
            "準備飛行", "確認系統列展開", "確認飛行", "準備收回系統列",
            "確認系統列收回", "準備聊天", "確認聊天清除", "前置完成",
        ):
            if self._fishing_prepare_step(frame, now):
                self._begin_fishing_channel_selection("send", "釣魚前置完成；下一步選擇上排『目前』")
            return

        # No-X is required before starting a new fishing action. Once a link has
        # been double-clicked, the game may legitimately show X while that action
        # runs. Toggling it here cancels the action and interleaves a repair click
        # between fishing steps. Disconnect/map/status monitoring remains active.
        fishing_action_active = self.fishing_phase in (
            "等待結果",
            "等待轉圖穩定",
            "轉圖後等待2秒",
            "轉圖後等待連結",
            "釣魚中",
        )
        if not fishing_action_active and not self._maintain_no_x_auto_battle(frame, now):
            return

        if self.fishing_phase == "待無X後重送":
            self._begin_fishing_channel_selection("send", "無 X 已恢復；重新確認上下『目前』後重做本次釣魚")
            return

        if self.fishing_phase == "釣魚中待無X":
            found, evidence = self._detect_fishing_status_dual(frame, priority="flow")
            if found:
                self.fishing_phase = "釣魚中"
                self.fishing_missing_checks = 0
                self._save_fishing_progress("正在釣魚")
                self.fishing_next_status_check = now + max(6.0, float(CONFIG.get("釣魚成功後複查秒", 10.0)))
                self.set_event("無 X 已恢復且仍在釣魚；繼續常駐監測")
                LOG.info("[%s] 修正 X 後『正在釣魚』仍存在：%s；不重送。", self.name, evidence)
            else:
                self._begin_fishing_channel_selection(
                    "send", "無 X 已恢復，但正在釣魚已消失；重新確認頻道後發送目前釣點"
                )
                LOG.warning("[%s] 修正 X 後未見『正在釣魚』：%s；從目前分頁重新發送。", self.name, evidence)
            return

        # Map-change observation uses the same already captured monitor frame.
        # It owns only the result/transition phases; disconnect and no-X guards
        # above remain higher priority at all times.
        if self._fishing_map_transition_step(frame, now):
            return

        if self.fishing_phase in ("待選目前分頁", "等待目前分頁變紅"):
            self._select_current_chat_tab_step(frame, now)
            return


        if self.fishing_phase in (
            "待確認發送目前頻道",
            "等待發送頻道選單",
            "等待發送頻道目前確認",
        ):
            self._select_sender_current_channel_step(frame, now)
            return

        if self.fishing_phase == "待發送":
            self._fishing_send(frame, now)
            return

        if self.fishing_phase == "發送重試":
            retry = max(2.0, float(CONFIG.get("釣魚發送重試秒", 4.0)))
            if now - self.fishing_send_at >= retry:
                self._begin_fishing_channel_selection("send", "釣魚：重新確認上下『目前』後再發送")
            return

        if self.fishing_phase == "等待連結":
            if now < self.fishing_next_locate_at:
                return
            self.fishing_next_locate_at = now + 1.0
            points, route = self._locate_fishing_links_dual(frame)
            if len(points) == len(self.fishing_links):
                self._fishing_click_current_link(frame, points, route, now)
                return
            if now >= self.fishing_locate_deadline:
                self.fishing_link_locate_timeouts += 1
                # Keep the exact message/link indices and yield this worker until
                # the next background capture. Other window workers continue.
                retry = max(4.0, float(CONFIG.get("釣魚連結定位輪巡秒", 6.0)))
                self.fishing_next_locate_at = now + retry
                self.fishing_locate_deadline = now + retry
                self.set_event(
                    f"釣魚：已發送但尚未定位完整座標；保留本次訊息，{retry:.0f} 秒後背景重找"
                )
                LOG.warning(
                    "[%s] 釣魚連結定位逾時第 %d 次：%s；不重送、不推算點擊，先輪巡其他視窗。",
                    self.name, self.fishing_link_locate_timeouts, route,
                )
            return

        if self.fishing_phase == "轉圖後等待連結":
            points, route = self._locate_fishing_links_dual(frame)
            if len(points) == len(self.fishing_links):
                self._fishing_click_current_link(frame, points, route, now, map_reclick=True)
                return
            if now >= self.fishing_reclick_locate_deadline:
                # The old chat row may have scrolled away during loading. Both
                # channels were just verified, so resend the same message group;
                # link/group indices are deliberately unchanged.
                self._begin_fishing_channel_selection(
                    "send", "轉圖後找不到原訊息；重新發送同一組，再雙擊同一釣點"
                )
                LOG.warning(
                    "[%s] 轉圖後未完整找到原訊息的 %d 個連結：%s；重送同一訊息組，不更換釣點。",
                    self.name, len(self.fishing_links), route,
                )
            return

        if self.fishing_phase == "等待結果":
            if now < self.fishing_deadline:
                return
            found, evidence = self._detect_fishing_status_dual(frame, priority="flow")
            if found:
                check_gap = max(6.0, float(CONFIG.get("釣魚成功後複查秒", 10.0)))
                self.fishing_phase = "釣魚中"
                self.fishing_next_status_check = now + check_gap
                self.fishing_missing_checks = 0
                self.set_event(f"釣魚成功：{self.fishing_profile.get('name', '')}；持續斷線監測")
                LOG.info("[%s] 釣魚成功確認：%s；接下來只在既有監測幀低頻複查，不增加擷取。", self.name, evidence)
                return
            old_group = self.fishing_message_group_index + 1
            old_group_count = max(1, len(self.fishing_message_groups))
            old_link = self.fishing_link_index + 1
            old_link_count = max(1, len(self.fishing_links))
            group_no, group_count, link_no, link_count = self._advance_fishing_target()
            self.fishing_deadline = 0.0
            self._begin_fishing_channel_selection(
                "send",
                f"釣魚未成功（{evidence}），改試訊息組 {group_no}/{group_count}、座標 {link_no}/{link_count}",
            )
            LOG.warning(
                "[%s] 訊息組 %d/%d、座標 %d/%d 雙擊後等待 %.0f 秒仍未看到『正在釣魚』：%s；"
                "下一次確認上下『目前』後改試訊息組 %d/%d、座標 %d/%d（完成輪數=%d）。",
                self.name, old_group, old_group_count, old_link, old_link_count,
                max(10.0, float(CONFIG.get("釣魚雙擊後驗證秒", 10.0))), evidence,
                group_no, group_count, link_no, link_count, self.fishing_round,
            )
            return

        if self.fishing_phase == "釣魚中":
            if now < self.fishing_next_status_check:
                return
            check_gap = max(6.0, float(CONFIG.get("釣魚成功後複查秒", 10.0)))
            self.fishing_next_status_check = now + check_gap
            found, evidence = self._detect_fishing_status_dual(frame, priority="flow")
            if found:
                self.fishing_missing_checks = 0
                return
            self.fishing_missing_checks += 1
            # Two OCR misses caused a live 120古 false reset followed by an
            # immediate recovery eight seconds later. Three spaced misses keep
            # monitoring responsive without restarting on one short OCR wobble.
            need = max(3, min(5, int(CONFIG.get("釣魚狀態消失確認次數", 3))))
            if self.fishing_missing_checks < need:
                self.set_event(f"釣魚狀態暫時未見，確認 {self.fishing_missing_checks}/{need}；斷線監測持續")
                return
            LOG.warning("[%s] 『正在釣魚』已連續 %d 次未見（%s）；從第1個座標重新輪詢。", self.name, need, evidence)
            self._reset_fishing_runtime("釣魚狀態消失")
            self.set_event("正在釣魚已消失，重新發送並從第1個座標循環")
            return

    def click_disconnect_verified(self, x: int, y: int, label: str, baseline: Optional[np.ndarray] = None) -> bool:
        """斷線確認：左側跨 DPI 只用目標 DPI context；正常 DPI 完整維持 V8.8。"""
        wait_s = float(CONFIG.get("背景點擊驗證等待秒", 0.14))
        self.disconnect_attempt += 1
        attempt = self.disconnect_attempt
        tried = []
        physical_attempted = False
        if baseline is None:
            baseline = WIO.capture(self.hwnd)
        g = WIO.geometry.get(int(self.hwnd))
        high_dpi_padded = bool(
            g is not None
            and WIO.surface_mode.get(int(self.hwnd)) == "logical-padded-crop"
            and int(g.monitor_dpi) != int(g.root_dpi)
        )

        if self.foreground_input_required:
            physical_attempted = True
            WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] {label} → 確認（已校準備援）")
            cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
            tried.append("前景實體點擊/已校準")
            if cleared is True:
                LOG.warning("[%s] 斷線確認已生效；方式=前景實體點擊（已校準），第%d輪。", self.name, attempt)
                self.set_event("斷線確認已生效（前景實體備援）")
                self.disconnect_attempt = 0
                return True
            if cleared is None:
                self.set_event("前景實體點擊後畫面暫時無法驗證")
                return False

        if high_dpi_padded:
            WIO.click_target_dpi_context(self.hwnd, x, y, f"[{self.name}] {label} → 確認")
            cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
            tried.append("目標DPI上下文/ShockwaveFlash")
            if cleared is True:
                LOG.info("[%s] 斷線確認已生效；方式=目標DPI上下文/ShockwaveFlash，第%d輪。", self.name, attempt)
                self.set_event("斷線確認已生效")
                self.disconnect_attempt = 0
                return True
            if cleared is None:
                LOG.warning("[%s] 目標DPI上下文點擊後暫時無有效背景畫面，不能假設成功。", self.name)
                self.set_event("斷線已辨識，但背景畫面暫時無法驗證輸入")
                return False

            # 實機紀錄已證實這類極端 DPI Flash 宿主可同時忽略全部訊息座標。
            # 最有原生語意的「目標 DPI 上下文」已由重新擷取證明失敗後，
            # 立即試真實輸入；只有真實輸入也不可用，才繼續舊的訊息路由。
            if (not physical_attempted and FOREGROUND_PHYSICAL_FALLBACK
                    and bool(CONFIG.get("允許前景實體輸入備援", True))):
                physical_attempted = True
                self.set_event("目標 DPI 背景輸入無效，立即嘗試前景實體點擊")
                sent = WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] {label} → 確認（高 DPI 實機備援）")
                tried.append("前景實體點擊/高DPI快速備援")
                if sent:
                    cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
                    if cleared is True:
                        self.foreground_input_required = True
                        WIO.clear_mouse_calibration(self.hwnd)
                        LOG.warning(
                            "[%s] 畫面已證實高 DPI 前景實體點擊生效；後續全流程改用同一模式。",
                            self.name,
                        )
                        self.set_event("斷線確認已生效（前景實體備援已校準）")
                        self.disconnect_attempt = 0
                        return True
                    if cleared is None:
                        self.set_event("前景實體點擊已送出，但畫面暫時無法驗證")
                        return False
                LOG.warning("[%s] 高 DPI 前景實體點擊也未生效，繼續嘗試剩餘訊息路由。", self.name)

            # 目標 DPI context 並非所有 Flash 11 宿主都接受。失敗時改走同一個
            # 邏輯點的 Windows 合法座標表示；每送一種就重新擷取，只有畫面真的
            # 離開斷線框才保存該路由。全程不移動滑鼠、不切前景。
            raw_modes: Dict[bool, List[str]] = {}
            for root_route in (False, True):
                _t, _c, _p, _vals = WIO._message_point_candidates(
                    self.hwnd, x, y, root=root_route
                )
                raw_modes[root_route] = [m for m, _mx, _my in _vals]
            preferred = [
                "邏輯畫布直送",
                "目標原生邏輯",
                "Windows視窗映射",
                "視窗客戶區",
                "實體相對",
                "Flash輸入基準",
                "接收客戶區尺寸映射",
                "接收視窗實際尺寸映射",
            ]
            routes: List[Tuple[str, bool]] = []
            for wanted in preferred:
                for root_route in (False, True):
                    if wanted in raw_modes.get(root_route, []) and (wanted, root_route) not in routes:
                        routes.append((wanted, root_route))
            for mode, root_route in routes:
                place = "頂層" if root_route else "Flash子視窗"
                WIO.click_interactive(
                    self.hwnd,
                    x,
                    y,
                    f"[{self.name}] {label} → 確認",
                    root=root_route,
                    mode=mode,
                )
                cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
                tried.append(f"{place}/{mode}")
                if cleared is True:
                    WIO.confirm_click_mode(self.hwnd, mode, f"斷線確認/{place}", root=root_route)
                    LOG.info(
                        "[%s] 高 DPI 斷線確認已生效；方式=%s/%s，第%d輪。",
                        self.name, place, mode, attempt,
                    )
                    self.set_event("斷線確認已生效")
                    self.disconnect_attempt = 0
                    return True
                if cleared is None:
                    LOG.warning("[%s] %s/%s 後無法驗證背景畫面；停止本輪避免重複輸入。", self.name, place, mode)
                    self.set_event("斷線已辨識，但背景畫面暫時無法驗證輸入")
                    return False

            WIO.press_enter(self.hwnd, f"[{self.name}] {label} → 確認")
            cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
            tried.append("Enter備援")
            if cleared is True:
                WIO.clear_mouse_calibration(self.hwnd)
                LOG.info("[%s] 高 DPI 斷線確認已生效；方式=Enter備援，第%d輪。", self.name, attempt)
                self.set_event("斷線確認已生效")
                self.disconnect_attempt = 0
                return True
        else:
            # 以下為 V8.8 正常 DPI 原流程。
            for root in (False, True):
                modes = WIO.candidate_modes(self.hwnd, x, y, root=root)
                for mode in modes:
                    place = "頂層" if root else "Flash子視窗"
                    WIO.click_mode(self.hwnd, x, y, mode, f"[{self.name}] {label} → 確認", root=root)
                    cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
                    tried.append(f"{place}/{mode}")
                    if cleared is True:
                        WIO.confirm_click_mode(self.hwnd, mode, f"斷線確認/{place}", root=root)
                        LOG.info("[%s] 斷線確認已生效；方式=%s/%s，第%d輪。", self.name, place, mode, attempt)
                        self.set_event("斷線確認已生效")
                        self.disconnect_attempt = 0
                        return True
                    if cleared is None:
                        LOG.warning("[%s] %s/%s 點擊後暫時無有效背景畫面，不能假設成功。", self.name, place, mode)
                    else:
                        LOG.warning("[%s] %s/%s 點擊後原斷線畫面仍存在，繼續下一種原生座標。", self.name, place, mode)
            WIO.press_enter(self.hwnd, f"[{self.name}] {label} → 確認")
            cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
            if cleared is True:
                WIO.clear_mouse_calibration(self.hwnd)
                LOG.info("[%s] 斷線確認已生效；方式=Enter備援，第%d輪。已清除滑鼠校準，強制登入將從邏輯座標重新探測。", self.name, attempt)
                self.disconnect_attempt = 0
                return True

        # 實機紀錄已證明：某些 32-bit Flash 11 宿主在極端 DPI 虛擬化下，
        # PostMessage/SendMessage/AttachThreadInput/Enter 全部會被接收但完全不執行。
        # 所有背景路由都由重新擷取證明無效後，才進入這條最後備援。
        if (not physical_attempted and FOREGROUND_PHYSICAL_FALLBACK
                and bool(CONFIG.get("允許前景實體輸入備援", True))):
            self.set_event("背景輸入全部無效，嘗試前景實體點擊備援")
            sent = WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] {label} → 確認（最後備援）")
            tried.append("前景實體點擊")
            if sent:
                cleared = self._disconnect_button_cleared(x, y, wait_s, baseline=baseline)
                if cleared is True:
                    self.foreground_input_required = True
                    WIO.clear_mouse_calibration(self.hwnd)
                    LOG.warning(
                        "[%s] 畫面已證實前景實體點擊生效；本 Flash 後續選線、選角、進入遊戲改用同一模式。",
                        self.name,
                    )
                    self.set_event("斷線確認已生效（前景實體備援已校準）")
                    self.disconnect_attempt = 0
                    return True
                if cleared is None:
                    self.set_event("前景實體點擊已送出，但畫面暫時無法驗證")
                    return False

        self.set_event(f"斷線已辨識，但背景確認未生效；第{attempt}輪稍後重試")
        LOG.warning("[%s] 斷線確認本輪未生效；已嘗試 %s。", self.name, ", ".join(tried))
        return False

    def _force_login_changed(self, wait_s: float = 0.34) -> Optional[bool]:
        """True=登入頁已離開/線路或角色已出現；False=強制登入仍在；None=無法擷取。"""
        time.sleep(max(0.08, wait_s))
        fresh = WIO.capture(self.hwnd)
        if fresh is None:
            return None
        if detect_line_header(fresh) or detect_character_screen(fresh):
            return True
        return detect_login_force(fresh) is None

    def click_force_login_verified(self, m: Match) -> Optional[bool]:
        """強制登入專用：子視窗點擊失敗就立刻改送頂層，不等 6 秒才重試。"""
        x, y = m.center
        WIO.click(self.hwnd, x, y, f"[{self.name}] 強制登入")
        changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
        if changed is True:
            LOG.info("[%s] 強制登入點擊已生效（同步子視窗）。", self.name)
            return True
        if changed is None:
            LOG.warning("[%s] 強制登入後暫時無法擷取，先進入等待後續畫面。", self.name)
            return None
        LOG.warning("[%s] 強制登入仍在，立即改用同步頂層背景點擊。", self.name)
        WIO.click_root(self.hwnd, x, y, f"[{self.name}] 強制登入（頂層備援）")
        changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
        if changed is True:
            LOG.info("[%s] 強制登入點擊已生效（同步頂層）。", self.name)
            return True
        if changed is False:
            if FOREGROUND_PHYSICAL_FALLBACK and bool(CONFIG.get("允許前景實體輸入備援", True)):
                WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] 強制登入（最後備援）")
                changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
                if changed is True:
                    self.foreground_input_required = True
                    LOG.warning("[%s] 強制登入已由畫面證實生效；方式=前景實體點擊。", self.name)
                    return True
            LOG.warning("[%s] 背景與前景備援點擊後仍停在強制登入頁；不假裝成功，稍後重試。", self.name)
        return changed

    def click_force_login_target_dpi_verified(self, m: Match) -> bool:
        x, y = m.center
        physical_attempted = False
        if self.foreground_input_required:
            physical_attempted = True
            WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] 強制登入（已校準備援）")
            changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
            if changed is True:
                LOG.warning("[%s] 強制登入已由畫面證實生效；方式=前景實體點擊（已校準）。", self.name)
                return True
            if changed is None:
                LOG.warning("[%s] 前景實體強制登入後暫時無法擷取，不開始 20 秒。", self.name)
                return False
            LOG.warning("[%s] 已校準的前景實體強制登入未生效，本輪繼續完整備援路由。", self.name)

        WIO.click_target_dpi_context(self.hwnd, x, y, f"[{self.name}] 強制登入")
        changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
        if changed is True:
            LOG.info("[%s] 強制登入已由重新擷取證實生效；方式=目標DPI上下文/ShockwaveFlash。", self.name)
            return True
        if changed is None:
            LOG.warning("[%s] 目標DPI上下文強制登入後暫時無有效背景畫面；不開始 20 秒。", self.name)
            return False

        if (not physical_attempted and FOREGROUND_PHYSICAL_FALLBACK
                and bool(CONFIG.get("允許前景實體輸入備援", True))):
            physical_attempted = True
            WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] 強制登入（高 DPI 實機備援）")
            changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
            if changed is True:
                self.foreground_input_required = True
                WIO.clear_mouse_calibration(self.hwnd)
                LOG.warning(
                    "[%s] 高 DPI 強制登入已由畫面證實生效；方式=前景實體點擊。"
                    "後續選線、選角、進入遊戲改用同一模式。",
                    self.name,
                )
                return True
            if changed is None:
                LOG.warning("[%s] 前景實體強制登入後無法驗證畫面；不開始 20 秒。", self.name)
                return False

        LOG.warning("[%s] 目標DPI上下文強制登入未生效；開始逐路由驗證。", self.name)
        raw_modes: Dict[bool, List[str]] = {}
        for root_route in (False, True):
            _t, _c, _p, _vals = WIO._message_point_candidates(
                self.hwnd, x, y, root=root_route
            )
            raw_modes[root_route] = [mode for mode, _mx, _my in _vals]
        preferred = [
            "邏輯畫布直送",
            "目標原生邏輯",
            "Windows視窗映射",
            "視窗客戶區",
            "實體相對",
            "Flash輸入基準",
            "接收客戶區尺寸映射",
            "接收視窗實際尺寸映射",
        ]
        routes: List[Tuple[str, bool]] = []
        for wanted in preferred:
            for root_route in (False, True):
                if wanted in raw_modes.get(root_route, []) and (wanted, root_route) not in routes:
                    routes.append((wanted, root_route))

        for mode, root_route in routes:
            WIO.click_interactive(
                self.hwnd,
                x,
                y,
                f"[{self.name}] 強制登入",
                root=root_route,
                mode=mode,
            )
            changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
            place = "頂層" if root_route else "Flash子視窗"
            if changed is True:
                WIO.confirm_click_mode(self.hwnd, mode, f"強制登入/{place}", root=root_route)
                LOG.info("[%s] 高 DPI 強制登入已證實；方式=%s/%s。", self.name, place, mode)
                return True
            if changed is None:
                LOG.warning("[%s] %s/%s 後背景畫面無法驗證；停止本輪。", self.name, place, mode)
                return False

        WIO.press_enter(self.hwnd, f"[{self.name}] 強制登入")
        changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
        if changed is True:
            WIO.clear_mouse_calibration(self.hwnd)
            LOG.info("[%s] 高 DPI 強制登入已證實；方式=Enter備援。", self.name)
            return True
        if (not physical_attempted and FOREGROUND_PHYSICAL_FALLBACK
                and bool(CONFIG.get("允許前景實體輸入備援", True))):
            WIO.click_foreground_physical(self.hwnd, x, y, f"[{self.name}] 強制登入（最後備援）")
            changed = self._force_login_changed(float(CONFIG.get("強制登入點擊驗證等待秒", 0.34)))
            if changed is True:
                self.foreground_input_required = True
                WIO.clear_mouse_calibration(self.hwnd)
                LOG.warning(
                    "[%s] 高 DPI 強制登入已由畫面證實生效；方式=前景實體點擊。"
                    "後續選線、選角、進入遊戲改用同一模式。",
                    self.name,
                )
                return True
            if changed is None:
                LOG.warning("[%s] 前景實體強制登入後無法驗證畫面；不開始 20 秒。", self.name)
                return False
        LOG.warning("[%s] 高 DPI 強制登入所有背景與前景驗證路由均未生效；稍後重試。", self.name)
        return False

    def scan_interval(self) -> float:
        """V6.3 依流程階段決定下一幀時間；不再讓所有狀態共用同一個慢輪詢。"""
        if not bool(CONFIG.get("高速狀態機", True)):
            return (max(0.15, float(CONFIG.get("正常監測間隔秒", 0.30)))
                    if self.state == "監看" else max(0.05, float(CONFIG.get("重連流程掃描間隔秒", 0.16))))
        if self.state == "監看":
            if FLOW_COORDINATOR.any_active() and not FLOW_COORDINATOR.is_active(id(self)) and not FLOW_COORDINATOR.in_discovery_burst():
                return max(0.35, float(CONFIG.get("同時重連時閒置監測間隔秒", 0.85)))
            return max(0.20, float(CONFIG.get("正常監測間隔秒", 0.36)))
        key = {
            "等待斷線確認": "高速等待登入掃描秒",
            "等待登入畫面": "高速等待登入掃描秒",
            "強制登入固定等待": "高速等待登入後掃描秒",
            "等待登入後畫面": "高速等待登入後掃描秒",
            "選擇線路": "高速選線掃描秒",
            "等待角色畫面": "高速等待角色掃描秒",
            "選擇角色": "高速選角掃描秒",
            "等待進入遊戲按鈕": "高速進入遊戲掃描秒",
            "進入遊戲後整理": "高速整理掃描秒",
        }.get(self.state)
        if key:
            return max(0.045, float(CONFIG.get(key, 0.08)))
        return max(0.06, float(CONFIG.get("重連流程掃描間隔秒", 0.08)))

    def run(self):
        LOG.info("[%s] 開始背景監看，視窗=%s，優先角色=%s", self.name, self.hwnd, self.profile.get("優先角色", ""))
        fail_interval = max(0.30, float(CONFIG.get("背景擷取失敗重試秒", 0.80)))

        # 多視窗啟動時錯開一點，避免所有視窗同一毫秒搶著擷取。
        time.sleep((self.hwnd % 7) * 0.018)

        try:
            while not self.stop_event.is_set() and WIO.is_window(self.hwnd):
                if self.pause_event.is_set():
                    time.sleep(0.20)
                    continue
                # 唯一的固定等待：強制登入按下後至少 20 秒。這段不擷取畫面、不重按，
                # 其餘線路／角色／進入遊戲全部維持 V6.3 高速事件驅動。
                if self.state == "強制登入固定等待":
                    now_wait = time.monotonic()
                    if now_wait < self.force_wait_until:
                        time.sleep(min(0.25, max(0.02, self.force_wait_until - now_wait)))
                        continue
                    self.force_post_wait_until = now_wait + max(0.0, float(CONFIG.get("強制登入後轉頁觀察秒", 3.0)))
                    self.force_login_seen_frames = 0
                    self.set_state("等待登入後畫面")
                    self.set_event("強制登入 20 秒等待完成，優先辨識下一頁")
                loop_started = time.monotonic()
                if self.state == "監看":
                    wait_monitor_capture_slot(self.stop_event)
                    if self.stop_event.is_set():
                        break
                    loop_started = time.monotonic()
                cap_started = loop_started
                minimized_probe = False
                frame, minimized_probe = self.capture_worker_frame()
                cap_spent = time.monotonic() - cap_started
                if self.flow_started_at > 0.0:
                    self.flow_capture_seconds += cap_spent
                if frame is not None:
                    self.capture_ok = True
                    self.last_capture_wall = time.time()
                    step_started = time.monotonic()
                    try:
                        self.step(frame)
                    except Exception as e:
                        self.set_event(f"辨識錯誤：{e}")
                        LOG.exception("[%s] 辨識流程錯誤：%s", self.name, e)
                    step_spent = time.monotonic() - step_started
                    if self.flow_started_at > 0.0:
                        self.flow_step_seconds += step_spent
                        self.flow_frame_count += 1
                    interval = self.scan_interval()
                else:
                    try:
                        minimized_waiting = bool(
                            win32gui.IsIconic(self.hwnd)
                            and self._minimized_monitoring_enabled()
                            and self.last_minimized_probe_wall > 0.0
                        )
                    except Exception:
                        minimized_waiting = False
                    self.capture_ok = bool(minimized_waiting)
                    interval = fail_interval
                self.finish_minimized_probe(minimized_probe, frame)
                spent = time.monotonic() - loop_started
                time.sleep(max(0.005, interval - spent))
        finally:
            FLOW_COORDINATOR.deactivate(id(self))
            if self.restore_minimized_after_flow:
                self._return_to_minimized("監測程序結束")
        LOG.info("[%s] 停止監看。", self.name)

    def detect_disconnect_dual(self, frame: np.ndarray, allow_ocr: bool) -> Optional[Tuple[int, int, str]]:
        """V8.2：先用邏輯畫布；漏判時再用同一次擷取的 raw 原始畫面。

        raw 路徑不會多做 PrintWindow，因此不增加擷取負擔。若 raw 命中，點位再轉回
        邏輯畫布，後面的背景輸入/驗證流程完全沿用既有穩定邏輯。
        """
        hit = detect_disconnect(frame, allow_ocr=allow_ocr)
        if hit:
            return hit
        raw = WIO.get_last_raw(self.hwnd)
        if raw is None or raw is frame:
            return None
        try:
            if raw.shape[:2] == frame.shape[:2]:
                return None
        except Exception:
            return None
        rhit = detect_disconnect(raw, allow_ocr=allow_ocr)
        if not rhit:
            return None
        rx, ry, label = rhit
        lx, ly = WIO.raw_point_to_logical(self.hwnd, rx, ry)
        LOG.info(
            "[%s] 跨 DPI 斷線辨識由 raw 原始畫面命中：raw=%sx%s → logical=%sx%s；點位=%s,%s→%s,%s；%s",
            self.name, raw.shape[1], raw.shape[0], frame.shape[1], frame.shape[0], rx, ry, lx, ly, label,
        )
        return lx, ly, label

    def detect_battle_scene_dual(self, frame: np.ndarray) -> Tuple[bool, str]:
        """V11.7：同一次擷取同時允許 logical/raw 判定戰鬥場景。

        戰鬥標題模板只存在於遊戲畫面，不會因解析度/DPI 寫死尺寸；raw 僅使用
        已經擷取好的同一幀，不額外 PrintWindow。
        """
        if is_battle_scene(frame):
            return True, "目前邏輯幀"
        raw = WIO.get_last_raw(self.hwnd)
        if raw is not None and raw is not frame:
            try:
                if raw.shape[:2] != frame.shape[:2] and is_battle_scene(raw):
                    return True, "目前原始幀"
            except Exception:
                pass
        return False, ""

    def step(self, frame: np.ndarray):
        now = time.monotonic()

        # V6.0：每個 Flash 視窗啟動後先做短暫自我檢查。
        # 這段只確認背景擷取能連續得到有效畫面，不做任何點擊，避免程式一啟動就在尚未穩定的畫面上操作。
        if not self.warmup_done:
            self.warmup_good_frames += 1
            need_frames = max(2, int(CONFIG.get("啟動自我檢查有效幀", 3)))
            remain = max(0.0, self.warmup_until - now)
            if now < self.warmup_until or self.warmup_good_frames < need_frames:
                self.set_event(f"自我檢查中：背景畫面 {self.warmup_good_frames}/{need_frames}，約 {remain:.1f} 秒")
                return
            self.warmup_done = True
            self.set_event("自我檢查通過，開始監測")
            LOG.info("[%s] 啟動自我檢查通過：背景擷取連續 %d 幀正常。", self.name, self.warmup_good_frames)

        user_busy_remaining = USER_ACTIVITY_GUARD.remaining(self.hwnd)
        if user_busy_remaining > 0.0:
            if not self.user_activity_paused:
                self.user_activity_pause_started_at = now
            self.user_activity_paused = True
            self.set_event(f"使用者正在操作此視窗；等待最後操作後 3 分鐘（剩餘約 {int(user_busy_remaining + 0.999)} 秒）")
            if now - self.user_activity_log_at >= 30.0:
                self.user_activity_log_at = now
                LOG.info(
                    "[%s] 使用者正在操作此遊戲視窗；本視窗暫停所有自動輸入，其他視窗照常，剩餘約 %d 秒。",
                    self.name,
                    int(user_busy_remaining + 0.999),
                )
            return
        if self.user_activity_paused:
            paused_for = max(0.0, now - self.user_activity_pause_started_at)
            for attr in (
                "fishing_prepare_deadline", "fishing_current_tab_deadline",
                "fishing_current_tab_click_at", "fishing_sender_channel_deadline",
                "fishing_sender_channel_click_at", "fishing_send_at",
                "fishing_locate_deadline", "fishing_deadline",
                "fishing_next_status_check", "fishing_map_wait_deadline",
                "fishing_map_settle_until", "fishing_reclick_locate_deadline",
                "fishing_release_at", "fishing_state_ocr_at",
                "fishing_menu_expand_next_at",
            ):
                value = float(getattr(self, attr, 0.0) or 0.0)
                if value > 0.0:
                    setattr(self, attr, value + paused_for)
            self.user_activity_paused = False
            self.user_activity_pause_started_at = 0.0
            LOG.info("[%s] 已連續 3 分鐘沒有使用者輸入；恢復此視窗原有流程。", self.name)
            self.set_event("使用者操作等待結束；恢復原有流程")

        # V11.7.4：DPI 相容設定留給未來自然重開，但目前程序不能再被設成零輸入。
        # logical-padded-crop 已把 PrintWindow 左上有效面裁成 Flash 邏輯畫布；下方的
        # 斷線確認會逐一嘗試合法座標路由，且每次都用重新擷取驗證是否真的生效。
        _surface = WIO.surface_mode.get(int(self.hwnd), "")
        _geom = WIO.geometry.get(int(self.hwnd))
        _dpi_virtualized = bool(
            _surface == "logical-padded-crop"
            and _geom is not None
            and int(_geom.monitor_dpi) != int(_geom.root_dpi)
        )
        self.dpi_virtualized = _dpi_virtualized
        if _dpi_virtualized and bool(CONFIG.get("DPI即時相容", True)):
            if not self.dpi_block_logged:
                exe = dpi_policy.get_window_process_exe(int(self.hwnd))
                self.dpi_block_exe = exe
                result = dpi_policy.apply_high_dpi_aware(exe) if exe else {"ok": False, "reason": "無法取得 Flash 宿主 EXE"}
                LOG.warning(
                    "[%s] 啟用 DPI 即時相容：surface=%s 螢幕DPI=%s rootDPI=%s；宿主=%s；未來相容政策=%s。"
                    "目前程序不阻擋、不強制重開或改尺寸；辨識使用邏輯裁切，輸入使用逐路由驗證。",
                    self.name, _surface, _geom.monitor_dpi, _geom.root_dpi, exe or "未知",
                    "已套用" if result.get("ok") else f"失敗:{result.get('reason','未知')}"
                )
                self.dpi_block_logged = True
        elif self.dpi_block_logged:
            LOG.info("[%s] DPI 虛擬化已消失，維持統一背景流程。", self.name)
            self.dpi_block_logged = False

        # 個別角色可關閉自動重連。這個閘門放在所有斷線辨識及重連輸入之前，
        # 因此關閉後不會進入原重連狀態機；釣魚若獨立開啟，仍沿用原釣魚流程。
        if not self.reconnect_enabled:
            if self.flow_started_at > 0.0:
                self.finish_flow(now, "使用者停用自動重連")
                self.set_state("監看")
                self.fishing_prerequisites_ready = False
            if not self.fishing_profile:
                self.set_event("自動重連與釣魚均已關閉；此監看程序不發送輸入")
                return

        # 斷線永遠最高優先。已知斷線模板每幀都可快速檢查；
        # 只有未知彈窗才需要 OCR，並對每個視窗做節流，避免 10+ 視窗同時吃滿資源。
        ocr_gap = max(0.35, float(CONFIG.get("斷線OCR最短間隔秒", 0.85)))
        disc = None
        primary_disc_state = self.state in ("監看", "等待斷線確認")
        flow_disc_gap = max(0.60, float(CONFIG.get("流程中斷線複查間隔秒", 1.20)))
        probe_disc = self.reconnect_enabled and (
            primary_disc_state or (now - self.last_flow_disconnect_probe >= flow_disc_gap)
        )
        if probe_disc:
            self.last_flow_disconnect_probe = now
            allow_disc_ocr = primary_disc_state and ((now - self.last_disconnect_ocr_at) >= ocr_gap)
            if allow_disc_ocr:
                self.last_disconnect_ocr_at = now
            disc = self.detect_disconnect_dual(frame, allow_ocr=allow_disc_ocr)

        # V11.7：只保存上一個「無斷線」監看幀，不在平常額外跑戰鬥模板。
        # 斷線彈窗若遮住右上角戰鬥標題，下一段可回看這個乾淨幀再判定。
        if self.state == "監看" and not disc:
            self.last_clean_monitor_frame = frame
            self.last_clean_monitor_frame_at = now

        # V5.7：聊天／活動視窗可能蓋住斷線框。V5.8 將探測間隔縮短，
        # 但仍然只有「已看到部分斷線文字」才允許關閉遮擋，不會平常亂關視窗。
        # 遮擋 OCR 是低優先、低頻備援；不能和選線/選角搶 OCR。
        occlusion_interval = max(1.80, float(CONFIG.get("遮擋檢查間隔秒", 0.65)))
        if not disc and self.state in ("監看", "等待斷線確認") and now - self.last_occlusion_probe >= occlusion_interval:
            self.last_occlusion_probe = now
            # 若中央黃字確認鈕仍清楚可見，detect_disconnect 會自行處理；
            # 不要再跑第二次低優先 OCR。只有「看得到中央對話框、但確認鈕被遮住」才做遮擋文字探測。
            if has_disconnect_dialog_shape(frame) and find_disconnect_confirm_visual(frame) is None:
                evidence = partial_disconnect_evidence(frame)
                if evidence:
                    chat = detect_chat_overlay(frame)
                    if chat:
                        self.set_event(f"疑似斷線被聊天視窗遮擋（{evidence}），先關閉聊天再重新辨識")
                        LOG.warning("[%s] 疑似斷線文字=%s；聊天視窗遮住中央彈窗，先背景關閉聊天視窗。", self.name, evidence)
                        close_chat_overlay(self.hwnd, chat, f"[{self.name}] 關閉遮擋的聊天視窗")
                        return
                    close = find_popup_close(frame)
                    if close:
                        x, y, label = close
                        self.set_event(f"疑似斷線被活動視窗遮擋（{evidence}），先關閉遮擋再重新辨識")
                        LOG.warning("[%s] 疑似斷線文字=%s；先關閉已知活動遮擋視窗。", self.name, evidence)
                        self.click_flow_action(x, y, f"[{self.name}] {label}（斷線遮擋處理）")
                        return

        retry_s = max(0.8, float(CONFIG.get("斷線確認重試間隔秒", 2.0)))
        if disc and now - self.last_disconnect_click > retry_s:
            # 只在這一輪智慧重連真正開始時記一次，不讓同一個斷線彈窗的重試刷新時間。
            if self.flow_started_at <= 0.0:
                self.last_disconnect_wall = time.time()
            self.start_flow(now, "偵測到斷線")
            # V11.6 起不再改使用者視窗幾何；只刷新程式內輸入基準。
            if self._enter_automation_geometry("偵測到斷線"):
                self.last_disconnect_click = 0.0
                self.set_state("等待斷線確認")
                return

            # V11.7 根因修正：戰鬥判定不得被「斷線確認是否點得掉」綁住。
            # 舊流程雖然在點擊前算過 battle，卻把真正的重開分支放在 click_disconnect_verified
            # 成功之後；戰鬥畫面的確認鈕若背景輸入沒生效，就永遠到不了重開分支。
            battle_now, battle_source = self.detect_battle_scene_dual(frame)
            battle = bool(battle_now)
            recent_window = max(1.5, float(CONFIG.get("戰鬥場景記憶秒", 5.0)))
            if (not battle and self.last_clean_monitor_frame is not None
                    and self.last_clean_monitor_frame_at > 0.0
                    and (now - self.last_clean_monitor_frame_at) <= recent_window):
                try:
                    if is_battle_scene(self.last_clean_monitor_frame):
                        battle = True
                        battle_source = f"斷線前乾淨幀({now - self.last_clean_monitor_frame_at:.1f}秒前)"
                except Exception:
                    pass

            x, y, label = disc
            self.last_disconnect_click = now
            self.force_clicked_at = 0.0
            self.force_lock_until = 0.0
            self.force_wait_until = 0.0
            self.force_post_wait_until = 0.0
            self.force_login_seen_frames = 0
            self.pending_force_mode = None
            self.pending_force_root = False
            self.force_tried_routes = []
            self.force_transport = ""
            self.line_seen_frames = 0
            self.role_seen_frames = 0
            self.line_ocr_attempts = 0
            self.line_ocr_votes = []
            self.line_ocr_next_at = 0.0
            self.selected_line_no = 0
            self.line_click_attempts = 0
            self.cached_line_header = None
            self.cached_role_title = None
            self.role_click_point = None
            self.enter_click_at = 0.0
            self.enter_click_attempts = 0
            self.enter_input_exhausted = False
            self.last_enter_verify_at = 0.0
            self.enter_clear_frames = 0
            self.enter_transition_started_at = 0.0
            self.post_popup_clear_frames = 0
            self.post_popup_quiet_since = 0.0
            self.post_popup_seen = set()
            self.auto_target_seen_frames = 0
            self.auto_x_seen_frames = 0
            self.auto_correction_pending = False
            self.auto_correction_started_at = 0.0
            self.last_auto_guard_warning_at = 0.0
            self.last_auto_ocr_at = 0.0
            self.last_auto_no_x_at = 0.0
            self.fishing_prerequisites_ready = False

            if battle:
                # 戰鬥場景的正確動作就是重開綁定捷徑；舊視窗稍後會被關閉，
                # 因此不再先要求舊視窗的「確定」必須背景點擊成功。
                self.set_event(f"偵測到戰鬥場景斷線（{battle_source}），正在重開原捷徑")
                LOG.info("[%s] 戰鬥場景斷線已判定；來源=%s。跳過一般斷線確認依賴，直接用原綁定捷徑重開。", self.name, battle_source)
                self.set_state("戰鬥斷線重開")
                if self.relaunch_for_battle_disconnect():
                    self.last_clean_monitor_frame = None
                    self.last_clean_monitor_frame_at = 0.0
                    return
                LOG.warning("[%s] 戰鬥重開未完成，才退回一般斷線確認流程。", self.name)

            self.set_event(f"偵測到：{label}，正在背景確認")
            if not self.click_disconnect_verified(x, y, label, baseline=frame):
                self.set_state("等待斷線確認")
                return

            self.set_state("等待登入畫面")
            return

        # A recognized disconnect always suppresses fishing, even during the
        # short retry throttle before the disconnect-confirm button may be sent.
        if disc:
            if self.fishing_profile:
                self.fishing_phase = "等待重連"
            return

        # V6.3：流程畫面依「目前狀態」辨識，不再每一幀同時掃登入、線路、角色三種大模板。
        # 等待登入畫面時只找登入；強制登入送出後才優先找線路／角色。
        startup_probe = self.reconnect_enabled and self.state == "監看" and now <= self.startup_login_probe_until
        header_now = None
        title_now = None

        if self.state == "等待登入後畫面" or startup_probe:
            # 快速尋找下一頁。線路標題第一次仍使用完整相容尺度；命中後立即快取位置。
            header_now = detect_line_header(frame, fast=True)
            if header_now is None:
                deep_gap = max(0.25, float(CONFIG.get("線路頁深度辨識間隔秒", 0.45)))
                if now - self.last_line_deep_probe >= deep_gap:
                    self.last_line_deep_probe = now
                    header_now = detect_line_header(frame, fast=False)
            if header_now:
                self.force_login_seen_frames = 0
                self.cached_line_header = header_now
                self.line_seen_frames += 1
                self.role_seen_frames = 0
                need = max(1, int(CONFIG.get("線路畫面連續確認幀", 2)))
                if self.line_seen_frames < need:
                    self.set_event(f"線路畫面確認 {self.line_seen_frames}/{need}")
                    return
                if self.pending_force_mode is not None and WIO.calibrated_mode(self.hwnd) is None:
                    WIO.confirm_click_mode(self.hwnd, self.pending_force_mode, "強制登入後看到線路頁", root=self.pending_force_root)
                self.pending_force_mode = None
                self.pending_force_root = False
                self.force_tried_routes = []
                self.line_ocr_attempts = 0
                self.line_ocr_votes = []
                self.line_ocr_next_at = 0.0
                self.selected_line_no = 0
                self.line_click_attempts = 0
                self.set_state("選擇線路")
                self.set_event("線路畫面確認完成，開始選線")
                return
            else:
                self.line_seen_frames = 0
                title_now = detect_character_screen(frame)
                if title_now:
                    self.force_login_seen_frames = 0
                    self.cached_role_title = title_now
                    self.role_seen_frames += 1
                    need_role = max(1, int(CONFIG.get("角色畫面連續確認幀", 2)))
                    if self.role_seen_frames < need_role:
                        self.set_event(f"角色畫面確認 {self.role_seen_frames}/{need_role}")
                        return
                    if self.pending_force_mode is not None and WIO.calibrated_mode(self.hwnd) is None:
                        WIO.confirm_click_mode(self.hwnd, self.pending_force_mode, "強制登入後看到角色頁", root=self.pending_force_root)
                    self.pending_force_mode = None
                    self.pending_force_root = False
                    self.force_tried_routes = []
                    self.role_wait_since = now
                    self.role_ocr_next_at = 0.0
                    self.set_state("選擇角色")
                    self.set_event("角色畫面確認完成，開始選角")
                    return
                else:
                    self.role_seen_frames = 0

        # 登入頁：等待登入時直接找強制登入，不先做線路/角色大範圍掃描。
        should_probe_login = self.state in ("等待登入畫面", "等待斷線確認") or startup_probe
        # 強制登入的 20 秒結束後，先留一個短觀察窗給線路／角色頁出現。
        # 只有觀察窗結束、而且登入頁連續多幀仍存在，才允許第二次強制登入。
        if self.state == "等待登入後畫面" and now >= max(self.force_lock_until, self.force_post_wait_until):
            should_probe_login = True

        force = None
        if should_probe_login:
            # 每幀先跑少尺度快速模板；固定間隔才跑完整尺度＋OCR，兼顧不同 DPI 與速度。
            force = detect_login_force(frame, allow_ocr=False, fast=True)
            if force is None:
                deep_gap = max(0.35, float(CONFIG.get("登入頁深度辨識間隔秒", 0.70)))
                if now - self.last_login_deep_probe >= deep_gap:
                    self.last_login_deep_probe = now
                    force = detect_login_force(frame, allow_ocr=True, fast=False)

        if force is None and self.state == "等待登入後畫面" and should_probe_login:
            self.force_login_seen_frames = 0

        if force:
            # 包含「程式啟動時已停在強制登入頁」的情境：輸入前先確保安全尺寸，
            # 改尺寸後必須下一幀重新辨識，禁止沿用舊座標直接點。
            if self._enter_automation_geometry("強制登入"):
                self.force_login_seen_frames = 0
                return
            if self.state == "等待登入後畫面":
                self.force_login_seen_frames += 1
                need_force = max(2, int(CONFIG.get("強制登入仍在確認幀", 3)))
                if self.force_login_seen_frames < need_force:
                    self.set_event(f"20 秒後仍疑似登入頁，確認 {self.force_login_seen_frames}/{need_force}；先不重按")
                    return
            else:
                self.force_login_seen_frames = 0

            _surface = WIO.surface_mode.get(int(self.hwnd), "")
            _g = WIO.geometry.get(int(self.hwnd))
            _high_dpi_padded = bool(
                _surface == "logical-padded-crop"
                and _g is not None
                and int(_g.monitor_dpi) != int(_g.root_dpi)
            )
            _verified_input_required = bool(_high_dpi_padded or self.foreground_input_required)
            if _verified_input_required:
                if now < self.force_lock_until:
                    return
                self.force_retry_count += 1
                self.set_event(f"按強制登入並驗證（第{self.force_retry_count}次）")
                if not self.click_force_login_target_dpi_verified(force):
                    self.force_lock_until = time.monotonic() + max(0.75, float(CONFIG.get("高DPI強制登入輸入重試秒", 0.90)))
                    self.set_event("強制登入驗證輸入尚未生效")
                    return
                click_confirmed_at = time.monotonic()
                self.force_clicked_at = click_confirmed_at
                self.force_login_seen_frames = 0
                self.force_post_wait_until = 0.0
                self.line_seen_frames = 0
                self.role_seen_frames = 0
                self.line_ocr_attempts = 0
                self.line_ocr_votes = []
                self.line_ocr_next_at = 0.0
                self.selected_line_no = 0
                self.line_click_attempts = 0
                self.cached_line_header = None
                self.cached_role_title = None
                wait_s = max(20.0, float(CONFIG.get("強制登入後固定等待秒", 20.0)))
                self.force_wait_until = click_confirmed_at + wait_s
                self.force_lock_until = self.force_wait_until
                LOG.info("[%s] 強制登入已由重新擷取證實；輸入=%s；固定等待 %.1f 秒。", self.name, "前景實體備援" if self.foreground_input_required else "目標DPI背景路由", wait_s)
                self.set_state("強制登入固定等待")
                return
            else:
                # V8.8：強制登入的輸入模式按「這個動作」獨立探測。
                # 特別是 logical-padded-crop 不可沿用斷線確認留下的實際尺寸映射，
                # 否則 901x568 的有效畫布會被送成 684x767 之類落在黑底的座標。
                self.force_retry_count += 1
                x, y = force.center

                surface = WIO.surface_mode.get(int(self.hwnd), "")
                route_candidates: List[Tuple[str, bool]] = []
                if surface == "logical-padded-crop":
                    raw_modes: Dict[bool, List[str]] = {}
                    for root_route in (False, True):
                        # 強制登入必須繞過全域校準過濾，直接取得這個動作的所有原生候選。
                        _t, _c, _p, _vals = WIO._message_point_candidates(self.hwnd, x, y, root=root_route)
                        raw_modes[root_route] = [m for m, _mx, _my in _vals]
                    # 先把「1:1 邏輯」在 Flash 與頂層都試完，再考慮任何尺寸放大映射。
                    # 這避免 901x568 有效畫布又被送成 684x767 落在黑底。
                    preferred = ["邏輯畫布直送", "目標原生邏輯", "Windows視窗映射", "視窗客戶區", "實體相對", "接收客戶區尺寸映射", "接收視窗實際尺寸映射"]
                    for want in preferred:
                        for root_route in (False, True):
                            if want in raw_modes.get(root_route, []) and (want, root_route) not in route_candidates:
                                route_candidates.append((want, root_route))
                else:
                    cm = WIO.calibrated_mode(self.hwnd)
                    if cm is not None:
                        route_candidates.append((str(cm), bool(WIO.calibrated_root(self.hwnd))))
                    for root_route in (False, True):
                        for m in WIO.candidate_modes(self.hwnd, x, y, root=root_route):
                            if (m, root_route) not in route_candidates:
                                route_candidates.append((m, root_route))

                chosen = None
                for cand in route_candidates:
                    if cand not in self.force_tried_routes:
                        chosen = cand
                        break
                if chosen is None:
                    # 全部都試過才開始第二輪，不允許固定卡在同一個失敗模式。
                    self.force_tried_routes = []
                    chosen = route_candidates[0] if route_candidates else ("視窗客戶區", False)
                mode, root_route = chosen
                self.force_tried_routes.append((str(mode), bool(root_route)))
                self.pending_force_mode = str(mode)
                self.pending_force_root = bool(root_route)
                self.force_transport = "互動同步" if surface == "logical-padded-crop" else "一般背景"
                self.set_event(f"按強制登入（第{self.force_retry_count}次）")
                self.force_clicked_at = now
                self.force_login_seen_frames = 0
                self.force_post_wait_until = 0.0
                if surface == "logical-padded-crop":
                    WIO.click_interactive(self.hwnd, x, y, f"[{self.name}] 強制登入", root=bool(root_route), mode=str(mode))
                else:
                    WIO.click_mode(self.hwnd, x, y, str(mode), f"[{self.name}] 強制登入", root=bool(root_route))
                self.line_seen_frames = 0
                self.role_seen_frames = 0
                self.line_ocr_attempts = 0
                self.line_ocr_votes = []
                self.line_ocr_next_at = 0.0
                self.selected_line_no = 0
                self.line_click_attempts = 0
                self.cached_line_header = None
                self.cached_role_title = None
                wait_s = max(20.0, float(CONFIG.get("強制登入後固定等待秒", 20.0)))
                self.force_wait_until = now + wait_s
                self.force_lock_until = self.force_wait_until
                LOG.info("[%s] 強制登入已送出；輸入模式=%s/%s/%s；固定等待 %.1f 秒，期間禁止重按。", self.name, self.force_transport or "一般背景", "頂層" if root_route else "Flash", str(mode), wait_s)
                self.set_state("強制登入固定等待")
                return

        if self.state == "監看":
            # 莊園工作流自行在既定步驟關閉莊園；其餘正常監看期間若有
            # 寵物、人物、背包等標準遊戲面板，必須先按右上角 X，不能
            # 讓聊天或釣魚辨識在遮擋畫面上繼續輸入。
            if self._close_unexpected_panel_step(frame, now):
                return
            # 剛做完 DPI 修復但目前其實已在遊戲中時，等啟動登入探測窗結束後
            # 才恢復使用者尺寸；若停在登入/線路/角色頁，上面的探測會先接手，不會提前放大。
            if self.flow_started_at <= 0.0 and now > self.startup_login_probe_until:
                self._restore_user_geometry("監看穩定")
            self._fishing_step(frame, now)
            return

        if self.state == "等待斷線確認":
            return

        if self.state == "等待登入畫面":
            if now - self.state_since > 20.0:
                LOG.warning("[%s] 尚未看到登入／強制登入畫面，持續等待。", self.name)
                self.save_debug_frame(frame, "等待登入畫面")
                self.state_since = now
            return

        if self.state == "等待登入後畫面":
            wait_limit = max(10.0, float(CONFIG.get("登入後畫面最長等待秒", 30.0)))
            if self.force_clicked_at > 0.0 and now - self.force_clicked_at > wait_limit:
                LOG.warning("[%s] 強制登入後 %.0f 秒仍沒有穩定辨識到線路／角色畫面；保存診斷畫面並繼續等待。", self.name, wait_limit)
                self.save_debug_frame(frame, "強制登入後未見線路")
                # 不立刻連點；下一輪只有真的仍辨識成完整登入頁、且超過重試時間才會補按。
                self.force_clicked_at = now - max(2.0, float(CONFIG.get("強制登入重試秒", 6.0)))
            return

        if self.state == "選擇線路":
            header = self.cached_line_header or header_now
            if header is None:
                header = detect_line_header(frame)
                if header:
                    self.cached_line_header = header
            if not header:
                # 已進入選線狀態後，不因單幀辨識失敗退回強制登入；先等待並保存診斷。
                if now - self.state_since > 2.5:
                    self.set_event("已鎖定選線流程，但本幀看不清線路標題；等待下一幀")
                    self.save_debug_frame(frame, "選線狀態看不清標題")
                return

            settle = (max(0.08, float(CONFIG.get("高速線路畫面穩定等待秒", 0.12)))
                      if CONFIG.get("高速狀態機", True) else max(0.20, float(CONFIG.get("線路畫面穩定等待秒", 0.45))))
            if now - self.state_since < settle:
                self.set_event(f"線路畫面穩定中，{settle - (now - self.state_since):.1f} 秒後讀取")
                return
            if now < self.line_ocr_next_at:
                return

            fixed_line_no = self.preferred_line_no if self.preferred_line_no in range(1, 9) else None
            line_no = fixed_line_no or (self.selected_line_no if self.selected_line_no in range(1, 9) else None)
            if fixed_line_no is not None and self.selected_line_no != fixed_line_no:
                self.selected_line_no = fixed_line_no
                LOG.info(
                    "[%s] 使用角色指定線路：%s；略過最近登入線路辨識；找不到時改用第一線。",
                    self.name,
                    LINE_NAMES.get(fixed_line_no, f"{fixed_line_no}線"),
                )
                self.set_event(f"角色指定 {LINE_NAMES.get(fixed_line_no, f'{fixed_line_no}線')}，正在找實際按鈕")
            if line_no is None:
                read_no, read_conf, read_text = read_recent_line_detail(frame, header)
                self.line_ocr_attempts += 1
                if read_no in range(1, 9):
                    self.line_ocr_votes.append(int(read_no))
                    LOG.info("[%s] 最近線路第 %d 次讀取：%s（信心 %.2f）。", self.name, self.line_ocr_attempts, LINE_NAMES.get(int(read_no), f"{read_no}線"), read_conf)

                max_attempts = max(2, int(CONFIG.get("線路OCR重試次數", 3)))
                chosen = None
                # V8.1：明確解析到「線路:x」且 OCR >=0.80 即單次通過；
                # 舊設定若仍是 0.86，也在程式內上限 0.80，避免同一結果白等第二輪 OCR。
                high_conf = max(0.70, min(0.80, float(CONFIG.get("線路單次OCR高信心", 0.80))))
                if read_no in range(1, 9) and read_conf >= high_conf:
                    chosen = int(read_no)
                    LOG.info("[%s] 最近線路單次高信心通過：%s（%.2f）。", self.name, LINE_NAMES.get(chosen, f"{chosen}線"), read_conf)
                # 低信心仍保留兩次相同結果／多數決。
                if chosen is None:
                    for v in set(self.line_ocr_votes):
                        if self.line_ocr_votes.count(v) >= 2:
                            chosen = v
                            break
                if chosen is None and self.line_ocr_attempts < max_attempts:
                    retry_wait = max(0.20, float(CONFIG.get("線路OCR重試間隔秒", 0.35)))
                    self.line_ocr_next_at = now + retry_wait
                    shown = ",".join(str(x) for x in self.line_ocr_votes) or "尚無"
                    self.set_event(f"最近線路穩定讀取 {self.line_ocr_attempts}/{max_attempts}（目前：{shown}）")
                    return
                if chosen is None and self.line_ocr_votes:
                    # 讀滿後採多數決；票數相同時取最後一次有效結果。
                    chosen = max(set(self.line_ocr_votes), key=lambda v: (self.line_ocr_votes.count(v), max(i for i, x in enumerate(self.line_ocr_votes) if x == v)))
                if chosen is None:
                    chosen = int(CONFIG.get("線路辨識失敗備援", 1))
                    if chosen not in range(1, 9):
                        chosen = 1
                    LOG.warning("[%s] 最近線路穩定讀取仍失敗 → 使用備援 %s。", self.name, LINE_NAMES.get(chosen, f"{chosen}線"))
                else:
                    LOG.info("[%s] 最近線路穩定結果：%s；樣本=%s。", self.name, LINE_NAMES.get(chosen, f"{chosen}線"), self.line_ocr_votes)
                self.selected_line_no = int(chosen)
                line_no = int(chosen)

            # 第一層：在已確認的線路面板內找實際按鈕模板。
            line_button = find_line_button(frame, int(line_no), header)
            click_point = None
            click_source = ""
            if line_button:
                click_point = line_button.center
                click_source = f"圖片相似度 {line_button.score:.3f}"
            else:
                # 第二層：直接 OCR 讀按鈕文字位置。
                ocr_button = find_line_button_ocr(frame, header, int(line_no))
                if ocr_button:
                    click_point = (ocr_button[0], ocr_button[1])
                    click_source = f"文字辨識「{ocr_button[2]}」"

            # 角色指定線路的按鈕沒有任何實際證據時，依使用者規則改找第一線。
            # 第一線同樣必須由模板或 OCR 定位，不使用固定／推算座標。
            if click_point is None and fixed_line_no is not None and int(line_no) != 1:
                fallback_button = find_line_button(frame, FIXED_LINE_FALLBACK_NO, header)
                if fallback_button:
                    click_point = fallback_button.center
                    click_source = f"指定線路未找到；第一線圖片相似度 {fallback_button.score:.3f}"
                else:
                    fallback_ocr = find_line_button_ocr(frame, header, FIXED_LINE_FALLBACK_NO)
                    if fallback_ocr:
                        click_point = (fallback_ocr[0], fallback_ocr[1])
                        click_source = f"指定線路未找到；第一線文字辨識「{fallback_ocr[2]}」"
                if click_point is not None:
                    LOG.warning(
                        "[%s] 指定 %s 未取得按鈕證據 → 改用具有實際定位證據的第一線。",
                        self.name,
                        LINE_NAMES.get(fixed_line_no, f"{fixed_line_no}線"),
                    )
                    self.set_event(
                        f"指定 {LINE_NAMES.get(fixed_line_no, f'{fixed_line_no}線')} 找不到 → 改選第一線"
                    )
                    line_no = FIXED_LINE_FALLBACK_NO
                    self.selected_line_no = FIXED_LINE_FALLBACK_NO

            if click_point is None:
                fixed_note = "；指定線路與第一線都找不到，退讓 1 秒後再試" if fixed_line_no is not None else ""
                self.set_event(f"已決定 {LINE_NAMES.get(int(line_no), f'{line_no}線')}，但尚未穩定找到實際按鈕{fixed_note}")
                LOG.warning("[%s] 已決定選 %s，但圖片與文字兩種方式都尚未定位到實際按鈕；不猜座標%s。", self.name, LINE_NAMES.get(int(line_no), f"{line_no}線"), fixed_note)
                self.save_debug_frame(frame, f"找不到線路按鈕_{line_no}")
                self.line_ocr_next_at = now + (
                    FIXED_LINE_RETRY_BACKOFF_SECONDS if fixed_line_no is not None else 0.35
                )
                return

            self.line_click_attempts += 1
            self.set_event(f"穩定定位線路按鈕 → 選擇 {LINE_NAMES.get(int(line_no), f'{line_no}線')}（{click_source}）")
            LOG.info("[%s] 選線第 %d 次：%s；定位方式=%s。", self.name, self.line_click_attempts, LINE_NAMES.get(int(line_no), f"{line_no}線"), click_source)
            self.click_flow_action(*click_point, f"[{self.name}] 選擇 {LINE_NAMES.get(int(line_no), f'{line_no}線')}")
            self.line_click_at = now
            self.role_wait_since = now
            self.role_ocr_next_at = 0.0
            self.role_seen_frames = 0
            self.set_state("等待角色畫面")
            return

        if self.state == "等待角色畫面":
            title = title_now or detect_character_screen(frame)
            if title:
                self.cached_role_title = title
                self.role_seen_frames += 1
                need_role = max(1, int(CONFIG.get("角色畫面連續確認幀", 2)))
                if self.role_seen_frames < need_role:
                    self.set_event(f"選線後看到角色畫面，穩定確認 {self.role_seen_frames}/{need_role}")
                    return
                self.role_wait_since = now
                self.role_ocr_next_at = 0.0
                self.set_state("選擇角色")
                self.set_event("選線已生效，角色畫面確認完成")
                # 同一張畫面直接往下。
            else:
                self.role_seen_frames = 0
                retry_line = (max(0.45, min(0.90, float(CONFIG.get("選線未生效重試秒", 1.50))))
                              if CONFIG.get("高速狀態機", True) else max(0.80, float(CONFIG.get("選線未生效重試秒", 1.50))))
                max_clicks = max(1, int(CONFIG.get("選線最多重試次數", 3)))
                # 不要每幀重做昂貴線路標題搜尋；只有到重試時間才確認線路面板是否仍在。
                header = None
                if now - self.line_click_at >= retry_line:
                    header = detect_line_header(frame)
                    if header:
                        self.cached_line_header = header
                if header and now - self.line_click_at >= retry_line:
                    if self.line_click_attempts >= max_clicks:
                        self.set_event(f"線路畫面仍在；已重試 {self.line_click_attempts} 次，停止連點並保存診斷")
                        LOG.error("[%s] 選線連續 %d 次未生效，停止自動連點，等待畫面變化或人工檢查。", self.name, self.line_click_attempts)
                        self.save_debug_frame(frame, "選線點擊未生效")
                        return
                    LOG.warning("[%s] 線路畫面仍在；等待 %.1f 秒驗證後，準備第 %d 次重試同一線路。", self.name, retry_line, self.line_click_attempts + 1)
                    self.line_ocr_next_at = 0.0
                    self.set_state("選擇線路")
                return

        if self.state == "選擇角色":
            title = self.cached_role_title
            if title is None:
                title = detect_character_screen(frame)
                if title:
                    self.cached_role_title = title
            if not title:
                # 只在角色標題真的消失時才低頻確認是否退回線路頁。
                if now - self.state_since > 0.6:
                    header = detect_line_header(frame)
                    if header:
                        self.enter_clear_frames = 0
                        self.cached_line_header = header
                        self.set_state("選擇線路")
                        return

                    # V11.8.2：若先前已送過「進入遊戲」，角色／線路頁都消失
                    # 代表 PrintWindow 終於由舊角色幀刷新。舊程式在這裡只有 return，
                    # 因而已進遊戲仍永久卡在選角狀態。
                    probe_gap = 0.90
                    if now - self.last_in_game_evidence_probe >= probe_gap:
                        self.last_in_game_evidence_probe = now
                        evidence = self._detect_in_game_evidence(frame, allow_chat=True)
                        if evidence:
                            self._begin_post_entry_cleanup(now, evidence)
                            return
                    if self.enter_transition_started_at > 0.0:
                        self.enter_clear_frames += 1
                        need_clear = max(2, int(CONFIG.get("進入遊戲成功連續確認幀", 2)))
                        if self.enter_clear_frames >= need_clear:
                            self._begin_post_entry_cleanup(now, f"角色頁與線路頁連續消失 {self.enter_clear_frames} 幀")
                            return
                return

            self.enter_clear_frames = 0

            if now < self.role_ocr_next_at:
                return
            self.role_ocr_next_at = now + (max(0.12, float(CONFIG.get("高速角色OCR重試間隔秒", 0.20)))
                                               if CONFIG.get("高速狀態機", True) else max(0.20, float(CONFIG.get("角色OCR重試間隔秒", 0.45))))

            role_name = str(self.profile.get("優先角色", "")).strip()
            role_key = first_meaningful_char(role_name)
            found, role_reason = find_role(frame, role_name, title)
            if found:
                x, y, how = found
                self.set_event(f"選擇角色：{role_name}（首字 {role_key}）")
                LOG.info("[%s] 角色首字辨識成功：設定=%s，首字=%s，方式=%s", self.name, role_name, role_key, how)
                sent = self.click_flow_action(
                    x,
                    y,
                    f"[{self.name}] 選擇 {how}",
                    hold_s=0.12,
                    post_wait_s=0.35,
                )
                if not sent:
                    self.set_event("角色已辨識，但輸入未送出；稍後重試")
                    LOG.error("[%s] 角色點擊輸入失敗；不進入下一狀態。", self.name)
                    return
                self.role_click_point = (int(x), int(y))
                self.role_click_at = time.monotonic()
                self.enter_click_at = 0.0
                self.enter_click_attempts = 0
                self.enter_input_exhausted = False
                self.last_enter_verify_at = 0.0
                self.enter_clear_frames = 0
                self.set_state("等待進入遊戲按鈕")
                return

            wait_limit = float(CONFIG.get("角色找不到等待秒", 6.0))
            if now - self.role_wait_since >= wait_limit:
                reason = role_reason or f"找不到角色首字「{role_key or '?'}」"
                self.set_event(f"選角暫停：{reason}")
                LOG.warning("[%s] %s；不選第一格，持續辨識等待。", self.name, reason)
                self.role_wait_since = now
            return

        if self.state == "等待進入遊戲按鈕":
            retry_floor = max(1.20, float(CONFIG.get("進入遊戲實際重試最短秒", 2.50)))
            retry_enter = (max(retry_floor, float(CONFIG.get("高速進入遊戲按鈕重試秒", 1.05)))
                           if CONFIG.get("高速狀態機", True) else max(retry_floor, float(CONFIG.get("進入遊戲按鈕重試秒", 0.90))))
            verify_after = max(0.20, float(CONFIG.get("進入遊戲點擊後快速驗證秒", 0.32)))
            verify_gap = max(0.20, float(CONFIG.get("進入遊戲畫面驗證間隔秒", 0.34)))
            max_inputs = max(2, min(5, int(CONFIG.get("進入遊戲每輪最多輸入次數", 3))))

            if self.enter_click_at > 0.0:
                # 點過後先讓 Flash 有時間切頁。期間不再重跑「進入遊戲」模板，更不連續狂點。
                if now - self.enter_click_at < verify_after:
                    return
                if now - self.last_enter_verify_at >= verify_gap:
                    self.last_enter_verify_at = now
                    allow_chat = now - self.last_in_game_evidence_probe >= 0.90
                    if allow_chat:
                        self.last_in_game_evidence_probe = now
                    evidence = self._detect_in_game_evidence(frame, allow_chat=allow_chat)
                    if evidence:
                        self._begin_post_entry_cleanup(now, evidence)
                        return
                    title = detect_character_screen(frame)
                    if not title:
                        self.enter_clear_frames += 1
                        need_clear = max(2, int(CONFIG.get("進入遊戲成功連續確認幀", 2)))
                        if self.enter_clear_frames >= need_clear:
                            self._begin_post_entry_cleanup(
                                now,
                                f"角色選擇畫面連續消失 {self.enter_clear_frames} 幀",
                            )
                            return
                    else:
                        self.enter_clear_frames = 0
                # 角色頁仍在時，至少隔 retry_enter 才允許再點一次。
                if now - self.enter_click_at < retry_enter:
                    return

                if self.enter_click_attempts >= max_inputs:
                    if not self.enter_input_exhausted:
                        self.enter_input_exhausted = True
                        self.set_event("進入遊戲輸入已完成，停止連點並等待 Flash 畫面刷新")
                        LOG.warning(
                            "[%s] 已完成 %d 種進入遊戲輸入；停止連點但保持等待。"
                            "PrintWindow 可能仍回傳舊角色幀，絕不再錯退選角；斷線監測持續。",
                            self.name, max_inputs,
                        )
                        self.save_debug_frame(frame, "進入遊戲等待PrintWindow刷新")
                    return

            # 未點過，或上一次點擊超過重試時間仍留在角色頁，才重新找按鈕。
            enter = detect_enter_game(frame)
            if enter:
                self.enter_click_attempts += 1
                attempt = self.enter_click_attempts
                cx, cy = enter.center
                route = "滑鼠中心"
                if attempt == 2 and self.foreground_input_required:
                    route = "前景真實 Enter"
                    sent = WIO.press_enter_foreground_physical(
                        self.hwnd,
                        f"[{self.name}] 進入遊戲（滑鼠未生效後鍵盤備援）",
                        post_wait_s=0.45,
                    )
                elif attempt == 2:
                    route = "背景 Enter"
                    sent = WIO.press_enter(self.hwnd, f"[{self.name}] 進入遊戲（鍵盤備援）")
                else:
                    # 第三次仍限定在已辨識按鈕內，但略微往下避開舊 Flash 的邊界 hit-test。
                    use_y = int(cy + (max(2, enter.h // 8) if attempt >= 3 else 0))
                    route = "滑鼠中心偏下" if attempt >= 3 else "滑鼠中心"
                    sent = self.click_flow_action(
                        int(cx),
                        use_y,
                        f"[{self.name}] 進入遊戲（{route}）",
                        hold_s=0.13,
                        post_wait_s=0.45,
                    )
                self.set_event(f"進入遊戲輸入 {attempt}/{max_inputs}（{route}）")
                LOG.info(
                    "[%s] 進入遊戲第 %d/%d 次；按鈕相似度=%.3f 中心=%s,%s 輸入=%s 送出=%s。",
                    self.name, attempt, max_inputs, enter.score, cx, cy, route, bool(sent),
                )
                self.enter_click_at = time.monotonic()
                if sent and self.enter_transition_started_at <= 0.0:
                    self.enter_transition_started_at = self.enter_click_at
                self.last_enter_verify_at = 0.0
                return

            # 尚未成功看到按鈕，短暫等待後才回去重做角色首字 OCR。
            if self.enter_click_at <= 0.0 and now - self.role_click_at >= retry_enter:
                LOG.warning("[%s] 選角後尚未看到可用的進入遊戲按鈕，重新確認角色。", self.name)
                self.role_wait_since = now
                self.role_ocr_next_at = 0.0
                self.cached_role_title = None
                self.set_state("選擇角色")
            return

        if self.state == "進入遊戲後整理":
            close = find_popup_close(frame)
            popup_retry = (max(0.45, float(CONFIG.get("高速彈窗關閉後等待秒", 0.08)))
                           if CONFIG.get("高速狀態機", True) else max(0.55, float(CONFIG.get("彈窗關閉後等待秒", 0.12))))
            if close and now - self.last_popup_click > popup_retry:
                x, y, label = close
                sent = self.click_flow_action(x, y, f"[{self.name}] {label}")
                self.last_popup_click = now
                self.post_popup_clear_frames = 0
                self.post_popup_quiet_since = 0.0
                self.auto_target_seen_frames = 0
                self.auto_x_seen_frames = 0
                self.auto_correction_pending = False
                self.auto_correction_started_at = 0.0
                if sent:
                    self.post_popup_seen.add(str(label))
                self.set_event(f"進入遊戲整理：{label}；已處理 {len(self.post_popup_seen)}/2 類")
                # 彈窗剛關閉時 Flash 可能仍有透明 modal/hover 狀態；先穩定後再辨識右下角 X 狀態。
                self.auto_settle_until = max(self.auto_settle_until, now + max(0.45, float(CONFIG.get("自動戰鬥彈窗關閉後穩定秒", 0.80))))
                self.auto_toggle_attempts = 0
                return

            if close:
                # 還在點擊冷卻內；不可越過仍看得見的彈窗去碰自動戰鬥。
                self.post_popup_clear_frames = 0
                self.post_popup_quiet_since = 0.0
                return

            if self.post_popup_quiet_since <= 0.0:
                self.post_popup_quiet_since = now
            self.post_popup_clear_frames += 1
            popup_observe = max(1.0, float(CONFIG.get("進入遊戲彈窗觀察秒", 5.00)))
            popup_quiet = max(0.8, float(CONFIG.get("進入遊戲彈窗安靜確認秒", 2.00)))
            popup_clear_need = max(2, int(CONFIG.get("進入遊戲彈窗消失確認幀", 3)))
            if (
                now - self.entered_at < popup_observe
                or now - self.post_popup_quiet_since < popup_quiet
                or self.post_popup_clear_frames < popup_clear_need
            ):
                self.set_event(
                    f"進入遊戲整理：等待兩類彈窗，已處理 {len(self.post_popup_seen)}/2 類"
                )
                return

            if now < self.auto_settle_until:
                return

            if self._maintain_no_x_auto_battle(frame, now):
                self.fishing_prerequisites_ready = True
                if self.fishing_profile:
                    self._reset_fishing_runtime("彈窗已清除且右下角無 X 已確認")
                elapsed, breakdown = self.finish_flow(now, "完成")
                if elapsed > 0:
                    LOG.info("[%s] 智慧重連完成；兩類彈窗已掃描、右下角無 X 連續確認完成。整體耗時 %.2f 秒。", self.name, elapsed)
                    if breakdown:
                        LOG.info("[%s] 流程耗時拆解：%s", self.name, breakdown)
                    if self.last_perf_summary:
                        LOG.info("[%s] 程式處理統計：%s", self.name, self.last_perf_summary)
                else:
                    LOG.info("[%s] 釣魚前置檢查完成：兩類彈窗已掃描，右下角無 X 已確認。", self.name)
                self.force_retry_count = 0
                self.set_state("監看")
                # set_state 會寫「流程：監看」，所以完成訊息要最後寫，讓控制台不會瞬間把耗時蓋掉。
                if self.fishing_profile:
                    self.set_event("右下角無 X 已確認；下一步選擇上排『目前』")
                elif elapsed > 0:
                    self.set_event(f"智慧重連完成，總耗時 {elapsed:.1f} 秒")
                else:
                    self.set_event("智慧重連完成")
                self._restore_user_geometry("智慧重連完成")
                return

            # 硬性閘門：超時只能告警並繼續斷線監測，不能回到「監看」讓釣魚越級執行。
            if now - self.entered_at > float(CONFIG.get("進入遊戲最長等待秒", 30.0)):
                repeat = max(5.0, float(CONFIG.get("進入遊戲整理警告重複秒", 10.0)))
                if now - self.last_post_entry_warning_at >= repeat:
                    self.last_post_entry_warning_at = now
                    self.set_event("尚未確認右下角無 X；禁止釣魚，斷線監測持續")
                    LOG.warning(
                        "[%s] 進入遊戲後仍無法完成『關彈窗→右下角無 X』；"
                        "保持整理狀態並持續斷線監測，絕不跳過前置條件開始釣魚。",
                        self.name,
                    )
                    self.save_debug_frame(frame, "進遊戲整理尚未完成")
            return


# -----------------------------
# 視窗管理 / 捷徑管理
# -----------------------------

def enum_game_windows() -> List[int]:
    keys = [str(x) for x in CONFIG.get("視窗標題包含", ["Adobe Flash Player 11"]) if str(x)]
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd) or ""
            if any(k in title for k in keys):
                if is_ignored_hwnd(hwnd):
                    return
                w, h = WIO.client_size(hwnd)
                if w >= 500 and h >= 300:
                    found.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    # 依畫面位置排列，讓未使用捷徑自動綁定時順序固定。
    def pos(hwnd):
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            return (t, l, hwnd)
        except Exception:
            return (999999, 999999, hwnd)
    return sorted(set(found), key=pos)


def launch_and_bind_profiles(profiles: List[dict]) -> Dict[int, dict]:
    """Launch profiles explicitly marked for startup and bind their new windows.

    Already-open unbound Flash windows are admitted later with a blank safe
    monitoring profile; this function never guesses a shortcut identity.
    """
    bound: Dict[int, dict] = {}
    for idx, p in enumerate(profiles):
        path = str(p.get("捷徑路徑", "")).strip()
        if not p.get("啟動", False) or not path:
            continue
        full = Path(os.path.expandvars(path)).expanduser()
        if not full.exists():
            LOG.warning("捷徑不存在：%s", full)
            continue
        before = set(enum_game_windows())
        LOG.info("背景啟動捷徑：%s", full)
        if not launch_shortcut_no_activate(full):
            LOG.warning("捷徑『%s』背景啟動失敗。", p.get("名稱", idx + 1))
            continue
        deadline = time.monotonic() + 35.0
        new_hwnd = None
        while time.monotonic() < deadline:
            time.sleep(0.25)
            after = set(enum_game_windows())
            news = [h for h in after if h not in before and h not in bound]
            if news:
                new_hwnd = news[0]
                break
        if new_hwnd:
            bound[new_hwnd] = p
            LOG.info("捷徑『%s』已綁定新視窗 %s。", p.get("名稱", idx + 1), new_hwnd)
        else:
            LOG.warning("捷徑『%s』啟動後未找到新遊戲視窗。", p.get("名稱", idx + 1))
    return bound


def unbound_monitor_profile() -> dict:
    """Return a non-destructive profile for a newly discovered Flash window.

    It may detect and confirm a known disconnect, but it cannot relaunch a
    shortcut or choose a character until the user supplies that identity.
    """
    raw_profiles = list(DEFAULT_CONFIG.get("捷徑設定") or [])
    profile = dict(raw_profiles[0]) if raw_profiles else {}
    profile["名稱"] = "未綁定視窗"
    profile["捷徑路徑"] = ""
    profile["啟動"] = False
    profile["優先角色"] = ""
    profile["角色模板"] = ""
    profile["戰鬥斷線允許重開"] = False
    return profile


def write_runtime_status(workers: Dict[int, GameWorker], pause_event: threading.Event, running: bool = True):
    """提供控制台讀取的輕量狀態檔；採原子覆寫，避免 GUI 讀到半份 JSON。"""
    rows = []
    for w in list(workers.values()):
        if not w.is_alive() and running:
            continue
        try:
            title = win32gui.GetWindowText(w.hwnd) if WIO.is_window(w.hwnd) else "視窗已關閉"
            cw, ch = WIO.client_size(w.hwnd) if WIO.is_window(w.hwnd) else (0, 0)
            minimized = bool(win32gui.IsIconic(w.hwnd)) if WIO.is_window(w.hwnd) else False
        except Exception:
            title, cw, ch, minimized = "", 0, 0, False
        rows.append({
            "name": w.name,
            "shortcut_name": w.shortcut_name,
            "shortcut_path": w.shortcut_path,
            "preferred_role": str(w.profile.get("優先角色", "") or ""),
            "hwnd": int(w.hwnd),
            "pid": get_window_pid(w.hwnd),
            "title": title,
            "size": f"{cw}x{ch}" if cw and ch else "-",
            "state": w.state,
            "flow_elapsed": float(max(0.0, time.monotonic() - w.flow_started_at)) if w.flow_started_at > 0.0 else 0.0,
            "last_flow_duration": float(w.last_flow_duration),
            "last_flow_result": str(w.last_flow_result),
            "last_flow_finished_at": float(w.last_flow_finished_wall),
            "last_disconnect_at": float(w.last_disconnect_wall),
            "capture_ok": bool(w.capture_ok),
            "last_capture": float(w.last_capture_wall),
            "last_event": w.last_event,
            "last_event_at": float(w.last_event_wall),
            "managed": True,
            "bound": bool(w.shortcut_path),
            "dpi_virtualized": bool(w.dpi_virtualized),
            "input_mode": "foreground-physical-fallback" if w.foreground_input_required else "background-messages",
            "minimized": bool(minimized),
            "minimized_monitoring": bool(w._minimized_monitoring_enabled()),
            "minimized_paused": bool(w.minimized_paused),
            "last_minimized_probe": float(w.last_minimized_probe_wall),
            "surface_mode": str(WIO.surface_mode.get(int(w.hwnd), "")),
            "reconnect_enabled": bool(w.reconnect_enabled),
            "manor_enabled": bool(w.manor_enabled),
            "fishing_enabled": bool(w.fishing_enabled),
            "fishing_profile_id": str(w.fishing_profile_id),
            "fishing_profile_name": str((w.fishing_profile or {}).get("name", "")),
            "fishing_phase": str(w.fishing_phase),
            "fishing_message_group_index": int(w.fishing_message_group_index),
            "fishing_message_group_count": int(len(w.fishing_message_groups)),
            "fishing_link_index": int(w.fishing_link_index),
            "fishing_link_count": int(len(w.fishing_links)),
            "fishing_prerequisites_ready": bool(w.fishing_prerequisites_ready),
            "fishing_channel_intent": str(w.fishing_channel_intent),
            "fishing_map_transition_count": int(w.fishing_map_transition_count),
            "fishing_map_last_evidence": str(w.fishing_map_last_evidence),
            "post_entry_popups_seen": sorted(str(value) for value in w.post_popup_seen),
            "auto_no_x_seen_frames": int(w.auto_target_seen_frames),
            "auto_x_seen_frames": int(w.auto_x_seen_frames),
            "auto_correction_pending": bool(w.auto_correction_pending),
        })
    payload = {
        "running": bool(running),
        "pid": os.getpid(),
        "updated_at": time.time(),
        "paused": bool(pause_event.is_set()),
        "windows": rows,
    }
    tmp = STATUS_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATUS_PATH)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def main():
    global OCR, TB
    # 背景監測本身使用較低 Windows 排程優先度；即使辨識短暫忙碌，也優先讓遊戲與桌面保持流暢。
    try:
        kernel32 = ctypes.windll.kernel32
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    # 供控制台辨識背景監測程序，並支援不搶焦點的停止訊號。
    try:
        STOP_SIGNAL_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    except Exception:
        pass

    # V5.4：把耗時/可能失敗的初始化放到 PID 建立之後，並逐階段寫紀錄。
    # 這樣控制台不會再只看到一行 OCR 後就不知道程式停在哪裡。
    try:
        LOG.info("啟動階段 1/4：初始化 OCR（文字辨識）。")
        OCR = OCRReader(bool(CONFIG.get("啟用OCR", True)))
        LOG.info("啟動階段 2/4：載入畫面辨識模板。")
        TB = TemplateBank()
        LOG.info("畫面模板載入完成：%d 個可用%s。", len(TB.data), f"；{len(TB.missing)} 個缺少/無法讀取" if TB.missing else "")
        LOG.info("啟動階段 3/4：掃描 Adobe Flash Player 11 遊戲視窗。")
    except Exception:
        LOG.exception("背景監測初始化失敗。")
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return

    profiles = list(CONFIG.get("捷徑設定") or [])
    if not profiles:
        profiles = [dict(DEFAULT_CONFIG["捷徑設定"][0])]

    LOG.info("智慧重連 V11.8.4-test23 角色獨立功能開關版啟動。F8（暫停/繼續），F9（結束）。")
    LOG.info("模式：不改使用者視窗尺寸/位置、不因綁定重開遊戲；程式內正規化辨識座標，DPI 相容只準備未來自然啟動。")
    LOG.info("流程：V11.8.4 斷線→回遊戲→關兩類彈窗→常駐無X→上下目前→發送→同點雙擊→轉圖穩定+2秒重雙擊→10秒驗證；斷線永遠優先。")

    # V11.6：先補全舊綁定的「實際 Flash 程序身分」，之後自然重啟可自動接回。
    # 這只讀程序資訊與更新 SmartReconnect 自己的 LocalAppData，不關窗、不改尺寸。
    _migrated = backfill_live_binding_process_identities()
    if _migrated:
        LOG.info("V11.6 已補全 %d 個既有綁定的程序身分；後續 HWND/PID 改變可自動接回。", _migrated)

    # V11：先對「目前實際 Flash 宿主」與所有已保存捷徑目標套用 DPI 政策。
    # 現有程序若已經建立視窗，政策要到它下次重開才生效；worker 會自我檢查並在此之前零輸入。
    try:
        _raw_bindings, _saved_profiles = _read_binding_store_unlocked()
        _shortcuts = []
        for _item in list(_raw_bindings.values()) + list(_saved_profiles.values()):
            if isinstance(_item, dict):
                _sp = str(_item.get("shortcut_path", "") or "").strip()
                if _sp and _sp not in _shortcuts:
                    _shortcuts.append(_sp)
        _policy = dpi_policy.apply_unified_policy(enum_game_windows(), _shortcuts)
        _changed = sum(1 for _r in _policy.get("targets", []) if isinstance(_r, dict) and _r.get("changed"))
        LOG.info("V11.6 DPI 相容準備已檢查：目標=%d，新增=%d；既有使用者 DPI 設定不覆蓋。", len(_policy.get("targets", [])), _changed)
    except Exception as _e:
        LOG.warning("V11 DPI 統一政策準備失敗：%s", _e)

    bound = launch_and_bind_profiles(profiles)
    # GUI 精確綁定優先；這些才是允許監看的既有視窗。
    _live_bindings = load_window_bindings()
    for _hwnd in _live_bindings:
        bound.setdefault(int(_hwnd), dict(profiles[0]))
    if not bound:
        LOG.info("目前沒有既有綁定；仍會自動納管所有 Flash 視窗做基礎斷線監測。")
    else:
        LOG.info("初始允許監看的已綁定/明確啟動視窗：%d 個；未綁定 Flash 不建立 worker。", len(bound))
    LOG.info("啟動階段 4/4：進入背景監測迴圈。")

    stop_event = threading.Event()
    pause_event = threading.Event()
    # worker 不能再用 hwnd 當字典鍵：戰鬥斷線重開後，同一 worker 會改綁新的 hwnd。
    workers: Dict[int, GameWorker] = {}
    last_status_write = 0.0
    last_window_discovery = 0.0
    window_discovery_interval = max(0.80, float(CONFIG.get("視窗發現掃描間隔秒", 1.20)))
    status_write_interval = max(0.50, float(CONFIG.get("狀態檔更新間隔秒", 1.00)))
    write_runtime_status(workers, pause_event, running=True)
    manor_manager = manor_runtime.ManorManager(stop_event, load_window_bindings, LOG)
    manor_manager.start()

    def start_new_windows():
        nonlocal bound
        if RELAUNCH_GATE.is_set():
            return

        current = enum_game_windows()
        claimed = {w.hwnd for w in workers.values() if w.is_alive()}
        # V11.6：遊戲由使用者自然關閉/重開後，以綁定時保存的實際程序身分自動接回。
        # 僅在唯一精確匹配時轉移；不猜、不重開、不要求再綁一次。
        auto_rebind_profiles_to_live_windows(current, claimed)
        live_bindings = load_window_bindings()
        for hwnd in current:
            if hwnd in claimed or is_ignored_hwnd(hwnd):
                continue
            profile = bound.get(hwnd)
            binding = live_bindings.get(int(hwnd))
            # V11.7.4：未綁定視窗也必須建立 worker，否則新電腦雖顯示
            # 「監測執行中」卻完全不處理斷線。未綁定 worker 使用空白安全設定：
            # 可辨識/確認斷線，但不猜捷徑、不重開程序、不亂選角色。
            if profile is None and binding is None:
                if not bool(CONFIG.get("未綁定視窗基礎監測", True)):
                    continue
                profile = unbound_monitor_profile()
                bound[hwnd] = profile
                LOG.info("新 Flash 視窗 %s 尚未綁定；已自動納入基礎監測。", hwnd)
            if profile is None:
                profile = dict(profiles[0])
                bound[hwnd] = profile
            worker = GameWorker(hwnd, profile, stop_event, pause_event)
            workers[id(worker)] = worker
            worker.start()

    try:
        while not stop_event.is_set():
            if STOP_SIGNAL_PATH.exists():
                LOG.info("收到控制台停止指令，結束程式。")
                stop_event.set()
                break

            now_mono = time.monotonic()
            if now_mono - last_window_discovery >= window_discovery_interval:
                start_new_windows()
                last_window_discovery = now_mono

            now_wall = time.time()
            if now_wall - last_status_write >= status_write_interval:
                # 控制台可在監測不中斷的情況下即時修改捷徑／角色綁定。
                live_bindings = load_window_bindings()
                for worker in list(workers.values()):
                    if worker.is_alive():
                        worker.apply_binding(live_bindings.get(int(worker.hwnd)), announce=True)
                write_runtime_status(workers, pause_event, running=True)
                last_status_write = now_wall

            # F8：暫停/繼續；F9：結束。GetAsyncKeyState 的低位表示「自上次查詢後按過」。
            if win32api.GetAsyncKeyState(win32con.VK_F8) & 1:
                if pause_event.is_set():
                    pause_event.clear()
                    LOG.info("已繼續。")
                else:
                    pause_event.set()
                    LOG.info("已暫停。")
            if win32api.GetAsyncKeyState(win32con.VK_F9) & 1:
                LOG.info("收到 F9，結束程式。")
                stop_event.set()
                break

            # 清掉已關閉的 worker。
            dead = [h for h, w in workers.items() if not w.is_alive()]
            for h in dead:
                workers.pop(h, None)

            time.sleep(0.12)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        for w in workers.values():
            w.join(timeout=1.0)
        manor_manager.join(timeout=2.0)
        LOG.info("智慧重連已結束。")
        try:
            write_runtime_status(workers, pause_event, running=False)
        except Exception:
            pass
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            STOP_SIGNAL_PATH.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
