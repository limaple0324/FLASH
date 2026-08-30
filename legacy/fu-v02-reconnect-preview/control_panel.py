# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import time
try:
    import msvcrt
except Exception:
    msvcrt = None
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import dpi_policy
import fishing_profiles
import window_geometry
from manor_assistant.models import CROP_BY_LABEL, CROP_OPTIONS
from runtime_paths import (
    APP_DATA_DIR,
    APP_VERSION,
    IS_FROZEN,
    RESOURCE_DIR,
    USER_DATA_DIR,
    sanitized_record,
)

# pywin32 是可安裝依賴。控制台必須能在套件尚未安裝時開啟依賴提示，
# 但一旦 pywin32 已存在，就必須在所有綁定/DPI 重開流程使用同一組模組。
try:
    import win32gui
    import win32con
except Exception:
    win32gui = None
    win32con = None

FROZEN = IS_FROZEN
BASE_DIR = APP_DATA_DIR
MONITOR_SCRIPT = RESOURCE_DIR / "smart_reconnect.py"
CONFIG_PATH = APP_DATA_DIR / "config.json"
LOG_DIR = APP_DATA_DIR / "logs"
PID_PATH = APP_DATA_DIR / "monitor.pid"
STOP_SIGNAL_PATH = APP_DATA_DIR / "stop.signal"
STATUS_PATH = APP_DATA_DIR / "runtime_status.json"
MANOR_STATUS_PATH = APP_DATA_DIR / "manor_status.json"
STARTUP_LOG_PATH = LOG_DIR / "monitor_startup.log"
LEGACY_BINDINGS_PATH = APP_DATA_DIR / "bindings.json"
BINDINGS_PATH = USER_DATA_DIR / "bindings.json"
BINDINGS_LOCK_PATH = USER_DATA_DIR / "bindings.lock"
IDENTITY_PATH = USER_DATA_DIR / "identity_profiles.json"
UI_CONFIG_PATH = USER_DATA_DIR / "ui_config.json"
INSTALL_BAT = RESOURCE_DIR / "install_requirements.bat"
ASSET_DIR = RESOURCE_DIR / "assets"
APP_ICON_PNG = ASSET_DIR / "auto_reconnect_icon.png"
APP_ICON_ICO = ASSET_DIR / "auto_reconnect_icon.ico"
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
VERSION = APP_VERSION

LINE_OPTIONS = (
    ("依最近登入線路", 0),
    ("第一線", 1),
    ("公會專線（第二線）", 2),
    ("第三線", 3),
    ("第四線", 4),
    ("第五線", 5),
    ("第六線", 6),
    ("第七線", 7),
    ("郵寄拍賣專線（第八線）", 8),
)
LINE_NO_BY_LABEL = {label: number for label, number in LINE_OPTIONS}
LINE_LABEL_BY_NO = {number: label for label, number in LINE_OPTIONS}


def line_setting_label(value) -> str:
    try:
        number = int(value or 0)
    except Exception:
        number = 0
    return LINE_LABEL_BY_NO.get(number, LINE_LABEL_BY_NO[0])


def enable_per_monitor_dpi_v2():
    try:
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        ctx = ctypes.c_void_p(ctypes.c_ssize_t(-4).value)
        if fn(ctx):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

enable_per_monitor_dpi_v2()


def set_app_user_model_id():
    """讓工作列使用本程式自己的視窗圖示，不被 Python 圖示分組覆蓋。"""
    try:
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [wintypes.LPCWSTR]
        fn.restype = ctypes.c_long
        fn("SmartReconnect.AutoReconnect.11.8.4")
    except Exception:
        pass


@contextmanager
def binding_file_lock(timeout: float = 2.5):
    """跨程序鎖定綁定檔，避免控制台與背景監測同時覆寫造成剛綁定又消失。"""
    if msvcrt is None:
        yield
        return
    BINDINGS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    f = open(BINDINGS_LOCK_PATH, "a+b")
    locked = False
    try:
        if f.tell() == 0:
            f.write(b"\0")
            f.flush()
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                time.sleep(0.025)
        if not locked:
            raise TimeoutError("綁定資料目前正被另一個程序使用，請稍後再試。")
        yield
    finally:
        if locked:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        try:
            f.close()
        except Exception:
            pass


def _read_binding_store_unlocked():
    raw, profiles = {}, {}
    try:
        if not BINDINGS_PATH.exists():
            return raw, profiles
        obj = json.loads(BINDINGS_PATH.read_text(encoding="utf-8-sig"))
        r = obj.get("bindings", obj) if isinstance(obj, dict) else {}
        p = obj.get("profiles", {}) if isinstance(obj, dict) else {}
        if isinstance(p, dict):
            profiles = {str(k): sanitized_record(v) for k, v in p.items() if isinstance(v, dict)}
        if isinstance(r, dict):
            for k, v in r.items():
                if not isinstance(v, dict):
                    continue
                try:
                    hwnd = int(k)
                except Exception:
                    continue
                raw[hwnd] = sanitized_record(v)
                pk = _profile_key(v.get("shortcut_path", ""))
                if pk and pk not in profiles:
                    x = sanitized_record(v)
                    x.pop("pid", None)
                    x["profile_key"] = pk
                    profiles[pk] = x
    except Exception:
        pass
    return raw, profiles


def migrate_legacy_user_data_once():
    """把舊版程式資料夾內的綁定搬到 LocalAppData；之後換版本/覆蓋資料夾不再清空。"""
    try:
        if not BINDINGS_PATH.exists() and LEGACY_BINDINGS_PATH.exists():
            obj = json.loads(LEGACY_BINDINGS_PATH.read_text(encoding="utf-8-sig"))
            raw = obj.get("bindings", obj) if isinstance(obj, dict) else {}
            profiles = obj.get("profiles", {}) if isinstance(obj, dict) else {}
            if raw or profiles:
                BINDINGS_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

migrate_legacy_user_data_once()


