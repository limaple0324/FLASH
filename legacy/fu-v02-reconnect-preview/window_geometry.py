# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time

try:
    import win32gui
except Exception:
    win32gui = None

USER_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "SmartReconnect"
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = USER_DATA_DIR / "window_geometry.json"


def _norm(path: str) -> str:
    try:
        return str(Path(os.path.expandvars(str(path or ""))).expanduser().resolve()).lower()
    except Exception:
        return os.path.abspath(os.path.expandvars(str(path or ""))).lower()


def _read() -> dict:
    try:
        obj = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write(obj: dict) -> None:
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, STATE_PATH)


def save_profile(shortcut_path: str, user_rect, safe_client) -> None:
    """保存 DPI 重開所需幾何。

    V11.5 起：
    - user_rect = DWM 可見外框的「實體螢幕像素」矩形；這才是使用者實際看到的尺寸/位置。
    - safe_client 舊欄位名稱為相容性保留，語意改成 input_base_client：
      只用來把任意目前畫面點正規化回 Flash 已驗證輸入基準，絕不能拿去 resize Windows 視窗。
    """
    key = _norm(shortcut_path)
    if not key:
        return
    data = _read()
    entries = data.get('entries') if isinstance(data.get('entries'), dict) else {}
    rect = [int(v) for v in user_rect] if user_rect and len(user_rect) == 4 else None
    client = [max(1, int(safe_client[0])), max(1, int(safe_client[1]))] if safe_client else None
    entries[key] = {
        'shortcut_path': str(shortcut_path or ''),
        'user_visible_rect': rect,
        'input_base_client': client,
        # 舊鍵保留一版，避免舊檔/舊程式讀取直接失敗；V11.5 本身不再以 safe_client 改窗。
        'user_rect': rect,
        'safe_client': client,
        'updated_at': time.time(),
    }
    _write({'version': 2, 'entries': entries})


def load_profile(shortcut_path: str) -> dict:
    key = _norm(shortcut_path)
    data = _read()
    entries = data.get('entries') if isinstance(data.get('entries'), dict) else {}
    item = entries.get(key)
    return dict(item) if isinstance(item, dict) else {}


def client_size(hwnd: int):
    if win32gui is None:
        return None
    try:
        l, t, r, b = win32gui.GetClientRect(int(hwnd))
        return max(1, int(r)-int(l)), max(1, int(b)-int(t))
    except Exception:
        return None


def outer_rect(hwnd: int):
    if win32gui is None:
        return None
    try:
        l, t, r, b = win32gui.GetWindowRect(int(hwnd))
        return int(l), int(t), int(r), int(b)
    except Exception:
        return None


DWMWA_EXTENDED_FRAME_BOUNDS = 9


def visible_rect(hwnd: int):
    """回傳 DWM 實體可見外框；不受舊程序 DPI 虛擬化的邏輯尺寸污染。"""
    if win32gui is None:
        return None
    try:
        rect = wintypes.RECT()
        dwm = ctypes.windll.dwmapi
        fn = dwm.DwmGetWindowAttribute
        fn.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
        fn.restype = ctypes.c_long
        hr = int(fn(wintypes.HWND(int(hwnd)), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)))
        if hr == 0 and int(rect.right) > int(rect.left) and int(rect.bottom) > int(rect.top):
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        pass
    return outer_rect(hwnd)


def legacy_outer_to_visible_for_window(hwnd: int, outer) -> tuple | None:
    """把 V11.4 舊版保存的 GetWindowRect 外框轉成 V11.5 的 DWM 可見外框。

    使用新視窗當下的 invisible resize-border margin 做轉換；僅用於一次性舊資料遷移。
    """
    if win32gui is None or not outer or len(outer) != 4:
        return None
    try:
        ol, ot, or_, ob = [int(v) for v in outer]
        wl, wt, wr, wb = win32gui.GetWindowRect(int(hwnd))
        vis = visible_rect(int(hwnd))
        if not vis:
            return ol, ot, or_, ob
        vl, vt, vr, vb = [int(v) for v in vis]
        ml = vl - int(wl)
        mt = vt - int(wt)
        mr = int(wr) - vr
        mb = int(wb) - vb
        out = (ol + ml, ot + mt, or_ - mr, ob - mb)
        if out[2] > out[0] and out[3] > out[1]:
            return out
    except Exception:
        pass
    return None


def restore_visible_rect_noactivate(hwnd: int, rect) -> bool:
    """讓新 DPI 程序的「DWM 可見外框」精確回到重開前實體矩形。

    GetWindowRect 可能包含不可見 resize border，且舊/新 DPI awareness 的數值語意不同；
    因此每次以目前 DWM visible bounds 與 outer rect 推導 border margin，再迭代校正。
    """
    if win32gui is None or not rect or len(rect) != 4:
        return False
    try:
        dl, dt, dr, db = [int(v) for v in rect]
        if dr <= dl or db <= dt:
            return False
        flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
        for _ in range(4):
            wl, wt, wr, wb = win32gui.GetWindowRect(int(hwnd))
            vis = visible_rect(int(hwnd))
            if not vis:
                vis = (int(wl), int(wt), int(wr), int(wb))
            vl, vt, vr, vb = [int(v) for v in vis]
            # outer 相對 visible 的不可見 margin
            ml = vl - int(wl)
            mt = vt - int(wt)
            mr = int(wr) - vr
            mb = int(wb) - vb
            target_wl = dl - ml
            target_wt = dt - mt
            target_wr = dr + mr
            target_wb = db + mb
            win32gui.SetWindowPos(
                int(hwnd), 0, int(target_wl), int(target_wt),
                max(1, int(target_wr-target_wl)), max(1, int(target_wb-target_wt)), flags
            )
            time.sleep(0.10)
            got = visible_rect(int(hwnd))
            if got and all(abs(int(a)-int(b)) <= 1 for a,b in zip(got, (dl,dt,dr,db))):
                return True
        got = visible_rect(int(hwnd))
        return bool(got and all(abs(int(a)-int(b)) <= 2 for a,b in zip(got, (dl,dt,dr,db))))
    except Exception:
        return False


def resize_client_noactivate(hwnd: int, client_w: int, client_h: int, left=None, top=None) -> bool:
    if win32gui is None:
        return False
    try:
        flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
        for _ in range(3):
            l, t, r, b = win32gui.GetWindowRect(int(hwnd))
            cl, ct, cr, cb = win32gui.GetClientRect(int(hwnd))
            ow, oh = max(1, int(r-l)), max(1, int(b-t))
            cw, ch = max(1, int(cr-cl)), max(1, int(cb-ct))
            tw = max(1, ow + (int(client_w) - cw))
            th = max(1, oh + (int(client_h) - ch))
            x = int(l if left is None else left)
            y = int(t if top is None else top)
            win32gui.SetWindowPos(int(hwnd), 0, x, y, int(tw), int(th), flags)
            time.sleep(0.10)
            got = client_size(hwnd)
            if got and abs(got[0]-int(client_w)) <= 1 and abs(got[1]-int(client_h)) <= 1:
                return True
        got = client_size(hwnd)
        return bool(got and abs(got[0]-int(client_w)) <= 2 and abs(got[1]-int(client_h)) <= 2)
    except Exception:
        return False


def restore_outer_noactivate(hwnd: int, rect) -> bool:
    if win32gui is None or not rect or len(rect) != 4:
        return False
    try:
        l, t, r, b = [int(v) for v in rect]
        flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
        win32gui.SetWindowPos(int(hwnd), 0, l, t, max(1, r-l), max(1, b-t), flags)
        return True
    except Exception:
        return False