def load_ui_config():
    data = {"auto_start": False}
    try:
        if UI_CONFIG_PATH.exists():
            obj = json.loads(UI_CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(obj, dict):
                data.update(obj)
    except Exception:
        pass
    return data


def save_ui_config(data):
    try:
        UI_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def read_runtime_status():
    try:
        if not STATUS_PATH.exists():
            return {}
        return json.loads(STATUS_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def read_manor_status():
    try:
        if not MANOR_STATUS_PATH.exists():
            return {}
        return json.loads(MANOR_STATUS_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def get_window_pid(hwnd: int) -> int:
    try:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def get_window_dpi(hwnd: int) -> int:
    try:
        fn = ctypes.windll.user32.GetDpiForWindow
        fn.argtypes = [wintypes.HWND]
        fn.restype = wintypes.UINT
        dpi = int(fn(int(hwnd)))
        return dpi if dpi >= 48 else 96
    except Exception:
        return 96


def get_monitor_dpi(hwnd: int) -> int:
    try:
        user32 = ctypes.windll.user32
        mon = user32.MonitorFromWindow(int(hwnd), 2)
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


def get_window_geometry(hwnd: int):
    try:
        user32 = ctypes.windll.user32
        wr = wintypes.RECT()
        cr = wintypes.RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(wr)):
            return {}
        if not user32.GetClientRect(int(hwnd), ctypes.byref(cr)):
            return {}
        cw, ch = int(cr.right-cr.left), int(cr.bottom-cr.top)
        dpi = get_window_dpi(hwnd)
        monitor_dpi = get_monitor_dpi(hwnd)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = user32.MonitorFromWindow(int(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            ml, mt, mr, mb = int(mi.rcWork.left), int(mi.rcWork.top), int(mi.rcWork.right), int(mi.rcWork.bottom)
        else:
            ml = int(user32.GetSystemMetrics(76)); mt = int(user32.GetSystemMetrics(77))
            mr = ml + int(user32.GetSystemMetrics(78)); mb = mt + int(user32.GetSystemMetrics(79))
        mw, mh = max(1,mr-ml), max(1,mb-mt)
        norm = [(wr.left-ml)/mw, (wr.top-mt)/mh, max(1,wr.right-wr.left)/mw, max(1,wr.bottom-wr.top)/mh]
        return {
            "window_rect": [int(wr.left), int(wr.top), int(wr.right), int(wr.bottom)],
            "client_size": [cw,ch],
            "logical_client_size": [round(cw*96.0/max(48,monitor_dpi),2), round(ch*96.0/max(48,monitor_dpi),2)],
            "norm_rect": [round(float(v),6) for v in norm],
            "dpi": int(dpi),
            "monitor_dpi": int(monitor_dpi),
        }
    except Exception:
        return {}


def _profile_key(path_text):
    text = str(path_text or "").strip()
    if not text:
        return ""
    try:
        text = os.path.abspath(os.path.expandvars(os.path.expanduser(text)))
    except Exception:
        pass
    return os.path.normcase(text)


def _read_identity_profiles_unlocked():
    out = {}
    try:
        if IDENTITY_PATH.exists():
            obj = json.loads(IDENTITY_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(obj, dict):
                raw = obj.get("profiles", obj)
                if isinstance(raw, dict):
                    out = {str(k): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        out = {}
    return out


def _write_identity_profiles_unlocked(profiles):
    payload = {
        "version": 1,
        "updated_at": time.time(),
        "profiles": {str(k): sanitized_record(v) for k, v in profiles.items() if isinstance(v, dict)},
    }
    tmp = IDENTITY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, IDENTITY_PATH)


def read_identity_profiles():
    try:
        with binding_file_lock():
            return _read_identity_profiles_unlocked()
    except Exception:
        return {}


def save_identity_profile(shortcut_path, shortcut_name=None, preferred_role=None):
    pkey = _profile_key(shortcut_path)
    if not pkey:
        return
    with binding_file_lock():
        profiles = _read_identity_profiles_unlocked()
        item = dict(profiles.get(pkey, {}))
        item["shortcut_path"] = str(shortcut_path or "")
        if shortcut_name is not None:
            item["shortcut_name"] = str(shortcut_name or "")
        if preferred_role is not None:
            item["preferred_role"] = str(preferred_role or "")
        item["profile_key"] = pkey
        item["updated_at"] = time.time()
        profiles[pkey] = item
        _write_identity_profiles_unlocked(profiles)


def remove_identity_profile(shortcut_path):
    pkey = _profile_key(shortcut_path)
    if not pkey:
        return
    try:
        with binding_file_lock():
            profiles = _read_identity_profiles_unlocked()
            if pkey in profiles:
                profiles.pop(pkey, None)
                _write_identity_profiles_unlocked(profiles)
    except Exception:
        pass


def enrich_binding_identity(item):
    item = dict(item or {})
    pkey = _profile_key(item.get("shortcut_path", ""))
    if not pkey:
        return item
    ident = read_identity_profiles().get(pkey, {})
    if ident:
        # 捷徑名稱與角色名稱屬於使用者設定；背景程序不得覆蓋。
        if "shortcut_name" in ident:
            item["shortcut_name"] = str(ident.get("shortcut_name", "") or "")
        if "preferred_role" in ident:
            item["preferred_role"] = str(ident.get("preferred_role", "") or "")
    return item


def migrate_identity_profiles_once():
    try:
        if IDENTITY_PATH.exists() and IDENTITY_PATH.stat().st_size > 20:
            return
        raw, profiles = _read_binding_store_unlocked()
        out = {}
        for item in list(profiles.values()) + list(raw.values()):
            if not isinstance(item, dict):
                continue
            pkey = _profile_key(item.get("shortcut_path", ""))
            if not pkey:
                continue
            cur = dict(out.get(pkey, {}))
            cur["shortcut_path"] = str(item.get("shortcut_path", "") or "")
            if item.get("shortcut_name"):
                cur["shortcut_name"] = str(item.get("shortcut_name", "") or "")
            if "preferred_role" in item and item.get("preferred_role"):
                cur["preferred_role"] = str(item.get("preferred_role", "") or "")
            cur["profile_key"] = pkey
            cur["updated_at"] = time.time()
            out[pkey] = cur
        if out:
            _write_identity_profiles_unlocked(out)
    except Exception:
        pass


migrate_identity_profiles_once()


def read_binding_store():
    try:
        with binding_file_lock():
            return _read_binding_store_unlocked()
    except Exception:
        return {}, {}


def read_bindings():
    """表格與背景核心採同一規則：精確 HWND 還是目前 Flash 視窗就沿用綁定。

    PID 只作診斷，不再因控制台/背景程序某次取得的 PID 資訊不同就把角色顯示成未知。
    """
    raw,_profiles = read_binding_store()
    identities = read_identity_profiles()
    out={}
    title_keys = ["Adobe Flash Player 11"]
    for hwnd,value in raw.items():
        try:
            if not win32gui.IsWindow(int(hwnd)):
                continue
            title = win32gui.GetWindowText(int(hwnd)) or ""
            if title_keys and not any(k in title for k in title_keys):
                continue
        except Exception:
            continue
        item = dict(value)
        item["pid"] = get_window_pid(hwnd) or int(item.get("pid",0) or 0)
        pkey = _profile_key(item.get("shortcut_path", ""))
        ident = identities.get(pkey, {}) if pkey else {}
        if ident:
            if "shortcut_name" in ident:
                item["shortcut_name"] = str(ident.get("shortcut_name", "") or "")
            if "preferred_role" in ident:
                item["preferred_role"] = str(ident.get("preferred_role", "") or "")
        out[hwnd]=item
    return out


def save_bindings(bindings, remove_profile_path=""):
    # 整個「讀目前資料 → 合併 → 寫回」放在同一個跨程序鎖內，
    # 背景監測不能再用舊快照把控制台剛寫入的捷徑名稱蓋掉。
    with binding_file_lock():
        _raw, profiles = _read_binding_store_unlocked()
        if remove_profile_path:
            profiles.pop(_profile_key(remove_profile_path), None)
        clean = {int(k): dict(v) for k, v in bindings.items() if isinstance(v, dict)}
        for hwnd, item in clean.items():
            geom = get_window_geometry(hwnd)
            if geom:
                item.update(geom)
            item["pid"] = get_window_pid(hwnd)
            item["last_hwnd"] = int(hwnd)
            pk = _profile_key(item.get("shortcut_path", ""))
            if pk:
                merged = dict(profiles.get(pk, {}))
                keep = {k: v for k, v in item.items() if k != "pid"}
                merged.update(keep)
                merged["profile_key"] = pk
                merged["updated_at"] = time.time()
                profiles[pk] = merged
                item["profile_key"] = pk
        payload = {
            "version": 3,
            "updated_at": time.time(),
            "bindings": {str(int(k)): v for k, v in clean.items()},
            "profiles": profiles,
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


def set_fishing_assignment(hwnd: int, fishing_profile_id: str):
    """Atomically assign one fishing profile to one bound game identity."""
    hwnd = int(hwnd)
    wanted = str(fishing_profile_id or "").strip()
    if wanted and fishing_profiles.profile_by_id(wanted) is None:
        raise ValueError("所選釣魚設定已不存在。")
    with binding_file_lock():
        raw, profiles = _read_binding_store_unlocked()
        item = dict(raw.get(hwnd, {}))
        if not str(item.get("shortcut_path", "") or "").strip():
            raise ValueError("請先綁定這個遊戲視窗，才能保存常駐釣魚設定。")
        item["fishing_profile_id"] = wanted
        # Selecting a profile enables fishing; removing the assignment disables
        # it.  The separate feature switch can later pause fishing without
        # forgetting the selected profile.
        item["fishing_enabled"] = bool(wanted)
        item["updated_at"] = time.time()
        raw[hwnd] = item
        pkey = str(item.get("profile_key", "") or _profile_key(item.get("shortcut_path", "")))
        if pkey:
            saved = dict(profiles.get(pkey, {}))
            saved.update({k: v for k, v in item.items() if k != "pid"})
            saved["profile_key"] = pkey
            saved["fishing_profile_id"] = wanted
            saved["fishing_enabled"] = bool(wanted)
            saved["updated_at"] = time.time()
            profiles[pkey] = saved
        payload = {
            "version": 4,
            "updated_at": time.time(),
            "bindings": {str(int(k)): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)},
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
    return item


def set_feature_switches(
    hwnd: int,
    reconnect_enabled: bool,
    manor_enabled: bool,
    fishing_enabled: bool,
):
    """Atomically save three independent automation switches for one role."""
    hwnd = int(hwnd)
    with binding_file_lock():
        raw, profiles = _read_binding_store_unlocked()
        item = dict(raw.get(hwnd, {}))
        if not str(item.get("shortcut_path", "") or "").strip():
            raise ValueError("請先綁定這個遊戲視窗，才能保存功能開關。")
        item["reconnect_enabled"] = bool(reconnect_enabled)
        item["manor_enabled"] = bool(manor_enabled)
        item["fishing_enabled"] = bool(fishing_enabled)
        item["updated_at"] = time.time()
        raw[hwnd] = item
        pkey = str(item.get("profile_key", "") or _profile_key(item.get("shortcut_path", "")))
        if pkey:
            saved = dict(profiles.get(pkey, {}))
            saved.update({k: v for k, v in item.items() if k != "pid"})
            saved["profile_key"] = pkey
            saved["updated_at"] = time.time()
            profiles[pkey] = saved
        payload = {
            "version": 7,
            "updated_at": time.time(),
            "bindings": {str(int(k)): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)},
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
    return item


def set_line_assignment(hwnd: int, preferred_line_no: int):
    """Atomically save one role's login-line policy (0=last login, 1..8=fixed)."""
    hwnd = int(hwnd)
    preferred_line_no = int(preferred_line_no)
    if preferred_line_no not in range(0, 9):
        raise ValueError("登入線路只能是依最近登入或第一至第八線。")
    with binding_file_lock():
        raw, profiles = _read_binding_store_unlocked()
        item = dict(raw.get(hwnd, {}))
        if not str(item.get("shortcut_path", "") or "").strip():
            raise ValueError("請先綁定這個遊戲視窗，才能保存登入線路。")
        item["preferred_line_no"] = preferred_line_no
        item["updated_at"] = time.time()
        raw[hwnd] = item
        pkey = str(item.get("profile_key", "") or _profile_key(item.get("shortcut_path", "")))
        if pkey:
            saved = dict(profiles.get(pkey, {}))
            saved.update({k: v for k, v in item.items() if k != "pid"})
            saved["profile_key"] = pkey
            saved["preferred_line_no"] = preferred_line_no
            saved["updated_at"] = time.time()
            profiles[pkey] = saved
        payload = {
            "version": 6,
            "updated_at": time.time(),
            "bindings": {str(int(k)): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)},
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
    return item


def set_manor_settings(hwnd: int, enabled: bool, crop_key: str, quantity: int, retry: bool = False):
    """Save manor settings on the same persistent identity used by reconnect."""
    hwnd = int(hwnd)
    valid_keys = {option.key for option in CROP_OPTIONS}
    if crop_key not in valid_keys:
        raise ValueError("莊園作物設定無效。")
    quantity = max(1, min(16, int(quantity)))
    with binding_file_lock():
        raw, profiles = _read_binding_store_unlocked()
        item = dict(raw.get(hwnd, {}))
        if not str(item.get("shortcut_path", "") or "").strip():
            raise ValueError("請先綁定這個遊戲視窗，才能保存莊園設定。")
        item["manor_enabled"] = bool(enabled)
        item["manor_crop_key"] = crop_key
        item["manor_quantity"] = quantity
        if retry:
            item["manor_retry_token"] = time.time()
        item["updated_at"] = time.time()
        raw[hwnd] = item
        pkey = str(item.get("profile_key", "") or _profile_key(item.get("shortcut_path", "")))
        if pkey:
            saved = dict(profiles.get(pkey, {}))
            saved.update({k: v for k, v in item.items() if k != "pid"})
            saved["profile_key"] = pkey
            profiles[pkey] = saved
        payload = {
            "version": 5,
            "updated_at": time.time(),
            "bindings": {str(int(k)): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)},
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
    return item


def clear_deleted_fishing_assignments(fishing_profile_id: str):
    """Clear a deleted user profile without disturbing other saved bindings."""
    wanted = str(fishing_profile_id or "").strip()
    if not wanted:
        return 0
    changed = 0
    dirty = False
    with binding_file_lock():
        raw, profiles = _read_binding_store_unlocked()
        for key, item in list(raw.items()):
            if isinstance(item, dict) and str(item.get("fishing_profile_id", "")) == wanted:
                item = dict(item)
                item["fishing_profile_id"] = ""
                raw[key] = item
                changed += 1
                dirty = True
        for key, item in list(profiles.items()):
            if isinstance(item, dict) and str(item.get("fishing_profile_id", "")) == wanted:
                item = dict(item)
                item["fishing_profile_id"] = ""
                profiles[key] = item
                dirty = True
        if dirty:
            payload = {
                "version": 4,
                "updated_at": time.time(),
                "bindings": {str(int(k)): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)},
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
    return changed


def desktop_dir():
    try:
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        p = home / "Desktop"
        if p.exists():
            return p
    except Exception:
        pass
    return Path.home()


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def current_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text(encoding="ascii").strip())
        return pid if process_alive(pid) else None
    except Exception:
        return None


def enum_game_windows():
    cfg = read_config()
    keys = [str(x) for x in cfg.get("視窗標題包含", ["Adobe Flash Player 11"]) if str(x)]
    if not keys:
        return []
    user32 = ctypes.windll.user32
    rows = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @EnumWindowsProc
    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if not any(k in title for k in keys):
                return True
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                w = int(rect.right - rect.left)
                h = int(rect.bottom - rect.top)
                if w >= 500 and h >= 300:
                    wr = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(wr)):
                        pos_text = f"{int(wr.left)},{int(wr.top)}"
                    else:
                        pos_text = "-"
                    rows.append({"hwnd": int(hwnd), "pid": get_window_pid(int(hwnd)), "title": title, "size": f"{w}x{h}", "position": pos_text})
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return rows


def dependency_status():
    # 必須以「目前控制台正在使用的同一個 Python」實際 import，
    # 避免電腦同時有多個 Python 時，安裝成功卻被另一版控制台誤判。
    required = [
        ("cv2", "opencv-python"),
        ("numpy", "numpy"),
        ("win32gui", "pywin32"),
        ("win32api", "pywin32"),
        ("win32con", "pywin32"),
        ("win32ui", "pywin32"),
    ]
    missing = []
    for module_name, package_name in required:
        try:
            __import__(module_name)
        except Exception as e:
            missing.append({"module": module_name, "package": package_name, "error": str(e)})

    ocr = False
    for name in ("rapidocr_onnxruntime", "rapidocr"):
        try:
            __import__(name)
            ocr = True
            break
        except Exception:
            pass
    return missing, ocr


def console_python_path():
    # pythonw.exe 沒有主控台；安裝時改用同資料夾的 python.exe，
    # 但仍然是「同一個 Python 環境」。
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _read_log_text(path: Path) -> str:
    """優先讀 UTF-8；相容舊版曾以 Windows 繁中編碼寫出的啟動紀錄。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def latest_log_text(max_lines=80):
    try:
        # 正常執行時顯示核心自己的 UTF-8 紀錄，不再把 subprocess 的啟動轉送紀錄
        # 當成主要紀錄來源，避免 Windows 主控台編碼造成亂碼。
        logs = sorted(
            [p for p in LOG_DIR.glob("*.log") if p.name.lower() != STARTUP_LOG_PATH.name.lower()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if logs:
            text = _read_log_text(logs[0])
            return "\n".join(text.splitlines()[-max_lines:])
        if STARTUP_LOG_PATH.exists():
            text = _read_log_text(STARTUP_LOG_PATH)
            return "\n".join(text.splitlines()[-max_lines:])
        return "尚無執行紀錄。"
    except Exception as e:
        return f"無法讀取紀錄：{e}"


def save_user_binding_atomic(hwnd: int, shortcut_path: str, shortcut_name: str, preferred_role: str):
    """V6.9 單一交易保存捷徑+角色。背景程序看不到「捷徑已寫、角色尚未寫」的中間狀態。"""
    hwnd = int(hwnd)
    shortcut_path = str(shortcut_path or "")
    shortcut_name = str(shortcut_name or "")
    preferred_role = str(preferred_role or "").strip()
    pkey = _profile_key(shortcut_path)
    with binding_file_lock():
        raw, profiles = _read_binding_store_unlocked()
        identities = _read_identity_profiles_unlocked()

        ident = dict(identities.get(pkey, {})) if pkey else {}
        if pkey:
            ident.update({
                "shortcut_path": shortcut_path,
                "shortcut_name": shortcut_name,
                "preferred_role": preferred_role,
                "profile_key": pkey,
                "updated_at": time.time(),
            })
            identities[pkey] = ident
            _write_identity_profiles_unlocked(identities)

        item = dict(raw.get(hwnd, {}))
        proc_ident = dpi_policy.window_process_identity(hwnd)
        item.update({
            "shortcut_path": shortcut_path,
            "shortcut_name": shortcut_name,
            "preferred_role": preferred_role,
            "pid": get_window_pid(hwnd),
            "bound_at": time.time(),
            "last_hwnd": hwnd,
            "process_exe": str(proc_ident.get("process_exe", "") or ""),
            "process_identity": str(proc_ident.get("process_identity", "") or ""),
        })
        # Backward compatibility: roles created before the three independent
        # switches keep their previous behavior on first upgrade.
        item.setdefault("reconnect_enabled", True)
        item.setdefault("manor_enabled", False)
        item.setdefault("fishing_enabled", bool(str(item.get("fishing_profile_id", "") or "").strip()))
        geom = get_window_geometry(hwnd)
        if geom:
            item.update(geom)
        if pkey:
            item["profile_key"] = pkey
            prof = dict(profiles.get(pkey, {}))
            prof.update({k: v for k, v in item.items() if k != "pid"})
            prof["profile_key"] = pkey
            prof["updated_at"] = time.time()
            profiles[pkey] = prof
        raw[hwnd] = item

        payload = {
            "version": 4,
            "updated_at": time.time(),
            "bindings": {str(int(k)): sanitized_record(v) for k, v in raw.items() if isinstance(v, dict)},
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
    return item


class App(tk.Tk):
    def __init__(self):
        set_app_user_model_id()
        super().__init__()
        self._icon_photo = None
        self.title("自動重連＋魔力莊園（測試版）")
        self._apply_window_icon()
        self.geometry("1080x760")
        self.minsize(960, 680)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.child = None
        self.startup_log_handle = None
        self.child_start_deadline = 0.0
        self.ui_cfg = load_ui_config()
        self.auto_start_var = tk.BooleanVar(value=False)
        # V6.9：靜態身分（捷徑/角色）與即時流程完全分離。
        # 背景狀態刷新即使短暫讀檔失敗，也不能把剛輸入的角色洗成「未知」。
        self.identity_cache = {}
        self.dpi_pending_count = 0
        self.fishing_panel_hwnd = 0
        self.fishing_panel_signature = None
        self.fishing_edit_profile_id = ""
        self.fishing_delete_confirm_id = ""
        self.fishing_panel_updating = False
        self.fishing_window_var = tk.StringVar(value="目前視窗：請先在上方選擇已綁定視窗")
        self.fishing_name_var = tk.StringVar(value="")
        self.fishing_editor_state_var = tk.StringVar(value="請選擇設定，或按『新增設定』")
        self.manor_enabled_var = tk.BooleanVar(value=False)
        self.reconnect_enabled_var = tk.BooleanVar(value=True)
        self.fishing_enabled_var = tk.BooleanVar(value=False)
        self.feature_panel_updating = False
        self.feature_state_var = tk.StringVar(value="請先選擇角色")
        self.manor_crop_var = tk.StringVar(value=CROP_OPTIONS[0].label)
        self.manor_quantity_var = tk.IntVar(value=16)
        self.manor_state_var = tk.StringVar(value="請先在上方選擇已綁定視窗")
        self.manor_panel_updating = False
        self.manor_panel_cache = {}
        self.manor_dirty_hwnd = 0
        self.manor_save_after_id = None
        self.manor_pending_snapshot = None
        self.manor_quantity_var.trace_add("write", self.on_manor_quantity_changed)
        self.line_var = tk.StringVar(value=LINE_LABEL_BY_NO[0])
        self.line_state_var = tk.StringVar(value="請先選擇角色")
        self.line_panel_updating = False
        # 控制台開啟時只顯示狀態，不自動啟動背景監測。
        # 避免使用者只是打開介面就立即載入 OCR / PrintWindow 工作程序造成 CPU 尖峰。
        self.ui_cfg["auto_start"] = False
        try:
            save_ui_config(self.ui_cfg)
        except Exception:
            pass
        self._build()
        self.after(250, self.refresh)

    def _apply_window_icon(self):
        """同一張圖同時套用到視窗標題列與 Windows 工作列。"""
        try:
            if APP_ICON_ICO.exists():
                self.iconbitmap(default=str(APP_ICON_ICO))
        except Exception:
            pass
        try:
            if APP_ICON_PNG.exists():
                self._icon_photo = tk.PhotoImage(file=str(APP_ICON_PNG))
                self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _build(self):
        # V11.7 介面精簡：移除版本標題與說明區，只保留實際操作資訊。
        stat = tk.Frame(self, padx=16, pady=10)
        stat.pack(fill="x")
        self.status_label = tk.Label(stat, text="監測：讀取中", anchor="w", font=("Microsoft JhengHei UI", 11, "bold"))
        self.status_label.pack(side="left")

        controls = tk.Frame(self, padx=16, pady=0)
        controls.pack(fill="x", pady=(0, 10))
        self.toggle_button = tk.Button(controls, text="開始監測", width=12, command=self.toggle_monitor)
        self.toggle_button.pack(side="left", padx=(0, 8))
        tk.Button(controls, text="綁定遊戲視窗", width=14, command=self.bind_foreground_after_delay).pack(side="left", padx=(0, 8))
        tk.Button(controls, text="設定角色", width=11, command=self.set_selected_role).pack(side="left", padx=(0, 8))
        # 保留內部訊息物件供既有流程寫入，但不再佔用主介面空間。
        # 綁定失敗／錯誤仍會使用原有對話框提示，核心流程不受影響。
        self.hint_label = tk.Label(controls, text="", anchor="w", justify="left")

        win_frame = tk.LabelFrame(self, text="角色與執行狀態", padx=8, pady=8)
        win_frame.pack(fill="x", padx=16, pady=(0, 10))
        # 狀態與上次斷線時間直接顯示在各自視窗的「流程」格內。
        cols = ("role", "reconnect", "manor", "fishing", "line", "state")
        self.tree = ttk.Treeview(win_frame, columns=cols, show="headings", height=6)
        self.tree.heading("role", text="角色 / 捷徑")
        self.tree.heading("reconnect", text="自動重連")
        self.tree.heading("manor", text="莊園")
        self.tree.heading("fishing", text="釣魚")
        self.tree.heading("line", text="登入線路")
        self.tree.heading("state", text="流程")
        self.tree.column("role", width=210, minwidth=170, anchor="w", stretch=True)
        self.tree.column("reconnect", width=82, minwidth=76, anchor="center", stretch=False)
        self.tree.column("manor", width=72, minwidth=66, anchor="center", stretch=False)
        self.tree.column("fishing", width=105, minwidth=82, anchor="center", stretch=False)
        self.tree.column("line", width=150, minwidth=130, anchor="center", stretch=False)
        self.tree.column("state", width=300, minwidth=240, anchor="w", stretch=True)
        self.tree.pack(fill="x")
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Button-3>", self.on_tree_right_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_game_selection_changed)

        self.row_menu = tk.Menu(self, tearoff=0)
        self.row_menu.add_command(label="綁定所選視窗", command=self.bind_selected_row)
        self.row_menu.add_command(label="設定角色", command=self.set_selected_role)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="解除綁定", command=self.unbind_selected_row)
        self.row_menu.add_command(label="開啟紀錄資料夾", command=self.open_logs)

        selected_frame = tk.LabelFrame(self, text="所選角色設定", padx=10, pady=9)
        selected_frame.pack(fill="x", padx=16, pady=(0, 10))

        feature_box = tk.Frame(selected_frame)
        feature_box.pack(fill="x")
        tk.Label(feature_box, text="功能開關", width=10, anchor="w", font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
        tk.Checkbutton(
            feature_box, text="自動重連", variable=self.reconnect_enabled_var,
            command=self.on_feature_switches_toggle,
        ).pack(side="left", padx=(0, 16))
        tk.Checkbutton(
            feature_box, text="莊園", variable=self.manor_enabled_var,
            command=self.on_feature_switches_toggle,
        ).pack(side="left", padx=(0, 16))
        tk.Checkbutton(
            feature_box, text="釣魚", variable=self.fishing_enabled_var,
            command=self.on_feature_switches_toggle,
        ).pack(side="left", padx=(0, 16))
        tk.Label(feature_box, textvariable=self.feature_state_var, anchor="w", fg="#444444").pack(side="left", fill="x", expand=True)

        line_box = tk.Frame(selected_frame)
        line_box.pack(fill="x", pady=(9, 0))
        tk.Label(line_box, text="登入線路", width=10, anchor="w", font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
        self.line_combo = ttk.Combobox(
            line_box,
            textvariable=self.line_var,
            values=[label for label, _number in LINE_OPTIONS],
            state="readonly",
            width=25,
        )
        self.line_combo.pack(side="left", padx=(0, 8))
        self.line_combo.bind("<<ComboboxSelected>>", self.on_line_assignment_changed)
        tk.Label(line_box, textvariable=self.line_state_var, anchor="w", fg="#444444").pack(side="left", fill="x", expand=True)

        manor_row = tk.Frame(selected_frame)
        manor_row.pack(fill="x", pady=(9, 0))
        tk.Label(manor_row, text="莊園", width=10, anchor="w", font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
        tk.Label(manor_row, text="養殖").pack(side="left")
        self.manor_crop_combo = ttk.Combobox(
            manor_row,
            textvariable=self.manor_crop_var,
            values=[option.label for option in CROP_OPTIONS],
            state="readonly",
            width=18,
        )
        self.manor_crop_combo.pack(side="left", padx=(5, 12))
        self.manor_crop_combo.bind("<<ComboboxSelected>>", self.on_manor_crop_changed)
        tk.Label(manor_row, text="最多新種").pack(side="left")
        self.manor_quantity_spinbox = tk.Spinbox(
            manor_row,
            from_=1,
            to=16,
            textvariable=self.manor_quantity_var,
            width=4,
        )
        self.manor_quantity_spinbox.pack(side="left", padx=(5, 12))
        self.manor_quantity_spinbox.bind("<Return>", self.flush_manor_quantity_edit)
        self.manor_quantity_spinbox.bind("<FocusOut>", self.flush_manor_quantity_edit)
        tk.Button(manor_row, text="儲存", width=8, command=self.save_manor_settings).pack(side="left", padx=(0, 6))
        tk.Button(manor_row, text="立即重試", width=9, command=self.retry_manor_now).pack(side="left", padx=(0, 10))
        tk.Label(manor_row, textvariable=self.manor_state_var, anchor="w", fg="#444444").pack(side="left", fill="x", expand=True)

        # 釣魚設定直接嵌入主介面。選擇遊戲視窗後可立即勾選魚級，
        # 新增／編輯也在同一區完成，不再建立任何釣魚 Toplevel 視窗。
        self.fishing_frame = tk.LabelFrame(self, text="釣魚設定", padx=8, pady=7)
        self.fishing_frame.pack(fill="x", padx=16, pady=(0, 10))

        fish_head = tk.Frame(self.fishing_frame)
        fish_head.pack(fill="x", pady=(0, 5))
        tk.Label(fish_head, textvariable=self.fishing_window_var, anchor="w", font=("Microsoft JhengHei UI", 9, "bold")).pack(side="left")
        tk.Label(
            fish_head,
            text="每個已綁定角色可各選一項；彼此不會同步覆蓋",
            anchor="e",
            fg="#444444",
        ).pack(side="right")

        fish_cols = ("checked", "name", "links", "message")
        fish_table = tk.Frame(self.fishing_frame)
        fish_table.pack(fill="x")
        self.fishing_tree = ttk.Treeview(fish_table, columns=fish_cols, show="headings", height=5, selectmode="browse")
        self.fishing_tree.heading("checked", text="啟用")
        self.fishing_tree.heading("name", text="魚級名稱")
        self.fishing_tree.heading("links", text="組數/座標")
        self.fishing_tree.heading("message", text="發送字串")
        self.fishing_tree.column("checked", width=58, minwidth=55, anchor="center", stretch=False)
        self.fishing_tree.column("name", width=120, minwidth=100, anchor="w", stretch=False)
        self.fishing_tree.column("links", width=88, minwidth=82, anchor="center", stretch=False)
        self.fishing_tree.column("message", width=720, minwidth=380, anchor="w", stretch=True)
        fish_scroll = ttk.Scrollbar(fish_table, orient="vertical", command=self.fishing_tree.yview)
        self.fishing_tree.configure(yscrollcommand=fish_scroll.set)
        self.fishing_tree.pack(side="left", fill="x", expand=True)
        fish_scroll.pack(side="right", fill="y")
        self.fishing_tree.bind("<Button-1>", self.on_fishing_tree_click)
        self.fishing_tree.bind("<<TreeviewSelect>>", self.on_fishing_profile_select)

        editor = tk.Frame(self.fishing_frame)
        editor.pack(fill="x", pady=(7, 0))
        tk.Label(editor, text="名稱").grid(row=0, column=0, sticky="w")
        self.fishing_name_entry = tk.Entry(editor, textvariable=self.fishing_name_var, width=18)
        self.fishing_name_entry.grid(row=0, column=1, sticky="ew", padx=(5, 10))
        tk.Label(editor, text="發送字串（每行一組）").grid(row=0, column=2, sticky="nw")
        self.fishing_message_box = tk.Text(editor, height=3, wrap="word", font=("Consolas", 9))
        self.fishing_message_box.grid(row=0, column=3, sticky="ew", padx=(5, 0))
        editor.grid_columnconfigure(3, weight=1)

        fish_buttons = tk.Frame(self.fishing_frame)
        fish_buttons.pack(fill="x", pady=(6, 0))
        tk.Button(fish_buttons, text="新增設定", width=10, command=self.new_fishing_profile).pack(side="left", padx=(0, 6))
        tk.Button(fish_buttons, text="儲存", width=9, command=self.save_fishing_profile).pack(side="left", padx=(0, 6))
        self.fishing_delete_button = tk.Button(fish_buttons, text="刪除", width=12, command=self.delete_fishing_profile)
        self.fishing_delete_button.pack(side="left", padx=(0, 6))
        tk.Button(fish_buttons, text="取消勾選", width=10, command=self.cancel_fishing_assignment).pack(side="left", padx=(0, 10))
        tk.Label(fish_buttons, textvariable=self.fishing_editor_state_var, anchor="w", fg="#444444").pack(side="left", fill="x", expand=True)

        # 最近紀錄改成可展開／收回；預設收回，讓主畫面保持精簡。
        log_toggle = tk.Frame(self, padx=14, pady=0)
        log_toggle.pack(fill="x", pady=(0, 10))
        self.log_toggle_button = tk.Button(log_toggle, text="展開最近紀錄 ▼", width=16, command=self.toggle_logs)
        self.log_toggle_button.pack(side="right")

        self.log_frame = tk.LabelFrame(self, text="最近紀錄", padx=8, pady=8)
        self.log_box = ScrolledText(self.log_frame, wrap="word", height=10, font=("Microsoft JhengHei UI", 9))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")
        self.logs_expanded = False
        self._dependency_cache = ([], False)
        self._dependency_cache_at = 0.0

    def toggle_logs(self):
        """展開／收回最近紀錄；只改控制台排版，不影響背景重連流程。"""
        self.update_idletasks()
        width = max(self.winfo_width(), 920)
        if self.logs_expanded:
            self.log_frame.pack_forget()
            self.logs_expanded = False
            self.log_toggle_button.config(text="展開最近紀錄 ▼")
            self.update_idletasks()
            collapsed_h = max(self.winfo_reqheight() + 6, 630)
            self.geometry(f"{width}x{collapsed_h}")
        else:
            self.log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
            self.logs_expanded = True
            self.log_toggle_button.config(text="收回最近紀錄 ▲")
            self.update_idletasks()
            expanded_h = max(self.winfo_height(), 880)
            self.geometry(f"{width}x{expanded_h}")

    def toggle_monitor(self):
        if current_pid():
            self.stop_monitor()
        else:
            self.start_monitor()

    def on_tree_double_click(self, _event=None):
        hwnd = self.selected_hwnd()
        if not hwnd:
            return
        if int(hwnd) in read_bindings():
            self.set_selected_role()
        else:
            self.bind_selected_row()

    def on_tree_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            try:
                self.row_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.row_menu.grab_release()

    def _all_shortcut_paths(self):
        paths = []
        try:
            raw, profiles = read_binding_store()
            for item in list(raw.values()) + list(profiles.values()):
                if not isinstance(item, dict):
                    continue
                path = str(item.get("shortcut_path", "") or "").strip()
                if path and path not in paths:
                    paths.append(path)
        except Exception:
            pass
        return paths

    def apply_unified_dpi_policy(self, show_result=False):
        try:
            rows = enum_game_windows()
            hwnds = [int(r.get("hwnd", 0)) for r in rows if int(r.get("hwnd", 0) or 0)]
            state = dpi_policy.apply_unified_policy(hwnds, self._all_shortcut_paths())
            pending = [h for h in hwnds if dpi_policy.window_needs_restart_for_dpi(h)]
            self.dpi_pending_count = len(pending)
            changed = [r for r in state.get("targets", []) if isinstance(r, dict) and r.get("changed")]
            failed = [r for r in state.get("targets", []) if isinstance(r, dict) and not r.get("ok")]
            if show_result:
                if failed:
                    messagebox.showwarning("統一 DPI 設定", "有部分遊戲宿主無法套用 DPI 設定：\n" + "\n".join(str(x.get("reason", "未知")) for x in failed[:4]))
                elif pending:
                    messagebox.showinfo(
                        "統一 DPI 設定",
                        f"已套用 Windows『由應用程式處理高 DPI』設定。\n\n目前 {len(pending)} 個已開啟視窗仍是舊的 DPI 虛擬化程序；設定只會在遊戲重新啟動後生效。\n\n程式不會強制重開目前遊戲；設定會在你下次自然啟動該遊戲時生效，並自動接回原綁定。",
                    )
                else:
                    messagebox.showinfo("統一 DPI 設定", f"DPI 統一設定已完成。\n已更新 {len(changed)} 個程式；目前視窗不需要因 DPI 重新啟動。")
            return state, pending
        except Exception as e:
            if show_result:
                messagebox.showerror("統一 DPI 設定失敗", str(e))
            return {}, []

    @staticmethod
    def _launch_shortcut_no_activate(path: str) -> bool:
        try:
            full = Path(os.path.expandvars(str(path))).expanduser()
            shell_execute = ctypes.windll.shell32.ShellExecuteW
            shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
            shell_execute.restype = ctypes.c_void_p
            rc = shell_execute(None, "open", str(full), None, str(full.parent), 4)
            return int(rc or 0) > 32
        except Exception:
            return False

    def _resolve_binding_for_dpi_relaunch(self, hwnd: int):
        """為 DPI 重開解析『目前這一列已綁定的身分』。

        V11.0 的錯誤是只信 read_bindings() 單次結果；表格本身卻允許在讀檔鎖競爭時
        沿用 identity_cache，因此會出現畫面明明顯示已綁定、按 DPI 修復卻說未綁定。

        這裡只接受精確目前 HWND 的既有證據，不猜其他視窗：
        1. read_bindings() 的目前有效精確 HWND
        2. bindings.json 原始精確 HWND
        3. profile.last_hwnd 精確等於目前 HWND
        4. 控制台已經對同一 HWND 顯示過的 identity_cache
        """
        hwnd = int(hwnd)
        try:
            live_hwnds = {int(r.get("hwnd", 0) or 0) for r in enum_game_windows()}
            if hwnd not in live_hwnds:
                return None, "所選視窗已不存在"
        except Exception:
            # selected_hwnd 來自目前表格；列舉若短暫失敗，不因此抹掉既有綁定。
            pass

        def valid(item):
            if not isinstance(item, dict):
                return None
            path = str(item.get("shortcut_path", "") or "").strip()
            if not path:
                return None
            return dict(item)

        # 最多三次短重試；任何一次成功就停止。
        # 這不是重新綁定，也不會改寫資料，只是避免跨程序檔案鎖的瞬時讀空。
        for attempt in range(3):
            try:
                item = valid(read_bindings().get(hwnd))
                if item:
                    return item, "目前有效綁定"
            except Exception:
                pass

            try:
                raw, profiles = read_binding_store()
                item = valid(raw.get(hwnd))
                if item:
                    return item, "原始精確視窗綁定"

                matches = []
                seen_paths = set()
                for prof in profiles.values():
                    if not isinstance(prof, dict):
                        continue
                    try:
                        last_hwnd = int(prof.get("last_hwnd", 0) or 0)
                    except Exception:
                        last_hwnd = 0
                    if last_hwnd != hwnd:
                        continue
                    candidate = valid(prof)
                    if not candidate:
                        continue
                    key = _profile_key(candidate.get("shortcut_path", ""))
                    if key and key not in seen_paths:
                        seen_paths.add(key)
                        matches.append(candidate)
                if len(matches) == 1:
                    return matches[0], "持久身分記錄"
            except Exception:
                pass

            # identity_cache 只以精確 HWND 為鍵，且只在先前成功讀到/完成綁定時寫入。
            cached = valid(self.identity_cache.get(hwnd))
            if cached:
                return cached, "控制台已驗證快取"

            if attempt < 2:
                try:
                    self.update_idletasks(); self.update()
                except Exception:
                    pass
                time.sleep(0.08)

        return None, "找不到精確綁定證據"

    def relaunch_selected_unified_dpi(self):
        if win32gui is None or win32con is None:
            messagebox.showerror(
                "DPI 修復重開",
                (
                    "免安裝封裝缺少 Windows 介面模組；請重新取得完整成品。"
                    if FROZEN
                    else "開發環境缺少 pywin32；請依專案鎖定檔安裝。"
                ),
            )
            return
        hwnd = self.selected_hwnd()
        if not hwnd:
            messagebox.showwarning("DPI 修復重開", "請先在遊戲視窗表格選取要修復的已綁定視窗。")
            return
        item, binding_source = self._resolve_binding_for_dpi_relaunch(int(hwnd))
        if not item:
            # 不能把『讀取不到』誤報成『使用者沒綁定』。
            shown = ""
            try:
                iid = f"h{int(hwnd)}"
                if self.tree.exists(iid):
                    vals = list(self.tree.item(iid, "values"))
                    shown = str(vals[0] if vals else "" or "")
            except Exception:
                pass
            label = f"（表格目前顯示：{shown}）" if shown and shown != "未綁定" else ""
            messagebox.showerror(
                "DPI 修復重開",
                "這個視窗的綁定資料目前無法安全解析" + label + "。\n\n"
                "這是綁定資料讀取問題，不代表你沒有綁定；程式不會要求你重新綁一次。\n"
                "請保留目前遊戲視窗，關閉此提示後再按一次；若仍出現，紀錄會保留原綁定身分供修復。",
            )
            return
        shortcut = str(item.get("shortcut_path", "") or "").strip()
        if not shortcut or not Path(os.path.expandvars(shortcut)).expanduser().exists():
            messagebox.showerror("DPI 修復重開", "已綁定的捷徑不存在，為避免重開錯誤視窗，本次不執行。")
            return
        name = str(item.get("shortcut_name", "") or Path(shortcut).stem or "遊戲")
        role = str(item.get("preferred_role", "") or "")
        fishing_profile_id = str(item.get("fishing_profile_id", "") or "")
        reconnect_enabled = bool(item.get("reconnect_enabled", True))
        manor_enabled = bool(item.get("manor_enabled", False))
        fishing_enabled = bool(item.get("fishing_enabled", bool(fishing_profile_id)))
        # V11.5：DPI 修復是對「目前選取的單一已綁定視窗」執行，不再跳逐視窗確認窗。
        # 結果與錯誤仍寫到主控制台；未選取/無法安全解析時原有保護照舊。
        self.status_label.config(text=f"DPI 修復重開中：{name}")
        self.hint_label.config(text=f"{name}：保留目前實際可見尺寸與位置，重開後自動還原；不處理其他遊戲視窗。")

        # Stop monitor first so no worker can act on the window while it is being replaced.
        if current_pid():
            self.stop_monitor()
            deadline = time.monotonic() + 5.0
            while current_pid() and time.monotonic() < deadline:
                self.update_idletasks(); self.update(); time.sleep(0.08)
            if current_pid():
                messagebox.showerror("DPI 修復重開", "背景監測在 5 秒內沒有停止；為避免監測程序與重開流程同時操作，本次已停止。")
                return

        try:
            dpi_policy.apply_unified_policy([int(hwnd)], [shortcut])
        except Exception as e:
            messagebox.showerror("DPI 修復重開", f"無法套用 DPI 設定：\n{e}")
            return

        # V11.5：保存「使用者眼睛實際看到」的視窗可見外框（DWM 實體像素）。
        # 不能再把 Flash 邏輯 900x590 或 DPI 虛擬化後的 GetWindowRect 當成使用者尺寸。
        # 若使用者是從 V11.4 升級，而且目前窗剛好仍停在 V11.4 強制的 safe_client，
        # 優先保留 V11.4 已記錄的「重開前外框」做一次性遷移，避免把被縮小後的 900x590 當成新偏好。
        previous_geometry = window_geometry.load_profile(shortcut)
        legacy_outer_rect = None
        try:
            legacy_rect = previous_geometry.get("user_rect") if isinstance(previous_geometry, dict) else None
            has_v115_visible = bool(previous_geometry.get("user_visible_rect")) if isinstance(previous_geometry, dict) else False
            # V11.4 的 user_rect 就是「程式改窗以前」保存的舊外框；V11.5 第一次看到舊格式時
            # 一律優先遷移它，而不是把 V11.4 可能已縮小的目前視窗誤認成新偏好。
            if not has_v115_visible and isinstance(legacy_rect, (list, tuple)) and len(legacy_rect) == 4:
                legacy_outer_rect = tuple(int(v) for v in legacy_rect)
        except Exception:
            legacy_outer_rect = None
        old_user_rect = window_geometry.visible_rect(int(hwnd))
        if old_user_rect is None:
            old_user_rect = window_geometry.outer_rect(int(hwnd))
        baseline = {int(r.get("hwnd", 0)) for r in enum_game_windows()}
        try:
            win32gui.PostMessage(int(hwnd), win32con.WM_CLOSE, 0, 0)
        except Exception as e:
            messagebox.showerror("DPI 修復重開", f"無法關閉舊視窗：\n{e}")
            return

        close_deadline = time.monotonic() + 8.0
        while win32gui.IsWindow(int(hwnd)) and time.monotonic() < close_deadline:
            self.update_idletasks(); self.update(); time.sleep(0.10)
        if win32gui.IsWindow(int(hwnd)):
            messagebox.showerror("DPI 修復重開", "舊遊戲視窗在 8 秒內沒有關閉；為避免重複啟動，本次已停止。")
            return

        # Once the old process has exited, the AppCompat DPI policy is applied at process creation.
        before = {int(r.get("hwnd", 0)) for r in enum_game_windows()}
        if not self._launch_shortcut_no_activate(shortcut):
            messagebox.showerror("DPI 修復重開", "原捷徑背景啟動失敗。DPI 設定已保留，但沒有建立新視窗。")
            return

        new_hwnd = 0
        deadline = time.monotonic() + 35.0
        while time.monotonic() < deadline:
            self.update_idletasks(); self.update(); time.sleep(0.20)
            current = {int(r.get("hwnd", 0)) for r in enum_game_windows()}
            news = [h for h in current if h not in before and h not in baseline]
            if not news:
                # If the old HWND was the only difference, any new HWND not present immediately before launch is valid.
                news = [h for h in current if h not in before]
            if news:
                new_hwnd = int(news[0]); break
        if not new_hwnd:
            messagebox.showerror("DPI 修復重開", "35 秒內沒有偵測到新 Flash 視窗。DPI 設定已完成，但無法自動轉移綁定。")
            return

        # V11.5：先記住「新 DPI 程序原生啟動時」的 client 尺寸，只把它當 Flash 輸入基準。
        # 接著立即把新視窗的 DWM 可見外框恢復到重開前的實體尺寸/位置；
        # 不把 input_base_client 拿去改 Windows 視窗大小。
        input_base_client = window_geometry.client_size(int(new_hwnd))
        desired_visible_rect = old_user_rect
        migrated_legacy_size = False
        if legacy_outer_rect:
            migrated = window_geometry.legacy_outer_to_visible_for_window(int(new_hwnd), legacy_outer_rect)
            if migrated:
                desired_visible_rect = migrated
                migrated_legacy_size = True
        restore_ok = False
        if desired_visible_rect:
            restore_ok = window_geometry.restore_visible_rect_noactivate(int(new_hwnd), desired_visible_rect)
            time.sleep(0.25)
        window_geometry.save_profile(shortcut, desired_visible_rect, input_base_client)

        # Persist the same identity on the new HWND, then remove the dead HWND record.
        save_user_binding_atomic(new_hwnd, shortcut, name, role)
        if fishing_profile_id:
            set_fishing_assignment(new_hwnd, fishing_profile_id)
        set_feature_switches(new_hwnd, reconnect_enabled, manor_enabled, fishing_enabled)
        try:
            with binding_file_lock():
                raw, profiles = _read_binding_store_unlocked()
                raw.pop(int(hwnd), None)
                payload = {
                    "version": 4,
                    "updated_at": time.time(),
                    "bindings": {str(int(k)): v for k, v in raw.items() if isinstance(v, dict)},
                    "profiles": profiles,
                }
                tmp = BINDINGS_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, BINDINGS_PATH)
        except Exception:
            pass

        self.identity_cache.pop(int(hwnd), None)
        self.identity_cache[int(new_hwnd)] = {
            "shortcut_path": shortcut,
            "shortcut_name": name,
            "preferred_role": role,
            "fishing_profile_id": fishing_profile_id,
        }
        mdpi = dpi_policy.get_monitor_dpi(new_hwnd)
        wdpi = dpi_policy.get_window_dpi(new_hwnd)
        base_text = f"；輸入基準={input_base_client[0]}x{input_base_client[1]}" if input_base_client else ""
        size_text = ("；已從 V11.4 舊尺寸紀錄還原" if migrated_legacy_size and restore_ok else ("；原可見尺寸已還原" if restore_ok else "；原可見尺寸還原未確認"))
        if int(mdpi) != int(wdpi):
            self.status_label.config(text=f"DPI 修復未通過：{name}")
            self.hint_label.config(text=f"{name} 已重開，但螢幕 DPI={mdpi}、視窗 DPI={wdpi}；本視窗維持零輸入，不跳額外提示視窗。")
        else:
            self.status_label.config(text=f"DPI 修復完成：{name}")
            self.hint_label.config(text=f"{name}：DPI {mdpi}/{wdpi} 通過{base_text}{size_text}；不再以輸入基準改變 Windows 視窗尺寸。")
        # V11.5：成功/自我檢查結果只顯示在主控制台，不再每個遊戲跳一個 messagebox。
        self.start_monitor(silent=True)

    def save_options(self):
        self.ui_cfg["auto_start"] = False
        save_ui_config(self.ui_cfg)

    def auto_start_if_needed(self):
        if current_pid() is None:
            missing, _ = dependency_status()
            if not missing:
                self.start_monitor(silent=True)

    def start_monitor(self, silent=False):
        # V11: idempotently prepare the actual Flash host/shortcut before monitoring.
        self.apply_unified_dpi_policy(show_result=False)
        pid = current_pid()
        if pid:
            if not silent:
                messagebox.showinfo("智慧重連", f"背景監測已經在執行。\n程序編號：{pid}")
            return
        missing, _ = dependency_status()
        if missing:
            names = sorted({m.get("package", m.get("module", "未知")) for m in missing})
            if FROZEN:
                message = (
                    "免安裝封裝不完整、已損壞，或被防毒軟體移除檔案：\n"
                    + "、".join(names)
                    + "\n\n請重新解壓完整 ZIP 或重新取得單檔版；安裝 Python 或執行 pip 無法修復。"
                )
            else:
                message = (
                    "目前開發環境缺少：\n"
                    + "、".join(names)
                    + "\n\n請依專案鎖定檔安裝必要套件。"
                )
            messagebox.showwarning(
                "封裝自我檢查失敗" if FROZEN else "開發環境不完整",
                message,
            )
            return
        try:
            STOP_SIGNAL_PATH.unlink(missing_ok=True)
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            LOG_DIR.mkdir(exist_ok=True)
            try:
                STARTUP_LOG_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            self.startup_log_handle = open(STARTUP_LOG_PATH, "a", encoding="utf-8", errors="replace")
            child_env = os.environ.copy()
            # 強制背景 Python 的 stdout/stderr 使用 UTF-8。Windows 繁中系統預設可能是 CP950，
            # 若直接轉送到檔案，控制台以 UTF-8 讀取就會出現整段亂碼。
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            if FROZEN:
                child_cmd = [sys.executable, "--monitor"]
            else:
                child_cmd = [sys.executable, str(MONITOR_SCRIPT)]
            self.child = subprocess.Popen(
                child_cmd,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=self.startup_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                env=child_env,
            )
            self.child_start_deadline = time.monotonic() + 30.0
            self.status_label.config(text="監測程序：正在啟動……")
            self.hint_label.config(text="正在初始化 OCR、畫面模板並掃描遊戲視窗；每一步都會寫入下方紀錄。")
            self.after(500, self.check_child_startup)
        except Exception as e:
            try:
                if self.startup_log_handle:
                    self.startup_log_handle.close()
            except Exception:
                pass
            self.startup_log_handle = None
            messagebox.showerror("啟動失敗", f"無法啟動背景監測：\n{e}")

    def check_child_startup(self):
        """V5.4：持續追蹤啟動，不再只檢查一次後就放棄。"""
        child = self.child
        if child is None:
            return

        # PID 檔已建立且程序仍存活，代表核心已進入正式啟動流程。
        pid = current_pid()
        if pid:
            self.status_label.config(text=f"監測程序：執行中（程序編號 {pid}）")
            self.hint_label.config(text="背景監測核心已啟動；下方紀錄會顯示 OCR、模板、視窗掃描與監測迴圈各階段。")
            return

        code = child.poll()
        if code is None:
            # OCR/模型第一次載入可能超過數秒；30 秒內持續確認，不誤判成未啟動。
            if time.monotonic() < self.child_start_deadline:
                self.status_label.config(text="監測程序：正在初始化……")
                self.after(500, self.check_child_startup)
                return
            self.status_label.config(text="監測程序：初始化逾時")
            self.hint_label.config(text="背景程序仍存在，但 30 秒內未建立監測狀態。請開啟紀錄查看最後啟動階段。")
            return

        try:
            if self.startup_log_handle:
                self.startup_log_handle.flush()
                self.startup_log_handle.close()
        except Exception:
            pass
        self.startup_log_handle = None
        detail = ""
        try:
            if STARTUP_LOG_PATH.exists():
                lines = STARTUP_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                detail = "\n".join(lines[-18:]).strip()
        except Exception:
            pass
        self.status_label.config(text=f"監測程序：啟動失敗（結束碼 {code}）")
        self.hint_label.config(text="背景核心啟動後退出；最後啟動階段與錯誤已保留。")
        if detail:
            messagebox.showerror("背景監測啟動失敗", "背景核心啟動後退出。\n\n最後紀錄：\n" + detail)
        else:
            messagebox.showerror("背景監測啟動失敗", f"背景核心啟動後退出，結束碼：{code}。請按『開啟紀錄』查看。")

    def stop_monitor(self):
        pid = current_pid()
        if not pid:
            self.status_label.config(text="監測程序：未啟動")
            return
        try:
            STOP_SIGNAL_PATH.write_text("stop", encoding="ascii")
            self.status_label.config(text="監測程序：正在停止……")
        except Exception as e:
            messagebox.showerror("停止失敗", f"無法送出停止指令：\n{e}")

    def open_config(self):
        try:
            subprocess.Popen(["notepad.exe", str(CONFIG_PATH)])
        except Exception as e:
            messagebox.showerror("開啟失敗", str(e))

    def open_logs(self):
        LOG_DIR.mkdir(exist_ok=True)
        try:
            os.startfile(str(LOG_DIR))
        except Exception as e:
            messagebox.showerror("開啟失敗", str(e))

    def install_deps(self):
        if FROZEN:
            messagebox.showinfo(
                "免安裝版本",
                "此版本已內含 Python 與全部必要套件，不應另外安裝。\n\n"
                "若自我檢查失敗，代表檔案不完整、損壞或遭防毒隔離，請重新取得完整成品。",
            )
            return
        installer = BASE_DIR / "install_requirements.py"
        if not installer.exists():
            messagebox.showerror("檔案不存在", "找不到安裝必要套件程式。")
            return
        try:
            pyexe = console_python_path()
            subprocess.Popen(
                [pyexe, str(installer)],
                cwd=str(BASE_DIR),
                creationflags=0x00000010,  # CREATE_NEW_CONSOLE
            )
            messagebox.showinfo(
                "開始安裝",
                "已用『目前控制台同一個 Python』開始安裝。\n\n"
                "安裝視窗最後看到『安裝完成』後直接關閉即可；控制台會自動重新檢查，不需要重開也不需要再裝第二次。",
            )
        except Exception as e:
            messagebox.showerror("啟動失敗", str(e))

    def selected_hwnd(self):
        sel = self.tree.selection()
        if not sel:
            return None
        item = sel[0]
        try:
            if str(item).startswith("h"):
                return int(str(item)[1:])
        except Exception:
            pass
        return None

    def bind_foreground_after_delay(self):
        # 不再先跳說明視窗。按下後直接開始 3 秒倒數；
        # 使用者只要點一下目標 Flash，倒數結束立即跳出捷徑選擇視窗。
        self._bind_countdown_left = 3
        self.hint_label.config(text="綁定：3 秒內點一下要綁定的 Flash 遊戲視窗……")
        self.after(1000, self._bind_countdown_tick)

    def _bind_countdown_tick(self):
        left = int(getattr(self, "_bind_countdown_left", 0)) - 1
        self._bind_countdown_left = left
        if left > 0:
            self.hint_label.config(text=f"綁定：請點目標 Flash 視窗，{left} 秒後自動選擇捷徑……")
            self.after(1000, self._bind_countdown_tick)
            return
        self.capture_foreground_for_binding()

    def capture_foreground_for_binding(self):
        try:
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            hwnd = 0
        valid = {int(r.get("hwnd", 0)) for r in enum_game_windows()}
        if hwnd <= 0 or hwnd not in valid:
            self.hint_label.config(text="綁定失敗：倒數結束時前景不是受監測的 Flash 視窗。")
            messagebox.showwarning("沒有抓到遊戲視窗", "沒有抓到 Flash 遊戲視窗。請按『綁定遊戲視窗』後，在 3 秒內點一下目標遊戲。")
            return
        self.choose_shortcut_for_hwnd(hwnd)

    def bind_selected_row(self):
        hwnd = self.selected_hwnd()
        if not hwnd:
            messagebox.showwarning("尚未選擇", "請先在下方表格選一個遊戲視窗。\n如果你不知道哪一列是哪個視窗，請用『綁定遊戲視窗』。")
            return
        self.choose_shortcut_for_hwnd(hwnd)

    def choose_shortcut_for_hwnd(self, hwnd: int):
        path = filedialog.askopenfilename(
            title="選擇這個遊戲視窗對應的捷徑",
            initialdir=str(desktop_dir()),
            filetypes=[
                ("Windows 捷徑", "*.lnk"),
                ("網址捷徑", "*.url"),
                ("可執行檔", "*.exe"),
                ("所有檔案", "*.*"),
            ],
        )
        if not path:
            return
        path = str(Path(path))
        name = Path(path).stem
        bindings = read_bindings()
        _raw_all, profiles = read_binding_store()
        identities = read_identity_profiles()

        same = []
        norm = os.path.normcase(os.path.abspath(path))
        for other_hwnd, item in list(bindings.items()):
            other_path = str(item.get("shortcut_path", "") or "")
            if other_hwnd != int(hwnd) and other_path:
                try:
                    if os.path.normcase(os.path.abspath(other_path)) == norm:
                        same.append(other_hwnd)
                except Exception:
                    pass
        if same:
            if not messagebox.askyesno(
                "捷徑已被綁定",
                f"『{name}』目前已綁在另一個遊戲視窗。\n要把它改綁到目前這個視窗嗎？",
            ):
                return
            # 先移除舊 HWND 對應；捷徑身分資料仍保留。
            with binding_file_lock():
                raw, profs = _read_binding_store_unlocked()
                for other_hwnd in same:
                    raw.pop(int(other_hwnd), None)
                payload = {
                    "version": 4, "updated_at": time.time(),
                    "bindings": {str(int(k)): v for k, v in raw.items() if isinstance(v, dict)},
                    "profiles": profs,
                }
                tmp = BINDINGS_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, BINDINGS_PATH)
                for other_hwnd in same:
                    self.identity_cache.pop(int(other_hwnd), None)

        role_default = ""
        pkey = _profile_key(path)
        profile = dict(identities.get(pkey, {}) or profiles.get(pkey, {}) or {})
        if profile.get("preferred_role"):
            role_default = str(profile.get("preferred_role", "") or "")

        # V6.9：先詢問角色，再一次寫入捷徑+角色；不留下半秒的中間狀態。
        role = simpledialog.askstring(
            "設定角色名稱",
            f"捷徑：{name}\n\n請輸入完整角色名稱。\n實際選角以名稱第一個字為主要辨識條件。\n例如：嘻の百二補師 → 嘻。\n\n取消或留空時，角色為『未知』。",
            initialvalue=role_default,
            parent=self,
        )
        saved_role = role_default if role is None else role.strip()
        item = save_user_binding_atomic(int(hwnd), path, name, saved_role)
        # V11.6：綁定只保存身分；DPI 只做「未設定才補」的未來啟動準備。
        # 不關閉、不重開、不改視窗大小/位置，也不覆蓋使用者既有 DPI 相容設定。
        try:
            dpi_policy.apply_unified_policy([int(hwnd)], [path])
        except Exception:
            pass
        self.identity_cache[int(hwnd)] = {
            "shortcut_path": path,
            "shortcut_name": name,
            "preferred_role": saved_role,
            "preferred_line_no": int(item.get("preferred_line_no", 0) or 0),
            "fishing_profile_id": str(item.get("fishing_profile_id", "") or ""),
            "reconnect_enabled": bool(item.get("reconnect_enabled", True)),
            "fishing_enabled": bool(item.get("fishing_enabled", bool(item.get("fishing_profile_id", "")))),
            "manor_enabled": bool(item.get("manor_enabled", False)),
        }

        iid = f"h{int(hwnd)}"
        if self.tree.exists(iid):
            vals = list(self.tree.item(iid, "values"))
            if vals:
                role_text = f"{saved_role}（{saved_role[0]}）" if saved_role else "未知"
                vals[0] = f"{role_text}  ·  {name}"
                if len(vals) > 4:
                    fishing_id = str(item.get("fishing_profile_id", "") or "")
                    vals[1] = "☑" if bool(item.get("reconnect_enabled", True)) else "☐"
                    vals[2] = "☑" if bool(item.get("manor_enabled", False)) else "☐"
                    vals[3] = "☑" if bool(item.get("fishing_enabled", bool(fishing_id))) else "☐"
                    vals[4] = line_setting_label(item.get("preferred_line_no", 0))
                self.tree.item(iid, values=vals)
                self.tree.selection_set(iid)
                self.tree.focus(iid)

        if saved_role:
            self.hint_label.config(text=f"已完成綁定：{name} → {saved_role}；之後 HWND/PID 改變會自動接回，不需要重綁，也不會因綁定而重開遊戲。")
        else:
            self.hint_label.config(text=f"已綁定：{name}；角色目前為未知。綁定不會重開遊戲或改變視窗設定。")

    def prompt_role_for_hwnd(self, hwnd: int, after_bind: bool = False):
        bindings = read_bindings()
        item = bindings.get(int(hwnd))
        if not item:
            messagebox.showwarning("尚未綁定", "請先把這個遊戲視窗綁定到它的桌面捷徑。")
            return

        old = str(item.get("preferred_role", "") or "")
        prompt_prefix = "捷徑綁定完成。\n\n" if after_bind else ""
        role = simpledialog.askstring(
            "設定角色名稱",
            prompt_prefix
            + f"捷徑：{item.get('shortcut_name', '未知')}\n\n"
              "請輸入完整角色名稱。\n"
              "實際選角以名稱第一個字為主要辨識條件。\n"
              "例如：嘻の百二補師 → 嘻。\n\n"
              "取消或留空時，角色會維持『未知』，程式不會自動選角色。",
            initialvalue=old,
            parent=self,
        )

        # 取消：不改現有值。新捷徑沒有舊角色時自然保持「未知」。
        if role is None:
            saved_role = old
        else:
            item = save_user_binding_atomic(
                int(hwnd),
                item.get("shortcut_path", ""),
                item.get("shortcut_name", ""),
                role.strip(),
            )
            saved_role = role.strip()
            self.identity_cache[int(hwnd)] = {
                "shortcut_path": str(item.get("shortcut_path", "") or ""),
                "shortcut_name": str(item.get("shortcut_name", "") or ""),
                "preferred_role": saved_role,
                "preferred_line_no": int(item.get("preferred_line_no", 0) or 0),
                "fishing_profile_id": str(item.get("fishing_profile_id", "") or ""),
                "reconnect_enabled": bool(item.get("reconnect_enabled", True)),
                "fishing_enabled": bool(item.get("fishing_enabled", bool(item.get("fishing_profile_id", "")))),
                "manor_enabled": bool(item.get("manor_enabled", False)),
            }

        # 不等背景程序回寫，直接刷新表格角色欄。
        iid = f"h{int(hwnd)}"
        if self.tree.exists(iid):
            vals = list(self.tree.item(iid, "values"))
            if vals:
                if len(vals) > 0:
                    shortcut_name = str(item.get("shortcut_name", "") or "未綁定")
                    role_text = f"{saved_role}（{saved_role[0]}）" if saved_role else "未知"
                    vals[0] = f"{role_text}  ·  {shortcut_name}"
                if len(vals) > 4:
                    fishing_id = str(item.get("fishing_profile_id", "") or "")
                    vals[1] = "☑" if bool(item.get("reconnect_enabled", True)) else "☐"
                    vals[2] = "☑" if bool(item.get("manor_enabled", False)) else "☐"
                    vals[3] = "☑" if bool(item.get("fishing_enabled", bool(fishing_id))) else "☐"
                    vals[4] = line_setting_label(item.get("preferred_line_no", 0))
                self.tree.item(iid, values=vals)
                self.tree.selection_set(iid)
                self.tree.focus(iid)

        if role is None:
            if saved_role:
                self.hint_label.config(text=f"角色設定未變更：{saved_role}。")
            else:
                self.hint_label.config(text=f"『{item.get('shortcut_name', '')}』已綁定；角色目前為未知。")
        elif saved_role:
            self.hint_label.config(
                text=f"已完成綁定：{item.get('shortcut_name', '')} → {saved_role}；辨識首字：{saved_role[0]}。"
            )
        else:
            self.hint_label.config(text=f"『{item.get('shortcut_name', '')}』已綁定；角色目前為未知。")

    def set_selected_role(self):
        hwnd = self.selected_hwnd()
        if not hwnd:
            messagebox.showwarning("尚未選擇", "請先選擇一個已綁定的遊戲視窗。")
            return
        self.prompt_role_for_hwnd(int(hwnd), after_bind=False)

    def on_game_selection_changed(self, _event=None):
        self.after_idle(self.sync_feature_panel_to_selection)
        self.after_idle(self.sync_line_panel_to_selection)
        self.after_idle(self.sync_fishing_panel_to_selection)
        self.after_idle(self.sync_manor_panel_to_selection)

    def sync_feature_panel_to_selection(self):
        hwnd = int(self.selected_hwnd() or 0)
        binding = read_bindings().get(hwnd, {}) if hwnd else {}
        self.feature_panel_updating = True
        try:
            if not binding:
                self.reconnect_enabled_var.set(False)
                self.manor_enabled_var.set(False)
                self.fishing_enabled_var.set(False)
                self.feature_state_var.set("請先選擇已綁定角色")
                return
            assigned = str(binding.get("fishing_profile_id", "") or "").strip()
            reconnect_enabled = bool(binding.get("reconnect_enabled", True))
            manor_enabled = bool(binding.get("manor_enabled", False))
            fishing_enabled = bool(binding.get("fishing_enabled", bool(assigned)))
            self.reconnect_enabled_var.set(reconnect_enabled)
            self.manor_enabled_var.set(manor_enabled)
            self.fishing_enabled_var.set(fishing_enabled)
            enabled = []
            if reconnect_enabled:
                enabled.append("自動重連")
            if manor_enabled:
                enabled.append("莊園")
            if fishing_enabled:
                enabled.append("釣魚")
            self.feature_state_var.set("已啟用：" + "、".join(enabled) if enabled else "三項均未啟用；程式不會操作此角色")
        finally:
            self.feature_panel_updating = False

    def on_feature_switches_toggle(self):
        if self.feature_panel_updating:
            return
        hwnd = int(self.selected_hwnd() or 0)
        if not hwnd:
            self.feature_state_var.set("請先選擇已綁定角色")
            return
        binding = read_bindings().get(hwnd, {})
        if not binding:
            self.feature_state_var.set("請先綁定這個遊戲視窗")
            return
        assigned = str(binding.get("fishing_profile_id", "") or "").strip()
        if self.fishing_enabled_var.get() and not assigned:
            self.feature_panel_updating = True
            try:
                self.fishing_enabled_var.set(False)
            finally:
                self.feature_panel_updating = False
            self.feature_state_var.set("釣魚尚未選擇魚級；請先在下方勾選一項釣魚設定")
            return
        try:
            saved = set_feature_switches(
                hwnd,
                self.reconnect_enabled_var.get(),
                self.manor_enabled_var.get(),
                self.fishing_enabled_var.get(),
            )
        except Exception as exc:
            self.feature_state_var.set(f"功能開關儲存失敗：{exc}")
            self.sync_feature_panel_to_selection()
            return
        self.identity_cache.setdefault(hwnd, {}).update({
            "reconnect_enabled": bool(saved.get("reconnect_enabled", True)),
            "manor_enabled": bool(saved.get("manor_enabled", False)),
            "fishing_enabled": bool(saved.get("fishing_enabled", False)),
        })
        self.manor_panel_cache[hwnd] = dict(saved)
        self.feature_state_var.set("功能開關已立即儲存；三項彼此獨立")
        self.update_tree(read_runtime_status(), enum_game_windows())

    def sync_line_panel_to_selection(self):
        hwnd = self.selected_hwnd()
        binding = read_bindings().get(int(hwnd), {}) if hwnd else {}
        self.line_panel_updating = True
        try:
            if not binding:
                self.line_var.set(LINE_LABEL_BY_NO[0])
                self.line_state_var.set("請先選擇已綁定角色")
                return
            line_no = int(binding.get("preferred_line_no", 0) or 0)
            if line_no not in range(0, 9):
                line_no = 0
            self.line_var.set(line_setting_label(line_no))
            if line_no:
                self.line_state_var.set("優先選指定線路；找不到則使用第一線")
            else:
                self.line_state_var.set("維持原流程：讀取最近一次登入線路")
        finally:
            self.line_panel_updating = False

    def on_line_assignment_changed(self, _event=None):
        if self.line_panel_updating:
            return
        hwnd = self.selected_hwnd()
        if not hwnd:
            self.line_state_var.set("請先選擇已綁定角色")
            return
        line_no = LINE_NO_BY_LABEL.get(self.line_var.get(), 0)
        try:
            saved = set_line_assignment(hwnd, line_no)
        except Exception as exc:
            self.line_state_var.set(f"儲存失敗：{exc}")
            return
        self.identity_cache.setdefault(int(hwnd), {}).update(
            {"preferred_line_no": int(saved.get("preferred_line_no", 0) or 0)}
        )
        self.line_state_var.set(
            "已儲存；指定線路找不到時使用第一線" if line_no else "已儲存；依最近一次登入線路"
        )
        self.update_tree(read_runtime_status(), enum_game_windows())

    def sync_manor_panel_to_selection(self):
        hwnd = self.selected_hwnd()
        # 使用者剛改作物或格數時，先等即時保存完成；一秒刷新不得把舊值蓋回來。
        if hwnd and int(hwnd) == int(self.manor_dirty_hwnd or 0):
            return
        binding = read_bindings().get(int(hwnd), {}) if hwnd else {}
        if hwnd and not binding:
            binding = dict(self.manor_panel_cache.get(int(hwnd), {}))
        self.manor_panel_updating = True
        try:
            self._sync_manor_panel_values(hwnd, binding)
        finally:
            self.manor_panel_updating = False

    def _sync_manor_panel_values(self, hwnd, binding):
        if not binding:
            self.manor_enabled_var.set(False)
            self.manor_crop_var.set(CROP_OPTIONS[0].label)
            self.manor_quantity_var.set(16)
            self.manor_state_var.set("請先在上方選擇已綁定視窗")
            return
        self.manor_panel_cache[int(hwnd)] = dict(binding)
        crop_key = str(binding.get("manor_crop_key", CROP_OPTIONS[0].key) or CROP_OPTIONS[0].key)
        crop = next((item for item in CROP_OPTIONS if item.key == crop_key), CROP_OPTIONS[0])
        self.manor_enabled_var.set(bool(binding.get("manor_enabled", False)))
        self.manor_crop_var.set(crop.label)
        self.manor_quantity_var.set(max(1, min(16, int(binding.get("manor_quantity", 16) or 16))))
        status = read_manor_status().get("profiles", {}).get(str(int(hwnd)), {})
        state_text = str(status.get("status", "") or "")
        if not state_text:
            state_text = "已啟用；開始監測後立即執行" if self.manor_enabled_var.get() else "未啟用"
        self.manor_state_var.set(state_text)

    def on_manor_enabled_toggle(self):
        """Persist a checkbox click immediately so the one-second refresh cannot undo it."""
        if self.manor_panel_updating or self.feature_panel_updating:
            return
        self.on_feature_switches_toggle()

    def _cancel_pending_manor_save(self):
        if self.manor_save_after_id is not None:
            try:
                self.after_cancel(self.manor_save_after_id)
            except Exception:
                pass
        self.manor_save_after_id = None
        self.manor_pending_snapshot = None

    def _current_manor_snapshot(self):
        hwnd = int(self.selected_hwnd() or 0)
        if not hwnd:
            return None
        crop = CROP_BY_LABEL.get(self.manor_crop_var.get(), CROP_OPTIONS[0])
        try:
            quantity = int(self.manor_quantity_var.get())
        except (TypeError, ValueError, tk.TclError):
            return None
        if quantity not in range(1, 17):
            return None
        return (hwnd, bool(self.manor_enabled_var.get()), crop.key, quantity)

    def _persist_manor_snapshot(self, snapshot):
        if snapshot != self.manor_pending_snapshot:
            return
        self.manor_save_after_id = None
        self.manor_pending_snapshot = None
        hwnd, enabled, crop_key, quantity = snapshot
        try:
            saved = set_manor_settings(hwnd, enabled, crop_key, quantity)
        except Exception as exc:
            if int(self.selected_hwnd() or 0) == int(hwnd):
                self.manor_state_var.set(f"即時保存失敗：{exc}")
            return
        self.manor_panel_cache[int(hwnd)] = dict(saved)
        if int(self.manor_dirty_hwnd or 0) == int(hwnd):
            self.manor_dirty_hwnd = 0
        if int(self.selected_hwnd() or 0) == int(hwnd):
            self.manor_state_var.set("作物與格數已立即保存。")
            self.update_tree(read_runtime_status(), enum_game_windows())

    def _queue_manor_autosave(self, delay_ms=250):
        if self.manor_panel_updating:
            return
        snapshot = self._current_manor_snapshot()
        hwnd = int(self.selected_hwnd() or 0)
        if hwnd:
            self.manor_dirty_hwnd = hwnd
        if snapshot is None:
            self._cancel_pending_manor_save()
            self.manor_dirty_hwnd = hwnd
            self.manor_state_var.set("格數請輸入 1～16；輸入完成前不會被刷新覆蓋。")
            return
        self._cancel_pending_manor_save()
        self.manor_dirty_hwnd = int(snapshot[0])
        self.manor_pending_snapshot = snapshot
        self.manor_save_after_id = self.after(
            max(0, int(delay_ms)),
            lambda saved_snapshot=snapshot: self._persist_manor_snapshot(saved_snapshot),
        )

    def on_manor_crop_changed(self, _event=None):
        self._queue_manor_autosave(delay_ms=0)

    def on_manor_quantity_changed(self, *_args):
        self._queue_manor_autosave(delay_ms=250)

    def flush_manor_quantity_edit(self, _event=None):
        snapshot = self._current_manor_snapshot()
        if snapshot is None:
            # 無效輸入離開欄位時才恢復該角色已保存值。
            self._cancel_pending_manor_save()
            self.manor_dirty_hwnd = 0
            self.sync_manor_panel_to_selection()
            return
        self._queue_manor_autosave(delay_ms=0)

    def save_manor_settings(self, show_saved=True):
        hwnd = self.selected_hwnd()
        if not hwnd:
            self.manor_state_var.set("請先選擇上方已綁定的遊戲視窗。")
            return
        crop = CROP_BY_LABEL.get(self.manor_crop_var.get(), CROP_OPTIONS[0])
        try:
            quantity = max(1, min(16, int(self.manor_quantity_var.get())))
            self._cancel_pending_manor_save()
            saved = set_manor_settings(hwnd, self.manor_enabled_var.get(), crop.key, quantity)
        except Exception as exc:
            self.manor_state_var.set(f"儲存失敗：{exc}")
            return
        self.manor_panel_cache[int(hwnd)] = dict(saved)
        self.manor_dirty_hwnd = 0
        if show_saved:
            self.manor_state_var.set("已儲存；啟用後會立即執行第一次，完成後 60 分鐘再執行。")
        else:
            self.manor_state_var.set("莊園勾選已立即儲存。")
        self.update_tree(read_runtime_status(), enum_game_windows())

    def retry_manor_now(self):
        hwnd = self.selected_hwnd()
        if not hwnd:
            self.manor_state_var.set("請先選擇上方已綁定的遊戲視窗。")
            return
        crop = CROP_BY_LABEL.get(self.manor_crop_var.get(), CROP_OPTIONS[0])
        try:
            quantity = max(1, min(16, int(self.manor_quantity_var.get())))
            set_manor_settings(hwnd, True, crop.key, quantity, retry=True)
            self.manor_enabled_var.set(True)
        except Exception as exc:
            self.manor_state_var.set(f"立即重試失敗：{exc}")
            return
        self.manor_state_var.set("已要求立即重試；若監測尚未開始，按『開始監測』後執行。")

    def _selected_fishing_profile_id(self):
        selection = self.fishing_tree.selection()
        return str(selection[0]) if selection else ""

    def _set_fishing_editor(self, profile: dict | None):
        self.fishing_edit_profile_id = str((profile or {}).get("id", "") or "")
        self.fishing_delete_confirm_id = ""
        self.fishing_delete_button.config(text="刪除")
        self.fishing_name_var.set(str((profile or {}).get("name", "") or ""))
        self.fishing_message_box.delete("1.0", "end")
        self.fishing_message_box.insert("1.0", str((profile or {}).get("message", "") or ""))
        if profile:
            built_in = bool(profile.get("built_in", False))
            self.fishing_editor_state_var.set(
                "內建設定只能查看；按『新增設定』建立自訂項目。" if built_in else "編輯模式：修改後按『儲存』。"
            )
        else:
            self.fishing_editor_state_var.set("新增模式：輸入名稱與完整發送字串後按『儲存』。")

    def sync_fishing_panel_to_selection(self, force_editor: bool = False):
        if not hasattr(self, "fishing_tree"):
            return
        hwnd = int(self.selected_hwnd() or 0)
        changed = hwnd != int(self.fishing_panel_hwnd or 0)
        self.fishing_panel_hwnd = hwnd
        bindings = read_bindings()
        binding = bindings.get(hwnd, {}) if hwnd else {}
        bound = bool(binding and str(binding.get("shortcut_path", "") or "").strip())
        if not hwnd:
            self.fishing_window_var.set("目前視窗：請先在上方選擇已綁定視窗")
        elif not bound:
            self.fishing_window_var.set(f"目前視窗：{hwnd}（尚未綁定，不能保存釣魚設定）")
        else:
            window_name = str(binding.get("shortcut_name", "") or f"視窗 {hwnd}")
            self.fishing_window_var.set(f"目前視窗：{window_name}")

        assigned = str(binding.get("fishing_profile_id", "") or "") if bound else ""
        previous = self._selected_fishing_profile_id()
        profiles = fishing_profiles.load_profiles(force=True)
        signature = (
            hwnd,
            assigned,
            tuple(
                (
                    str(profile.get("id", "") or ""),
                    str(profile.get("name", "") or ""),
                    str(profile.get("message", "") or ""),
                )
                for profile in profiles
            ),
        )
        if signature == self.fishing_panel_signature and not changed and not force_editor:
            return
        self.fishing_panel_signature = signature
        wanted = assigned if changed else previous
        if not wanted and profiles:
            wanted = str(profiles[0].get("id", "") or "")

        self.fishing_panel_updating = True
        try:
            for row in self.fishing_tree.get_children():
                self.fishing_tree.delete(row)
            for profile in profiles:
                pid = str(profile.get("id", "") or "")
                groups = fishing_profiles.message_groups(profile)
                link_counts = fishing_profiles.profile_group_link_counts(profile)
                total_links = sum(link_counts)
                summary = " ／ ".join(groups)
                self.fishing_tree.insert(
                    "",
                    "end",
                    iid=pid,
                    values=(
                        "☑" if pid == assigned else "☐",
                        profile.get("name", ""),
                        f"{len(groups)}組/{total_links}點",
                        summary,
                    ),
                )
            if wanted and self.fishing_tree.exists(wanted):
                self.fishing_tree.selection_set(wanted)
                self.fishing_tree.focus(wanted)
                self.fishing_tree.see(wanted)
        finally:
            self.fishing_panel_updating = False

        if changed or force_editor:
            selected = fishing_profiles.profile_by_id(wanted) if wanted else None
            self._set_fishing_editor(selected)

    def on_fishing_profile_select(self, _event=None):
        if self.fishing_panel_updating:
            return
        profile = fishing_profiles.profile_by_id(self._selected_fishing_profile_id())
        if profile:
            self._set_fishing_editor(profile)

    def on_fishing_tree_click(self, event):
        row = self.fishing_tree.identify_row(event.y)
        column = self.fishing_tree.identify_column(event.x)
        if not row:
            return
        self.fishing_tree.selection_set(row)
        self.fishing_tree.focus(row)
        if column == "#1":
            self.after_idle(self.toggle_fishing_assignment)

    def toggle_fishing_assignment(self, _event=None):
        hwnd = int(self.selected_hwnd() or 0)
        pid = self._selected_fishing_profile_id()
        if not hwnd or not pid:
            self.fishing_editor_state_var.set("請先選擇上方遊戲視窗與一項魚級設定。")
            return
        binding = read_bindings().get(hwnd, {})
        if not binding or not str(binding.get("shortcut_path", "") or "").strip():
            self.fishing_editor_state_var.set("這個遊戲視窗尚未綁定，不能啟用常駐釣魚。")
            return
        current = str(binding.get("fishing_profile_id", "") or "")
        wanted = "" if current == pid else pid
        try:
            set_fishing_assignment(hwnd, wanted)
        except Exception as e:
            self.fishing_editor_state_var.set(f"套用失敗：{e}")
            return
        profile = fishing_profiles.profile_by_id(wanted) if wanted else None
        if profile:
            self.fishing_editor_state_var.set(
                f"已只套用並啟用目前角色『{profile.get('name', '')}』；清彈窗、確認無 X 與上下『目前』後開始。"
            )
        else:
            self.fishing_editor_state_var.set("已取消釣魚；斷線監測仍持續。")
        self.sync_fishing_panel_to_selection()
        self.sync_feature_panel_to_selection()

    def new_fishing_profile(self):
        self.fishing_panel_updating = True
        try:
            self.fishing_tree.selection_remove(*self.fishing_tree.selection())
        finally:
            self.fishing_panel_updating = False
        self._set_fishing_editor(None)
        self.fishing_name_entry.focus_set()

    def save_fishing_profile(self):
        name = self.fishing_name_var.get().strip()
        message = self.fishing_message_box.get("1.0", "end-1c").strip()
        valid, error = fishing_profiles.validate_profile(name, message)
        if valid is None:
            self.fishing_editor_state_var.set(f"格式錯誤：{error}")
            return
        pid = str(self.fishing_edit_profile_id or "")
        current = fishing_profiles.profile_by_id(pid) if pid else None
        if current and bool(current.get("built_in", False)):
            self.fishing_editor_state_var.set("內建設定不可修改；請先按『新增設定』再儲存。")
            return
        if current:
            saved, error = fishing_profiles.update_profile(pid, name, message)
        else:
            saved, error = fishing_profiles.add_profile(name, message)
        if saved is None:
            self.fishing_editor_state_var.set(f"儲存失敗：{error}")
            return
        saved_id = str(saved.get("id", "") or "")
        self.sync_fishing_panel_to_selection()
        if self.fishing_tree.exists(saved_id):
            self.fishing_tree.selection_set(saved_id)
            self.fishing_tree.focus(saved_id)
        self._set_fishing_editor(saved)
        self.fishing_editor_state_var.set(f"已儲存『{saved.get('name', '')}』；點左側方框即可套用到目前視窗。")

    def delete_fishing_profile(self):
        pid = self._selected_fishing_profile_id() or str(self.fishing_edit_profile_id or "")
        profile = fishing_profiles.profile_by_id(pid)
        if not profile:
            self.fishing_editor_state_var.set("請先選擇要刪除的自訂設定。")
            return
        if bool(profile.get("built_in", False)):
            self.fishing_editor_state_var.set("內建一到七級魚設定不能刪除。")
            return
        if self.fishing_delete_confirm_id != pid:
            self.fishing_delete_confirm_id = pid
            self.fishing_delete_button.config(text="再按一次確認刪除")
            self.fishing_editor_state_var.set(f"再按一次刪除『{profile.get('name', '')}』；不會跳出額外視窗。")
            return
        ok, error = fishing_profiles.delete_profile(pid)
        self.fishing_delete_confirm_id = ""
        self.fishing_delete_button.config(text="刪除")
        if not ok:
            self.fishing_editor_state_var.set(f"刪除失敗：{error}")
            return
        clear_deleted_fishing_assignments(pid)
        self.sync_fishing_panel_to_selection(force_editor=True)
        self.fishing_editor_state_var.set(f"已刪除『{profile.get('name', '')}』，相關視窗已取消勾選。")

    def cancel_fishing_assignment(self):
        hwnd = int(self.selected_hwnd() or 0)
        if not hwnd:
            self.fishing_editor_state_var.set("請先選擇上方遊戲視窗。")
            return
        try:
            set_fishing_assignment(hwnd, "")
        except Exception as e:
            self.fishing_editor_state_var.set(f"取消失敗：{e}")
            return
        self.fishing_editor_state_var.set("已取消釣魚；斷線監測仍持續。")
        self.sync_fishing_panel_to_selection()
        self.sync_feature_panel_to_selection()

    def open_fishing_settings(self):
        """Compatibility entry point: focus the embedded panel, never open a window."""
        self.sync_fishing_panel_to_selection(force_editor=True)
        self.fishing_frame.focus_set()

    def unbind_selected_row(self):
        hwnd = self.selected_hwnd()
        if not hwnd:
            messagebox.showwarning("尚未選擇", "請先選擇要解除綁定的遊戲視窗。")
            return
        bindings = read_bindings()
        item = bindings.get(int(hwnd))
        if not item:
            return
        name = str(item.get("shortcut_name", "") or "這個捷徑")
        if not messagebox.askyesno("解除綁定", f"確定解除『{name}』與這個遊戲視窗的綁定嗎？"):
            return
        bindings.pop(int(hwnd), None)
        remove_identity_profile(str(item.get("shortcut_path", "") or ""))
        save_bindings(bindings, remove_profile_path=str(item.get("shortcut_path", "") or ""))
        self.identity_cache.pop(int(hwnd), None)
        self.hint_label.config(text="已解除綁定，持久綁定記錄也已刪除。")

    def update_tree(self, status, raw_windows):
        keep_hwnd = self.selected_hwnd()
        for row in self.tree.get_children():
            self.tree.delete(row)
        now = time.time()
        rows = status.get("windows", []) if isinstance(status, dict) else []
        seen = set()
        bindings = read_bindings()

        for r in rows:
            try:
                hwnd = int(r.get("hwnd", 0))
            except Exception:
                hwnd = 0
            seen.add(hwnd)
            bind = bindings.get(hwnd, {})
            if bind:
                # 只要成功讀到靜態資料就更新本機快取；之後即使某一幀讀檔/鎖競爭失敗也不閃回未知。
                self.identity_cache[hwnd] = {
                    "shortcut_path": str(bind.get("shortcut_path", "") or ""),
                    "shortcut_name": str(bind.get("shortcut_name", "") or ""),
                    "preferred_role": str(bind.get("preferred_role", "") or ""),
                    "preferred_line_no": int(bind.get("preferred_line_no", 0) or 0),
                    "fishing_profile_id": str(bind.get("fishing_profile_id", "") or ""),
                    "reconnect_enabled": bool(bind.get("reconnect_enabled", True)),
                    "fishing_enabled": bool(bind.get("fishing_enabled", bool(bind.get("fishing_profile_id", "")))),
                    "manor_enabled": bool(bind.get("manor_enabled", False)),
                    "manor_crop_key": str(bind.get("manor_crop_key", "") or ""),
                    "manor_quantity": int(bind.get("manor_quantity", 16) or 16),
                }
            # 精確 HWND 的靜態身分優先；若本次讀檔短暫失敗，沿用同一 HWND 的本機快取，
            # 不讓捷徑/角色欄半秒閃回「未綁定／未知」。
            static = bind or self.identity_cache.get(hwnd, {})
            # V6.9：捷徑/角色絕不再從 runtime_status 取值。runtime 只負責流程與時間。
            shortcut = str(static.get("shortcut_name", "") or "未綁定")
            role_raw = str(static.get("preferred_role", "") or "")
            role = f"{role_raw}（{role_raw[0]}）" if role_raw else "未知"
            role_display = f"{role}  ·  {shortcut}" if shortcut != "未綁定" else role
            line_text = line_setting_label(static.get("preferred_line_no", 0))
            fishing_id = str(static.get("fishing_profile_id", "") or "")
            fishing_profile = fishing_profiles.profile_by_id(fishing_id)
            reconnect_enabled = bool(static.get("reconnect_enabled", True))
            manor_enabled = bool(static.get("manor_enabled", False))
            fishing_enabled = bool(static.get("fishing_enabled", bool(fishing_id)))
            reconnect_text = "☑" if reconnect_enabled else "☐"
            manor_text = "☑" if manor_enabled else "☐"
            if fishing_enabled and fishing_profile:
                fishing_text = f"☑ {fishing_profile.get('name', '')}"
            elif fishing_profile:
                fishing_text = f"☐ {fishing_profile.get('name', '')}"
            else:
                fishing_text = "☐ 未選"
            flow_elapsed = float(r.get("flow_elapsed", 0) or 0)
            internal_state = str(r.get("state", ""))
            last_cap = float(r.get("last_capture", 0) or 0)
            cap_ok = bool(r.get("capture_ok")) and now - last_cap <= 4.0
            bound_ok = bool(r.get("bound", bool(bind)))
            dpi_virtualized = bool(r.get("dpi_virtualized", False))
            minimized = bool(r.get("minimized", False))
            minimized_monitoring = bool(r.get("minimized_monitoring", False))
            fishing_phase = str(r.get("fishing_phase", "") or "")
            fishing_link_index = int(r.get("fishing_link_index", 0) or 0)
            fishing_link_count = int(r.get("fishing_link_count", 0) or 0)
            last_event = str(r.get("last_event", "") or "").strip()

            # 每一個遊戲視窗顯示自己的狀態與自己的上次斷線時間；
            # 不再把不同角色的時間合併成一個全域值。
            last_disconnect_at = float(r.get("last_disconnect_at", 0) or 0)
            last_disconnect_text = (
                time.strftime("%H:%M", time.localtime(last_disconnect_at))
                if last_disconnect_at > 0.0
                else "--:--"
            )
            if not reconnect_enabled and not manor_enabled and not fishing_enabled:
                event_text = "三項功能均關閉；不操作此角色"
            elif minimized:
                event_text = "視窗已最小化，監測與釣魚暫停"
            elif not cap_ok:
                event_text = "背景擷取失敗"
            elif internal_state == "等待斷線確認":
                event_text = last_event or "斷線已辨識，正在驗證背景確認"
            elif internal_state == "監看" and fishing_phase not in ("", "停用"):
                point_text = f" {fishing_link_index + 1}/{fishing_link_count}" if fishing_link_count else ""
                event_text = last_event or f"釣魚：{fishing_phase}{point_text}"
            elif dpi_virtualized and internal_state == "監看":
                event_text = "DPI即時相容監測中"
            elif not bound_ok and internal_state == "監看":
                event_text = "基礎監測中（未綁定）"
            elif internal_state == "監看" and flow_elapsed <= 0.05:
                event_text = "已納管，遊戲中"
            else:
                event_text = last_event or f"重連中：{internal_state or '辨識中'}"
            flow_text = f"{event_text}|上次斷線時間:{last_disconnect_text}"
            self.tree.insert(
                "", "end", iid=f"h{hwnd}",
                values=(role_display, reconnect_text, manor_text, fishing_text, line_text, flow_text),
            )

        for r in raw_windows:
            hwnd = int(r.get("hwnd", 0))
            if hwnd in seen:
                continue
            bind = bindings.get(hwnd, {})
            if bind:
                self.identity_cache[hwnd] = {
                    "shortcut_path": str(bind.get("shortcut_path", "") or ""),
                    "shortcut_name": str(bind.get("shortcut_name", "") or ""),
                    "preferred_role": str(bind.get("preferred_role", "") or ""),
                    "preferred_line_no": int(bind.get("preferred_line_no", 0) or 0),
                    "fishing_profile_id": str(bind.get("fishing_profile_id", "") or ""),
                    "reconnect_enabled": bool(bind.get("reconnect_enabled", True)),
                    "fishing_enabled": bool(bind.get("fishing_enabled", bool(bind.get("fishing_profile_id", "")))),
                    "manor_enabled": bool(bind.get("manor_enabled", False)),
                    "manor_crop_key": str(bind.get("manor_crop_key", "") or ""),
                    "manor_quantity": int(bind.get("manor_quantity", 16) or 16),
                }
            static = bind or self.identity_cache.get(hwnd, {})
            shortcut = str(static.get("shortcut_name", "") or "未綁定")
            role_raw = str(static.get("preferred_role", "") or "")
            role = f"{role_raw}（{role_raw[0]}）" if role_raw else "未知"
            role_display = f"{role}  ·  {shortcut}" if shortcut != "未綁定" else role
            line_text = line_setting_label(static.get("preferred_line_no", 0))
            fishing_id = str(static.get("fishing_profile_id", "") or "")
            fishing_profile = fishing_profiles.profile_by_id(fishing_id)
            reconnect_enabled = bool(static.get("reconnect_enabled", True))
            manor_enabled = bool(static.get("manor_enabled", False))
            fishing_enabled = bool(static.get("fishing_enabled", bool(fishing_id)))
            reconnect_text = "☑" if reconnect_enabled else "☐"
            manor_text = "☑" if manor_enabled else "☐"
            if fishing_enabled and fishing_profile:
                fishing_text = f"☑ {fishing_profile.get('name', '')}"
            elif fishing_profile:
                fishing_text = f"☐ {fishing_profile.get('name', '')}"
            else:
                fishing_text = "☐ 未選"
            raw_state = (
                "三項功能均關閉；不操作此角色"
                if not reconnect_enabled and not manor_enabled and not fishing_enabled
                else ("等待監測程序納管" if current_pid() else "等待開始監測")
            )
            self.tree.insert(
                "", "end", iid=f"h{hwnd}",
                values=(role_display, reconnect_text, manor_text, fishing_text, line_text, raw_state),
            )

        if keep_hwnd and self.tree.exists(f"h{keep_hwnd}"):
            self.tree.selection_set(f"h{keep_hwnd}")
            self.tree.focus(f"h{keep_hwnd}")
        self.after_idle(self.sync_fishing_panel_to_selection)
        self.after_idle(self.sync_manor_panel_to_selection)
        self.after_idle(self.sync_line_panel_to_selection)
        self.after_idle(self.sync_feature_panel_to_selection)

    def refresh(self):
        pid = current_pid()
        status = read_runtime_status()
        now = time.time()
        updated = float(status.get("updated_at", 0) or 0) if isinstance(status, dict) else 0.0
        stale = bool(pid and (not updated or now - updated > 4.0))

        try:
            raw_windows = enum_game_windows()
        except Exception:
            raw_windows = []
        live_rows = status.get("windows", []) if isinstance(status, dict) else []
        active_count = sum(1 for r in live_rows if str(r.get("state", "")) != "監看" and float(r.get("flow_elapsed", 0) or 0) > 0.0)
        bindings_now = read_bindings()
        unbound_count = sum(1 for r in raw_windows if int(r.get("hwnd", 0) or 0) not in bindings_now)
        self.dpi_pending_count = sum(1 for r in raw_windows if dpi_policy.window_needs_restart_for_dpi(int(r.get("hwnd", 0) or 0)))

        if pid and stale:
            self.status_label.config(text=f"監測：狀態更新逾時（{pid}）")
            self.toggle_button.config(text="停止監測")
        elif pid:
            paused = bool(status.get("paused", False))
            managed_count = len(live_rows)
            discovered_count = len(raw_windows)
            self.status_label.config(
                text=(
                    "監測：已暫停"
                    if paused
                    else f"監測：執行中（已納管 {managed_count}/{discovered_count}）"
                )
            )
            self.toggle_button.config(text="停止監測")
        else:
            self.status_label.config(text="監測：未啟動")
            self.toggle_button.config(text="開始監測")
            try:
                if PID_PATH.exists():
                    PID_PATH.unlink()
            except Exception:
                pass

        if now - self._dependency_cache_at >= 10.0 or self._dependency_cache_at <= 0.0:
            self._dependency_cache = dependency_status()
            self._dependency_cache_at = now
        missing, ocr = self._dependency_cache
        if missing:
            names = sorted({m.get("package", m.get("module", "未知")) for m in missing})
            if FROZEN:
                self.hint_label.config(text="封裝不完整或檔案遭隔離：" + "、".join(names) + "；請重新取得完整成品")
            else:
                self.hint_label.config(text="開發環境缺少：" + "、".join(names) + "；請依鎖定檔安裝")
        elif pid and any(not bool(r.get("capture_ok")) for r in live_rows):
            self.hint_label.config(text="有視窗背景擷取失敗；表格會直接標示。")
        elif self.dpi_pending_count:
            self.hint_label.config(text=f"{self.dpi_pending_count} 個 Flash 使用 DPI 即時相容；目前程序會持續辨識與驗證輸入，不再等待重開。")
        elif pid and unbound_count:
            self.hint_label.config(text=f"{unbound_count} 個視窗已自動納入基礎監測但尚未綁定；選角與戰鬥斷線重開仍需指定捷徑及角色。")
        elif pid:
            self.hint_label.config(text="背景監測正常；捷徑與角色已永久保存。角色未設定時顯示「未知」，不會執行角色辨識。")
        else:
            self.hint_label.config(text="控制台已就緒；需要監測時按『開始監測』。")

        self.update_tree(status, raw_windows)

        if self.logs_expanded:
            txt = latest_log_text(max_lines=45)
            self.log_box.configure(state="normal")
            old = self.log_box.get("1.0", "end-1c")
            if old != txt:
                self.log_box.delete("1.0", "end")
                self.log_box.insert("1.0", txt)
                self.log_box.see("end")
            self.log_box.configure(state="disabled")
        refresh_ms = int(max(500, float(read_config().get("控制台刷新間隔秒", 1.0)) * 1000))
        self.after(refresh_ms, self.refresh)

    def on_close(self):
        pid = current_pid()
        if pid:
            ans = messagebox.askyesnocancel(
                "關閉控制台",
                "背景監測目前仍在執行。\n\n「是」：停止監測並關閉\n「否」：只關閉控制台，背景監測繼續\n「取消」：返回控制台",
            )
            if ans is None:
                return
            if ans:
                try:
                    STOP_SIGNAL_PATH.write_text("stop", encoding="ascii")
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
