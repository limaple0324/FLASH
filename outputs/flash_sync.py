# -*- coding: utf-8 -*-
"""
Flash multi-window click synchronizer for Windows.

Stable version:
- No low-level hooks.
- No extra Python packages.
- Mirrors left-clicks from the master window.
- Uses one custom keyboard key or mouse side button as a start/stop hotkey.
- Supports per-follower click coordinate offsets.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, simpledialog, ttk
from ctypes import wintypes


APP_DISPLAY_NAME = "輔V0.1"
APP_VERSION = "V.01"
APP_VERSION_CODE = "v0.1"
APP_STABLE_BASELINE_NAME = "輔V0.1 穩定基準"

# Stable module API boundaries for v0.1.
# Future work should only touch a module listed in the user's change list.
STABLE_MODULE_APIS_V01 = (
    {
        "id": "GroupAPI",
        "name": "組別管理",
        "contract": "負責組別新增、刪除、改名、排序、取得目前組別。",
    },
    {
        "id": "LaunchAPI",
        "name": "啟動與整理本組",
        "contract": "負責啟動本組、整理本組、恢復位置、補開缺少視窗。",
    },
    {
        "id": "MainWindowAPI",
        "name": "主窗口",
        "contract": "負責設定主窗、上鎖、解鎖、取得主窗。",
    },
    {
        "id": "SyncAPI",
        "name": "同步執行",
        "contract": "負責開始同步、停止同步、同步狀態、快捷鍵觸發。",
    },
    {
        "id": "HotkeyAPI",
        "name": "快捷鍵設定",
        "contract": "負責快捷鍵設定、清除、重複衝突處理。",
    },
    {
        "id": "CharacterAPI",
        "name": "角色身份",
        "contract": "負責讀取角色ID、校正角色ID、檔名備援、跨組身份。",
    },
    {
        "id": "MonitorAPI",
        "name": "偵測監控",
        "contract": "負責斷線偵測、掃描狀態。",
    },
    {
        "id": "TrayAPI",
        "name": "系統匣",
        "contract": "負責系統匣、喚醒、關閉、X收回。",
    },
    {
        "id": "StatusWindowAPI",
        "name": "小狀態窗",
        "contract": "負責小狀態窗、多組顯示、拖曳、快速整理。",
    },
    {
        "id": "SettingsAPI",
        "name": "設定保存",
        "contract": "負責AppData設定、outputs備份、machine id。",
    },
)


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32


DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)


def enable_stable_dpi_awareness() -> None:
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = wintypes.BOOL
        if set_context(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE):
            return
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(1) == 0:  # PROCESS_SYSTEM_DPI_AWARE
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


enable_stable_dpi_awareness()

try:
    SetThreadDpiAwarenessContext = user32.SetThreadDpiAwarenessContext
    SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
except AttributeError:
    SetThreadDpiAwarenessContext = None

def enter_flash_window_dpi_context():
    if SetThreadDpiAwarenessContext is None:
        return None
    try:
        return SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)
    except Exception:
        return None


def leave_flash_window_dpi_context(previous) -> None:
    if SetThreadDpiAwarenessContext is None or previous is None:
        return
    try:
        SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
    except Exception:
        pass


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.after_id: str | None = None
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None) -> None:
        self.cancel()
        self.after_id = self.widget.after(self.delay_ms, self.show)

    def cancel(self) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def show(self) -> None:
        self.after_id = None
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(
            self.window,
            text=self.text,
            justify="left",
            relief="solid",
            borderwidth=1,
            padding=(8, 5),
            background="#ffffe0",
        )
        label.pack()

    def hide(self, _event=None) -> None:
        self.cancel()
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1
WM_CONTEXTMENU = 0x007B
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_NULL = 0x0000
HWND_BROADCAST = 0xFFFF
ERROR_ALREADY_EXISTS = 183
MAPVK_VK_TO_VSC = 0
WH_MOUSE_LL = 14
HC_ACTION = 0

MK_LBUTTON = 0x0001

VK_LBUTTON = 0x01
VK_RETURN = 0x0D
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010

GA_ROOT = 2
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
SRCCOPY = 0x00CC0020
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
SW_SHOW = 5
SW_RESTORE = 9
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NOTIFYICON_VERSION_4 = 4
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DIB_RGB_COLORS = 0
SINGLE_INSTANCE_MUTEX_NAME = "Local\\FlashSyncSynchronizerSingleInstanceV2"
SINGLE_INSTANCE_RESTORE_MESSAGE_NAME = "FlashSyncSynchronizerRestoreMessageV2"
SINGLE_INSTANCE_MUTEX_HANDLE = None
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SUBPROCESS_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HDC = wintypes.HANDLE
HBITMAP = wintypes.HANDLE
HGDIOBJ = wintypes.HANDLE
HICON = wintypes.HANDLE
HMENU = wintypes.HANDLE
UINT_PTR = wintypes.WPARAM
LRESULT = ctypes.c_ssize_t
HCURSOR = wintypes.HANDLE
HBRUSH = wintypes.HANDLE
DEFAULT_FLASH_CLIENT_WIDTH = 1000
DEFAULT_FLASH_CLIENT_HEIGHT = 700
GAME_TIME_REGION_WIDTH = 100
GAME_TIME_REGION_HEIGHT = 18
GAME_TIME_TRIGGER_OFFSET_X = 18
GAME_TIME_TRIGGER_OFFSET_Y = 42
MAX_TIME_TEMPLATES_PER_CHAR = 5
TIME_TEMPLATE_MAX_SCORE = 32
DAY_MINUTES = 24 * 60
GAME_TIME_CURSOR_SEARCH_LEFT = 25
GAME_TIME_CURSOR_SEARCH_RIGHT = 190
GAME_TIME_CURSOR_SEARCH_TOP = 8
GAME_TIME_CURSOR_SEARCH_BOTTOM = 72
GAME_TIME_CHANGE_MIN_DIFF = 18
GAME_TIME_CHANGE_MAX_DIFF = 260
GAME_TIME_CHANGE_STABLE_READS = 6
GAME_TIME_CHANGE_STABLE_SECONDS = 0.35
ROLE_ID_REGION = (87, 13, 177, 37)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", HICON),
    ]


WindowProc = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WindowProc),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    HMENU,
    wintypes.HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = HMENU
user32.AppendMenuW.argtypes = [HMENU, wintypes.UINT, UINT_PTR, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.UINT
user32.DestroyMenu.argtypes = [HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.ExtractIconExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.HICON),
    ctypes.POINTER(wintypes.HICON),
    wintypes.UINT,
]
shell32.ExtractIconExW.restype = wintypes.UINT
user32.ChildWindowFromPointEx.argtypes = [
    wintypes.HWND,
    POINT,
    wintypes.UINT,
]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
user32.mouse_event.restype = None
LowLevelMouseProc = ctypes.WINFUNCTYPE(
    wintypes.LPARAM,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    LowLevelMouseProc,
    wintypes.HINSTANCE,
    wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.CallNextHookEx.restype = wintypes.LPARAM
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
SINGLE_INSTANCE_RESTORE_MESSAGE = user32.RegisterWindowMessageW(
    SINGLE_INSTANCE_RESTORE_MESSAGE_NAME
)
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, HDC]
user32.ReleaseDC.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = HBITMAP
gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.BitBlt.argtypes = [
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    HDC,
    HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


def make_lparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def make_wparam(low: int, high: int) -> int:
    return (low & 0xFFFF) | ((high & 0xFFFF) << 16)


def make_key_lparam(vk: int, key_up: bool = False) -> int:
    scan = int(user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC)) & 0xFF
    value = 1 | (scan << 16)
    if key_up:
        value |= 1 << 30
        value |= 1 << 31
    return value


def get_cursor_pos() -> POINT:
    pt = POINT()
    previous = enter_flash_window_dpi_context()
    try:
        user32.GetCursorPos(ctypes.byref(pt))
    finally:
        leave_flash_window_dpi_context(previous)
    return pt


def get_cursor_pos_raw() -> POINT:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt


def get_window_under_cursor() -> int:
    pt = get_cursor_pos()
    return get_window_at_point(pt.x, pt.y)


def get_window_at_point(x: int, y: int) -> int:
    pt = POINT(int(x), int(y))
    previous = enter_flash_window_dpi_context()
    try:
        hwnd = int(user32.WindowFromPoint(pt))
        root = int(user32.GetAncestor(hwnd, GA_ROOT))
        return root or hwnd
    finally:
        leave_flash_window_dpi_context(previous)


def rect_contains_point(rect: RECT, x: int, y: int) -> bool:
    return int(rect.left) <= int(x) < int(rect.right) and int(rect.top) <= int(y) < int(rect.bottom)


def get_foreground_root_window() -> int:
    hwnd = int(user32.GetForegroundWindow())
    if not hwnd:
        return 0
    root = int(user32.GetAncestor(hwnd, GA_ROOT))
    return root or hwnd


def get_window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def get_window_process_filename(hwnd: int) -> str:
    path = get_window_process_path(hwnd)
    return os.path.basename(path) if path else ""


def get_window_process_id(hwnd: int) -> int:
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value or 0)


def get_window_process_path(hwnd: int) -> str:
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def get_window_display_name(hwnd: int) -> str:
    filename = get_window_process_filename(hwnd)
    title = get_window_title(hwnd)
    if filename and title and filename.lower() not in title.lower():
        return f"{filename} - {title}"
    return filename or title or "(無標題)"


def get_window_rect(hwnd: int) -> RECT:
    rect = RECT()
    previous = enter_flash_window_dpi_context()
    try:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    finally:
        leave_flash_window_dpi_context(previous)
    return rect


def get_window_normal_rect(hwnd: int) -> RECT | None:
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(WINDOWPLACEMENT)
    previous = enter_flash_window_dpi_context()
    try:
        if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return None
        return placement.rcNormalPosition
    finally:
        leave_flash_window_dpi_context(previous)


def get_window_launch_match_rect(hwnd: int) -> RECT:
    if user32.IsIconic(hwnd):
        normal_rect = get_window_normal_rect(hwnd)
        if normal_rect is not None:
            return normal_rect
    return get_window_rect(hwnd)


def run_powershell_json(script: str, timeout: int = 10):
    command = (
        "$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        + script
    )
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=SUBPROCESS_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    output = (proc.stdout or "").strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except Exception:
        return None


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def path_key(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(path or "")


def resolve_launch_specs(paths: list[str]) -> dict[str, dict[str, str]]:
    unique_paths: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = path_key(path)
        if key and key not in seen:
            seen.add(key)
            unique_paths.append(path)
    if not unique_paths:
        return {}

    paths_json = json.dumps(unique_paths, ensure_ascii=False)
    script = f"""
$paths = @'
{paths_json}
'@ | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$result = @()
foreach ($path in $paths) {{
  $exists = Test-Path -LiteralPath $path
  $target = ""
  $args = ""
  $working = ""
  if ($exists -and [System.IO.Path]::GetExtension($path).ToLowerInvariant() -eq ".lnk") {{
    $shortcut = $shell.CreateShortcut($path)
    $target = [string]$shortcut.TargetPath
    $args = [string]$shortcut.Arguments
    $working = [string]$shortcut.WorkingDirectory
  }} elseif ($exists) {{
    $target = [string](Resolve-Path -LiteralPath $path)
  }}
  $result += [PSCustomObject]@{{
    Path = [string]$path
    Exists = [bool]$exists
    Target = [string]$target
    Args = [string]$args
    WorkingDir = [string]$working
  }}
}}
$result | ConvertTo-Json -Compress
"""
    specs: dict[str, dict[str, str]] = {}
    for item in as_list(run_powershell_json(script, timeout=15)):
        if not isinstance(item, dict):
            continue
        item_path = str(item.get("Path") or "")
        specs[path_key(item_path)] = {
            "path": item_path,
            "target": str(item.get("Target") or ""),
            "args": str(item.get("Args") or ""),
            "working_dir": str(item.get("WorkingDir") or ""),
            "exists": "1" if item.get("Exists") else "",
        }
    return specs


def flash_process_infos() -> dict[int, dict[str, str]]:
    script = """
$items = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -like "*flashplayer*" -or $_.CommandLine -like "*GameLoader.swf*"
} | ForEach-Object {
  [PSCustomObject]@{
    ProcessId = [int]$_.ProcessId
    ExecutablePath = [string]$_.ExecutablePath
    CommandLine = [string]$_.CommandLine
  }
}
$items | ConvertTo-Json -Compress
"""
    infos: dict[int, dict[str, str]] = {}
    for item in as_list(run_powershell_json(script, timeout=10)):
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        infos[pid] = {
            "path": str(item.get("ExecutablePath") or ""),
            "command_line": str(item.get("CommandLine") or ""),
        }
    return infos


def launch_identity_from_text(text: str) -> str:
    lowered = (text or "").lower()
    user_match = re.search(r"(?:[?&])user=([0-9a-f]+)", lowered)
    pass_match = re.search(r"(?:[?&])pass=([0-9a-f]+)", lowered)
    if user_match and pass_match:
        return f"user={user_match.group(1)}&pass={pass_match.group(1)}"
    if user_match:
        return f"user={user_match.group(1)}"
    return ""


def get_client_rect(hwnd: int) -> RECT:
    rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect


def get_client_size(hwnd: int) -> tuple[int, int]:
    rect = get_client_rect(hwnd)
    return max(0, int(rect.right - rect.left)), max(0, int(rect.bottom - rect.top))


def set_window_client_size(hwnd: int, width: int, height: int) -> bool:
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    previous = enter_flash_window_dpi_context()
    try:
        rect = get_window_rect(hwnd)
        outer_width = int(rect.right - rect.left)
        outer_height = int(rect.bottom - rect.top)
        client_width, client_height = get_client_size(hwnd)
        frame_width = max(0, outer_width - client_width)
        frame_height = max(0, outer_height - client_height)
        target_outer_width = max(100, int(width) + frame_width)
        target_outer_height = max(100, int(height) + frame_height)
        return bool(
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(0),
                int(rect.left),
                int(rect.top),
                target_outer_width,
                target_outer_height,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
        )
    finally:
        leave_flash_window_dpi_context(previous)


def set_window_recorded_rect(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.05)
    previous = enter_flash_window_dpi_context()
    try:
        return bool(
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(0),
                int(x),
                int(y),
                max(100, int(width)),
                max(100, int(height)),
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
        )
    finally:
        leave_flash_window_dpi_context(previous)


def screen_to_client(hwnd: int, x: int, y: int) -> POINT:
    pt = POINT(x, y)
    previous = enter_flash_window_dpi_context()
    try:
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
    finally:
        leave_flash_window_dpi_context(previous)
    return pt


def screen_to_client_raw(hwnd: int, x: int, y: int) -> POINT:
    pt = POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt


def client_to_screen(hwnd: int, x: int, y: int) -> POINT:
    pt = POINT(x, y)
    previous = enter_flash_window_dpi_context()
    try:
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
    finally:
        leave_flash_window_dpi_context(previous)
    return pt


def point_in_client(hwnd: int, x: int, y: int) -> tuple[bool, int, int]:
    pt = screen_to_client(hwnd, x, y)
    rect = get_client_rect(hwnd)
    inside = 0 <= pt.x < rect.right and 0 <= pt.y < rect.bottom
    return inside, int(pt.x), int(pt.y)


def cursor_point_in_client(hwnd: int) -> tuple[bool, int, int]:
    rect = get_client_rect(hwnd)
    candidates: list[POINT] = []
    for pt in (get_cursor_pos(), get_cursor_pos_raw()):
        if not any(existing.x == pt.x and existing.y == pt.y for existing in candidates):
            candidates.append(pt)
    fallback = (False, 0, 0)
    for cursor in candidates:
        for converter in (screen_to_client, screen_to_client_raw):
            try:
                local = converter(hwnd, cursor.x, cursor.y)
            except Exception:
                continue
            inside = 0 <= local.x < rect.right and 0 <= local.y < rect.bottom
            fallback = (False, int(local.x), int(local.y))
            if inside:
                return True, int(local.x), int(local.y)
    return fallback


def window_summary(hwnd: int) -> str:
    if not hwnd or not user32.IsWindow(hwnd):
        return "未選取"
    title = get_window_display_name(hwnd)
    rect = get_window_rect(hwnd)
    return f"0x{hwnd:08X}  {title}  [{rect.left},{rect.top},{rect.right},{rect.bottom}]"


def get_window_role_id(hwnd: int) -> str:
    title = get_window_title(hwnd).strip()
    filename = get_window_process_filename(hwnd).strip()
    generic_titles = {"adobe flash player 11", "adobe flash player"}
    if title and title.lower() not in generic_titles and "flash player" not in title.lower():
        return title
    if filename and "flashplayer" not in filename.lower():
        return os.path.splitext(filename)[0]
    return "未讀取"


def clean_role_id_text(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    text = re.sub(r"^[0-9Il|]+", "", text)
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "", text)
    return text[:24] if text else ""


def role_id_templates_path() -> str:
    return app_writable_path("role_id_templates.json")


def role_id_signature_from_image(path: str) -> tuple[int, int, str, int]:
    width, height, mask = read_bmp_mask(path)
    bits = ["1" if mask[y][x] else "0" for y in range(height) for x in range(width)]
    count = sum(1 for bit in bits if bit == "1")
    return width, height, "".join(bits), count


def load_role_id_templates() -> list[dict[str, object]]:
    path = role_id_templates_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as template_in:
            data = json.load(template_in)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_role_id_template(role_id: str, image_path: str) -> tuple[bool, str]:
    role_id = clean_role_id_text(role_id)
    if not role_id:
        return False, "角色ID不可空白。"
    width, height, signature, count = role_id_signature_from_image(image_path)
    if count < 6:
        return False, "截圖區域內沒有足夠文字特徵，請確認角色ID位置有被截到。"

    templates = load_role_id_templates()
    templates = [item for item in templates if item.get("role_id") != role_id]
    templates.append(
        {
            "role_id": role_id,
            "width": width,
            "height": height,
            "signature": signature,
            "count": count,
            "region": list(ROLE_ID_REGION),
        }
    )
    with open(role_id_templates_path(), "w", encoding="utf-8") as template_out:
        json.dump(templates, template_out, ensure_ascii=False, indent=2)
    return True, f"已保存角色ID範本：{role_id}"


def match_role_id_template(image_path: str) -> tuple[str, str]:
    width, height, signature, count = role_id_signature_from_image(image_path)
    if count < 6:
        return "", "截圖區域內沒有足夠文字特徵。"
    best_role = ""
    best_score = 1.0
    best_distance = 0
    for item in load_role_id_templates():
        template_sig = str(item.get("signature", ""))
        if not template_sig:
            continue
        if int(item.get("width", 0)) != width or int(item.get("height", 0)) != height:
            continue
        distance = signature_distance(signature, template_sig)
        score = distance / max(1, len(signature))
        if score < best_score:
            best_score = score
            best_distance = distance
            best_role = str(item.get("role_id", ""))
    if best_role and best_score <= 0.08:
        return best_role, f"相似度差異 {best_score:.3f}"
    if best_role:
        return "", f"最接近 {best_role}，但差異過大 {best_score:.3f}（距離 {best_distance}）"
    return "", "沒有可用的角色ID範本，請先校正角色ID。"


def find_tesseract_exe() -> str | None:
    env_path = os.environ.get("TESSERACT_CMD")
    candidates = [
        env_path,
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def ocr_role_id_from_bmp(path: str) -> tuple[str, str]:
    tesseract = find_tesseract_exe()
    if not tesseract:
        return "", "找不到 Tesseract，請先安裝 Tesseract-OCR。"
    language_sets = ("chi_tra+chi_sim+eng", "chi_tra+eng", "chi_sim+eng", "eng")
    last_error = ""
    for langs in language_sets:
        cmd = [
            tesseract,
            path,
            "stdout",
            "-l",
            langs,
            "--psm",
            "7",
            "--dpi",
            "300",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            last_error = str(exc)
            continue
        raw = (proc.stdout or "").strip()
        cleaned = clean_role_id_text(raw)
        if cleaned:
            return cleaned, ""
        last_error = (proc.stderr or raw or "OCR 沒有讀到文字").strip()
    return "", last_error or "OCR 沒有讀到文字"


def is_flash_window(hwnd: int) -> bool:
    filename = get_window_process_filename(hwnd).lower()
    title = get_window_title(hwnd).lower()
    return (
        "flashplayer" in filename
        or "flash player" in title
        or "adobe flash player" in title
    )


def enumerate_flash_windows() -> list[int]:
    windows: list[int] = []

    def callback(hwnd, _lparam):
        hwnd = int(hwnd)
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        rect = get_window_rect(hwnd)
        if (
            not user32.IsIconic(hwnd)
            and (rect.right <= rect.left or rect.bottom <= rect.top)
        ):
            return True
        if is_flash_window(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def find_flash_window_at_point(x: int, y: int) -> int:
    hwnd = get_window_at_point(x, y)
    if hwnd and user32.IsWindow(hwnd) and is_flash_window(hwnd):
        return hwnd

    foreground = get_foreground_root_window()
    if foreground and user32.IsWindow(foreground) and is_flash_window(foreground):
        try:
            if rect_contains_point(get_window_rect(foreground), x, y):
                return foreground
        except Exception:
            pass

    for candidate in enumerate_flash_windows():
        if not candidate or not user32.IsWindow(candidate) or user32.IsIconic(candidate):
            continue
        try:
            if rect_contains_point(get_window_rect(candidate), x, y):
                return candidate
        except Exception:
            continue
    return 0


def child_at_client_point(hwnd: int, x: int, y: int) -> tuple[int, int, int]:
    target = hwnd
    previous = enter_flash_window_dpi_context()
    try:
        screen_pt = POINT(x, y)
        user32.ClientToScreen(hwnd, ctypes.byref(screen_pt))
        for _ in range(6):
            local = POINT(screen_pt.x, screen_pt.y)
            user32.ScreenToClient(target, ctypes.byref(local))
            child = int(
                user32.ChildWindowFromPointEx(
                    target,
                    POINT(local.x, local.y),
                    CWP_SKIPINVISIBLE | CWP_SKIPDISABLED,
                )
            )
            if not child or child == target:
                return target, int(local.x), int(local.y)
            target = child
        local = POINT(screen_pt.x, screen_pt.y)
        user32.ScreenToClient(target, ctypes.byref(local))
        return target, int(local.x), int(local.y)
    finally:
        leave_flash_window_dpi_context(previous)


def normalize_rect_points(
    p1: tuple[int, int], p2: tuple[int, int]
) -> tuple[int, int, int, int]:
    x1, y1 = p1
    x2, y2 = p2
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    return left, top, right, bottom


def capture_client_region_to_bmp(
    hwnd: int, rect: tuple[int, int, int, int], path: str
) -> None:
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    source_dc = user32.GetDC(hwnd)
    if not source_dc:
        raise OSError("無法讀取窗口畫面。")
    memory_dc = gdi32.CreateCompatibleDC(source_dc)
    bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
    old_bitmap = None
    try:
        if not memory_dc or not bitmap:
            raise OSError("無法建立截圖緩衝區。")
        old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, source_dc, left, top, SRCCOPY):
            raise OSError("截取窗口畫面失敗。")

        bitmap_info = BITMAPINFO()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bitmap_info.bmiHeader.biWidth = width
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = 0
        image_size = width * height * 4
        bitmap_info.bmiHeader.biSizeImage = image_size
        pixels = ctypes.create_string_buffer(image_size)
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixels,
            ctypes.byref(bitmap_info),
            DIB_RGB_COLORS,
        )
        if copied != height:
            raise OSError("轉存截圖資料失敗。")

        pixel_offset = 14 + 40
        file_size = pixel_offset + image_size
        with open(path, "wb") as image_file:
            image_file.write(struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset))
            image_file.write(
                struct.pack(
                    "<IiiHHIIiiII",
                    40,
                    width,
                    -height,
                    1,
                    32,
                    0,
                    image_size,
                    0,
                    0,
                    0,
                    0,
                )
            )
            image_file.write(pixels.raw)
    finally:
        if old_bitmap:
            gdi32.SelectObject(memory_dc, old_bitmap)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, source_dc)


TIME_TEMPLATE_SIZE = (12, 18)
TIME_CHARS = set("0123456789:")
DISCONNECT_DETECT_COOLDOWN_SECONDS = 5.0


def time_template_path() -> str:
    return os.path.join(os.path.dirname(__file__), "game_time_templates.json")


def current_machine_id() -> str:
    computer = os.environ.get("COMPUTERNAME", "").strip().lower()
    user = os.environ.get("USERNAME", "").strip().lower()
    return f"{computer}|{user}"


def app_data_dir() -> str:
    base = (
        os.environ.get("APPDATA")
        or os.environ.get("LOCALAPPDATA")
        or os.path.expanduser("~")
    )
    path = os.path.join(base, "同步器")
    os.makedirs(path, exist_ok=True)
    return path


def legacy_writable_dirs() -> list[str]:
    dirs: list[str] = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    dirs.append(os.path.dirname(__file__))
    bundled = getattr(sys, "_MEIPASS", "")
    if bundled:
        dirs.append(bundled)
    unique: list[str] = []
    for path in dirs:
        if path and path not in unique:
            unique.append(path)
    return unique


def launch_config_entry_count(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        return 0
    count = 0
    for group in data.get("groups", []):
        if isinstance(group, dict):
            entries = group.get("launch_entries", [])
            if isinstance(entries, list):
                count += len(entries)
    return count


def should_copy_legacy_writable(name: str, target: str, legacy: str) -> bool:
    if not os.path.exists(target):
        return True
    if name == "sync_launch_config.json":
        return (
            launch_config_entry_count(target) == 0
            and launch_config_entry_count(legacy) > 0
        )
    return False


def app_resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
    return os.path.join(base, name)


def app_writable_path(name: str) -> str:
    target = os.path.join(app_data_dir(), name)
    for folder in legacy_writable_dirs():
        legacy = os.path.join(folder, name)
        if os.path.abspath(legacy) == os.path.abspath(target):
            continue
        if (
            os.path.exists(legacy)
            and os.path.getsize(legacy) > 0
            and should_copy_legacy_writable(name, target, legacy)
        ):
            try:
                shutil.copy2(legacy, target)
                break
            except Exception:
                pass
    return target


def read_bmp_mask(path: str) -> tuple[int, int, list[list[bool]]]:
    with open(path, "rb") as image_file:
        data = image_file.read()
    if data[:2] != b"BM":
        raise ValueError("只支援 BMP 截圖。")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    header_size = struct.unpack_from("<I", data, 14)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    raw_height = struct.unpack_from("<i", data, 22)[0]
    bit_count = struct.unpack_from("<H", data, 28)[0]
    if bit_count not in (24, 32):
        raise ValueError("BMP 格式不支援。")
    top_down = raw_height < 0
    height = abs(raw_height)
    bytes_per_pixel = bit_count // 8
    stride = ((width * bytes_per_pixel + 3) // 4) * 4
    mask = [[False for _ in range(width)] for _ in range(height)]
    for y in range(height):
        source_y = y if top_down else height - 1 - y
        row_offset = pixel_offset + source_y * stride
        for x in range(width):
            offset = row_offset + x * bytes_per_pixel
            b, g, r = data[offset], data[offset + 1], data[offset + 2]
            bright = max(r, g, b)
            dark = min(r, g, b)
            # The game clock text is light/white. This ignores most colored map pixels.
            mask[y][x] = bright >= 170 and (bright - dark) <= 95
    return width, height, mask


def read_bmp_pixels(path: str) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    with open(path, "rb") as image_file:
        data = image_file.read()
    if data[:2] != b"BM":
        raise ValueError("只支援 BMP 截圖。")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    raw_height = struct.unpack_from("<i", data, 22)[0]
    bit_count = struct.unpack_from("<H", data, 28)[0]
    if bit_count not in (24, 32):
        raise ValueError("BMP 格式不支援。")
    top_down = raw_height < 0
    height = abs(raw_height)
    bytes_per_pixel = bit_count // 8
    stride = ((width * bytes_per_pixel + 3) // 4) * 4
    pixels: list[list[tuple[int, int, int]]] = []
    for y in range(height):
        source_y = y if top_down else height - 1 - y
        row_offset = pixel_offset + source_y * stride
        row: list[tuple[int, int, int]] = []
        for x in range(width):
            offset = row_offset + x * bytes_per_pixel
            b, g, r = data[offset], data[offset + 1], data[offset + 2]
            row.append((r, g, b))
        pixels.append(row)
    return width, height, pixels


def is_disconnect_button_pixel(r: int, g: int, b: int) -> bool:
    return g >= 135 and b >= 105 and r <= 95 and g >= r + 55 and b >= r + 45


def is_disconnect_dark_panel_pixel(r: int, g: int, b: int) -> bool:
    return 10 <= r <= 80 and 45 <= g <= 135 and 60 <= b <= 160 and b >= r + 25


def is_disconnect_text_pixel(r: int, g: int, b: int) -> bool:
    bright = max(r, g, b)
    dark = min(r, g, b)
    white = bright >= 165 and bright - dark <= 85
    yellow = r >= 145 and g >= 125 and b <= 95
    return white or yellow


def count_pixels_in_box(
    pixels: list[list[tuple[int, int, int]]],
    box: tuple[int, int, int, int],
    predicate,
) -> int:
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    left, top, right, bottom = box
    left = max(0, min(width, left))
    right = max(0, min(width, right))
    top = max(0, min(height, top))
    bottom = max(0, min(height, bottom))
    count = 0
    for y in range(top, bottom):
        row = pixels[y]
        for x in range(left, right):
            if predicate(*row[x]):
                count += 1
    return count


def find_disconnect_confirm_button(
    width: int, height: int, pixels: list[list[tuple[int, int, int]]]
) -> tuple[int, int] | None:
    scan_left = width // 4
    scan_right = width * 3 // 4
    scan_top = max(0, height // 20)
    scan_bottom = height * 4 // 5
    visited: set[tuple[int, int]] = set()
    candidates: list[tuple[int, int, int, int, int]] = []

    for y in range(scan_top, scan_bottom):
        for x in range(scan_left, scan_right):
            if (x, y) in visited or not is_disconnect_button_pixel(*pixels[y][x]):
                continue
            stack = [(x, y)]
            visited.add((x, y))
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if (
                        nx < scan_left
                        or nx >= scan_right
                        or ny < scan_top
                        or ny >= scan_bottom
                        or (nx, ny) in visited
                    ):
                        continue
                    if is_disconnect_button_pixel(*pixels[ny][nx]):
                        visited.add((nx, ny))
                        stack.append((nx, ny))
            box_width = max_x - min_x + 1
            box_height = max_y - min_y + 1
            if 56 <= box_width <= 115 and 14 <= box_height <= 32 and area >= 650:
                candidates.append((min_x, min_y, max_x + 1, max_y + 1, area))

    for left, top, right, bottom, _area in sorted(candidates, key=lambda item: item[4], reverse=True):
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        panel_box = (center_x - 260, top - 95, center_x + 260, bottom + 35)
        text_box = (center_x - 240, top - 75, center_x + 240, top - 15)
        panel_count = count_pixels_in_box(pixels, panel_box, is_disconnect_dark_panel_pixel)
        text_count = count_pixels_in_box(pixels, text_box, is_disconnect_text_pixel)
        if text_count >= 1800:
            return center_x, center_y
    return None


def find_button_like_components(
    width: int,
    height: int,
    pixels: list[list[tuple[int, int, int]]],
    region: tuple[int, int, int, int],
) -> list[tuple[int, int, int, int, int]]:
    left, top, right, bottom = region
    left = max(0, min(width, left))
    right = max(0, min(width, right))
    top = max(0, min(height, top))
    bottom = max(0, min(height, bottom))
    visited: set[tuple[int, int]] = set()
    components: list[tuple[int, int, int, int, int]] = []

    for y in range(top, bottom):
        for x in range(left, right):
            if (x, y) in visited or not is_disconnect_button_pixel(*pixels[y][x]):
                continue
            stack = [(x, y)]
            visited.add((x, y))
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if (
                        nx < left
                        or nx >= right
                        or ny < top
                        or ny >= bottom
                        or (nx, ny) in visited
                    ):
                        continue
                    if is_disconnect_button_pixel(*pixels[ny][nx]):
                        visited.add((nx, ny))
                        stack.append((nx, ny))
            box_width = max_x - min_x + 1
            box_height = max_y - min_y + 1
            if 45 <= box_width <= 360 and 14 <= box_height <= 70 and area >= 180:
                components.append((min_x, min_y, max_x + 1, max_y + 1, area))
    return components


def button_center(box: tuple[int, int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom, _area = box
    return (left + right) // 2, (top + bottom) // 2


def trim_mask_box(
    mask: list[list[bool]], box: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = box
    xs = []
    ys = []
    for y in range(top, bottom):
        for x in range(left, right):
            if mask[y][x]:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def segment_time_characters(path: str) -> tuple[list[tuple[int, int, int, int]], int, int, list[list[bool]]]:
    width, height, mask = read_bmp_mask(path)
    cols = [sum(1 for y in range(height) if mask[y][x]) for x in range(width)]
    active = [count > 0 for count in cols]
    raw_segments = []
    start = None
    for x, is_active in enumerate(active + [False]):
        if is_active and start is None:
            start = x
        elif not is_active and start is not None:
            raw_segments.append((start, x))
            start = None

    merged = []
    for left, right in raw_segments:
        if merged and left - merged[-1][1] <= 2:
            merged[-1] = (merged[-1][0], right)
        else:
            merged.append((left, right))

    boxes = []
    for left, right in merged:
        if right - left <= 0:
            continue
        trimmed = trim_mask_box(mask, (left, 0, right, height))
        if not trimmed:
            continue
        l, t, r, b = trimmed
        if (r - l) * (b - t) < 3:
            continue
        boxes.append(trimmed)
    return boxes, width, height, mask


def pattern_from_box(
    mask: list[list[bool]], box: tuple[int, int, int, int]
) -> str:
    left, top, right, bottom = box
    target_w, target_h = TIME_TEMPLATE_SIZE
    source_w = max(1, right - left)
    source_h = max(1, bottom - top)
    bits = []
    for ty in range(target_h):
        y1 = top + int(ty * source_h / target_h)
        y2 = top + max(1, int((ty + 1) * source_h / target_h))
        for tx in range(target_w):
            x1 = left + int(tx * source_w / target_w)
            x2 = left + max(1, int((tx + 1) * source_w / target_w))
            total = 0
            on = 0
            for y in range(min(y1, bottom - 1), min(y2, bottom)):
                for x in range(min(x1, right - 1), min(x2, right)):
                    total += 1
                    if mask[y][x]:
                        on += 1
            bits.append("1" if total and on / total >= 0.25 else "0")
    return "".join(bits)


def clean_time_sample(text: str) -> str:
    cleaned = "".join(ch for ch in text.strip() if ch in TIME_CHARS)
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if ":" not in cleaned:
        if len(digits) == 4:
            return f"{digits[:2]}:{digits[2:]}"
        if len(digits) == 3:
            return f"{digits[:1]}:{digits[1:]}"
    return cleaned


def loaded_templates_are_suspicious(templates: dict[str, object]) -> bool:
    total = 0
    for ch, patterns in templates.items():
        if ch not in TIME_CHARS or not isinstance(patterns, list):
            continue
        total += len(patterns)
        if len(patterns) > MAX_TIME_TEMPLATES_PER_CHAR:
            return True
    return total > len(TIME_CHARS) * MAX_TIME_TEMPLATES_PER_CHAR


def equal_slice_boxes(
    width: int, height: int, count: int
) -> list[tuple[int, int, int, int]]:
    boxes = []
    for index in range(count):
        left = round(index * width / count)
        right = round((index + 1) * width / count)
        boxes.append((left, 0, max(left + 1, right), height))
    return boxes


def equal_slice_region(
    left: int, top: int, right: int, bottom: int, count: int
) -> list[tuple[int, int, int, int]]:
    boxes = []
    width = max(1, right - left)
    for index in range(count):
        box_left = left + round(index * width / count)
        box_right = left + round((index + 1) * width / count)
        boxes.append((box_left, top, max(box_left + 1, box_right), bottom))
    return boxes


def row_bands_from_mask(
    mask: list[list[bool]], width: int, height: int
) -> list[tuple[int, int]]:
    row_counts = [sum(1 for x in range(width) if mask[y][x]) for y in range(height)]
    active = [count >= 2 for count in row_counts]
    bands = []
    start = None
    gap = 0
    for y, is_active in enumerate(active + [False, False, False]):
        if is_active:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 2:
                end = y - gap + 1
                if end - start >= 4:
                    bands.append((start, end))
                start = None
                gap = 0
    return bands


def whole_popup_time_boxes(
    width: int, height: int, mask: list[list[bool]], count: int
) -> list[tuple[int, int, int, int]] | None:
    if width < 80 or height < 14:
        return None
    bands = row_bands_from_mask(mask, width, height)
    if not bands:
        return None

    # The popup has local time on the first line and server time on the lower line.
    top, bottom = bands[-1]
    top = max(0, top - 2)
    bottom = min(height, bottom + 2)
    active_x = [
        x
        for x in range(width)
        if any(mask[y][x] for y in range(top, bottom))
    ]
    if not active_x:
        return None

    right = min(width, max(active_x) + 2)
    time_width = min(width, max(44, min(70, int(width * 0.45))))
    left = max(0, right - time_width)
    return equal_slice_region(left, top, right, bottom, count)


def boxes_from_time_image(path: str, expected_count: int | None = None):
    width, height, mask = read_bmp_mask(path)
    if expected_count:
        popup_boxes = whole_popup_time_boxes(width, height, mask, expected_count)
        if popup_boxes:
            return popup_boxes, width, height, mask

    boxes, width, height, mask = segment_time_characters(path)
    if expected_count and len(boxes) != expected_count:
        # Some game fonts/glows connect the digits into one blob. In that case,
        # use the user-entered time length to split the selected image evenly.
        boxes = equal_slice_boxes(width, height, expected_count)
    return boxes, width, height, mask


def save_time_templates_from_image(path: str, sample_text: str) -> tuple[bool, str]:
    cleaned = clean_time_sample(sample_text)
    if not cleaned:
        return False, "請輸入目前看到的時間，例如 14:26。"
    boxes, _width, _height, mask = boxes_from_time_image(path, len(cleaned))
    if len(boxes) != len(cleaned):
        return (
            False,
            f"校正失敗：框到 {len(boxes)} 個字元，但輸入是 {len(cleaned)} 個。請只框數字時間，例如 14:26。",
        )

    templates: dict[str, list[str]] = {}
    template_file = time_template_path()
    if os.path.exists(template_file):
        try:
            with open(template_file, "r", encoding="utf-8") as template_in:
                loaded = json.load(template_in)
            if isinstance(loaded, dict) and not loaded_templates_are_suspicious(loaded):
                templates = {str(k): list(v) for k, v in loaded.items()}
        except Exception:
            templates = {}

    for ch, box in zip(cleaned, boxes):
        pattern = pattern_from_box(mask, box)
        values = templates.setdefault(ch, [])
        if pattern not in values:
            values.append(pattern)
        if len(values) > MAX_TIME_TEMPLATES_PER_CHAR:
            del values[:-MAX_TIME_TEMPLATES_PER_CHAR]
    with open(template_file, "w", encoding="utf-8") as template_out:
        json.dump(templates, template_out, ensure_ascii=False, indent=2)
    known = "".join(ch for ch in "0123456789:" if ch in templates)
    return True, f"校正完成，已記住：{known}"


def pattern_distance(left: str, right: str) -> int:
    return sum(1 for a, b in zip(left, right) if a != b) + abs(len(left) - len(right))


def read_time_from_templates(path: str) -> tuple[str | None, str]:
    template_file = time_template_path()
    if not os.path.exists(template_file):
        return None, "尚未校正時間字型。請先輸入目前時間並按『校正時間字型』。"
    try:
        with open(template_file, "r", encoding="utf-8") as template_in:
            templates = json.load(template_in)
    except Exception as exc:
        return None, f"讀取校正檔失敗：{exc}"
    if not isinstance(templates, dict) or not templates:
        return None, "校正檔是空的，請重新校正。"

    expected_count = 5 if ":" in templates else None
    boxes, _width, _height, mask = boxes_from_time_image(path, expected_count)
    if not boxes:
        return None, "沒有在框選區域找到時間文字。"

    chars = []
    scores = []
    for box in boxes:
        pattern = pattern_from_box(mask, box)
        best_char = None
        best_score = 10**9
        for ch, patterns in templates.items():
            if ch not in TIME_CHARS:
                continue
            for template in patterns:
                score = pattern_distance(pattern, str(template))
                if score < best_score:
                    best_score = score
                    best_char = ch
        if best_char is None:
            continue
        chars.append(best_char)
        scores.append(best_score)

    text = "".join(chars)
    if scores and max(scores) > TIME_TEMPLATE_MAX_SCORE:
        return None, f"內建辨識信心不足：{text} 分數：{scores}。請在目前時間清楚顯示時重新校正。"
    match = re.search(r"\d{1,2}:\d{2}(?::\d{2})?", text)
    if match:
        return match.group(0), f"內建辨識：{text} 分數：{scores}"
    if text:
        return None, f"內建辨識未形成標準時間：{text} 分數：{scores}。請用目前畫面時間重新校正。"
    return None, "內建辨識失敗，請重新校正或縮小框選範圍。"


def read_time_text_from_image(path: str) -> tuple[str | None, str]:
    template_value, template_detail = read_time_from_templates(path)
    if template_value:
        return template_value, template_detail

    tesseract = shutil.which("tesseract")
    if not tesseract:
        return template_value, template_detail

    command = [
        tesseract,
        path,
        "stdout",
        "--psm",
        "7",
        "-c",
        "tessedit_char_whitelist=0123456789:",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=8, check=False
        )
    except Exception as exc:
        return read_time_from_templates(path)

    raw_text = (result.stdout or "").strip()
    compact = re.sub(r"\s+", "", raw_text)
    match = re.search(r"\d{1,2}:\d{2}(?::\d{2})?", compact)
    if match:
        return match.group(0), raw_text
    if compact:
        return None, raw_text
    return template_value, template_detail


def locate_time_line_rect_in_image(
    path: str, cursor_y: int
) -> tuple[int, int, int, int] | None:
    width, height, mask = read_bmp_mask(path)
    bands = row_bands_from_mask(mask, width, height)
    if not bands:
        return None
    cursor_y = max(0, min(height - 1, cursor_y))
    candidates = []
    for band_top, band_bottom in bands:
        if not 4 <= band_bottom - band_top <= 24:
            continue
        active_x = [
            x
            for x in range(width)
            if any(mask[y][x] for y in range(band_top, band_bottom))
        ]
        if not active_x:
            continue
        left = max(0, min(active_x) - 3)
        right = min(width, max(active_x) + 4)
        if right - left < 35:
            continue
        candidates.append((band_top, band_bottom, left, right))

    if not candidates:
        return None

    # When the cursor is on the clock icon, the popup appears to the right/below it.
    # The local time is the first text line below the cursor; server time is the second.
    below_cursor = [
        candidate
        for candidate in candidates
        if ((candidate[0] + candidate[1]) // 2) >= cursor_y + 8
    ]
    if len(below_cursor) >= 2:
        top, bottom, left, right = below_cursor[1]
    else:
        top, bottom, left, right = min(
            candidates,
            key=lambda candidate: abs(((candidate[0] + candidate[1]) // 2) - cursor_y),
        )

    top = max(0, top - 2)
    bottom = min(height, bottom + 2)
    if right - left < 30 or bottom - top < 8:
        return None
    return left, top, right, bottom


def time_image_signature(path: str) -> tuple[str, int] | None:
    width, height, mask = read_bmp_mask(path)
    bits = ["1" if mask[y][x] else "0" for y in range(height) for x in range(width)]
    count = sum(1 for bit in bits if bit == "1")
    if count < 8:
        return None
    return "".join(bits), count


def signature_distance(left: str, right: str) -> int:
    return sum(1 for a, b in zip(left, right) if a != b) + abs(len(left) - len(right))


@dataclass
class MouseMirrorEvent:
    group_index: int
    message: int
    x: int
    y: int


@dataclass
class MouseWheelMirrorEvent:
    group_index: int
    x: int
    y: int
    delta: int


@dataclass
class KeyboardMirrorEvent:
    group_index: int
    message: int
    vk: int


@dataclass
class OffsetSetting:
    enabled: bool = False
    dx: int = 0
    dy: int = 0
    delay_ms: int = 0


@dataclass
class LaunchEntry:
    path: str
    role: str = "同步窗口"
    delay_ms: int = 0
    x: int = 80
    y: int = 80
    width: int = DEFAULT_FLASH_CLIENT_WIDTH
    height: int = DEFAULT_FLASH_CLIENT_HEIGHT


@dataclass(frozen=True)
class CustomInput:
    kind: str
    value: int
    display: str


DEFAULT_KEYBOARD_SYNC_KEYS = ["ESC"]


@dataclass
class SyncGroup:
    name: str
    master_hwnd: int | None = None
    followers: list[int] = field(default_factory=list)
    launch_hwnds: dict[int, int] = field(default_factory=dict)
    offsets: dict[int, OffsetSetting] = field(default_factory=dict)
    role_ids: dict[int, str] = field(default_factory=dict)
    offset_base_point: tuple[int, int] | None = None
    running: bool = False
    custom_key_display: str = "XBUTTON1"
    hotkey_state: bool = False
    launch_hotkey_display: str = ""
    launch_hotkey_state: bool = False
    master_locked: bool = True
    sync_left_enabled: bool = True
    button_state: dict[str, bool] = field(default_factory=dict)
    active_buttons: set[str] = field(default_factory=set)
    last_button_pos: dict[str, tuple[int, int]] = field(default_factory=dict)
    sync_keyboard_enabled: bool = False
    keyboard_key_displays: list[str] = field(default_factory=lambda: list(DEFAULT_KEYBOARD_SYNC_KEYS))
    keyboard_state: dict[str, bool] = field(default_factory=dict)
    fishing_route_name: str = "東郊"
    launch_entries: list[LaunchEntry] = field(default_factory=list)


FISHING_ROUTES: dict[str, str] = {
    "東郊": "[@N|1189|一][@N|1190|一][@N|1191|一][@N|1192|一][@N|1193|一]",
    "湖北": "[@N|1194|一][@N|1195|一][@N|1196|一][@N|1197|一]",
    "雲天": "[@N|1199|二][@N|1200|二][@N|1201|二][@N|1202|二][@N|1203|二][@N|1204|二]",
    "平原": "[@N|1205|三][@N|1206|三][@N|1207|三][@N|1208|三]",
}


KEY_ALIASES: dict[str, int] = {
    "BACKSPACE": 0x08,
    "BACK": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "SHIFT": 0x10,
    "SHIFTL": 0x10,
    "SHIFTR": 0x10,
    "CTRL": 0x11,
    "CTRLL": 0x11,
    "CTRLR": 0x11,
    "CONTROL": 0x11,
    "CONTROLL": 0x11,
    "CONTROLR": 0x11,
    "ALT": 0x12,
    "ALTL": 0x12,
    "ALTR": 0x12,
    "PAUSE": 0x13,
    "CAPSLOCK": 0x14,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "INS": 0x2D,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "KPENTER": 0x0D,
}


SIDE_BUTTON_ALIASES: dict[str, int] = {
    "XBUTTON1": 1,
    "X1": 1,
    "MOUSE4": 1,
    "SIDE1": 1,
    "MB4": 1,
    "XBUTTON2": 2,
    "X2": 2,
    "MOUSE5": 2,
    "SIDE2": 2,
    "MB5": 2,
}


def normalize_key_name(text: str) -> str:
    return text.strip().upper().replace(" ", "").replace("-", "").replace("_", "")


def parse_custom_input(text: str) -> CustomInput:
    raw = text.strip()
    name = normalize_key_name(raw)
    if not name:
        raise ValueError("請先輸入要送出的按鍵，例如 F1、A、SPACE、XBUTTON1。")

    if name in SIDE_BUTTON_ALIASES:
        value = SIDE_BUTTON_ALIASES[name]
        return CustomInput("xbutton", value, f"XBUTTON{value}")

    if name in KEY_ALIASES:
        return CustomInput("key", KEY_ALIASES[name], raw.upper())

    if name.startswith("F") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return CustomInput("key", 0x70 + number - 1, f"F{number}")

    if name.startswith("NUM") and name[3:].isdigit():
        number = int(name[3:])
        if 0 <= number <= 9:
            return CustomInput("key", 0x60 + number, f"NUM{number}")

    if name.startswith("KP") and name[2:].isdigit():
        number = int(name[2:])
        if 0 <= number <= 9:
            return CustomInput("key", 0x60 + number, f"NUM{number}")

    if len(name) == 1 and ("A" <= name <= "Z" or "0" <= name <= "9"):
        return CustomInput("key", ord(name), name)

    if name.startswith("0X"):
        try:
            value = int(name, 16)
        except ValueError:
            value = -1
        if 1 <= value <= 255:
            return CustomInput("key", value, f"VK 0x{value:02X}")

    raise ValueError("不認得這個按鍵。可用例子：F1、A、SPACE、ENTER、XBUTTON1、XBUTTON2。")


class FlashSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.fixed_tk_scaling = float(self.tk.call("tk", "scaling"))
        except Exception:
            self.fixed_tk_scaling = 1.0
        self.title(APP_DISPLAY_NAME)
        try:
            self.iconbitmap(app_resource_path("sync_plus_icon.ico"))
        except Exception:
            pass
        self.geometry("820x440")
        self.minsize(760, 190)
        self.resizable(True, True)

        self.active_group_index = tk.IntVar(value=0)
        self.pending_active_group_name = ""
        self.pending_active_group_index = 0
        self.pending_section_visibility: dict[str, bool] = {}
        self.pending_window_geometry = ""
        self.pending_disconnect_detect_enabled = False
        self.pending_disconnect_restore_minimized = False
        self.pending_disconnect_detect_interval_ms = "3000"
        self.groups = [
            SyncGroup(name="第1組", custom_key_display="XBUTTON1", fishing_route_name="東郊"),
            SyncGroup(name="第2組", custom_key_display="F2", fishing_route_name="湖北"),
            SyncGroup(name="第3組", custom_key_display="F3", fishing_route_name="雲天"),
            SyncGroup(name="第4組", custom_key_display="F4", fishing_route_name="平原"),
            SyncGroup(name="第5組", custom_key_display="F5", fishing_route_name="東郊"),
        ]
        self.load_launch_config()
        self.pending_hotkey_conflict_notes = self.normalize_loaded_hotkey_conflicts()
        self.last_window_geometry = (
            self.normalize_main_window_geometry(self.pending_window_geometry)
            or "820x440+80+80"
        )
        self.main_window_geometry_ready = False
        self.main_geometry_save_after_id: str | None = None
        self.main_geometry_programmatic = False
        if not self.groups:
            self.groups = [SyncGroup(name="第1組", custom_key_display="未設定")]
        self.group_selector_text = tk.StringVar(value="")
        self.group_name_text = tk.StringVar(value=self.groups[0].name)
        self.group_combo: ttk.Combobox | None = None
        self.floating_status_window: tk.Toplevel | None = None
        self.floating_status_text = tk.StringVar(value="")
        self.floating_master_text = tk.StringVar(value="")
        self.floating_drag_offset: tuple[int, int] | None = None
        self.floating_resize_state: tuple[int, int, int, int] | None = None
        self.floating_drag_bindtag = f"FloatingStatusDrag{id(self)}"
        self.floating_drag_class_bound = False
        self.tray_added = False
        self.tray_icon_handle = None
        self.tray_hwnd: int | None = None
        self.tray_wndproc: WindowProc | None = None
        self.tray_class_name = f"FlashSyncTrayHost{id(self)}"
        self.tray_class_registered = False
        self.tray_menu_after_id: str | None = None
        self.tray_restore_after_id: str | None = None
        self.tray_restore_poll_after_id: str | None = None
        self.pending_tray_restore_request = False
        self.tray_last_open_request_at = 0.0
        self.restoring_from_tray = False
        self.hiding_main_to_tray = False
        self.main_hidden_to_tray = False
        self.closing_app = False
        self.poll_after_id: str | None = None
        self.capture_after_id: str | None = None
        self.capture_follower_after_id: str | None = None
        self.hotkey_after_id: str | None = None
        self.events: queue.Queue[
            MouseMirrorEvent | MouseWheelMirrorEvent | KeyboardMirrorEvent | None
        ] = queue.Queue()
        self.mouse_hook: wintypes.HHOOK | None = None
        self.mouse_hook_proc: LowLevelMouseProc | None = None
        self.section_order: list[str] = []
        self.section_visible_vars: dict[str, tk.BooleanVar] = {}
        self.section_frames: dict[str, ttk.Frame] = {}
        self.section_expand: dict[str, bool] = {}
        self.section_menu: tk.Menu | None = None
        self.content_frame: ttk.Frame | None = None
        self.required_sections = {"窗口"}
        self.fit_window_after_id: str | None = None
        self.launch_tree: ttk.Treeview | None = None
        self.launch_wait_after_id: str | None = None
        self.pending_sync_start_groups: set[int] = set()
        self.title_status_text = tk.StringVar(value=f"{self.groups[0].name} - 未開啟")

        self.window_size_width_text = tk.StringVar(value=str(DEFAULT_FLASH_CLIENT_WIDTH))
        self.window_size_height_text = tk.StringVar(value=str(DEFAULT_FLASH_CLIENT_HEIGHT))
        self.auto_resize_flash = tk.BooleanVar(value=False)
        self.auto_resize_after_id: str | None = None
        self.auto_resize_known: dict[int, tuple[int, int]] = {}
        self.disconnect_detect_enabled = tk.BooleanVar(value=self.pending_disconnect_detect_enabled)
        self.disconnect_restore_minimized = tk.BooleanVar(
            value=self.pending_disconnect_restore_minimized
        )
        self.disconnect_detect_interval_ms_text = tk.StringVar(
            value=self.pending_disconnect_detect_interval_ms
        )
        self.disconnect_detect_status_text = tk.StringVar(
            value="斷線偵測：啟用中" if self.pending_disconnect_detect_enabled else "斷線偵測：未啟用"
        )
        self.disconnect_detect_after_id: str | None = None
        self.disconnect_last_detect: dict[int, float] = {}
        self.disconnect_detected_names: list[str] = []
        self.disconnect_detected_group_index: int | None = None
        self.disconnect_scan_index = 0
        self.relogin_auto_enabled = tk.BooleanVar(value=False)
        self.relogin_status_text = tk.StringVar(value="重登流程：未啟用")
        self.relogin_after_ids: dict[int, list[str]] = {}
        self.relogin_resume_groups: dict[int, int] = {}
        self.restore_fishing_enabled = tk.BooleanVar(value=False)
        self.restore_fishing_route_text = tk.StringVar(value=self.groups[0].fishing_route_name)
        self.restore_fishing_overlay_windows: list[tk.Toplevel] = []

        self.sync_left = tk.BooleanVar(value=True)
        self.sync_keyboard = tk.BooleanVar(value=False)
        self.master_locked = tk.BooleanVar(value=self.groups[0].master_locked)
        self.custom_key_text = tk.StringVar(value="未設定")
        self.launch_hotkey_text = tk.StringVar(value="")
        self.role_id_overlay_windows: list[tk.Toplevel] = []
        self.capture_custom_input = False
        self.capture_input_target: str | None = None
        self.capture_mouse_state: dict[int, bool] = {}
        self.capture_follower_click = False
        self.capture_follower_multi = False
        self.capture_follower_mouse_down = False
        self.capture_window_target: str | None = None
        self.capture_window_group_index: int | None = None
        self.master_lock_button: tk.Button | None = None
        self.capture_master_button: ttk.Button | None = None
        self.batch_capture_button: tk.Button | None = None
        self.add_launch_files_button: ttk.Button | None = None
        self.remove_launch_entries_button: ttk.Button | None = None
        self.record_positions_button: ttk.Button | None = None
        self.offset_x_text = tk.StringVar(value="0")
        self.offset_y_text = tk.StringVar(value="0")
        self.delay_ms_text = tk.StringVar(value="0")
        self.game_time_rect: tuple[int, int, int, int] | None = None
        self.game_time_point1: tuple[int, int] | None = None
        self.game_time_point2: tuple[int, int] | None = None
        self.game_time_hover_point: tuple[int, int] | None = None
        self.game_time_text = tk.StringVar(value="遊戲時間：尚未讀取")
        self.game_time_sample_text = tk.StringVar(value="")
        self.auto_game_time = tk.BooleanVar(value=True)
        self.game_time_poll_ms_text = tk.StringVar(value="50")
        self.system_time_offset_ms_text = tk.StringVar(value="0")
        self.game_time_auto_after_id: str | None = None
        self.game_time_tick_after_id: str | None = None
        self.game_time_anchor_minutes: int | None = None
        self.game_time_anchor_perf: float | None = None
        self.game_time_last_read_minutes: int | None = None
        self.game_time_baseline_signature: str | None = None
        self.game_time_baseline_count: int | None = None
        self.game_time_candidate_signature: str | None = None
        self.game_time_candidate_count: int | None = None
        self.game_time_candidate_seen = 0
        self.game_time_candidate_perf: float | None = None
        self.game_time_overlay_windows: list[tk.Toplevel] = []
        self.timed_click_enabled = tk.BooleanVar(value=False)
        self.timed_click_target_text = tk.StringVar(value="")
        self.timed_click_lead_ms_text = tk.StringVar(value="120")
        self.timed_click_repeat_count_text = tk.StringVar(value="2")
        self.timed_click_repeat_interval_ms_text = tk.StringVar(value="250")
        self.timed_click_status_text = tk.StringVar(value="定時按下：未啟用")
        self.timed_click_point_text = tk.StringVar(value="按鈕位置：未設定")
        self.timed_click_hwnd: int | None = None
        self.timed_click_point: tuple[int, int] | None = None
        self.timed_click_after_id: str | None = None
        self.timed_click_fired = False
        self.autoclick_interval_ms_text = tk.StringVar(value="20")
        self.autoclick_button_text = tk.StringVar(value="左鍵")
        self.autoclick_repeat_forever = tk.BooleanVar(value=True)
        self.autoclick_repeat_count_text = tk.StringVar(value="1")
        self.autoclick_status_text = tk.StringVar(value="自動點擊：未啟用")
        self.autoclick_hotkey_text = tk.StringVar(value="F1")
        self.autoclick_hotkey = parse_custom_input("F1")
        self.autoclick_hotkey_state = False
        self.autoclick_running = False
        self.autoclick_after_id: str | None = None
        self.autoclick_sent_count = 0

        self.status_text = tk.StringVar(value=f"{self.groups[0].name}同步狀態：未開啟")
        self.master_text = tk.StringVar(value=f"{self.groups[0].name}主窗口：未選取")

        self.configure_rpg_theme()
        self._build_ui()
        self.refresh_group_ui()
        self.apply_pending_hotkey_conflict_notes()
        self.autoclick_status_text.trace_add(
            "write", lambda *_: self.update_autoclick_section_title()
        )
        self.update_autoclick_section_title()
        self.update_window_title()
        self.create_floating_status_window()
        self.setup_tray_icon()
        self._start_worker()
        self.bind_all("<KeyPress>", self.on_capture_key, add="+")
        self.bind("<Unmap>", self.on_window_unmap, add="+")
        self.bind("<Configure>", self.on_main_window_configure, add="+")
        self.schedule_hotkey_poll()
        self.schedule_disconnect_detect()
        self.schedule_tray_restore_poll()
        self.protocol("WM_DELETE_WINDOW", self.hide_main_to_tray)
        self.after(500, self.enable_main_window_geometry_tracking)

    def current_group(self) -> SyncGroup:
        index = max(0, min(len(self.groups) - 1, int(self.active_group_index.get())))
        if index != int(self.active_group_index.get()):
            self.active_group_index.set(index)
        return self.groups[index]

    def parse_main_window_geometry(self, geometry: str) -> tuple[int, int, int, int] | None:
        text = str(geometry or "").strip()
        match = re.match(r"^(\d+)x(\d+)(?:(\+|-)\d+(\+|-)\d+)?$", text)
        if not match:
            return None
        position = re.search(r"((?:\+|-)\d+)((?:\+|-)\d+)$", text)
        width = int(match.group(1))
        height = int(match.group(2))
        x = int(position.group(1)) if position else 80
        y = int(position.group(2)) if position else 80
        return width, height, x, y

    def virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        try:
            x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
            y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
            width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
            height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
            if width > 0 and height > 0:
                return x, y, width, height
        except Exception:
            pass
        return 0, 0, max(800, self.winfo_screenwidth()), max(600, self.winfo_screenheight())

    def normalize_main_window_geometry(self, geometry: str) -> str:
        parsed = self.parse_main_window_geometry(geometry)
        if parsed is None:
            return ""
        width, height, x, y = parsed
        min_width, min_height = self.minsize()
        width = max(min_width, min(1800, int(width)))
        height = max(min_height, min(1400, int(height)))
        vx, vy, vw, vh = self.virtual_screen_bounds()
        max_x = vx + max(0, vw - min(160, width))
        max_y = vy + max(0, vh - min(90, height))
        x = max(vx, min(int(x), max_x))
        y = max(vy, min(int(y), max_y))
        return f"{width}x{height}{x:+d}{y:+d}"

    def current_main_window_geometry(self) -> str:
        try:
            if self.state() in ("withdrawn", "iconic"):
                return ""
            return self.normalize_main_window_geometry(self.geometry())
        except Exception:
            return ""

    def stable_main_window_geometry_from_current(self) -> str:
        current = self.current_main_window_geometry()
        return current or self.last_window_geometry

    def remember_main_window_geometry(self) -> str:
        geometry = self.stable_main_window_geometry_from_current()
        if geometry:
            self.last_window_geometry = geometry
        return self.last_window_geometry

    def set_main_window_geometry(self, geometry: str, remember: bool = False) -> None:
        geometry = self.normalize_main_window_geometry(geometry)
        if not geometry:
            return
        self.main_geometry_programmatic = True
        try:
            self.geometry(geometry)
            if remember:
                self.last_window_geometry = geometry
        except Exception:
            pass
        finally:
            self.after(120, self.clear_main_geometry_programmatic)

    def clear_main_geometry_programmatic(self) -> None:
        self.main_geometry_programmatic = False

    def apply_last_main_window_geometry(self) -> None:
        self.set_main_window_geometry(self.last_window_geometry, remember=False)

    def lock_tk_scaling(self) -> None:
        try:
            current = float(self.tk.call("tk", "scaling"))
            if abs(current - self.fixed_tk_scaling) > 0.001:
                self.tk.call("tk", "scaling", self.fixed_tk_scaling)
        except Exception:
            pass

    def enable_main_window_geometry_tracking(self) -> None:
        self.lock_tk_scaling()
        self.main_window_geometry_ready = True
        self.remember_main_window_geometry()

    def on_main_window_configure(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        if not getattr(self, "main_window_geometry_ready", False):
            return
        if self.closing_app or self.restoring_from_tray:
            return
        geometry = self.current_main_window_geometry()
        if not geometry or geometry == self.last_window_geometry:
            return
        self.last_window_geometry = geometry
        self.lock_tk_scaling()
        if self.main_geometry_save_after_id:
            try:
                self.after_cancel(self.main_geometry_save_after_id)
            except Exception:
                pass
        self.main_geometry_save_after_id = self.after(900, self.save_main_window_geometry)

    def save_main_window_geometry(self) -> None:
        self.main_geometry_save_after_id = None
        self.save_launch_config()

    def group_owned_hwnds(self, except_group: SyncGroup | None = None) -> set[int]:
        owned: set[int] = set()
        for group in self.groups:
            if group is except_group:
                continue
            candidates = [group.master_hwnd, *group.followers, *group.launch_hwnds.values()]
            for hwnd in candidates:
                if hwnd and user32.IsWindow(hwnd):
                    owned.add(int(hwnd))
        return owned

    def can_share_cross_group_for_launch_match(
        self,
        group: SyncGroup,
        index: int,
        hwnd: int,
        explicit: bool = False,
        identity_match: bool = False,
        position_match: bool = False,
    ) -> bool:
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        if index == 0:
            return True
        if identity_match:
            return True
        if position_match:
            return explicit and hwnd in group.followers
        return explicit and hwnd in group.followers

    def create_floating_status_window(self) -> None:
        if self.floating_status_window is not None:
            return
        window = tk.Toplevel(self)
        self.floating_status_window = window
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=self.rpg_border)

        outer = tk.Frame(window, bg=self.rpg_border, padx=3, pady=3)
        outer.pack(fill="both", expand=True)
        header = tk.Frame(outer, bg=self.rpg_button_active)
        header.pack(fill="x")
        body = tk.Frame(outer, bg=self.rpg_panel, padx=12, pady=10)
        body.pack(fill="both", expand=True)

        title = tk.Label(
            header,
            text=f"{APP_DISPLAY_NAME}狀態",
            bg=self.rpg_button_active,
            fg=self.rpg_ink,
            font=(self.rpg_font_family, 11, "bold"),
        )
        title.pack(side="left", padx=(8, 6), pady=4)
        quit_button = tk.Button(
            header,
            text="關",
            width=2,
            command=self.close_from_tray,
            bg=self.rpg_button,
            fg=self.rpg_ink,
            activebackground=self.rpg_button_hover,
            relief="ridge",
            bd=1,
            font=self.rpg_font,
        )
        quit_button.pack(side="right", padx=(0, 2), pady=3)
        organize_button = tk.Button(
            header,
            text="整",
            width=2,
            command=self.launch_current_group_files,
            bg=self.rpg_button,
            fg=self.rpg_ink,
            activebackground=self.rpg_button_hover,
            relief="ridge",
            bd=1,
            font=self.rpg_font,
        )
        organize_button.pack(side="right", padx=(0, 2), pady=3)

        status = tk.Label(
            body,
            textvariable=self.floating_status_text,
            bg=self.rpg_border,
            fg=self.rpg_select_text,
            font=(self.rpg_font_family, 13, "bold"),
            padx=12,
            pady=4,
        )
        status.pack(fill="x", pady=(0, 8))
        master = tk.Label(
            body,
            textvariable=self.floating_master_text,
            bg=self.rpg_panel,
            fg=self.rpg_ink,
            font=(self.rpg_font_family, 10),
            anchor="w",
            justify="left",
        )
        master.pack(fill="x")

        resize_row = tk.Frame(outer, bg=self.rpg_panel)
        resize_row.pack(fill="x")
        resize_grip = tk.Label(
            resize_row,
            text="◢",
            bg=self.rpg_panel,
            fg=self.rpg_ink,
            font=(self.rpg_font_family, 9, "bold"),
            cursor="size_nw_se",
            anchor="se",
            padx=2,
            pady=0,
        )
        resize_grip.pack(side="right")
        resize_grip.bind("<ButtonPress-1>", self.start_floating_status_resize, add="+")
        resize_grip.bind("<B1-Motion>", self.resize_floating_status, add="+")
        resize_grip.bind("<ButtonRelease-1>", self.stop_floating_status_resize, add="+")

        self.bind_floating_status_drag(window)

        self.update_floating_status()
        self.after(100, self.place_floating_status_default)

    def place_floating_status_default(self) -> None:
        window = self.floating_status_window
        if window is None or not window.winfo_exists():
            return
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        x = self.winfo_rootx() + self.winfo_width() + 12
        y = self.winfo_rooty() + 80
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        if x + width > screen_width - 10:
            x = max(10, screen_width - width - 24)
        if y + height > screen_height - 40:
            y = max(10, screen_height - height - 60)
        window.geometry(f"+{x}+{y}")

    def start_floating_status_drag(self, event) -> None:
        window = self.floating_status_window
        if window is None or not window.winfo_exists():
            return
        self.floating_drag_offset = (
            event.x_root - window.winfo_x(),
            event.y_root - window.winfo_y(),
        )

    def drag_floating_status(self, event) -> None:
        window = self.floating_status_window
        if window is None or self.floating_drag_offset is None:
            return
        offset_x, offset_y = self.floating_drag_offset
        x = event.x_root - offset_x
        y = event.y_root - offset_y
        window.geometry(f"+{x}+{y}")

    def stop_floating_status_drag(self, _event=None) -> None:
        self.floating_drag_offset = None

    def start_floating_status_resize(self, event) -> None:
        window = self.floating_status_window
        if window is None or not window.winfo_exists():
            return
        self.floating_resize_state = (
            event.x_root,
            event.y_root,
            max(1, window.winfo_width()),
            max(1, window.winfo_height()),
        )
        return "break"

    def resize_floating_status(self, event) -> None:
        window = self.floating_status_window
        if window is None or self.floating_resize_state is None:
            return
        start_x, start_y, start_width, start_height = self.floating_resize_state
        min_width = max(150, window.winfo_reqwidth())
        min_height = max(90, window.winfo_reqheight())
        width = max(min_width, start_width + event.x_root - start_x)
        height = max(min_height, start_height + event.y_root - start_y)
        window.geometry(f"{int(width)}x{int(height)}+{window.winfo_x()}+{window.winfo_y()}")
        return "break"

    def stop_floating_status_resize(self, _event=None) -> None:
        self.floating_resize_state = None
        return "break"

    def bind_floating_status_drag(self, widget: tk.Widget) -> None:
        if not self.floating_drag_class_bound:
            self.bind_class(
                self.floating_drag_bindtag,
                "<ButtonPress-1>",
                self.start_floating_status_drag,
                add="+",
            )
            self.bind_class(
                self.floating_drag_bindtag,
                "<B1-Motion>",
                self.drag_floating_status,
                add="+",
            )
            self.bind_class(
                self.floating_drag_bindtag,
                "<ButtonRelease-1>",
                self.stop_floating_status_drag,
                add="+",
            )
            self.bind_class(
                self.floating_drag_bindtag,
                "<Double-Button-1>",
                lambda _event: self.restore_from_tray(),
                add="+",
            )
            self.floating_drag_class_bound = True
        tags = widget.bindtags()
        if self.floating_drag_bindtag not in tags:
            widget.bindtags((self.floating_drag_bindtag, *tags))
        for child in widget.winfo_children():
            self.bind_floating_status_drag(child)

    def show_floating_status_window(self) -> None:
        if self.floating_status_window is None:
            self.create_floating_status_window()
        if self.floating_status_window is not None:
            self.floating_status_window.deiconify()
            self.floating_status_window.attributes("-topmost", True)
            self.floating_status_window.lift()
            self.update_floating_status()

    def floating_status_master_name(self, group: SyncGroup) -> str:
        if group.master_hwnd and user32.IsWindow(group.master_hwnd):
            return self.master_display_name(group.master_hwnd, group)
        if group.launch_entries:
            return self.launch_entry_display_name(group.launch_entries[0])
        return "未選取"

    def floating_status_hotkey_text(self, group: SyncGroup) -> str:
        key = str(group.custom_key_display or "").strip()
        return key or "未設定"

    def group_window_counts(self, group: SyncGroup) -> tuple[int, int, int]:
        expected = len(group.launch_entries) if group.launch_entries else 0
        live: set[int] = set()
        if group.master_hwnd and user32.IsWindow(group.master_hwnd):
            live.add(int(group.master_hwnd))
        for hwnd in group.followers:
            if hwnd and user32.IsWindow(hwnd):
                live.add(int(hwnd))
        expected = max(expected, len(live))
        missing = max(0, expected - len(live))
        return len(live), expected, missing

    def group_shared_window_count(self, group: SyncGroup) -> int:
        own: set[int] = set()
        if group.master_hwnd and user32.IsWindow(group.master_hwnd):
            own.add(int(group.master_hwnd))
        own.update(int(hwnd) for hwnd in group.followers if hwnd and user32.IsWindow(hwnd))
        shared = 0
        for hwnd in own:
            owners = 0
            for other in self.groups:
                if other.master_hwnd == hwnd:
                    owners += 1
                if hwnd in other.followers:
                    owners += 1
            if owners > 1:
                shared += 1
        return shared

    def floating_status_group_line(self, group: SyncGroup) -> str:
        key = self.floating_status_hotkey_text(group)
        live, expected, missing = self.group_window_counts(group)
        parts = [f"{group.name}｜同步中", f"快捷鍵為：{key}", f"視窗：{live}/{expected}"]
        if missing:
            parts.append(f"未偵測到：{missing}隻")
        shared = self.group_shared_window_count(group)
        if shared:
            parts.append(f"共用：{shared}隻")
        return "\n".join(parts)

    def update_floating_status(self) -> None:
        if not hasattr(self, "floating_status_text"):
            return
        running = self.running_groups()
        if running:
            self.floating_status_text.set(
                "\n\n".join(self.floating_status_group_line(group) for group in running)
            )
            self.floating_master_text.set("")
        else:
            current = self.current_group()
            live, expected, missing = self.group_window_counts(current)
            extra = f"\n視窗：{live}/{expected}"
            if missing:
                extra += f"\n未偵測到：{missing}隻"
            self.floating_status_text.set(
                f"{current.name}｜未同步\n"
                f"快捷鍵為：{self.floating_status_hotkey_text(current)}"
                f"{extra}"
            )
            self.floating_master_text.set("")
        self.update_tray_tooltip()

    def tray_tip_text(self) -> str:
        running = self.running_groups()
        if running:
            summary = "、".join(
                f"{group.name}:{self.floating_status_hotkey_text(group)}"
                for group in running[:3]
            )
            if len(running) > 3:
                summary += f"、+{len(running) - 3}"
            return f"{APP_DISPLAY_NAME}｜{len(running)}組同步中｜{summary}"[:127]
        current = self.current_group()
        return (
            f"{APP_DISPLAY_NAME}｜{current.name}｜未開啟｜"
            f"快捷：{self.floating_status_hotkey_text(current)}"
        )[:127]

    def make_tray_data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = wintypes.HWND(self.tray_hwnd or int(self.winfo_id()))
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAYICON
        if self.tray_icon_handle:
            data.hIcon = self.tray_icon_handle
        data.szTip = self.tray_tip_text()
        return data

    def tray_icon_path_candidates(self) -> list[str]:
        candidates = [
            app_resource_path("sync_plus_icon.ico"),
            os.path.join(os.path.dirname(sys.executable), "sync_plus_icon.ico")
            if getattr(sys, "frozen", False)
            else "",
            os.path.join(os.path.dirname(__file__), "sync_plus_icon.ico"),
        ]
        unique: list[str] = []
        for path in candidates:
            if path and path not in unique:
                unique.append(path)
        return unique

    def load_tray_icon_handle(self) -> wintypes.HICON | None:
        for icon_path in self.tray_icon_path_candidates():
            if not os.path.exists(icon_path):
                continue
            handle = user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if handle:
                return wintypes.HICON(handle)

        exe_path = sys.executable if getattr(sys, "frozen", False) else ""
        if exe_path and os.path.exists(exe_path):
            large_icon = wintypes.HICON()
            small_icon = wintypes.HICON()
            count = shell32.ExtractIconExW(
                exe_path,
                0,
                ctypes.byref(large_icon),
                ctypes.byref(small_icon),
                1,
            )
            if count:
                if small_icon:
                    if large_icon:
                        user32.DestroyIcon(large_icon)
                    return small_icon
                if large_icon:
                    return large_icon
        return None

    def create_tray_host_window(self) -> int:
        if self.tray_hwnd and user32.IsWindow(wintypes.HWND(self.tray_hwnd)):
            return int(self.tray_hwnd)
        instance = kernel32.GetModuleHandleW(None)
        self.tray_wndproc = WindowProc(self.tray_window_proc)
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self.tray_wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = self.tray_class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if atom:
            self.tray_class_registered = True
        hwnd = user32.CreateWindowExW(
            0,
            self.tray_class_name,
            self.tray_class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        if not hwnd:
            raise OSError("無法建立系統匣訊息視窗。")
        self.tray_hwnd = int(hwnd)
        return int(hwnd)

    def setup_tray_icon(self) -> None:
        try:
            self.update_idletasks()
            self.tray_hwnd = self.create_tray_host_window()
            self.tray_icon_handle = self.load_tray_icon_handle()
            flags = NIF_MESSAGE | NIF_TIP
            if self.tray_icon_handle:
                flags |= NIF_ICON
            else:
                self.write_log("系統匣圖示載入失敗，已使用系統預設圖示。")
            data = self.make_tray_data(flags)
            self.tray_added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
            if self.tray_added:
                data.uTimeoutOrVersion = NOTIFYICON_VERSION_4
                shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
        except Exception as exc:
            self.tray_added = False
            self.write_log(f"系統匣圖示建立失敗：{exc}")

    def close_tray_menu(self) -> None:
        if self.tray_menu_after_id:
            try:
                self.after_cancel(self.tray_menu_after_id)
            except Exception:
                pass
            self.tray_menu_after_id = None
        if self.tray_restore_after_id:
            try:
                self.after_cancel(self.tray_restore_after_id)
            except Exception:
                pass
            self.tray_restore_after_id = None

    def update_tray_tooltip(self) -> None:
        if not getattr(self, "tray_added", False):
            return
        try:
            data = self.make_tray_data(NIF_TIP)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except Exception:
            pass

    def remove_tray_icon(self) -> None:
        if self.tray_added:
            try:
                data = self.make_tray_data(0)
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
            self.tray_added = False
        if self.tray_icon_handle:
            try:
                user32.DestroyIcon(self.tray_icon_handle)
            except Exception:
                pass
            self.tray_icon_handle = None
        if self.tray_hwnd:
            try:
                user32.DestroyWindow(wintypes.HWND(self.tray_hwnd))
            except Exception:
                pass
            self.tray_hwnd = None
        if self.tray_class_registered:
            try:
                user32.UnregisterClassW(
                    self.tray_class_name, kernel32.GetModuleHandleW(None)
                )
            except Exception:
                pass
            self.tray_class_registered = False

    def tray_window_proc(self, hwnd, msg, wparam, lparam):
        try:
            if SINGLE_INSTANCE_RESTORE_MESSAGE and msg == SINGLE_INSTANCE_RESTORE_MESSAGE:
                self.schedule_restore_from_tray()
                return 0
            event = int(lparam) & 0xFFFF
            if msg == WM_TRAYICON and event in (
                WM_LBUTTONDOWN,
                WM_LBUTTONUP,
                WM_LBUTTONDBLCLK,
                NIN_SELECT,
                NIN_KEYSELECT,
            ):
                self.schedule_restore_from_tray()
                return 0
            if msg == WM_TRAYICON and event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self.show_tray_menu()
                return 0
            if msg == WM_TRAYICON and event in (WM_RBUTTONDOWN, WM_RBUTTONDBLCLK):
                return 0
        except Exception:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def root_window_hwnd(self) -> int:
        try:
            hwnd = int(self.winfo_id())
            root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
            return int(getattr(root, "value", root) or hwnd)
        except Exception:
            return 0

    def main_window_is_visible(self) -> bool:
        try:
            hwnd = self.root_window_hwnd()
            if hwnd:
                return (
                    bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))
                    and self.state() not in ("withdrawn", "iconic")
                )
            return self.state() not in ("withdrawn", "iconic") and bool(self.winfo_viewable())
        except Exception:
            return False

    def schedule_restore_from_tray(self) -> None:
        self.tray_last_open_request_at = time.monotonic()
        self.pending_tray_restore_request = True

    def schedule_tray_restore_poll(self) -> None:
        if self.tray_restore_poll_after_id:
            return
        self.tray_restore_poll_after_id = self.after(120, self.poll_tray_restore_request)

    def poll_tray_restore_request(self) -> None:
        self.tray_restore_poll_after_id = None
        try:
            if self.pending_tray_restore_request and not self.closing_app:
                self.pending_tray_restore_request = False
                self.restore_or_focus_from_tray()
        finally:
            if not self.closing_app:
                self.schedule_tray_restore_poll()

    def restore_from_tray_if_hidden(self) -> None:
        self.restore_or_focus_from_tray()

    def show_tray_menu(self) -> None:
        self.tray_menu_after_id = None
        self.close_tray_menu()
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        hwnd = wintypes.HWND(self.tray_hwnd or int(self.winfo_id()))
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        command = 0
        try:
            user32.AppendMenuW(menu, MF_STRING, 1, f"顯示{APP_DISPLAY_NAME}")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, 2, f"關閉{APP_DISPLAY_NAME}")
            command = int(
                user32.TrackPopupMenu(
                    menu,
                    TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                    int(point.x),
                    int(point.y),
                    0,
                    hwnd,
                    None,
                )
            )
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        if command == 1:
            self.schedule_restore_from_tray()
        elif command == 2:
            self.after(0, self.close_from_tray)

    def focus_visible_main_window(self) -> None:
        self.lock_tk_scaling()
        try:
            self.deiconify()
            if self.state() == "iconic":
                self.state("normal")
            self.update_idletasks()
            self.lift()
            self.focus_force()
        except Exception:
            pass
        self.show_floating_status_window()
        self.update_tray_tooltip()

    def show_main_from_tray_menu(self) -> None:
        self.tray_restore_after_id = None
        self.restore_or_focus_from_tray()

    def restore_or_focus_from_tray(self) -> None:
        self.tray_restore_after_id = None
        if self.main_window_is_visible():
            self.focus_visible_main_window()
            return
        self.show_main_window_safely()

    def show_main_window_safely(self) -> None:
        self.tray_restore_after_id = None
        self.restoring_from_tray = True
        try:
            self.hiding_main_to_tray = False
            self.main_hidden_to_tray = False
            self.lock_tk_scaling()
            try:
                if self.state() == "iconic":
                    self.state("normal")
            except Exception:
                pass
            self.deiconify()
            self.update_idletasks()
            self.lift()
            try:
                self.focus_force()
            except Exception:
                pass
            self.protocol("WM_DELETE_WINDOW", self.hide_main_to_tray)
            self.show_floating_status_window()
            self.update_tray_tooltip()
            for delay_ms in (120, 400):
                self.after(delay_ms, self.ensure_main_window_visible_after_tray)
        except Exception as exc:
            try:
                self.write_log(f"從工作列顯示失敗：{exc}")
            except Exception:
                pass
        finally:
            self.after(180, self.finish_restore_from_tray)

    def ensure_main_window_visible_after_tray(self) -> None:
        if self.closing_app:
            return
        if self.main_window_is_visible():
            return
        self.hiding_main_to_tray = False
        self.main_hidden_to_tray = False
        try:
            self.deiconify()
            self.state("normal")
            self.update_idletasks()
            self.lift()
        except Exception:
            pass

    def close_from_tray(self) -> None:
        self.close_tray_menu()
        self.on_close()

    def on_window_unmap(self, _event=None) -> None:
        if self.closing_app or self.restoring_from_tray or self.hiding_main_to_tray:
            return
        self.after(80, self.hide_to_tray_if_iconic)

    def hide_to_tray_if_iconic(self) -> None:
        if self.closing_app or self.restoring_from_tray or self.hiding_main_to_tray:
            return
        try:
            if self.state() == "iconic":
                self.hide_main_to_tray()
        except Exception:
            pass

    def hide_main_to_tray(self) -> None:
        if self.closing_app:
            return
        self.hiding_main_to_tray = True
        self.restoring_from_tray = False
        if self.tray_restore_after_id:
            try:
                self.after_cancel(self.tray_restore_after_id)
            except Exception:
                pass
            self.tray_restore_after_id = None
        try:
            self.remember_main_window_geometry()
            self.save_launch_config()
        except Exception:
            pass
        try:
            if not self.tray_added:
                self.setup_tray_icon()
            self.withdraw()
            self.main_hidden_to_tray = True
            self.protocol("WM_DELETE_WINDOW", self.hide_main_to_tray)
            self.show_floating_status_window()
            self.update_tray_tooltip()
        except Exception as exc:
            try:
                self.write_log(f"收回工作列失敗：{exc}")
            except Exception:
                pass

        self.after(120, self.finish_hide_main_to_tray)

    def finish_hide_main_to_tray(self) -> None:
        self.hiding_main_to_tray = False

    def finish_restore_from_tray(self) -> None:
        self.restoring_from_tray = False
        try:
            self.protocol("WM_DELETE_WINDOW", self.hide_main_to_tray)
        except Exception:
            pass

    def restore_from_tray(self) -> None:
        self.restore_or_focus_from_tray()

    @property
    def master_hwnd(self) -> int | None:
        return self.current_group().master_hwnd

    @master_hwnd.setter
    def master_hwnd(self, value: int | None) -> None:
        self.current_group().master_hwnd = value

    @property
    def followers(self) -> list[int]:
        return self.current_group().followers

    @followers.setter
    def followers(self, value: list[int]) -> None:
        self.current_group().followers = value

    @property
    def offsets(self) -> dict[int, OffsetSetting]:
        return self.current_group().offsets

    @offsets.setter
    def offsets(self, value: dict[int, OffsetSetting]) -> None:
        self.current_group().offsets = value

    @property
    def role_ids(self) -> dict[int, str]:
        return self.current_group().role_ids

    @role_ids.setter
    def role_ids(self, value: dict[int, str]) -> None:
        self.current_group().role_ids = value

    @property
    def offset_base_point(self) -> tuple[int, int] | None:
        return self.current_group().offset_base_point

    @offset_base_point.setter
    def offset_base_point(self, value: tuple[int, int] | None) -> None:
        self.current_group().offset_base_point = value

    @property
    def running(self) -> bool:
        return self.current_group().running

    @running.setter
    def running(self, value: bool) -> None:
        self.current_group().running = value

    @property
    def hotkey_state(self) -> bool:
        return self.current_group().hotkey_state

    @hotkey_state.setter
    def hotkey_state(self, value: bool) -> None:
        self.current_group().hotkey_state = value

    @property
    def button_state(self) -> dict[str, bool]:
        return self.current_group().button_state

    @button_state.setter
    def button_state(self, value: dict[str, bool]) -> None:
        self.current_group().button_state = value

    @property
    def active_buttons(self) -> set[str]:
        return self.current_group().active_buttons

    @active_buttons.setter
    def active_buttons(self, value: set[str]) -> None:
        self.current_group().active_buttons = value

    @property
    def last_button_pos(self) -> dict[str, tuple[int, int]]:
        return self.current_group().last_button_pos

    @last_button_pos.setter
    def last_button_pos(self, value: dict[str, tuple[int, int]]) -> None:
        self.current_group().last_button_pos = value

    def configure_rpg_theme(self) -> None:
        self.rpg_font_family = "Microsoft JhengHei UI"
        self.rpg_font = (self.rpg_font_family, 10)
        self.rpg_font_bold = (self.rpg_font_family, 10, "bold")
        self.rpg_title_font = (self.rpg_font_family, 11, "bold")
        self.rpg_bg = "#c9a35d"
        self.rpg_panel = "#ead3a0"
        self.rpg_panel_light = "#fff0ca"
        self.rpg_field = "#fff6dd"
        self.rpg_ink = "#2b1a0a"
        self.rpg_muted = "#674b21"
        self.rpg_border = "#80591f"
        self.rpg_button = "#edd08e"
        self.rpg_button_active = "#8a5a24"
        self.rpg_button_hover = "#f4d99b"
        self.rpg_button_pressed = "#6f4317"
        self.rpg_select = "#8a5a24"
        self.rpg_select_text = "#fff2cf"

        self.configure(bg=self.rpg_bg)
        self.option_add("*Font", self.rpg_font)
        self.option_add("*Menu.background", self.rpg_panel)
        self.option_add("*Menu.foreground", self.rpg_ink)
        self.option_add("*Menu.activeBackground", self.rpg_button_active)
        self.option_add("*Menu.activeForeground", self.rpg_select_text)
        self.option_add("*Spinbox.background", self.rpg_field)
        self.option_add("*Spinbox.foreground", self.rpg_ink)
        self.option_add("*Spinbox.buttonBackground", self.rpg_button)
        self.option_add("*Spinbox.insertBackground", self.rpg_ink)
        self.option_add("*Spinbox.relief", "ridge")
        self.option_add("*Text.background", self.rpg_field)
        self.option_add("*Text.foreground", self.rpg_ink)
        self.option_add("*Text.insertBackground", self.rpg_ink)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            ".",
            background=self.rpg_bg,
            foreground=self.rpg_ink,
            fieldbackground=self.rpg_field,
            font=self.rpg_font,
        )
        style.configure("TFrame", background=self.rpg_bg)
        style.configure("RPGPanel.TFrame", background=self.rpg_panel)
        style.configure("TLabel", background=self.rpg_bg, foreground=self.rpg_ink, font=self.rpg_font)
        style.configure("Status.TLabel", background=self.rpg_bg, foreground=self.rpg_ink, font=self.rpg_title_font)
        style.configure(
            "TLabelframe",
            background=self.rpg_panel,
            foreground=self.rpg_border,
            bordercolor=self.rpg_border,
            borderwidth=2,
            relief="ridge",
        )
        style.configure(
            "TLabelframe.Label",
            background=self.rpg_bg,
            foreground=self.rpg_ink,
            font=self.rpg_title_font,
        )
        style.configure(
            "TButton",
            background=self.rpg_button,
            foreground=self.rpg_ink,
            bordercolor=self.rpg_border,
            lightcolor="#f8e6b8",
            darkcolor="#7a5321",
            borderwidth=2,
            focusthickness=1,
            focuscolor=self.rpg_border,
            padding=(10, 4),
            font=self.rpg_font_bold,
        )
        style.map(
            "TButton",
            background=[
                ("pressed", self.rpg_button_pressed),
                ("active", self.rpg_button_hover),
                ("disabled", "#d2bd88"),
            ],
            foreground=[
                ("pressed", self.rpg_select_text),
                ("disabled", "#8b7650"),
            ],
        )
        style.configure(
            "TCheckbutton",
            background=self.rpg_bg,
            foreground=self.rpg_ink,
            font=self.rpg_font,
            focuscolor=self.rpg_border,
        )
        style.map(
            "TCheckbutton",
            background=[("active", self.rpg_bg)],
            foreground=[("disabled", "#8b7650")],
        )
        style.configure(
            "TEntry",
            fieldbackground=self.rpg_field,
            foreground=self.rpg_ink,
            bordercolor=self.rpg_border,
            lightcolor="#f8e6b8",
            darkcolor="#7a5321",
            borderwidth=2,
            padding=(4, 2),
            font=self.rpg_font,
        )
        style.map(
            "TEntry",
            fieldbackground=[("readonly", self.rpg_field), ("disabled", "#d7c496")],
            foreground=[("readonly", self.rpg_ink), ("disabled", "#8b7650")],
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.rpg_field,
            background=self.rpg_button,
            foreground=self.rpg_ink,
            arrowcolor=self.rpg_border,
            bordercolor=self.rpg_border,
            lightcolor="#f8e6b8",
            darkcolor="#7a5321",
            borderwidth=2,
            padding=(4, 2),
            font=self.rpg_font,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.rpg_field)],
            foreground=[("readonly", self.rpg_ink)],
            selectbackground=[("readonly", self.rpg_button_active)],
            selectforeground=[("readonly", self.rpg_select_text)],
        )
        style.configure(
            "Treeview",
            background=self.rpg_field,
            fieldbackground=self.rpg_field,
            foreground=self.rpg_ink,
            bordercolor=self.rpg_border,
            lightcolor="#f8e6b8",
            darkcolor="#7a5321",
            borderwidth=2,
            rowheight=27,
            font=self.rpg_font,
        )
        style.map(
            "Treeview",
            background=[("selected", self.rpg_button_active)],
            foreground=[("selected", self.rpg_select_text)],
        )
        style.configure(
            "Treeview.Heading",
            background="#d3ad63",
            foreground=self.rpg_ink,
            bordercolor=self.rpg_border,
            relief="ridge",
            font=self.rpg_font_bold,
        )
        style.map("Treeview.Heading", background=[("active", self.rpg_button_hover)])
        style.configure(
            "TMenubutton",
            background=self.rpg_button,
            foreground=self.rpg_ink,
            bordercolor=self.rpg_border,
            borderwidth=2,
            padding=(8, 4),
            font=self.rpg_font_bold,
        )

    def make_section(
        self, title: str, visible_by_default: bool = True, expand: bool = False
    ) -> ttk.Frame:
        if self.content_frame is None:
            raise RuntimeError("content_frame is not ready")
        frame = ttk.LabelFrame(self.content_frame, text=title)
        self.section_order.append(title)
        self.section_frames[title] = frame
        self.section_expand[title] = expand
        self.section_visible_vars[title] = tk.BooleanVar(value=visible_by_default)
        return frame

    def refresh_visible_sections(self, allow_shrink: bool = True) -> None:
        for title in self.section_order:
            self.section_frames[title].pack_forget()
        for title in self.section_order:
            if title in self.required_sections:
                self.section_visible_vars[title].set(True)
            if not self.section_visible_vars[title].get():
                continue
            expand = self.section_expand.get(title, False)
            self.section_frames[title].pack(
                fill="both" if expand else "x",
                expand=expand,
                padx=10,
                pady=6,
            )
        self.schedule_fit_window_to_content(allow_shrink=allow_shrink)

    def rebuild_section_menu(self) -> None:
        if self.section_menu is None:
            return
        self.section_menu.delete(0, "end")
        self.section_menu.add_command(label="顯示區塊", state="disabled")
        for title in self.section_order:
            self.section_menu.add_checkbutton(
                label=title,
                variable=self.section_visible_vars[title],
                command=lambda: self.refresh_visible_sections(allow_shrink=True),
                state="disabled" if title in self.required_sections else "normal",
            )
        self.section_menu.add_separator()
        self.section_menu.add_command(label="全部展開", command=self.show_all_sections)
        self.section_menu.add_command(label="全部收起", command=self.hide_all_sections)

    def show_all_sections(self) -> None:
        for var in self.section_visible_vars.values():
            var.set(True)
        self.refresh_visible_sections(allow_shrink=True)

    def hide_all_sections(self) -> None:
        for title, var in self.section_visible_vars.items():
            var.set(title in self.required_sections)
        self.refresh_visible_sections(allow_shrink=True)

    def apply_pending_section_visibility(self) -> None:
        for title, visible in self.pending_section_visibility.items():
            if title in self.section_visible_vars:
                self.section_visible_vars[title].set(True if title in self.required_sections else bool(visible))
        for title in self.required_sections:
            if title in self.section_visible_vars:
                self.section_visible_vars[title].set(True)

    def apply_pending_window_geometry(self) -> None:
        self.apply_last_main_window_geometry()

    def schedule_fit_window_to_content(self, allow_shrink: bool = False) -> None:
        if self.fit_window_after_id:
            try:
                self.after_cancel(self.fit_window_after_id)
            except Exception:
                pass
        self.fit_window_after_id = self.after_idle(
            lambda shrink=allow_shrink: self.fit_window_to_content(allow_shrink=shrink)
        )

    def fit_window_to_content(self, allow_shrink: bool = False) -> None:
        self.fit_window_after_id = None
        try:
            self.update_idletasks()
            min_width, min_height = self.minsize()
            width = max(self.winfo_width(), min_width)
            requested_height = self.winfo_reqheight() + 8
            max_height = max(min_height, self.winfo_screenheight() - 90)
            if allow_shrink:
                height = min(max(requested_height, min_height), max_height)
            else:
                height = min(max(self.winfo_height(), requested_height, min_height), max_height)
            parts = self.parse_main_window_geometry(self.current_main_window_geometry())
            if parts is None:
                parts = self.parse_main_window_geometry(self.last_window_geometry)
            x = parts[2] if parts is not None else 80
            y = parts[3] if parts is not None else 80
            self.set_main_window_geometry(f"{width}x{height}{x:+d}{y:+d}", remember=True)
        except Exception:
            pass

    def update_autoclick_section_title(self) -> None:
        frame = self.section_frames.get("自動點擊")
        if frame is None:
            return
        frame.configure(
            text=f"自動點擊　模式：跟隨滑鼠　{self.autoclick_status_text.get()}"
        )

    def launch_config_path(self) -> str:
        return app_writable_path("sync_launch_config.json")

    def load_launch_config(self) -> None:
        path = self.launch_config_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return
        groups = data.get("groups", [])
        if not isinstance(groups, list):
            return
        app_state = data.get("app_state", {})
        if isinstance(app_state, dict):
            config_machine_id = str(app_state.get("machine_id") or "")
            if config_machine_id and config_machine_id != current_machine_id():
                return
            self.pending_active_group_name = str(app_state.get("active_group_name") or "")
            self.pending_active_group_index = self.int_from_text(
                app_state.get("active_group_index", 0), 0, 0, 10000
            )
            section_visibility = app_state.get("section_visibility", {})
            if isinstance(section_visibility, dict):
                self.pending_section_visibility = {
                    str(key): bool(value)
                    for key, value in section_visibility.items()
                }
            self.pending_window_geometry = str(app_state.get("window_geometry") or "")
            self.pending_disconnect_detect_enabled = bool(
                app_state.get("disconnect_detect_enabled", False)
            )
            self.pending_disconnect_restore_minimized = bool(
                app_state.get("disconnect_restore_minimized", False)
            )
            self.pending_disconnect_detect_interval_ms = str(
                app_state.get("disconnect_detect_interval_ms", "3000") or "3000"
            )
        loaded_groups: list[SyncGroup] = []
        for index, group_data in enumerate(groups):
            if not isinstance(group_data, dict):
                continue
            fallback = self.groups[index] if index < len(self.groups) else SyncGroup(
                name=f"第{index + 1}組",
                custom_key_display="未設定",
            )
            group = SyncGroup(
                name=fallback.name,
                custom_key_display=fallback.custom_key_display,
                fishing_route_name=fallback.fishing_route_name,
            )
            name = str(group_data.get("name") or group.name).strip()
            if name:
                group.name = name
            if "custom_key_display" in group_data:
                group.custom_key_display = str(group_data.get("custom_key_display") or "").strip()
            else:
                custom_key = str(group.custom_key_display).strip()
                if custom_key:
                    group.custom_key_display = custom_key
            group.launch_hotkey_display = str(group_data.get("launch_hotkey_display") or "").strip()
            group.master_locked = bool(group_data.get("master_locked", group.master_locked))
            group.sync_left_enabled = bool(group_data.get("sync_left_enabled", group.sync_left_enabled))
            group.sync_keyboard_enabled = bool(group_data.get("sync_keyboard_enabled", group.sync_keyboard_enabled))
            raw_keyboard_keys = group_data.get("keyboard_key_displays")
            if isinstance(raw_keyboard_keys, list):
                keys = [str(item).strip() for item in raw_keyboard_keys if str(item).strip()]
                if keys:
                    group.keyboard_key_displays = keys
            fishing_route = str(group_data.get("fishing_route_name") or group.fishing_route_name).strip()
            if fishing_route in FISHING_ROUTES:
                group.fishing_route_name = fishing_route
            entries = []
            for item in group_data.get("launch_entries", []):
                if not isinstance(item, dict):
                    continue
                item_path = str(item.get("path") or "").strip()
                if not item_path:
                    continue
                entries.append(
                    LaunchEntry(
                        path=item_path,
                        role=str(item.get("role") or "同步窗口"),
                        x=self.int_from_text(item.get("x", 80), 80, -10000, 10000),
                        y=self.int_from_text(item.get("y", 80), 80, -10000, 10000),
                        width=self.int_from_text(
                            item.get("width", DEFAULT_FLASH_CLIENT_WIDTH),
                            DEFAULT_FLASH_CLIENT_WIDTH,
                            100,
                            10000,
                        ),
                        height=self.int_from_text(
                            item.get("height", DEFAULT_FLASH_CLIENT_HEIGHT),
                            DEFAULT_FLASH_CLIENT_HEIGHT,
                            100,
                            10000,
                        ),
                        delay_ms=self.int_from_text(item.get("delay_ms", 0), 0, 0, 5000),
                    )
                )
            group.launch_entries = entries
            loaded_groups.append(group)
        if loaded_groups:
            self.groups = loaded_groups
            active_index = max(0, min(len(self.groups) - 1, self.pending_active_group_index))
            if self.pending_active_group_name:
                for index, group in enumerate(self.groups):
                    if group.name == self.pending_active_group_name:
                        active_index = index
                        break
            self.active_group_index.set(active_index)

    def save_launch_config(self) -> None:
        window_geometry = self.remember_main_window_geometry()
        data = {
            "app_state": {
                "machine_id": current_machine_id(),
                "active_group_index": int(self.active_group_index.get()),
                "active_group_name": self.current_group().name if self.groups else "",
                "window_geometry": window_geometry,
                "disconnect_detect_enabled": bool(self.disconnect_detect_enabled.get()),
                "disconnect_restore_minimized": bool(self.disconnect_restore_minimized.get()),
                "disconnect_detect_interval_ms": self.disconnect_detect_interval_ms_text.get(),
                "section_visibility": {
                    title: bool(var.get())
                    for title, var in self.section_visible_vars.items()
                },
            },
            "groups": [
                {
                    "name": group.name,
                    "custom_key_display": group.custom_key_display,
                    "launch_hotkey_display": group.launch_hotkey_display,
                    "master_locked": group.master_locked,
                    "sync_left_enabled": group.sync_left_enabled,
                    "sync_keyboard_enabled": group.sync_keyboard_enabled,
                    "keyboard_key_displays": group.keyboard_key_displays,
                    "fishing_route_name": group.fishing_route_name,
                    "launch_entries": [
                        {
                            "path": entry.path,
                            "role": entry.role,
                            "x": entry.x,
                            "y": entry.y,
                            "width": entry.width,
                            "height": entry.height,
                            "delay_ms": entry.delay_ms,
                        }
                        for entry in group.launch_entries
                    ],
                }
                for group in self.groups
            ]
        }
        try:
            config_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            target = self.launch_config_path()
            with open(target, "w", encoding="utf-8") as file:
                file.write(config_text)
            for folder in legacy_writable_dirs():
                backup = os.path.join(folder, "sync_launch_config.json")
                if os.path.abspath(backup) == os.path.abspath(target):
                    continue
                try:
                    with open(backup, "w", encoding="utf-8") as file:
                        file.write(config_text)
                except Exception:
                    pass
        except Exception as exc:
            self.write_log(f"啟動設定保存失敗：{exc}")

    def export_launch_config(self) -> None:
        source = self.launch_config_path()
        if not os.path.exists(source):
            self.save_launch_config()
        default_name = f"sync_launch_config_{time.strftime('%Y%m%d_%H%M%S')}.json"
        target = filedialog.asksaveasfilename(
            parent=self,
                title=f"匯出{APP_DISPLAY_NAME}設定",
            initialfile=default_name,
            defaultextension=".json",
            filetypes=(("JSON 設定檔", "*.json"), ("所有檔案", "*.*")),
        )
        if not target:
            return
        try:
            shutil.copyfile(source, target)
        except Exception as exc:
            messagebox.showwarning("匯出失敗", f"無法匯出設定：{exc}")
            return
        self.write_log(f"已匯出設定：{target}")

    def import_launch_config(self) -> None:
        source = filedialog.askopenfilename(
            parent=self,
                title=f"匯入{APP_DISPLAY_NAME}設定",
            filetypes=(("JSON 設定檔", "*.json"), ("所有檔案", "*.*")),
        )
        if not source:
            return
        try:
            with open(source, "r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
                raise ValueError(f"不是{APP_DISPLAY_NAME}設定檔")
            app_state = data.setdefault("app_state", {})
            if isinstance(app_state, dict):
                app_state["machine_id"] = current_machine_id()
            target = self.launch_config_path()
            with open(target, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            for folder in legacy_writable_dirs():
                backup = os.path.join(folder, "sync_launch_config.json")
                if os.path.abspath(backup) == os.path.abspath(target):
                    continue
                try:
                    shutil.copyfile(target, backup)
                except Exception:
                    pass
            self.load_launch_config()
            self.refresh_group_selector()
            self.refresh_group_ui()
            self.save_launch_config()
        except Exception as exc:
            messagebox.showwarning("匯入失敗", f"無法匯入設定：{exc}")
            return
        self.write_log(f"已匯入設定：{source}")

    def apply_group_name(self) -> None:
        group = self.current_group()
        name = self.group_name_text.get().strip()
        if not name:
            messagebox.showwarning("組名空白", "請輸入組名。")
            return
        group.name = name
        self.save_launch_config()
        self.refresh_group_selector()
        self.refresh_group_ui()
        self.write_log(f"同步組已改名：{name}")

    def group_selector_values(self) -> list[str]:
        return [f"{index + 1}. {group.name}" for index, group in enumerate(self.groups)]

    def refresh_group_selector(self) -> None:
        if self.group_combo is None:
            return
        values = self.group_selector_values()
        self.group_combo.configure(values=values)
        index = max(0, min(len(values) - 1, int(self.active_group_index.get())))
        if values:
            self.group_selector_text.set(values[index])

    def select_group_from_combo(self, _event=None) -> None:
        if self.group_combo is None:
            return
        index = self.group_combo.current()
        if index < 0:
            text = self.group_selector_text.get()
            for candidate_index, value in enumerate(self.group_selector_values()):
                if value == text:
                    index = candidate_index
                    break
        if 0 <= index < len(self.groups):
            self.active_group_index.set(index)
            self.switch_group()
            self.save_launch_config()

    def add_sync_group(self) -> None:
        default_name = f"第{len(self.groups) + 1}組"
        name = simpledialog.askstring(
            "新增同步組",
            "請輸入同步組名稱：",
            initialvalue=default_name,
            parent=self,
        )
        if name is None:
            return
        name = name.strip() or default_name
        self.groups.append(SyncGroup(name=name, custom_key_display="未設定"))
        self.active_group_index.set(len(self.groups) - 1)
        self.save_launch_config()
        self.refresh_group_selector()
        self.refresh_group_ui()
        self.write_log(f"已新增同步組：{name}")

    def delete_current_group(self) -> None:
        if len(self.groups) <= 1:
            messagebox.showwarning("不能刪除", "至少需要保留一個同步組。")
            return
        index = int(self.active_group_index.get())
        group = self.groups[index]
        if not messagebox.askyesno("刪除同步組", f"確定刪除「{group.name}」？"):
            return
        if group.running:
            self.stop_sync(index)
        del self.groups[index]
        self.active_group_index.set(max(0, min(index, len(self.groups) - 1)))
        self.save_launch_config()
        self.refresh_group_selector()
        self.refresh_group_ui()
        self.write_log(f"已刪除同步組：{group.name}")

    def move_current_group(self, direction: int) -> None:
        index = int(self.active_group_index.get())
        new_index = index + direction
        if new_index < 0 or new_index >= len(self.groups):
            return
        self.groups[index], self.groups[new_index] = self.groups[new_index], self.groups[index]
        self.active_group_index.set(new_index)
        self.save_launch_config()
        self.refresh_group_selector()
        self.refresh_group_ui()
        self.write_log(f"同步組順序已調整：{self.groups[new_index].name} -> 第 {new_index + 1} 位")

    def apply_group_fishing_route(self) -> None:
        route_name = self.restore_fishing_route_text.get().strip()
        if route_name not in FISHING_ROUTES:
            messagebox.showwarning("路徑錯誤", "請選擇東郊、湖北、雲天或平原。")
            return
        group = self.current_group()
        group.fishing_route_name = route_name
        self.save_launch_config()
        self.write_log(f"{group.name}恢復釣魚路徑已設為：{route_name}")

    def apply_sync_keyboard_enabled(self) -> None:
        group = self.current_group()
        group.sync_keyboard_enabled = bool(self.sync_keyboard.get())
        group.keyboard_state.clear()
        self.save_launch_config()
        state = "啟用" if group.sync_keyboard_enabled else "關閉"
        self.write_log(f"{group.name}鍵盤同步已{state}。")

    def apply_sync_left_enabled(self) -> None:
        group = self.current_group()
        group.sync_left_enabled = bool(self.sync_left.get())
        group.button_state.clear()
        group.active_buttons.clear()
        self.save_launch_config()
        state = "啟用" if group.sync_left_enabled else "關閉"
        self.write_log(f"{group.name}左鍵同步已{state}。")

    def parse_keyboard_sync_key_list(self, text: str) -> list[str]:
        displays: list[str] = []
        seen: set[str] = set()
        parts = [part for part in re.split(r"[\s,，;；/、]+", text.strip()) if part]
        for part in parts:
            custom = parse_custom_input(part)
            if custom.kind != "key":
                raise ValueError("鍵盤同步只接受鍵盤按鍵，不接受滑鼠鍵。")
            if custom.display not in seen:
                displays.append(custom.display)
                seen.add(custom.display)
        if not displays:
            raise ValueError("請至少輸入一個按鍵，例如 ESC。")
        return displays

    def configure_keyboard_sync_keys(self) -> None:
        group = self.current_group()
        layouts: list[tuple[str, list[tuple[str, str]]]] = [
            (
                "常用",
                [
                    ("ESC", "ESC"),
                    ("ENTER", "Enter"),
                    ("SPACE", "Space"),
                    ("TAB", "Tab"),
                    ("SHIFT", "Shift"),
                    ("CTRL", "Ctrl"),
                    ("ALT", "Alt"),
                ],
            ),
            (
                "移動",
                [
                    ("W", "W"),
                    ("A", "A"),
                    ("S", "S"),
                    ("D", "D"),
                    ("UP", "↑"),
                    ("LEFT", "←"),
                    ("DOWN", "↓"),
                    ("RIGHT", "→"),
                ],
            ),
            ("功能鍵", [(f"F{i}", f"F{i}") for i in range(1, 13)]),
            (
                "快捷欄",
                [
                    ("1", "1"),
                    ("2", "2"),
                    ("3", "3"),
                    ("4", "4"),
                    ("5", "5"),
                    ("6", "6"),
                    ("7", "7"),
                    ("8", "8"),
                    ("9", "9"),
                    ("0", "0"),
                ],
            ),
            (
                "其他常用",
                [
                    ("Q", "Q"),
                    ("E", "E"),
                    ("R", "R"),
                    ("T", "T"),
                    ("Z", "Z"),
                    ("X", "X"),
                    ("C", "C"),
                    ("V", "V"),
                    ("B", "B"),
                    ("G", "G"),
                    ("HOME", "Home"),
                    ("END", "End"),
                    ("PAGEUP", "PgUp"),
                    ("PAGEDOWN", "PgDn"),
                    ("INSERT", "Ins"),
                    ("DELETE", "Del"),
                ],
            ),
        ]

        ordered_keys: list[str] = []
        for _, keys in layouts:
            for display, _label in keys:
                if display not in ordered_keys:
                    ordered_keys.append(display)

        selected = {
            display
            for display in group.keyboard_key_displays
            if display in ordered_keys
        }
        buttons: dict[str, tk.Button] = {}

        dialog = tk.Toplevel(self)
        dialog.title("按鍵設定")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg=self.rpg_bg, padx=12, pady=10)

        ttk.Label(dialog, text="點亮的按鍵會同步；灰色按鍵不使用。").pack(
            anchor="w", pady=(0, 8)
        )

        preset_row = ttk.Frame(dialog)
        preset_row.pack(fill="x", pady=(0, 8))

        def refresh_button(display: str) -> None:
            button = buttons.get(display)
            if not button:
                return
            if display in selected:
                button.configure(
                    bg=self.rpg_button_active,
                    fg=self.rpg_select_text,
                    activebackground=self.rpg_button_pressed,
                    activeforeground=self.rpg_select_text,
                    relief="sunken",
                )
            else:
                button.configure(
                    bg=self.rpg_button,
                    fg=self.rpg_ink,
                    activebackground=self.rpg_button_hover,
                    activeforeground=self.rpg_ink,
                    relief="raised",
                )

        def refresh_all_buttons() -> None:
            for display in buttons:
                refresh_button(display)

        def toggle_key(display: str) -> None:
            if display in selected:
                selected.remove(display)
            else:
                selected.add(display)
            refresh_button(display)

        def set_preset(keys: list[str]) -> None:
            selected.clear()
            selected.update(key for key in keys if key in ordered_keys)
            refresh_all_buttons()

        ttk.Button(
            preset_row,
            text="基本",
            command=lambda: set_preset(["ESC", "ENTER", "SPACE"]),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            preset_row,
            text="移動",
            command=lambda: set_preset(["W", "A", "S", "D", "UP", "LEFT", "DOWN", "RIGHT"]),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            preset_row,
            text="技能",
            command=lambda: set_preset([f"F{i}" for i in range(1, 13)]),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            preset_row,
            text="清空",
            command=lambda: set_preset([]),
        ).pack(side="left")

        for title, keys in layouts:
            frame = ttk.LabelFrame(dialog, text=title)
            frame.pack(fill="x", pady=(0, 8))
            for index, (display, label) in enumerate(keys):
                button = tk.Button(
                    frame,
                    text=label,
                    width=7,
                    font=self.rpg_font,
                    bd=2,
                    command=lambda value=display: toggle_key(value),
                )
                button.grid(row=index // 8, column=index % 8, padx=3, pady=3)
                buttons[display] = button

        wheel_frame = ttk.LabelFrame(dialog, text="滑鼠")
        wheel_frame.pack(fill="x", pady=(0, 8))
        wheel_state = "同步中" if self.mouse_hook else "同步啟動時常駐"
        ttk.Label(wheel_frame, text=f"滑鼠滾輪：{wheel_state}").pack(
            anchor="w", padx=8, pady=6
        )

        button_row = ttk.Frame(dialog)
        button_row.pack(fill="x", pady=(4, 0))

        def apply_and_close() -> None:
            displays = [display for display in ordered_keys if display in selected]
            if not displays:
                messagebox.showwarning("按鍵設定", "請至少選一個要同步的按鍵。", parent=dialog)
                return
            group.keyboard_key_displays = displays
            group.keyboard_state = {
                custom.display: self.is_custom_input_down(custom)
                for custom in self.keyboard_sync_inputs(group)
            }
            self.save_launch_config()
            self.write_log(f"{group.name}鍵盤同步按鍵已設定：{'、'.join(displays)}")
            dialog.destroy()

        ttk.Button(button_row, text="套用", command=apply_and_close).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(button_row, text="取消", command=dialog.destroy).pack(side="right")

        refresh_all_buttons()
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(30, (self.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(30, (self.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f"+{x}+{y}")

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=(8, 2))
        self.section_menu = tk.Menu(toolbar, tearoff=False)
        ttk.Menubutton(toolbar, text="區塊 ▾", menu=self.section_menu).pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(toolbar, textvariable=self.title_status_text, style="Status.TLabel").pack(side="left")
        ttk.Button(
            toolbar,
            text="整理本組",
            command=self.launch_current_group_files,
        ).pack(side="left", padx=(10, 0))
        ttk.Label(toolbar, text="整理鍵：").pack(side="left", padx=(10, 0))
        ttk.Entry(
            toolbar,
            textvariable=self.launch_hotkey_text,
            width=9,
            state="readonly",
        ).pack(side="left", padx=(2, 4))
        ttk.Button(
            toolbar,
            text="設定",
            command=self.start_capture_launch_input,
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            toolbar,
            text="清除",
            command=self.clear_launch_hotkey,
        ).pack(side="left", padx=(0, 0))

        self.content_frame = ttk.Frame(self)
        self.content_frame.pack(fill="both", expand=True)

        window_tools = self.make_section("窗口", True)
        group_bar = ttk.Frame(window_tools)
        group_bar.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Label(group_bar, text="同步組：").pack(side="left")
        self.group_combo = ttk.Combobox(
            group_bar,
            textvariable=self.group_selector_text,
            values=self.group_selector_values(),
            state="readonly",
            width=18,
        )
        self.group_combo.pack(side="left", padx=(0, 8))
        self.group_combo.bind("<<ComboboxSelected>>", self.select_group_from_combo)
        ttk.Button(group_bar, text="上移", width=7, command=lambda: self.move_current_group(-1)).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(group_bar, text="下移", width=7, command=lambda: self.move_current_group(1)).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(group_bar, text="新增組", width=7, command=self.add_sync_group).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(group_bar, text="刪除組", width=7, command=self.delete_current_group).pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(group_bar, text="組名：").pack(side="left", padx=(8, 2))
        ttk.Entry(group_bar, textvariable=self.group_name_text, width=12).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(group_bar, text="改名", command=self.apply_group_name).pack(
            side="left", padx=(0, 12)
        )

        main_row = ttk.Frame(window_tools)
        main_row.pack(fill="x", padx=10, pady=4)
        ttk.Label(main_row, textvariable=self.master_text).pack(side="left", padx=(0, 18))
        self.master_lock_button = tk.Button(
            main_row,
            text="主窗：已上鎖",
            width=12,
            command=self.toggle_master_locked,
        )
        self.master_lock_button.pack(side="left", padx=(0, 8))
        self.capture_master_button = ttk.Button(
            main_row,
            text="點選主窗",
            command=self.capture_master,
        )
        self.capture_master_button.pack(
            side="left", padx=(0, 8)
        )
        self.batch_capture_button = tk.Button(
            main_row,
            text="批量加入",
            width=9,
            command=self.capture_follower,
        )
        self.batch_capture_button.pack(side="left", padx=(0, 8))
        ttk.Button(main_row, text="移除", command=self.remove_selected).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(main_row, text="清空", command=self.clear_followers).pack(
            side="left", padx=(0, 8)
        )

        sync_row = ttk.Frame(window_tools)
        sync_row.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Checkbutton(
            sync_row,
            text="同步左鍵",
            variable=self.sync_left,
            command=self.apply_sync_left_enabled,
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            sync_row,
            text="同步鍵盤",
            variable=self.sync_keyboard,
            command=self.apply_sync_keyboard_enabled,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            sync_row,
            text="按鍵設定",
            command=self.configure_keyboard_sync_keys,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(sync_row, text="啟停鍵：").pack(side="left")
        ttk.Entry(
            sync_row,
            textvariable=self.custom_key_text,
            width=10,
            state="readonly",
        ).pack(side="left", padx=(2, 4))
        ttk.Button(sync_row, text="設定", command=self.start_capture_custom_input).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(sync_row, text="清除", command=self.clear_custom_hotkey).pack(
            side="left", padx=(0, 8)
        )

        status_row = ttk.Frame(window_tools)
        status_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(status_row, textvariable=self.status_text).pack(side="left", padx=(0, 12))
        ttk.Label(status_row, textvariable=self.disconnect_detect_status_text).pack(
            side="left", padx=(0, 12)
        )

        detect_row = ttk.Frame(window_tools)
        detect_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Checkbutton(
            detect_row,
            text="斷線偵測",
            variable=self.disconnect_detect_enabled,
            command=self.toggle_disconnect_detect,
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            detect_row,
            text="偵測時可打開縮小視窗",
            variable=self.disconnect_restore_minimized,
            command=self.apply_disconnect_restore_minimized,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(detect_row, text="間隔ms：").pack(side="left")
        ttk.Entry(
            detect_row,
            textvariable=self.disconnect_detect_interval_ms_text,
            width=6,
        ).pack(side="left", padx=(2, 8))
        scan_once_button = ttk.Button(
            detect_row,
            text="單次掃描",
            command=self.scan_disconnect_once_visible,
        )
        scan_once_button.pack(side="left", padx=(0, 8))
        ToolTip(
            scan_once_button,
            "單次掃描目前組別。持續偵測間隔最低 1000ms。",
        )
        scan_restore_button = ttk.Button(
            detect_row,
            text="打開縮小並掃描",
            command=self.scan_disconnect_once_restore,
        )
        scan_restore_button.pack(side="left", padx=(0, 8))
        ToolTip(
            scan_restore_button,
            "打開縮小中的目前組別 Flash 後做一次斷線掃描；不會整理本組或移動位置。",
        )

        sync_tools = self.make_section("同步窗口", True)
        columns = ("offset", "role_id", "rect", "dx", "delay")
        self.tree = ttk.Treeview(sync_tools, columns=columns, show="headings", height=4)
        self.tree.heading("offset", text="偏移")
        self.tree.heading("role_id", text="角色ID")
        self.tree.heading("rect", text="位置")
        self.tree.heading("dx", text="左右偏移")
        self.tree.heading("delay", text="延遲ms")
        self.tree.column("offset", width=48, anchor="center", stretch=False)
        self.tree.column("role_id", width=180, anchor="w", stretch=True)
        self.tree.column("rect", width=260, anchor="w", stretch=True)
        self.tree.column("dx", width=76, anchor="e", stretch=False)
        self.tree.column("delay", width=70, anchor="e", stretch=False)
        self.tree.pack(fill="x", padx=10, pady=(8, 10))
        self.tree.bind("<<TreeviewSelect>>", self.on_window_selected)
        self.tree.bind("<Double-1>", self.on_window_double_click)

        offset_tools = ttk.Frame(sync_tools)
        offset_tools.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(
            offset_tools,
            text="校正角色ID",
            command=self.calibrate_selected_role_id,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            offset_tools,
            text="讀取角色ID",
            command=self.read_selected_role_ids,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(offset_tools, text="左右偏移：").pack(side="left", padx=(0, 8))
        ttk.Button(
            offset_tools,
            text="-",
            width=3,
            command=lambda: self.adjust_first_follower_dx(-1),
        ).pack(side="left", padx=(0, 4))
        tk.Spinbox(
            offset_tools,
            textvariable=self.offset_x_text,
            from_=-2000,
            to=2000,
            width=6,
            command=self.apply_first_follower_dx_from_text,
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            offset_tools,
            text="+",
            width=3,
            command=lambda: self.adjust_first_follower_dx(1),
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            offset_tools,
            text="歸零",
            command=self.reset_first_follower_dx,
        ).pack(side="left", padx=(0, 12))
        base_point_button = ttk.Button(
            offset_tools,
            text="設定主基準點",
            command=self.capture_offset_base_point,
        )
        base_point_button.pack(side="left", padx=(0, 8))
        self.offset_base_tooltip = ToolTip(
            base_point_button,
            "設定主基準點：先把滑鼠放在主窗口內要當作基準的位置，再點此按鈕；3 秒後會抓取目前滑鼠座標。",
        )
        ttk.Label(offset_tools, text="延遲ms：").pack(side="left", padx=(8, 4))
        tk.Spinbox(
            offset_tools,
            textvariable=self.delay_ms_text,
            from_=0,
            to=5000,
            increment=10,
            width=6,
            command=self.apply_selected_delay_from_text,
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            offset_tools,
            text="套用延遲",
            command=self.apply_selected_delay_from_text,
        ).pack(side="left", padx=(0, 8))

        launch_tools = self.make_section("組別啟動設定", False)
        launch_buttons = ttk.Frame(launch_tools)
        launch_buttons.pack(fill="x", padx=10, pady=(6, 4))
        self.add_launch_files_button = ttk.Button(
            launch_buttons,
            text="加入檔案",
            command=self.add_launch_files,
        )
        self.add_launch_files_button.pack(side="left", padx=(0, 8))
        self.remove_launch_entries_button = ttk.Button(
            launch_buttons,
            text="移除選取",
            command=self.remove_selected_launch_entries,
        )
        self.remove_launch_entries_button.pack(side="left", padx=(0, 8))
        self.record_positions_button = ttk.Button(
            launch_buttons,
            text="記錄目前位置",
            command=self.record_launch_positions_from_current_group,
        )
        self.record_positions_button.pack(side="left", padx=(0, 8))
        ttk.Button(
            launch_buttons,
            text="匯出設定",
            command=self.export_launch_config,
        ).pack(side="left", padx=(8, 8))
        ttk.Button(
            launch_buttons,
            text="匯入設定",
            command=self.import_launch_config,
        ).pack(side="left", padx=(0, 8))
        launch_columns = ("role", "path", "rect")
        self.launch_tree = ttk.Treeview(
            launch_tools,
            columns=launch_columns,
            show="headings",
            height=4,
        )
        self.launch_tree.heading("role", text="身份")
        self.launch_tree.heading("path", text="檔案")
        self.launch_tree.heading("rect", text="位置")
        self.launch_tree.column("role", width=76, anchor="center", stretch=False)
        self.launch_tree.column("path", width=420, anchor="w", stretch=True)
        self.launch_tree.column("rect", width=180, anchor="w", stretch=False)
        self.launch_tree.pack(fill="x", padx=10, pady=(0, 10))

        time_tools = self.make_section("遊戲時間", False)
        time_row = ttk.Frame(time_tools)
        time_row.pack(fill="x", padx=10, pady=6)
        ttk.Label(time_row, text="時間來源：系統時間").pack(side="left", padx=(0, 12))
        ttk.Label(time_row, text="偏移ms：").pack(side="left")
        tk.Spinbox(
            time_row,
            textvariable=self.system_time_offset_ms_text,
            from_=-60000,
            to=60000,
            increment=10,
            width=7,
        ).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(
            time_row,
            text="自動更新",
            variable=self.auto_game_time,
            command=self.toggle_auto_game_time,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(time_row, textvariable=self.game_time_text).pack(side="left")

        timed_tools = self.make_section("定時按下", False)
        timed_row = ttk.Frame(timed_tools)
        timed_row.pack(fill="x", padx=10, pady=6)
        ttk.Label(timed_row, text="目標時間：").pack(side="left")
        ttk.Entry(
            timed_row,
            textvariable=self.timed_click_target_text,
            width=13,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(timed_row, text="提前ms：").pack(side="left")
        tk.Spinbox(
            timed_row,
            textvariable=self.timed_click_lead_ms_text,
            from_=0,
            to=2000,
            increment=10,
            width=6,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(timed_row, text="連點：").pack(side="left")
        tk.Spinbox(
            timed_row,
            textvariable=self.timed_click_repeat_count_text,
            from_=1,
            to=10,
            width=4,
        ).pack(side="left", padx=(0, 4))
        ttk.Label(timed_row, text="間隔ms：").pack(side="left")
        tk.Spinbox(
            timed_row,
            textvariable=self.timed_click_repeat_interval_ms_text,
            from_=50,
            to=3000,
            increment=50,
            width=6,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            timed_row,
            text="設定按鈕位置",
            command=self.capture_timed_click_point,
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            timed_row,
            text="啟用定時",
            variable=self.timed_click_enabled,
            command=self.toggle_timed_click,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            timed_row,
            text="取消",
            command=self.cancel_timed_click,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(timed_row, textvariable=self.timed_click_point_text).pack(
            side="left", padx=(0, 12)
        )
        ttk.Label(timed_row, textvariable=self.timed_click_status_text).pack(side="left")

        autoclick_tools = self.make_section("自動點擊", True)
        autoclick_row = ttk.Frame(autoclick_tools)
        autoclick_row.pack(fill="x", padx=10, pady=6)
        ttk.Label(autoclick_row, text="間隔ms：").pack(side="left")
        tk.Spinbox(
            autoclick_row,
            textvariable=self.autoclick_interval_ms_text,
            from_=1,
            to=600000,
            increment=10,
            width=7,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(autoclick_row, text="按鍵：").pack(side="left")
        ttk.Combobox(
            autoclick_row,
            textvariable=self.autoclick_button_text,
            values=("左鍵", "右鍵"),
            width=5,
            state="readonly",
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            autoclick_row,
            text="無限",
            variable=self.autoclick_repeat_forever,
        ).pack(side="left", padx=(0, 4))
        ttk.Label(autoclick_row, text="次數：").pack(side="left")
        tk.Spinbox(
            autoclick_row,
            textvariable=self.autoclick_repeat_count_text,
            from_=1,
            to=999999,
            width=7,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(autoclick_row, text="快捷鍵：").pack(side="left")
        ttk.Entry(
            autoclick_row,
            textvariable=self.autoclick_hotkey_text,
            width=8,
            state="readonly",
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            autoclick_row,
            text="設定",
            command=self.start_capture_autoclick_input,
        ).pack(side="left", padx=(0, 8))

        flash_size_tools = self.make_section("還原調整 Flash 視窗", False)
        flash_size_row = ttk.Frame(flash_size_tools)
        flash_size_row.pack(fill="x", padx=10, pady=6)
        ttk.Label(flash_size_row, text="寬：").pack(side="left")
        tk.Spinbox(
            flash_size_row,
            textvariable=self.window_size_width_text,
            from_=200,
            to=5000,
            width=7,
        ).pack(side="left", padx=(0, 8))
        ttk.Label(flash_size_row, text="高：").pack(side="left")
        tk.Spinbox(
            flash_size_row,
            textvariable=self.window_size_height_text,
            from_=200,
            to=5000,
            width=7,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            flash_size_row,
            text="取主窗尺寸",
            command=self.load_master_window_size,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            flash_size_row,
            text="套用目前組",
            command=self.apply_window_size_to_current_group,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            flash_size_row,
            text="套用全部 Flash",
            command=self.apply_window_size_to_all_flash,
        ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            flash_size_row,
            text="新視窗自動套用",
            variable=self.auto_resize_flash,
            command=self.toggle_auto_resize_flash,
        ).pack(side="left", padx=(0, 8))

        log_frame = self.make_section("狀態紀錄", False, expand=True)
        self.log = tk.Text(log_frame, height=9, wrap="word")
        self.log.configure(
            bg=self.rpg_field,
            fg=self.rpg_ink,
            insertbackground=self.rpg_ink,
            selectbackground=self.rpg_button_active,
            selectforeground=self.rpg_select_text,
            relief="ridge",
            bd=2,
            font=self.rpg_font,
        )
        self.log.pack(fill="both", expand=True, padx=10, pady=10)
        self.apply_pending_section_visibility()
        self.apply_pending_window_geometry()
        self.rebuild_section_menu()
        self.refresh_visible_sections(allow_shrink=False)
        self.refresh_launch_entries()
        self.refresh_batch_capture_button()
        self.write_log("點選主窗/批量加入：點到哪個 Flash 就抓哪個。")
        self.write_log("區塊選單可顯示或隱藏功能。")
        self.schedule_game_time_tick()

    def write_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{timestamp}] {text}\n")
        self.log.see("end")

    def switch_group(self) -> None:
        self.cancel_capture_custom_input(log=False)
        self.cancel_capture_follower_click(log=False)
        self.refresh_group_ui()

    def toggle_master_locked(self) -> None:
        self.master_locked.set(not bool(self.master_locked.get()))
        self.apply_master_locked()

    def apply_master_locked(self) -> None:
        group = self.current_group()
        group.master_locked = bool(self.master_locked.get())
        if group.master_locked and self.capture_window_target == "master":
            self.cancel_capture_follower_click(log=False)
        self.save_launch_config()
        self.update_master_lock_controls()
        self.update_master_text()
        state = "上鎖" if group.master_locked else "解鎖"
        self.write_log(f"{group.name}主窗已{state}。")

    def update_master_lock_controls(self) -> None:
        group = self.current_group()
        state = "disabled" if group.master_locked else "normal"
        if self.master_lock_button is not None:
            if group.master_locked:
                self.master_lock_button.configure(
                    text="主窗：已上鎖",
                    bg=self.rpg_button_active,
                    fg=self.rpg_select_text,
                    activebackground=self.rpg_button_pressed,
                    activeforeground=self.rpg_select_text,
                    font=self.rpg_font_bold,
                    bd=2,
                    relief="sunken",
                )
            else:
                self.master_lock_button.configure(
                    text="主窗：未上鎖",
                    bg=self.rpg_button,
                    fg=self.rpg_ink,
                    activebackground=self.rpg_button_hover,
                    activeforeground=self.rpg_ink,
                    font=self.rpg_font_bold,
                    bd=2,
                    relief="raised",
                )
        for button in (
            self.capture_master_button,
            self.add_launch_files_button,
            self.remove_launch_entries_button,
            self.record_positions_button,
        ):
            if button is not None:
                button.configure(state=state)

    def refresh_group_ui(self) -> None:
        group = self.current_group()
        self.refresh_group_selector()
        self.group_name_text.set(group.name)
        self.sync_left.set(group.sync_left_enabled)
        self.sync_keyboard.set(group.sync_keyboard_enabled)
        self.master_locked.set(group.master_locked)
        self.launch_hotkey_text.set(group.launch_hotkey_display)
        if group.master_hwnd and user32.IsWindow(group.master_hwnd):
            self.custom_key_text.set(group.custom_key_display)
        else:
            self.custom_key_text.set(group.custom_key_display)
        self.update_master_text()
        self.update_master_lock_controls()
        self.offset_x_text.set("0")
        self.offset_y_text.set("0")
        if group.fishing_route_name not in FISHING_ROUTES:
            group.fishing_route_name = "東郊"
        self.restore_fishing_route_text.set(group.fishing_route_name)
        self.refresh_followers()
        self.refresh_launch_entries()
        self.update_sync_state_text()

    def refresh_launch_entries(self) -> None:
        if self.launch_tree is None:
            return
        self.launch_tree.delete(*self.launch_tree.get_children())
        for index, entry in enumerate(self.current_group().launch_entries):
            rect_text = f"{entry.x},{entry.y},{entry.width},{entry.height}"
            self.launch_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(entry.role, os.path.basename(entry.path), rect_text),
            )

    def launch_entry_display_name(self, entry: LaunchEntry) -> str:
        name = os.path.splitext(os.path.basename(entry.path))[0].strip()
        return name or entry.role or "未開啟"

    def launch_entry_for_hwnd(self, group: SyncGroup, hwnd: int) -> LaunchEntry | None:
        for raw_index, mapped_hwnd in group.launch_hwnds.items():
            index = int(raw_index)
            if mapped_hwnd == hwnd and 0 <= index < len(group.launch_entries):
                return group.launch_entries[index]
        if hwnd == group.master_hwnd and group.launch_entries:
            return group.launch_entries[0]
        for index, follower_hwnd in enumerate(group.followers, start=1):
            if follower_hwnd == hwnd and 0 <= index < len(group.launch_entries):
                return group.launch_entries[index]
        return None

    def launch_entry_index_for_hwnd(
        self,
        group: SyncGroup,
        hwnd: int,
        followers: list[int] | None = None,
    ) -> int | None:
        for raw_index, mapped_hwnd in group.launch_hwnds.items():
            index = int(raw_index)
            if mapped_hwnd == hwnd and 0 <= index < len(group.launch_entries):
                return index
        if hwnd == group.master_hwnd and group.launch_entries:
            return 0
        for index, follower_hwnd in enumerate(followers or group.followers, start=1):
            if follower_hwnd == hwnd and 0 <= index < len(group.launch_entries):
                return index
        pid = get_window_process_id(hwnd)
        process_info = flash_process_infos().get(pid, {})
        actual_identity = launch_identity_from_text(process_info.get("command_line", ""))
        if actual_identity:
            specs = resolve_launch_specs([entry.path for entry in group.launch_entries])
            for index, entry in enumerate(group.launch_entries):
                expected_identity = launch_identity_from_text(
                    " ".join(
                        [
                            entry.path,
                            specs.get(path_key(entry.path), {}).get("target", ""),
                            specs.get(path_key(entry.path), {}).get("args", ""),
                        ]
                    )
                )
                if expected_identity and expected_identity == actual_identity:
                    return index
        scored: list[tuple[int, int]] = []
        for index, entry in enumerate(group.launch_entries):
            score = self.launch_entry_position_score(hwnd, entry)
            if score is not None:
                scored.append((score, index))
        if scored:
            return sorted(scored)[0][1]
        return None

    def promote_launch_entry_to_master(self, group: SyncGroup, entry_index: int | None) -> None:
        if entry_index is None or entry_index <= 0 or entry_index >= len(group.launch_entries):
            return
        entry = group.launch_entries.pop(entry_index)
        group.launch_entries.insert(0, entry)
        for index, launch_entry in enumerate(group.launch_entries):
            launch_entry.role = "主窗口" if index == 0 else "同步窗口"

    def window_delay_ms(self, group: SyncGroup, hwnd: int) -> int:
        setting = group.offsets.get(hwnd)
        if setting and setting.delay_ms:
            return max(0, int(setting.delay_ms))
        entry = self.launch_entry_for_hwnd(group, hwnd)
        if entry:
            return max(0, int(entry.delay_ms))
        return 0

    def set_window_delay_ms(self, group: SyncGroup, hwnd: int, delay_ms: int) -> None:
        delay_ms = max(0, min(5000, int(delay_ms)))
        setting = group.offsets.setdefault(hwnd, OffsetSetting())
        setting.delay_ms = delay_ms
        entry = self.launch_entry_for_hwnd(group, hwnd)
        if entry:
            entry.delay_ms = delay_ms

    def window_display_name_for_group(self, group: SyncGroup, hwnd: int) -> str:
        entry = self.launch_entry_for_hwnd(group, hwnd)
        if entry:
            return self.launch_entry_display_name(entry)
        role_id = group.role_ids.get(hwnd)
        if role_id and role_id not in ("未讀取", "未校正"):
            return role_id
        return role_id or "未校正"

    def add_launch_files(self) -> None:
        group = self.current_group()
        if group.master_locked:
            self.write_log(f"{group.name}主窗上鎖中，請先解鎖再調整啟動清單。")
            return
        paths = filedialog.askopenfilenames(
            parent=self,
            title="選擇要啟動的遊戲檔案",
            filetypes=(
                ("可執行/捷徑/Flash", "*.exe *.lnk *.swf"),
                ("所有檔案", "*.*"),
            ),
        )
        if not paths:
            return
        for path in paths:
            abs_path = os.path.abspath(path)
            role = "主窗口" if not group.launch_entries else "同步窗口"
            index = len(group.launch_entries)
            group.launch_entries.append(
                LaunchEntry(
                    path=abs_path,
                    role=role,
                    x=80 + index * 36,
                    y=80 + index * 36,
                    width=DEFAULT_FLASH_CLIENT_WIDTH,
                    height=DEFAULT_FLASH_CLIENT_HEIGHT,
                )
            )
        self.sync_launch_hwnds_from_group_windows(group)
        self.save_launch_config()
        self.refresh_launch_entries()
        self.write_log(f"{group.name}已加入 {len(paths)} 個啟動檔案。")

    def selected_launch_indexes(self) -> list[int]:
        if self.launch_tree is None:
            return []
        indexes: list[int] = []
        for item in self.launch_tree.selection():
            try:
                indexes.append(int(item))
            except ValueError:
                continue
        return sorted(indexes, reverse=True)

    def remove_selected_launch_entries(self) -> None:
        group = self.current_group()
        if group.master_locked:
            self.write_log(f"{group.name}主窗上鎖中，請先解鎖再調整啟動清單。")
            return
        indexes = self.selected_launch_indexes()
        if not indexes:
            messagebox.showwarning("缺少選取", "請先在啟動清單選取要移除的檔案。")
            return
        entries = group.launch_entries
        for index in indexes:
            if 0 <= index < len(entries):
                entries.pop(index)
        group.launch_hwnds = {}
        if entries:
            entries[0].role = "主窗口"
            for entry in entries[1:]:
                entry.role = "同步窗口"
        self.save_launch_config()
        self.refresh_launch_entries()
        self.write_log("已移除選取啟動檔案。")

    def current_group_windows_in_order(self) -> list[int]:
        group = self.current_group()
        windows: list[int] = []
        if group.master_hwnd and user32.IsWindow(group.master_hwnd):
            windows.append(group.master_hwnd)
        for hwnd in group.followers:
            if hwnd and user32.IsWindow(hwnd) and hwnd not in windows:
                windows.append(hwnd)
        return windows

    def sync_launch_hwnds_from_group_windows(self, group: SyncGroup | None = None) -> None:
        group = group or self.current_group()
        entries = group.launch_entries
        if not entries:
            group.launch_hwnds = {}
            return
        mapping: dict[int, int] = {}
        valid_master = group.master_hwnd if group.master_hwnd and user32.IsWindow(group.master_hwnd) else None
        if valid_master:
            mapping[0] = valid_master
        else:
            group.master_hwnd = None
        valid_followers: list[int] = []
        for index, hwnd in enumerate(group.followers, start=1):
            if not hwnd or not user32.IsWindow(hwnd) or hwnd == valid_master:
                continue
            valid_followers.append(hwnd)
            if index < len(entries):
                mapping[index] = hwnd
        group.followers = valid_followers
        group.launch_hwnds = mapping

    def live_launch_hwnd_matches(
        self,
        group: SyncGroup,
        entries: list[LaunchEntry],
        allow_locked_master_identity_match: bool = False,
        allow_locked_master_position_match: bool = False,
    ) -> dict[int, int]:
        matches: dict[int, int] = {}
        cleaned: dict[int, int] = {}
        used: set[int] = set()
        specs = resolve_launch_specs([entry.path for entry in entries])
        entry_identities = {
            index: launch_identity_from_text(
                " ".join(
                    [
                        entry.path,
                        specs.get(path_key(entry.path), {}).get("target", ""),
                        specs.get(path_key(entry.path), {}).get("args", ""),
                    ]
                )
            )
            for index, entry in enumerate(entries)
        }
        process_infos = flash_process_infos()
        blocked_hwnds = self.group_owned_hwnds(except_group=group)

        def hwnd_matches_entry(index: int, hwnd: int) -> bool:
            expected = entry_identities.get(index, "")
            if not expected:
                return True
            pid = get_window_process_id(hwnd)
            actual = launch_identity_from_text(
                process_infos.get(pid, {}).get("command_line", "")
            )
            return bool(actual and actual == expected)

        valid_master = (
            group.master_hwnd
            if group.master_hwnd and user32.IsWindow(group.master_hwnd)
            else None
        )
        if valid_master and entries:
            matches[0] = valid_master
            cleaned[0] = valid_master
            used.add(valid_master)
        elif group.master_hwnd and not user32.IsWindow(group.master_hwnd):
            group.master_hwnd = None

        for raw_index, hwnd in sorted(group.launch_hwnds.items()):
            index = int(raw_index)
            if index == 0 and valid_master:
                continue
            if (
                0 <= index < len(entries)
                and hwnd
                and user32.IsWindow(hwnd)
                and (
                    hwnd not in blocked_hwnds
                    or self.can_share_cross_group_for_launch_match(
                        group, index, hwnd, explicit=True
                    )
                )
                and hwnd not in used
                and hwnd_matches_entry(index, hwnd)
            ):
                matches[index] = hwnd
                cleaned[index] = hwnd
                used.add(hwnd)
        group.launch_hwnds = cleaned

        valid_followers: list[int] = []
        for follower_index, hwnd in enumerate(group.followers, start=1):
            if (
                not hwnd
                or not user32.IsWindow(hwnd)
                or hwnd == valid_master
            ):
                continue
            valid_followers.append(hwnd)
            if (
                follower_index < len(entries)
                and hwnd not in used
                and hwnd_matches_entry(follower_index, hwnd)
            ):
                matches.setdefault(follower_index, hwnd)
                group.launch_hwnds.setdefault(follower_index, hwnd)
                used.add(hwnd)
        group.followers = valid_followers

        allow_locked_master_position_lookup = (
            bool(valid_master)
            or not group.master_locked
            or allow_locked_master_position_match
        )
        allow_locked_master_identity_lookup = (
            bool(valid_master)
            or not group.master_locked
            or allow_locked_master_identity_match
        )
        self.add_identity_matched_launch_hwnds(
            group,
            entries,
            matches,
            used,
            allow_master_match=allow_locked_master_identity_lookup,
        )
        self.add_position_matched_launch_hwnds(
            group,
            entries,
            matches,
            used,
            allow_master_match=allow_locked_master_position_lookup,
        )
        return matches

    def add_identity_matched_launch_hwnds(
        self,
        group: SyncGroup,
        entries: list[LaunchEntry],
        matches: dict[int, int],
        used: set[int],
        allow_master_match: bool = True,
    ) -> None:
        missing_indexes = [
            index
            for index in range(len(entries))
            if index not in matches and (allow_master_match or index != 0)
        ]
        if not missing_indexes:
            return
        specs = resolve_launch_specs([entry.path for entry in entries])
        entry_identities = {
            index: launch_identity_from_text(
                " ".join(
                    [
                        entry.path,
                        specs.get(path_key(entry.path), {}).get("target", ""),
                        specs.get(path_key(entry.path), {}).get("args", ""),
                    ]
                )
            )
            for index, entry in enumerate(entries)
        }
        wanted = {
            identity
            for index, identity in entry_identities.items()
            if index in missing_indexes and identity
        }
        if not wanted:
            return
        process_infos = flash_process_infos()
        blocked_hwnds = self.group_owned_hwnds(except_group=group)
        identity_to_hwnds: dict[str, list[int]] = {}
        for hwnd in enumerate_flash_windows():
            if hwnd in used or not user32.IsWindow(hwnd):
                continue
            pid = get_window_process_id(hwnd)
            info = process_infos.get(pid, {})
            identity = launch_identity_from_text(info.get("command_line", ""))
            if identity in wanted:
                identity_to_hwnds.setdefault(identity, []).append(hwnd)
        for hwnds in identity_to_hwnds.values():
            hwnds.sort(key=self.window_sort_key)
        for index in missing_indexes:
            identity = entry_identities.get(index, "")
            if not identity:
                continue
            hwnds = identity_to_hwnds.get(identity) or []
            while hwnds:
                hwnd = hwnds.pop(0)
                if (
                    hwnd in used
                    or not user32.IsWindow(hwnd)
                    or (
                        hwnd in blocked_hwnds
                        and not self.can_share_cross_group_for_launch_match(
                            group, index, hwnd, identity_match=True
                        )
                    )
                ):
                    continue
                matches[index] = hwnd
                group.launch_hwnds[index] = hwnd
                used.add(hwnd)
                break

    def launch_entry_position_score(self, hwnd: int, entry: LaunchEntry) -> int | None:
        try:
            rect = get_window_launch_match_rect(hwnd)
        except Exception:
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        dx = abs(int(rect.left) - int(entry.x))
        dy = abs(int(rect.top) - int(entry.y))
        dw = abs(width - int(entry.width))
        dh = abs(height - int(entry.height))
        if dx > 120 or dy > 120:
            return None
        return dx * 6 + dy * 6 + min(dw, 300) + min(dh, 300)

    def add_position_matched_launch_hwnds(
        self,
        group: SyncGroup,
        entries: list[LaunchEntry],
        matches: dict[int, int],
        used: set[int],
        allow_master_match: bool = True,
    ) -> None:
        missing_indexes = [
            index
            for index in range(len(entries))
            if index not in matches and (allow_master_match or index != 0)
        ]
        if not missing_indexes:
            return
        blocked_hwnds = self.group_owned_hwnds(except_group=group)
        all_candidates = [
            hwnd
            for hwnd in enumerate_flash_windows()
            if hwnd not in used and user32.IsWindow(hwnd)
        ]
        scored: list[tuple[int, int, int]] = []
        for index in missing_indexes:
            entry = entries[index]
            for hwnd in all_candidates:
                if (
                    hwnd in blocked_hwnds
                    and not self.can_share_cross_group_for_launch_match(
                        group, index, hwnd, position_match=True
                    )
                ):
                    continue
                score = self.launch_entry_position_score(hwnd, entry)
                if score is not None:
                    scored.append((score, index, hwnd))
        for _score, index, hwnd in sorted(scored):
            if index in matches or hwnd in used or not user32.IsWindow(hwnd):
                continue
            matches[index] = hwnd
            group.launch_hwnds[index] = hwnd
            used.add(hwnd)

    def window_sort_key(self, hwnd: int) -> tuple[int, int, int]:
        try:
            rect = get_window_rect(hwnd)
            return int(rect.top), int(rect.left), int(hwnd)
        except Exception:
            return 999999, 999999, int(hwnd)

    def record_launch_positions_from_current_group(self) -> None:
        group = self.current_group()
        if group.master_locked:
            self.write_log(f"{group.name}主窗上鎖中，請先解鎖再記錄位置。")
            return
        if not group.launch_entries:
            messagebox.showwarning("缺少檔案", "請先在組別啟動設定加入檔案。")
            return
        windows = self.current_group_windows_in_order()
        if not windows:
            messagebox.showwarning("缺少窗口", "請先點選主窗或批量加入同步窗口。")
            return
        count = min(len(group.launch_entries), len(windows))
        for index in range(count):
            hwnd = windows[index]
            rect = get_window_rect(hwnd)
            entry = group.launch_entries[index]
            entry.role = "主窗口" if index == 0 else "同步窗口"
            entry.x = int(rect.left)
            entry.y = int(rect.top)
            entry.width = int(rect.right - rect.left)
            entry.height = int(rect.bottom - rect.top)
            group.launch_hwnds[index] = hwnd
        group.launch_hwnds = {
            index: hwnd
            for index, hwnd in group.launch_hwnds.items()
            if 0 <= index < count and hwnd and user32.IsWindow(hwnd)
        }
        self.save_launch_config()
        self.refresh_launch_entries()
        self.write_log(f"已記錄 {count} 個窗口位置到 {group.name}。")

    def launch_current_group_files(self) -> None:
        self.ensure_group_launch_ready(
            int(self.active_group_index.get()),
            start_after_ready=False,
            show_missing_warning=True,
        )

    def launch_group_by_hotkey(self, group_index: int, hotkey: CustomInput) -> None:
        group = self.groups[group_index]
        if not group.launch_entries:
            self.write_log(f"整理鍵 {hotkey.display}：{group.name}沒有啟動清單。")
            return
        self.write_log(f"整理鍵 {hotkey.display}：整理{group.name}。")
        self.ensure_group_launch_ready(
            group_index,
            start_after_ready=False,
            show_missing_warning=False,
        )

    def ensure_group_launch_ready(
        self,
        group_index: int,
        start_after_ready: bool = False,
        show_missing_warning: bool = False,
    ) -> bool:
        group = self.groups[group_index]
        entries = list(group.launch_entries)
        if not entries:
            if show_missing_warning:
                messagebox.showwarning("缺少檔案", "請先在組別啟動設定加入要開啟的檔案。")
            return False
        existing_matches = self.live_launch_hwnd_matches(
            group,
            entries,
            allow_locked_master_identity_match=True,
        )
        existing_moved = self.apply_launch_entry_matches(group, entries, existing_matches)
        pending_entries = [
            (index, entry)
            for index, entry in enumerate(entries)
            if index not in existing_matches
        ]
        if not pending_entries:
            if group is self.current_group():
                self.refresh_group_ui()
            self.write_log(f"{group.name}已套用 {existing_moved} 個已綁定視窗位置。")
            if start_after_ready:
                self.pending_sync_start_groups.discard(group_index)
            return False
        before = set(enumerate_flash_windows())
        opened_entries: list[tuple[int, LaunchEntry]] = []
        for index, entry in pending_entries:
            if not os.path.exists(entry.path):
                self.write_log(f"啟動檔案不存在：{entry.path}")
                continue
            try:
                os.startfile(entry.path)
                opened_entries.append((index, entry))
            except Exception as exc:
                self.write_log(f"啟動失敗：{entry.path}，{exc}")
        if not opened_entries:
            if existing_moved:
                self.write_log(f"{group.name}已套用 {existing_moved} 個已綁定視窗位置。")
            if start_after_ready:
                self.pending_sync_start_groups.discard(group_index)
            return False
        if existing_moved:
            self.write_log(f"{group.name}已先套用 {existing_moved} 個已綁定視窗位置。")
        self.write_log(
            f"{group.name}缺少 {len(opened_entries)} 個視窗，已啟動對應檔案並等待新 Flash 視窗。"
        )
        self.wait_for_launched_windows(
            group_index,
            opened_entries,
            before,
            0,
            start_after_ready=start_after_ready,
        )
        return True

    def bind_existing_launch_windows_for_sync(self, group_index: int) -> int:
        group = self.groups[group_index]
        entries = list(group.launch_entries)
        if not entries:
            return 0
        matches = self.live_launch_hwnd_matches(
            group,
            entries,
            allow_locked_master_identity_match=True,
        )
        assigned = self.apply_launch_entry_matches(
            group,
            entries,
            matches,
            reset_group=False,
            move_windows=False,
        )
        if group is self.current_group():
            self.refresh_group_ui()
        if assigned:
            self.write_log(f"{group.name}同步前已綁定 {assigned} 個現有 Flash 視窗。")
        return assigned

    def wait_for_launched_windows(
        self,
        group_index: int,
        entries: list[tuple[int, LaunchEntry]],
        before: set[int],
        attempt: int,
        start_after_ready: bool = False,
    ) -> None:
        current = [hwnd for hwnd in enumerate_flash_windows() if hwnd not in before]
        needed = len(entries)
        if needed == 0:
            if self.groups[group_index] is self.current_group():
                self.refresh_group_ui()
            self.write_log(f"{self.groups[group_index].name}現有視窗已足夠，未重新綁定新視窗。")
            if start_after_ready:
                self.pending_sync_start_groups.discard(group_index)
                self.after(100, lambda gi=group_index: self.start_sync(gi, skip_prepare=True))
            return
        if len(current) < needed and attempt < 24:
            self.launch_wait_after_id = self.after(
                500,
                lambda gi=group_index, es=entries, bf=before, at=attempt + 1, sar=start_after_ready: self.wait_for_launched_windows(
                    gi, es, bf, at, sar
                ),
            )
            return
        self.bind_launched_windows_to_group(
            group_index,
            entries,
            current,
            start_after_ready=start_after_ready,
        )

    def bind_launched_windows_to_group(
        self,
        group_index: int,
        entries: list[tuple[int, LaunchEntry]],
        windows: list[int],
        start_after_ready: bool = False,
    ) -> None:
        group = self.groups[group_index]
        windows = [hwnd for hwnd in windows if hwnd and user32.IsWindow(hwnd)]
        specs = resolve_launch_specs([entry.path for _entry_index, entry in entries])
        entry_identities = {
            entry_index: launch_identity_from_text(
                " ".join(
                    [
                        entry.path,
                        specs.get(path_key(entry.path), {}).get("target", ""),
                        specs.get(path_key(entry.path), {}).get("args", ""),
                    ]
                )
            )
            for entry_index, entry in entries
        }
        process_infos = flash_process_infos()
        identity_to_hwnds: dict[str, list[int]] = {}
        for hwnd in windows:
            pid = get_window_process_id(hwnd)
            identity = launch_identity_from_text(
                process_infos.get(pid, {}).get("command_line", "")
            )
            if identity:
                identity_to_hwnds.setdefault(identity, []).append(hwnd)
        for hwnds in identity_to_hwnds.values():
            hwnds.sort(key=self.window_sort_key)

        matches: dict[int, int] = {}
        used: set[int] = set()
        unmatched_entries: list[tuple[int, LaunchEntry]] = []
        for entry_index, entry in entries:
            identity = entry_identities.get(entry_index, "")
            hwnds = identity_to_hwnds.get(identity) if identity else None
            matched_hwnd = 0
            while hwnds:
                hwnd = hwnds.pop(0)
                if hwnd in used or not user32.IsWindow(hwnd):
                    continue
                matched_hwnd = hwnd
                break
            if matched_hwnd:
                matches[entry_index] = matched_hwnd
                used.add(matched_hwnd)
            else:
                unmatched_entries.append((entry_index, entry))

        remaining_windows = [
            hwnd for hwnd in sorted(windows, key=self.window_sort_key)
            if hwnd not in used and user32.IsWindow(hwnd)
        ]
        for (entry_index, _entry), hwnd in zip(unmatched_entries, remaining_windows):
            matches[entry_index] = hwnd
            used.add(hwnd)
        assigned = self.apply_launch_entry_matches(
            group,
            group.launch_entries,
            matches,
            reset_group=False,
        )
        if group is self.current_group():
            self.refresh_group_ui()
        self.write_log(f"{group.name}已套用 {assigned} 個新開啟的 Flash 視窗位置。")
        if start_after_ready:
            self.pending_sync_start_groups.discard(group_index)
            self.after(300, lambda gi=group_index: self.start_sync(gi, skip_prepare=True))

    def apply_launch_entry_matches(
        self,
        group: SyncGroup,
        entries: list[LaunchEntry],
        matches: dict[int, int],
        reset_group: bool = False,
        move_windows: bool = True,
    ) -> int:
        if reset_group:
            group.master_hwnd = None
            group.followers = []
            group.launch_hwnds = {}
        existing_master = group.master_hwnd if group.master_hwnd and user32.IsWindow(group.master_hwnd) else None
        if existing_master:
            group.launch_hwnds = {
                index: hwnd
                for index, hwnd in group.launch_hwnds.items()
                if index != 0 or hwnd == existing_master
            }
            group.launch_hwnds[0] = existing_master
        assigned = 0
        for index, hwnd in sorted(matches.items()):
            if index < 0 or index >= len(entries):
                continue
            if not user32.IsWindow(hwnd):
                continue
            if index == 0 and existing_master and hwnd != existing_master:
                continue
            entry = entries[index]
            if move_windows:
                set_window_recorded_rect(hwnd, entry.x, entry.y, entry.width, entry.height)
                for delay_ms in (300, 900, 1800):
                    self.after(
                        delay_ms,
                        lambda h=hwnd, e=entry: set_window_recorded_rect(
                            h, e.x, e.y, e.width, e.height
                        ),
                    )
            group.launch_hwnds[index] = hwnd
            if entry.role == "主窗口" or index == 0:
                group.master_hwnd = hwnd
                group.followers = [h for h in group.followers if h != hwnd]
            else:
                if hwnd != group.master_hwnd and hwnd not in group.followers:
                    group.followers.append(hwnd)
            self.read_role_id_for_window(hwnd, log_success=False, group=group)
            assigned += 1
        group.launch_hwnds = {
            index: hwnd
            for index, hwnd in group.launch_hwnds.items()
            if 0 <= index < len(entries) and hwnd and user32.IsWindow(hwnd)
        }
        if group.launch_hwnds:
            if 0 in group.launch_hwnds:
                group.master_hwnd = group.launch_hwnds[0]
            elif existing_master:
                group.master_hwnd = existing_master
            mapped = {
                hwnd
                for hwnd in group.launch_hwnds.values()
                if hwnd and user32.IsWindow(hwnd)
            }
            ordered_followers = [
                hwnd
                for index, hwnd in sorted(group.launch_hwnds.items())
                if index != 0 and hwnd and user32.IsWindow(hwnd)
            ]
            extras = [
                hwnd
                for hwnd in group.followers
                if hwnd
                and user32.IsWindow(hwnd)
                and hwnd not in mapped
                and hwnd != group.master_hwnd
            ]
            group.followers = ordered_followers + extras
        return assigned

    def master_display_name(self, hwnd: int, group: SyncGroup | None = None) -> str:
        group = self.current_group() if group is None else group
        if group.master_locked and group.launch_entries and hwnd == group.master_hwnd:
            return self.launch_entry_display_name(group.launch_entries[0])
        role_id = group.role_ids.get(hwnd)
        if role_id and role_id not in ("未讀取", "未校正"):
            return role_id
        return self.window_display_name_for_group(group, hwnd)

    def update_master_text(self) -> None:
        group = self.current_group()
        lock_text = "上鎖" if group.master_locked else "未上鎖"
        if group.master_hwnd and user32.IsWindow(group.master_hwnd):
            self.master_text.set(
                f"{group.name}主窗口：{self.master_display_name(group.master_hwnd)}（{lock_text}）"
            )
        elif group.launch_entries:
            self.master_text.set(
                f"{group.name}主窗口：未開啟：{self.launch_entry_display_name(group.launch_entries[0])}（{lock_text}）"
            )
        else:
            self.master_text.set(f"{group.name}主窗口：未選取（{lock_text}）")
        self.update_floating_status()

    def refresh_batch_capture_button(self) -> None:
        button = self.batch_capture_button
        if button is None:
            return
        active = self.capture_follower_click and self.capture_window_target == "follower"
        if active:
            button.configure(
                bg=self.rpg_button_active,
                fg=self.rpg_select_text,
                activebackground=self.rpg_button_pressed,
                activeforeground=self.rpg_select_text,
                font=self.rpg_font_bold,
                bd=2,
                relief="sunken",
            )
        else:
            button.configure(
                bg=self.rpg_button,
                fg=self.rpg_ink,
                activebackground=self.rpg_button_hover,
                activeforeground=self.rpg_ink,
                font=self.rpg_font_bold,
                bd=2,
                relief="raised",
            )

    def capture_master(self) -> None:
        group = self.current_group()
        if group.master_locked:
            self.write_log(f"{group.name}主窗上鎖中，請先解鎖再點選主窗。")
            return
        self.cancel_capture_follower_click(log=False)
        self.capture_window_target = "master"
        self.capture_window_group_index = int(self.active_group_index.get())
        self.capture_follower_click = True
        self.capture_follower_multi = False
        self.capture_follower_mouse_down = self.is_button_down(VK_LBUTTON)
        self.refresh_batch_capture_button()
        self.write_log("點選主窗口：請直接用滑鼠左鍵點一下要設為主窗口的視窗。")
        self.schedule_capture_follower_click()

    def capture_follower(self) -> None:
        if self.capture_follower_click and self.capture_window_target == "follower":
            self.cancel_capture_follower_click(log=True)
            return
        self.cancel_capture_follower_click(log=False)
        self.capture_window_target = "follower"
        self.capture_window_group_index = int(self.active_group_index.get())
        self.capture_follower_click = True
        self.capture_follower_multi = True
        self.capture_follower_mouse_down = self.is_button_down(VK_LBUTTON)
        self.refresh_batch_capture_button()
        self.write_log("批量選取同步窗口：請依序點要加入的 Flash 視窗；按 Esc 或再按一次按鈕結束。")
        self.schedule_capture_follower_click()

    def _countdown(self, message: str, callback) -> None:
        self.write_log(message)
        self.after(3000, callback)

    def _set_master_from_cursor(self) -> None:
        pt = get_cursor_pos()
        self._set_master_from_point(pt.x, pt.y)

    def _set_master_from_point(self, x: int, y: int, group_index: int | None = None) -> None:
        if group_index is None:
            group = self.current_group()
        else:
            group_index = max(0, min(len(self.groups) - 1, int(group_index)))
            group = self.groups[group_index]
        if group.master_locked:
            self.write_log(f"{group.name}主窗上鎖中，已忽略主窗變更。")
            return
        hwnd = find_flash_window_at_point(x, y)
        if not hwnd or int(hwnd) == int(self.winfo_id()):
            self.write_log("沒有抓到有效窗口，請再試一次。")
            return
        if not is_flash_window(hwnd):
            self.write_log("點到的不是 Flash 視窗，已忽略。")
            return
        old_master = group.master_hwnd if group.master_hwnd and user32.IsWindow(group.master_hwnd) else None
        old_followers = list(group.followers)
        launch_entry_index = self.launch_entry_index_for_hwnd(group, hwnd, old_followers)
        group.master_hwnd = hwnd
        new_followers = [h for h in old_followers if h != hwnd and user32.IsWindow(h)]
        if old_master and old_master != hwnd and old_master not in new_followers:
            new_followers.insert(0, old_master)
        group.followers = new_followers
        self.promote_launch_entry_to_master(group, launch_entry_index)
        role_id = self.read_role_id_for_window(hwnd, log_success=True, group=group)
        if not role_id:
            self.after(500, lambda h=hwnd, g=group: self.retry_role_id_for_window(h, g))
        self.sync_launch_hwnds_from_group_windows(group)
        self.save_launch_config()
        if group is self.current_group():
            self.custom_key_text.set(group.custom_key_display)
            self.update_master_text()
            self.refresh_followers()
            self.refresh_launch_entries()
        self.write_log(f"{group.name}主窗口：" + window_summary(hwnd))

    def _add_follower_from_cursor(self) -> None:
        pt = get_cursor_pos()
        self._add_follower_from_point(pt.x, pt.y)

    def _add_follower_from_point(self, x: int, y: int) -> None:
        hwnd = find_flash_window_at_point(x, y)
        if not hwnd or int(hwnd) == int(self.winfo_id()):
            self.write_log("沒有抓到有效窗口，請再試一次。")
            return
        if not is_flash_window(hwnd):
            self.write_log("點到的不是 Flash 視窗，已忽略，批量選取仍在進行。")
            return
        if self.master_hwnd and hwnd == self.master_hwnd:
            self.write_log("這是主窗口，不加入同步列表。")
            return
        if hwnd not in self.followers:
            self.followers.append(hwnd)
        role_id = self.read_role_id_for_window(hwnd, log_success=True)
        if not role_id:
            group = self.current_group()
            self.after(500, lambda h=hwnd, g=group: self.retry_role_id_for_window(h, g))
        self.sync_launch_hwnds_from_group_windows()
        self.refresh_followers()
        if self.tree.exists(str(hwnd)):
            self.tree.selection_set(str(hwnd))
            self.tree.focus(str(hwnd))
        self.write_log(f"{self.current_group().name}同步窗口：" + window_summary(hwnd))

    def finish_capture_follower_click(self, x: int, y: int) -> None:
        if not self.capture_follower_click:
            return
        target = self.capture_window_target
        group_index = self.capture_window_group_index
        if target == "master":
            self.capture_follower_click = False
            self.capture_follower_multi = False
            self.capture_follower_mouse_down = False
            self.capture_window_target = None
            self.capture_window_group_index = None
            if self.capture_follower_after_id:
                try:
                    self.after_cancel(self.capture_follower_after_id)
                except Exception:
                    pass
                self.capture_follower_after_id = None
            self.refresh_batch_capture_button()
            self._set_master_from_point(x, y, group_index=group_index)
        else:
            self._add_follower_from_point(x, y)
            if self.capture_follower_multi:
                self.capture_follower_mouse_down = True
                self.schedule_capture_follower_click()
            else:
                self.capture_follower_click = False
                self.capture_follower_mouse_down = False
                self.capture_window_target = None
                self.capture_window_group_index = None
                if self.capture_follower_after_id:
                    try:
                        self.after_cancel(self.capture_follower_after_id)
                    except Exception:
                        pass
                    self.capture_follower_after_id = None
                self.refresh_batch_capture_button()

    def schedule_capture_follower_click(self) -> None:
        if self.capture_follower_click:
            self.capture_follower_after_id = self.after(15, self.poll_capture_follower_click)

    def cancel_capture_follower_click(self, log: bool = True) -> None:
        self.capture_follower_click = False
        self.capture_follower_multi = False
        self.capture_follower_mouse_down = False
        self.capture_window_target = None
        self.capture_window_group_index = None
        if self.capture_follower_after_id:
            try:
                self.after_cancel(self.capture_follower_after_id)
            except Exception:
                pass
            self.capture_follower_after_id = None
        self.refresh_batch_capture_button()
        if log:
            self.write_log("已取消點選窗口。")

    def poll_capture_follower_click(self) -> None:
        self.capture_follower_after_id = None
        if not self.capture_follower_click:
            return
        is_down = self.is_button_down(VK_LBUTTON)
        if is_down and not self.capture_follower_mouse_down:
            pt = get_cursor_pos()
            self.finish_capture_follower_click(pt.x, pt.y)
            return
        self.capture_follower_mouse_down = is_down
        self.schedule_capture_follower_click()

    def role_id_capture_path(self) -> str:
        return app_writable_path("role_id_capture.bmp")

    def role_id_region(self) -> tuple[int, int, int, int]:
        return ROLE_ID_REGION

    def read_role_id_for_window(
        self,
        hwnd: int,
        log_success: bool = False,
        group: SyncGroup | None = None,
        log_failure: bool = True,
    ) -> str:
        group = self.current_group() if group is None else group
        if not hwnd or not user32.IsWindow(hwnd):
            return ""
        path = self.role_id_capture_path()
        try:
            capture_client_region_to_bmp(hwnd, self.role_id_region(), path)
        except Exception as exc:
            group.role_ids[hwnd] = "未讀取"
            if log_failure:
                self.write_log(f"角色ID讀取失敗：無法截圖左上角區域：{exc}")
            return ""

        role_id, detail = match_role_id_template(path)
        if role_id:
            group.role_ids[hwnd] = role_id
            if log_success:
                self.write_log(f"角色ID：{role_id}（模板比對，{detail}）")
            return role_id

        group.role_ids[hwnd] = "未校正"
        if log_failure:
            self.write_log(f"角色ID讀取失敗：{detail}；已保存截圖：{path}")
        return ""

    def retry_role_id_for_window(
        self,
        hwnd: int,
        group: SyncGroup,
        attempts_left: int = 3,
    ) -> None:
        if not hwnd or not user32.IsWindow(hwnd) or attempts_left <= 0:
            return
        role_id = self.read_role_id_for_window(
            hwnd,
            log_success=False,
            group=group,
            log_failure=attempts_left == 1,
        )
        if role_id:
            self.write_log(f"角色ID補讀成功：{role_id}")
            if group is self.current_group():
                self.update_master_text()
                self.refresh_followers()
            return
        self.after(
            500,
            lambda h=hwnd, g=group, attempts=attempts_left - 1: self.retry_role_id_for_window(
                h,
                g,
                attempts,
            ),
        )

    def selected_or_single_follower_hwnds(self) -> list[int]:
        selected = self.selected_hwnds()
        if selected:
            return selected
        if len(self.followers) == 1:
            return list(self.followers)
        if self.master_hwnd and user32.IsWindow(self.master_hwnd):
            return [self.master_hwnd]
        return []

    def selected_or_master_hwnds(self) -> list[int]:
        selected = self.selected_hwnds()
        if selected:
            return selected
        if self.master_hwnd and user32.IsWindow(self.master_hwnd):
            return [self.master_hwnd]
        return []

    def calibrate_selected_role_id(self) -> None:
        hwnds = self.selected_or_master_hwnds()
        if not hwnds:
            messagebox.showwarning("缺少窗口", "請先點選主窗口，或在同步窗口列表選取要校正的窗口。")
            return
        hwnd = hwnds[0]
        path = self.role_id_capture_path()
        try:
            capture_client_region_to_bmp(hwnd, self.role_id_region(), path)
        except Exception as exc:
            messagebox.showwarning("校正失敗", f"無法截圖左上角角色ID區域：{exc}")
            return
        initial = self.role_ids.get(hwnd, "")
        if initial in ("未讀取", "未校正"):
            initial = ""
        role_id = simpledialog.askstring(
            "校正角色ID",
            "請輸入這個窗口的角色ID：",
            initialvalue=initial,
            parent=self,
        )
        if role_id is None:
            return
        ok, message = save_role_id_template(role_id, path)
        if not ok:
            messagebox.showwarning("校正失敗", message)
            return
        cleaned = clean_role_id_text(role_id)
        self.role_ids[hwnd] = cleaned
        self.update_master_text()
        self.refresh_followers()
        self.write_log(message)

    def read_selected_role_ids(self) -> None:
        hwnds = self.selected_or_single_follower_hwnds()
        if not hwnds:
            messagebox.showwarning("缺少選取", "請先選取同步窗口；若只有一個同步窗口，程式會自動選它。")
            return
        for hwnd in hwnds:
            self.read_role_id_for_window(hwnd, log_success=True)
        self.update_master_text()
        self.refresh_followers()

    def clear_role_id_overlay(self) -> None:
        for window in self.role_id_overlay_windows:
            try:
                window.destroy()
            except Exception:
                pass
        self.role_id_overlay_windows = []

    def show_role_id_region_overlay(self, duration_ms: int = 4000) -> None:
        self.clear_role_id_overlay()
        hwnds = self.selected_or_single_follower_hwnds()
        if not hwnds:
            messagebox.showwarning("缺少窗口", "請先點選主窗口，或在同步窗口列表選取要顯示框線的窗口。")
            return
        hwnd = hwnds[0]
        if not user32.IsWindow(hwnd):
            messagebox.showwarning("窗口不存在", "選取的窗口已不存在，請重新點選。")
            return
        left, top, right, bottom = self.role_id_region()
        screen_left_top = client_to_screen(hwnd, left, top)
        screen_right_bottom = client_to_screen(hwnd, right, bottom)
        x1, y1 = screen_left_top.x, screen_left_top.y
        x2, y2 = screen_right_bottom.x, screen_right_bottom.y
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        thickness = 3
        parts = [
            (x1, y1, width, thickness),
            (x1, y2 - thickness, width, thickness),
            (x1, y1, thickness, height),
            (x2 - thickness, y1, thickness, height),
        ]
        for part_x, part_y, part_width, part_height in parts:
            overlay = tk.Toplevel(self)
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            try:
                overlay.attributes("-toolwindow", True)
            except Exception:
                pass
            overlay.configure(bg="#ff2020")
            overlay.geometry(f"{part_width}x{part_height}{part_x:+d}{part_y:+d}")
            self.role_id_overlay_windows.append(overlay)
        self.write_log(
            f"角色ID框線：X={left}, Y={top}, 寬={right-left}, 高={bottom-top}"
        )
        self.after(duration_ms, self.clear_role_id_overlay)

    def refresh_followers(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        valid = []
        for hwnd in self.followers:
            if not user32.IsWindow(hwnd):
                continue
            valid.append(hwnd)

        group = self.current_group()

        def insert_live_row(hwnd: int) -> None:
            setting = self.offsets.setdefault(hwnd, OffsetSetting())
            role_id = self.window_display_name_for_group(group, hwnd)
            rect = get_window_rect(hwnd)
            rect_text = f"{rect.left},{rect.top},{rect.right},{rect.bottom}"
            delay_ms = self.window_delay_ms(group, hwnd)
            self.tree.insert(
                "",
                "end",
                iid=str(hwnd),
                values=(
                    "☑" if setting.enabled else "☐",
                    role_id,
                    rect_text,
                    setting.dx,
                    delay_ms,
                ),
            )

        shown: set[int] = set()
        if group.launch_entries:
            for index, entry in enumerate(group.launch_entries[1:], start=1):
                hwnd = int(group.launch_hwnds.get(index, 0) or 0)
                if hwnd and hwnd in valid and user32.IsWindow(hwnd):
                    insert_live_row(hwnd)
                    shown.add(hwnd)
                    continue
                rect_text = f"{entry.x},{entry.y},{entry.width},{entry.height}"
                self.tree.insert(
                    "",
                    "end",
                    iid=f"launch:{index}",
                    values=(
                        "",
                        f"未開啟：{self.launch_entry_display_name(entry)}",
                        rect_text,
                        "",
                        entry.delay_ms,
                    ),
                )

        for hwnd in valid:
            if hwnd in shown:
                continue
            insert_live_row(hwnd)
        for hwnd in list(self.offsets):
            if hwnd not in valid:
                self.offsets.pop(hwnd, None)
        for hwnd in list(self.role_ids):
            if hwnd not in valid:
                self.role_ids.pop(hwnd, None)
        self.followers = valid

    def remove_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        selected_ids: set[int] = set()
        for item in selected:
            try:
                selected_ids.add(int(item))
            except ValueError:
                continue
        if not selected_ids:
            messagebox.showwarning("缺少選取", "未開啟的啟動記錄請在「組別啟動設定」中移除。")
            return
        self.followers = [h for h in self.followers if h not in selected_ids]
        for hwnd in selected_ids:
            self.offsets.pop(hwnd, None)
            self.role_ids.pop(hwnd, None)
        self.sync_launch_hwnds_from_group_windows()
        self.refresh_followers()
        self.write_log("已移除選取窗口。")

    def clear_followers(self) -> None:
        if self.running:
            self.write_log("同步中不能清空，請先停止。")
            return
        self.followers = []
        self.offsets.clear()
        self.role_ids.clear()
        self.sync_launch_hwnds_from_group_windows()
        self.refresh_followers()
        self.write_log("已清空同步窗口。")

    def window_size_values(self) -> tuple[int, int]:
        width = self.int_from_text(
            self.window_size_width_text.get(),
            DEFAULT_FLASH_CLIENT_WIDTH,
            200,
            5000,
        )
        height = self.int_from_text(
            self.window_size_height_text.get(),
            DEFAULT_FLASH_CLIENT_HEIGHT,
            200,
            5000,
        )
        self.window_size_width_text.set(str(width))
        self.window_size_height_text.set(str(height))
        return width, height

    def load_master_window_size(self) -> None:
        if not self.master_hwnd or not user32.IsWindow(self.master_hwnd):
            messagebox.showwarning("缺少主窗口", "請先點選主窗口。")
            return
        width, height = get_client_size(self.master_hwnd)
        if width <= 0 or height <= 0:
            messagebox.showwarning("讀取失敗", "無法讀取主窗口尺寸。")
            return
        self.window_size_width_text.set(str(width))
        self.window_size_height_text.set(str(height))
        self.write_log(f"已讀取主窗口尺寸：{width}x{height}")

    def apply_window_size_to_hwnds(self, hwnds: list[int], label: str) -> int:
        width, height = self.window_size_values()
        unique_hwnds: list[int] = []
        for hwnd in hwnds:
            if hwnd and hwnd not in unique_hwnds:
                unique_hwnds.append(hwnd)
        applied = 0
        for hwnd in unique_hwnds:
            if not user32.IsWindow(hwnd):
                continue
            client_width, client_height = get_client_size(hwnd)
            if client_width == width and client_height == height:
                continue
            if set_window_client_size(hwnd, width, height):
                applied += 1
        self.refresh_group_ui()
        if applied:
            self.write_log(f"{label}：已套用 {applied} 個窗口為 {width}x{height}。")
        else:
            self.write_log(f"{label}：沒有需要調整的窗口。")
        return applied

    def apply_window_size_to_current_group(self) -> None:
        windows = self.broadcast_windows()
        if not windows:
            messagebox.showwarning("缺少窗口", "目前同步組沒有可套用的窗口。")
            return
        self.apply_window_size_to_hwnds(windows, "目前同步組")

    def apply_window_size_to_all_flash(self) -> None:
        windows = enumerate_flash_windows()
        if not windows:
            messagebox.showwarning("找不到 Flash", "目前找不到 Flash 視窗。")
            return
        self.apply_window_size_to_hwnds(windows, "全部 Flash 視窗")

    def toggle_auto_resize_flash(self) -> None:
        if self.auto_resize_flash.get():
            self.auto_resize_known.clear()
            width, height = self.window_size_values()
            self.write_log(f"新視窗自動套用已開啟：Flash 視窗會調整為 {width}x{height}。")
            self.poll_auto_resize_flash()
        else:
            if self.auto_resize_after_id:
                try:
                    self.after_cancel(self.auto_resize_after_id)
                except Exception:
                    pass
                self.auto_resize_after_id = None
            self.auto_resize_known.clear()
            self.write_log("新視窗自動套用已關閉。")

    def schedule_auto_resize_flash(self) -> None:
        if self.auto_resize_flash.get():
            self.auto_resize_after_id = self.after(1000, self.poll_auto_resize_flash)

    def poll_auto_resize_flash(self) -> None:
        self.auto_resize_after_id = None
        if not self.auto_resize_flash.get():
            return
        width, height = self.window_size_values()
        windows = enumerate_flash_windows()
        live_set = set(windows)
        for hwnd in list(self.auto_resize_known):
            if hwnd not in live_set:
                self.auto_resize_known.pop(hwnd, None)
        changed = 0
        for hwnd in windows:
            if not user32.IsWindow(hwnd):
                continue
            client_width, client_height = get_client_size(hwnd)
            if (client_width, client_height) == (width, height):
                self.auto_resize_known[hwnd] = (width, height)
                continue
            if set_window_client_size(hwnd, width, height):
                changed += 1
                self.auto_resize_known[hwnd] = (width, height)
        if changed:
            self.refresh_group_ui()
            self.write_log(f"已自動調整 {changed} 個 Flash 視窗為 {width}x{height}。")
        self.schedule_auto_resize_flash()

    def selected_hwnds(self) -> list[int]:
        selected = []
        for item in self.tree.selection():
            try:
                selected.append(int(item))
            except ValueError:
                continue
        return selected

    def on_window_selected(self, _event=None) -> None:
        hwnds = self.selected_hwnds()
        if not hwnds:
            return
        setting = self.offsets.setdefault(hwnds[0], OffsetSetting())
        self.offset_x_text.set(str(setting.dx))
        self.offset_y_text.set(str(setting.dy))
        self.delay_ms_text.set(str(self.window_delay_ms(self.current_group(), hwnds[0])))

    def first_follower_hwnd(self) -> int | None:
        for hwnd in self.followers:
            if user32.IsWindow(hwnd):
                return hwnd
        return None

    def set_first_follower_dx(self, dx: int) -> None:
        hwnd = self.first_follower_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少同步窗口", "請先新增至少一個同步窗口。")
            return
        setting = self.offsets.setdefault(hwnd, OffsetSetting())
        setting.enabled = dx != 0
        setting.dx = dx
        setting.dy = 0
        self.offset_x_text.set(str(setting.dx))
        self.offset_y_text.set("0")
        self.refresh_followers()
        if user32.IsWindow(hwnd):
            self.tree.selection_set(str(hwnd))
        self.write_log(f"第一個同步視窗左右偏移：X={setting.dx}")

    def adjust_first_follower_dx(self, delta: int) -> None:
        hwnd = self.first_follower_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少同步窗口", "請先新增至少一個同步窗口。")
            return
        current = self.offsets.setdefault(hwnd, OffsetSetting()).dx
        self.set_first_follower_dx(current + delta)

    def apply_first_follower_dx_from_text(self) -> None:
        try:
            dx = int(self.offset_x_text.get())
        except ValueError:
            messagebox.showwarning("格式錯誤", "左右偏移請輸入整數。")
            return
        self.set_first_follower_dx(dx)

    def reset_first_follower_dx(self) -> None:
        self.set_first_follower_dx(0)

    def apply_selected_delay_from_text(self) -> None:
        try:
            delay_ms = int(self.delay_ms_text.get())
        except ValueError:
            messagebox.showwarning("格式錯誤", "延遲 ms 請輸入整數。")
            return
        delay_ms = max(0, min(5000, delay_ms))
        hwnds = self.selected_hwnds()
        if not hwnds:
            hwnd = self.first_follower_hwnd()
            hwnds = [hwnd] if hwnd else []
        if not hwnds:
            messagebox.showwarning("缺少同步窗口", "請先選取同步窗口。")
            return
        group = self.current_group()
        for hwnd in hwnds:
            if hwnd and user32.IsWindow(hwnd):
                self.set_window_delay_ms(group, hwnd, delay_ms)
        self.delay_ms_text.set(str(delay_ms))
        self.save_launch_config()
        self.refresh_followers()
        self.write_log(f"已設定 {len(hwnds)} 個同步窗口延遲：{delay_ms} ms")

    def on_window_double_click(self, event) -> None:
        if self.tree.identify_column(event.x) == "#1":
            row_id = self.tree.identify_row(event.y)
            if row_id:
                self.tree.selection_set(row_id)
                self.toggle_selected_offsets()

    def capture_offset_base_point(self) -> None:
        if not self.master_hwnd or not user32.IsWindow(self.master_hwnd):
            messagebox.showwarning("缺少主窗口", "請先抓取主窗口。")
            return
        self.write_log("設定主基準點：3 秒後抓取滑鼠位置。")
        self.after(3000, self._set_offset_base_from_cursor)

    def _set_offset_base_from_cursor(self) -> None:
        if not self.master_hwnd or not user32.IsWindow(self.master_hwnd):
            self.write_log("主窗口不存在，無法設定基準點。")
            return
        pos = get_cursor_pos()
        inside, x, y = point_in_client(self.master_hwnd, pos.x, pos.y)
        if not inside:
            self.write_log("基準點沒有落在主窗口內，請再試一次。")
            return
        self.offset_base_point = (x, y)
        self.write_log(f"主基準點已設定：X={x}, Y={y}")

    def capture_selected_offset_target(self) -> None:
        hwnds = self.selected_hwnds()
        if len(hwnds) != 1:
            messagebox.showwarning("請選一個窗口", "請先在列表中選取一個同步窗口。")
            return
        if not self.offset_base_point:
            messagebox.showwarning("缺少基準點", "請先設定主基準點。")
            return
        hwnd = hwnds[0]
        if not user32.IsWindow(hwnd):
            self.write_log("選取窗口不存在，請重新新增。")
            self.refresh_followers()
            return
        self._countdown(
            "請把滑鼠移到選取窗口的目標點，例如另一個修練中，3 秒後抓取。",
            lambda h=hwnd: self._set_selected_offset_target_from_cursor(h),
        )

    def _set_selected_offset_target_from_cursor(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            self.write_log("選取窗口不存在，無法設定偏移。")
            self.refresh_followers()
            return
        if not self.offset_base_point:
            self.write_log("缺少主基準點，無法設定偏移。")
            return
        pos = get_cursor_pos()
        inside, x, y = point_in_client(hwnd, pos.x, pos.y)
        if not inside:
            self.write_log("目標點沒有落在選取窗口內，請再試一次。")
            return
        base_x, base_y = self.offset_base_point
        setting = self.offsets.setdefault(hwnd, OffsetSetting())
        setting.enabled = True
        setting.dx = x - base_x
        setting.dy = 0
        self.offset_x_text.set(str(setting.dx))
        self.offset_y_text.set("0")
        self.refresh_followers()
        self.tree.selection_set(str(hwnd))
        self.write_log(
            f"已設定左右偏移：0x{hwnd:08X}  X={setting.dx}"
        )

    def toggle_selected_offsets(self) -> None:
        hwnds = self.selected_hwnds()
        if not hwnds:
            messagebox.showwarning("請選窗口", "請先在列表中選取同步窗口。")
            return
        for hwnd in hwnds:
            setting = self.offsets.setdefault(hwnd, OffsetSetting())
            setting.enabled = not setting.enabled
        self.refresh_followers()
        for hwnd in hwnds:
            if user32.IsWindow(hwnd):
                self.tree.selection_add(str(hwnd))
        self.on_window_selected()
        self.write_log("已切換選取窗口的偏移啟用狀態。")

    def apply_offset_to_selected(self) -> None:
        hwnds = self.selected_hwnds()
        if not hwnds:
            messagebox.showwarning("請選窗口", "請先在列表中選取同步窗口。")
            return
        try:
            dx = int(self.offset_x_text.get())
            dy = int(self.offset_y_text.get())
        except ValueError:
            messagebox.showwarning("格式錯誤", "X 和 Y 請輸入整數。")
            return
        for hwnd in hwnds:
            setting = self.offsets.setdefault(hwnd, OffsetSetting())
            setting.enabled = True
            setting.dx = dx
            setting.dy = dy
        self.refresh_followers()
        for hwnd in hwnds:
            if user32.IsWindow(hwnd):
                self.tree.selection_add(str(hwnd))
        self.write_log(f"已套用偏移到選取窗口：X={dx}, Y={dy}")

    def clear_selected_offsets(self) -> None:
        hwnds = self.selected_hwnds()
        if not hwnds:
            messagebox.showwarning("請選窗口", "請先在列表中選取同步窗口。")
            return
        for hwnd in hwnds:
            self.offsets[hwnd] = OffsetSetting()
        self.offset_x_text.set("0")
        self.offset_y_text.set("0")
        self.refresh_followers()
        for hwnd in hwnds:
            if user32.IsWindow(hwnd):
                self.tree.selection_add(str(hwnd))
        self.write_log("已清除選取窗口偏移。")

    def game_time_hwnd(self) -> int | None:
        hwnds = self.selected_hwnds()
        if hwnds:
            hwnd = hwnds[0]
            if user32.IsWindow(hwnd):
                return hwnd
        if self.master_hwnd and user32.IsWindow(self.master_hwnd):
            return self.master_hwnd
        return None

    def capture_game_time_point(self, point_number: int) -> None:
        hwnd = self.game_time_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少窗口", "請先抓取主窗口，或在列表選取一個同步窗口。")
            return
        self._countdown(
            "請把滑鼠移到時鐘圖示上，3 秒後抓取伺服器時間。",
            lambda n=point_number, h=hwnd: self._set_game_time_point(n, h),
        )

    def capture_and_read_game_time_at_cursor(self) -> None:
        hwnd = self.game_time_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少窗口", "請先抓取主窗口，或在列表選取一個同步窗口。")
            return
        self._countdown(
            "請把滑鼠移到時鐘圖示上，3 秒後直接讀取伺服器時間。",
            lambda h=hwnd: self._set_game_time_point_and_read(h),
        )

    def _set_game_time_point_and_read(self, hwnd: int) -> None:
        if self._set_game_time_point_from_cursor(hwnd):
            self.read_selected_game_time(log_success=True)

    def capture_and_calibrate_game_time_at_cursor(self) -> None:
        hwnd = self.game_time_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少窗口", "請先抓取主窗口，或在列表選取一個同步窗口。")
            return
        self._countdown(
            "請把滑鼠移到時鐘圖示上，3 秒後直接校正伺服器時間。",
            lambda h=hwnd: self._set_game_time_point_and_calibrate(h),
        )

    def _set_game_time_point_and_calibrate(self, hwnd: int) -> None:
        if self._set_game_time_point_from_cursor(hwnd):
            self.calibrate_game_time_templates()

    def _set_game_time_point_from_cursor(self, hwnd: int) -> bool:
        if not user32.IsWindow(hwnd):
            self.write_log("窗口不存在，無法設定時間區域。")
            return False
        pos = get_cursor_pos()
        inside, x, y = point_in_client(hwnd, pos.x, pos.y)
        if not inside:
            self.write_log("時間位置沒有落在選取窗口內，請再試一次。")
            return False
        self.game_time_hover_point = (x, y)
        client_rect = get_client_rect(hwnd)
        search_left = max(0, x - GAME_TIME_CURSOR_SEARCH_LEFT)
        search_top = max(0, y - GAME_TIME_CURSOR_SEARCH_TOP)
        search_right = min(client_rect.right, x + GAME_TIME_CURSOR_SEARCH_RIGHT)
        search_bottom = min(client_rect.bottom, y + GAME_TIME_CURSOR_SEARCH_BOTTOM)
        if search_right - search_left < 20 or search_bottom - search_top < 12:
            self.write_log("滑鼠附近可擷取範圍太小，請再靠近伺服器時間。")
            return False

        folder = os.path.dirname(__file__)
        search_path = os.path.join(folder, "game_time_search.bmp")
        try:
            capture_client_region_to_bmp(
                hwnd,
                (search_left, search_top, search_right, search_bottom),
                search_path,
            )
        except Exception as exc:
            self.write_log(f"搜尋時間區域失敗：{exc}")
            return False

        local_rect = locate_time_line_rect_in_image(search_path, y - search_top)
        if local_rect:
            left, top, right, bottom = local_rect
            rect = (
                search_left + left,
                search_top + top,
                search_left + right,
                search_top + bottom,
            )
        else:
            rect_left = x + GAME_TIME_TRIGGER_OFFSET_X
            rect_top = y + GAME_TIME_TRIGGER_OFFSET_Y
            rect = (
                rect_left,
                rect_top,
                min(client_rect.right, rect_left + GAME_TIME_REGION_WIDTH),
                min(client_rect.bottom, rect_top + GAME_TIME_REGION_HEIGHT),
            )
            self.write_log("自動搜尋未命中，改用時鐘圖示固定偏移定位伺服器時間。")
        if rect[2] - rect[0] < 20 or rect[3] - rect[1] < 8:
            self.write_log("時間區域太小，請把滑鼠放在時鐘圖示中間再試一次。")
            return False
        self.game_time_point1 = (rect[0], rect[1])
        self.game_time_point2 = (rect[2], rect[3])
        self.game_time_rect = rect
        self.write_log(
            f"已自動定位伺服器時間區域：X={rect[0]}, Y={rect[1]}, 寬={rect[2] - rect[0]}, 高={rect[3] - rect[1]}"
        )
        self.show_game_time_region_overlay()
        return True

    def trigger_game_time_hover(self, hwnd: int) -> None:
        if not self.game_time_hover_point:
            return
        hover_x, hover_y = self.game_time_hover_point
        try:
            target_hwnd, local_x, local_y = child_at_client_point(hwnd, hover_x, hover_y)
            user32.PostMessageW(target_hwnd, WM_MOUSEMOVE, 0, make_lparam(local_x, local_y))
            user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, make_lparam(hover_x, hover_y))
            time.sleep(0.03)
        except Exception:
            pass

    def _set_game_time_point(self, point_number: int, hwnd: int) -> None:
        if point_number == 1:
            self._set_game_time_point_from_cursor(hwnd)
            return
        if not user32.IsWindow(hwnd):
            self.write_log("窗口不存在，無法設定時間區域。")
            return
        pos = get_cursor_pos()
        inside, x, y = point_in_client(hwnd, pos.x, pos.y)
        if not inside:
            self.write_log("時間位置沒有落在選取窗口內，請再試一次。")
            return
        x += GAME_TIME_TRIGGER_OFFSET_X
        y += GAME_TIME_TRIGGER_OFFSET_Y
        self.game_time_point2 = (x, y)
        self.write_log(f"遊戲時間右下角已設定：X={x}, Y={y}")
        if self.game_time_point1 and self.game_time_point2:
            rect = normalize_rect_points(self.game_time_point1, self.game_time_point2)
            if rect[2] - rect[0] < 4 or rect[3] - rect[1] < 4:
                self.write_log("時間區域太小，請重新設定兩個角。")
                self.game_time_rect = None
                return
            self.game_time_rect = rect
            self.write_log(
                f"遊戲時間區域已設定：{rect[0]},{rect[1]},{rect[2]},{rect[3]}"
            )
            self.show_game_time_region_overlay()

    def apply_game_time_size(self, log_only: bool = False) -> None:
        if not self.game_time_point1:
            if not log_only:
                messagebox.showwarning("缺少左上角", "請先設定時間左上角。")
            return
        width = GAME_TIME_REGION_WIDTH
        height = GAME_TIME_REGION_HEIGHT
        x, y = self.game_time_point1
        self.game_time_point2 = (x + width, y + height)
        self.game_time_rect = normalize_rect_points(self.game_time_point1, self.game_time_point2)
        self.write_log(
            f"已用寬高設定遊戲時間區域：X={x}, Y={y}, 寬={width}, 高={height}"
        )
        self.show_game_time_region_overlay()

    def clear_game_time_overlay(self) -> None:
        for window in self.game_time_overlay_windows:
            try:
                window.destroy()
            except Exception:
                pass
        self.game_time_overlay_windows = []

    def show_game_time_region_overlay(self, duration_ms: int = 2500) -> None:
        self.clear_game_time_overlay()
        hwnd = self.game_time_hwnd()
        if not hwnd or not self.game_time_rect:
            messagebox.showwarning("缺少時間區域", "請先設定時間位置。")
            return

        left, top, right, bottom = self.game_time_rect
        screen_left_top = client_to_screen(hwnd, left, top)
        screen_right_bottom = client_to_screen(hwnd, right, bottom)
        x = int(screen_left_top.x)
        y = int(screen_left_top.y)
        width = max(1, int(screen_right_bottom.x - screen_left_top.x))
        height = max(1, int(screen_right_bottom.y - screen_left_top.y))
        thickness = 2
        border_parts = [
            (x - thickness, y - thickness, width + thickness * 2, thickness),
            (x - thickness, y + height, width + thickness * 2, thickness),
            (x - thickness, y, thickness, height),
            (x + width, y, thickness, height),
        ]
        for part_x, part_y, part_width, part_height in border_parts:
            overlay = tk.Toplevel(self)
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            try:
                overlay.attributes("-toolwindow", True)
            except tk.TclError:
                pass
            overlay.configure(bg="#ff2020")
            overlay.geometry(f"{part_width}x{part_height}{part_x:+d}{part_y:+d}")
            self.game_time_overlay_windows.append(overlay)
        self.write_log(f"已顯示時間框線：{x},{y},{x + width},{y + height}")
        self.after(duration_ms, self.clear_game_time_overlay)

    def capture_game_time_image(self) -> str | None:
        hwnd = self.game_time_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少窗口", "請先抓取主窗口，或在列表選取一個同步窗口。")
            return None
        if not self.game_time_rect:
            messagebox.showwarning("缺少時間區域", "請先設定遊戲時間位置。")
            return None

        folder = os.path.dirname(__file__)
        image_path = os.path.join(folder, "game_time_capture.bmp")
        try:
            self.trigger_game_time_hover(hwnd)
            capture_client_region_to_bmp(hwnd, self.game_time_rect, image_path)
        except Exception as exc:
            self.write_log(f"截取遊戲時間失敗：{exc}")
            return None
        return image_path

    def calibrate_game_time_templates(self) -> None:
        image_path = self.capture_game_time_image()
        if not image_path:
            return
        ok, message = save_time_templates_from_image(
            image_path, self.game_time_sample_text.get()
        )
        if ok:
            seed_minutes = self.time_text_to_minutes(self.game_time_sample_text.get())
            self.reset_game_time_anchor(seed_minutes)
            baseline_ok = self.set_game_time_baseline_from_image(image_path)
            if seed_minutes is not None:
                self.game_time_text.set(
                    f"遊戲時間：{self.minutes_to_time_text(seed_minutes)}（等跳分校準）"
                )
            self.write_log(message)
            if baseline_ok:
                self.write_log("已建立時間基準圖；請開啟自動更新，等畫面跳到下一分鐘後會自動校準秒數。")
            else:
                self.write_log("校正完成，但時間基準圖不清楚；請把滑鼠放在伺服器時間那一行重新校正。")
        else:
            self.write_log(message)
            messagebox.showwarning("校正失敗", message)

    def time_text_to_minutes(self, value: str) -> int | None:
        cleaned = clean_time_sample(value)
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", cleaned)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return hour * 60 + minute

    def minutes_to_time_text(self, minutes: int) -> str:
        minutes %= DAY_MINUTES
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def reset_game_time_anchor(self, seed_minutes: int | None = None) -> None:
        self.game_time_anchor_minutes = None
        self.game_time_anchor_perf = None
        self.game_time_last_read_minutes = seed_minutes
        self.game_time_baseline_signature = None
        self.game_time_baseline_count = None
        self.game_time_candidate_signature = None
        self.game_time_candidate_count = None
        self.game_time_candidate_seen = 0
        self.game_time_candidate_perf = None

    def set_game_time_baseline_from_image(self, image_path: str) -> bool:
        signature = time_image_signature(image_path)
        if not signature:
            return False
        self.game_time_baseline_signature, self.game_time_baseline_count = signature
        self.game_time_candidate_signature = None
        self.game_time_candidate_count = None
        self.game_time_candidate_seen = 0
        self.game_time_candidate_perf = None
        return True

    def update_game_time_from_image_change(self, image_path: str) -> str:
        if self.game_time_baseline_signature is None or self.game_time_last_read_minutes is None:
            return "no_baseline"
        if self.game_time_anchor_minutes is not None:
            return "ok"
        signature_data = time_image_signature(image_path)
        if not signature_data:
            return "bad_image"
        signature, count = signature_data
        baseline_count = self.game_time_baseline_count or count
        if count < max(8, int(baseline_count * 0.45)) or count > max(20, int(baseline_count * 2.2)):
            return "bad_image"

        diff = signature_distance(self.game_time_baseline_signature, signature)
        if diff < GAME_TIME_CHANGE_MIN_DIFF:
            self.game_time_candidate_signature = None
            self.game_time_candidate_count = None
            self.game_time_candidate_seen = 0
            self.game_time_candidate_perf = None
            return "waiting" if self.game_time_anchor_minutes is None else "ok"
        if diff > GAME_TIME_CHANGE_MAX_DIFF:
            self.game_time_candidate_signature = None
            self.game_time_candidate_count = None
            self.game_time_candidate_seen = 0
            self.game_time_candidate_perf = None
            return "bad_image"

        now = time.perf_counter()
        if signature == self.game_time_candidate_signature:
            self.game_time_candidate_seen += 1
        else:
            self.game_time_candidate_signature = signature
            self.game_time_candidate_count = count
            self.game_time_candidate_seen = 1
            self.game_time_candidate_perf = now

        candidate_age = now - (self.game_time_candidate_perf or now)
        if (
            self.game_time_candidate_seen < GAME_TIME_CHANGE_STABLE_READS
            or candidate_age < GAME_TIME_CHANGE_STABLE_SECONDS
        ):
            return "candidate"

        new_minutes = (self.game_time_last_read_minutes + 1) % DAY_MINUTES
        self.game_time_anchor_minutes = new_minutes
        self.game_time_anchor_perf = self.game_time_candidate_perf or now
        self.game_time_last_read_minutes = new_minutes
        self.game_time_baseline_signature = self.game_time_candidate_signature
        self.game_time_baseline_count = self.game_time_candidate_count
        self.game_time_candidate_signature = None
        self.game_time_candidate_count = None
        self.game_time_candidate_seen = 0
        self.game_time_candidate_perf = None
        return "synced"

    def update_game_time_anchor(self, value: str) -> str:
        minutes = self.time_text_to_minutes(value)
        if minutes is None:
            return "invalid"
        now = time.perf_counter()
        previous = self.game_time_last_read_minutes
        if previous is not None and minutes not in (previous, (previous + 1) % DAY_MINUTES):
            return "ignored"

        if self.game_time_anchor_minutes is None:
            self.game_time_last_read_minutes = minutes
            if previous is not None and minutes == (previous + 1) % DAY_MINUTES:
                self.game_time_anchor_minutes = minutes
                self.game_time_anchor_perf = now
                return "synced"
            return "waiting"

        estimated_ms = self.estimated_game_time_ms()
        estimated_minutes = (estimated_ms // 60000) % DAY_MINUTES if estimated_ms is not None else minutes
        if minutes == estimated_minutes:
            self.game_time_last_read_minutes = minutes
            return "ok"
        if minutes == (estimated_minutes + 1) % DAY_MINUTES:
            self.game_time_anchor_minutes = minutes
            self.game_time_anchor_perf = now
            self.game_time_last_read_minutes = minutes
            return "synced"

        self.game_time_last_read_minutes = minutes
        return "ignored"

    def system_time_offset_ms(self) -> int:
        try:
            offset = int(self.system_time_offset_ms_text.get())
        except ValueError:
            return 0
        return max(-60000, min(60000, offset))

    def system_game_time_ms(self) -> int:
        now_ns = time.time_ns()
        now_seconds = now_ns // 1_000_000_000
        local = time.localtime(now_seconds)
        total_ms = (
            ((local.tm_hour * 60 + local.tm_min) * 60 + local.tm_sec) * 1000
            + (now_ns // 1_000_000) % 1000
        )
        return (total_ms + self.system_time_offset_ms()) % (24 * 60 * 60000)

    def game_time_ms_to_text(self, total_ms: int) -> str:
        total_ms %= 24 * 60 * 60000
        hour = total_ms // 3600000
        minute = (total_ms // 60000) % 60
        second = (total_ms // 1000) % 60
        milli = total_ms % 1000
        return f"{hour:02d}:{minute:02d}:{second:02d}.{milli:03d}"

    def estimated_game_time_text(self) -> str | None:
        return self.game_time_ms_to_text(self.system_game_time_ms())

    def estimated_game_time_ms(self) -> int | None:
        return self.system_game_time_ms()

    def update_estimated_game_time_label(self) -> None:
        estimated = self.estimated_game_time_text()
        if estimated:
            self.game_time_text.set(f"遊戲時間：{estimated}")

    def schedule_game_time_tick(self) -> None:
        if self.game_time_tick_after_id:
            try:
                self.after_cancel(self.game_time_tick_after_id)
            except Exception:
                pass
        self.game_time_tick_after_id = self.after(33, self.poll_game_time_tick)

    def poll_game_time_tick(self) -> None:
        self.game_time_tick_after_id = None
        self.update_estimated_game_time_label()
        if self.auto_game_time.get():
            self.schedule_game_time_tick()

    def read_selected_game_time(self, log_success: bool = True) -> str | None:
        image_path = self.capture_game_time_image()
        if not image_path:
            return None

        change_status = self.update_game_time_from_image_change(image_path)
        if change_status != "no_baseline":
            if change_status == "bad_image":
                if log_success:
                    self.game_time_text.set("遊戲時間：讀取失敗")
                    self.write_log("滑鼠附近沒有穩定的伺服器時間畫面，請重新用滑鼠處校正。")
                return None
            if change_status == "synced":
                self.update_estimated_game_time_label()
                try:
                    save_time_templates_from_image(
                        image_path,
                        self.minutes_to_time_text(self.game_time_last_read_minutes or 0),
                    )
                except Exception:
                    pass
                if log_success:
                    self.write_log("已偵測到分鐘跳動，秒數校準完成。")
                return self.minutes_to_time_text(self.game_time_last_read_minutes or 0)
            if self.game_time_anchor_minutes is None:
                if self.game_time_last_read_minutes is not None:
                    self.game_time_text.set(
                        f"遊戲時間：{self.minutes_to_time_text(self.game_time_last_read_minutes)}（等跳分校準）"
                    )
                if log_success:
                    self.write_log("尚未跳到下一分鐘，繼續等待校準。")
                return self.minutes_to_time_text(self.game_time_last_read_minutes or 0)
            self.update_estimated_game_time_label()
            return self.estimated_game_time_text()

        value, detail = read_time_text_from_image(image_path)
        if value:
            anchor_status = self.update_game_time_anchor(value)
            minutes = self.time_text_to_minutes(value)
            if anchor_status == "invalid":
                if log_success:
                    self.write_log(f"讀取結果不是標準時間：{value}")
                return None
            if anchor_status == "ignored":
                if log_success:
                    self.write_log(f"忽略不連續的遊戲時間讀取：{value}")
                return None
            if anchor_status == "waiting":
                if minutes is not None:
                    self.game_time_text.set(
                        f"遊戲時間：{self.minutes_to_time_text(minutes)}（等跳分校準）"
                    )
            else:
                self.update_estimated_game_time_label()
            if log_success or anchor_status == "synced":
                try:
                    save_time_templates_from_image(image_path, value)
                except Exception:
                    pass
            if log_success:
                self.write_log(f"讀取遊戲時間：{value}")
                if detail and detail != value:
                    self.write_log(str(detail))
                if anchor_status == "synced":
                    self.write_log("已偵測到分鐘跳動，秒數校準完成。")
            return value
        else:
            if log_success:
                self.game_time_text.set("遊戲時間：讀取失敗")
                self.write_log(f"讀取遊戲時間失敗：{detail} 截圖：{image_path}")
            return None

    def toggle_auto_game_time(self) -> None:
        if self.auto_game_time.get():
            self.write_log("自動更新遊戲時間已開啟：使用系統時間，不再讀取畫面或 RAM。")
            self.update_estimated_game_time_label()
            self.schedule_game_time_tick()
        else:
            self.write_log("自動更新遊戲時間已關閉。")
            if self.game_time_auto_after_id:
                try:
                    self.after_cancel(self.game_time_auto_after_id)
                except Exception:
                    pass
                self.game_time_auto_after_id = None
            if self.game_time_tick_after_id:
                try:
                    self.after_cancel(self.game_time_tick_after_id)
                except Exception:
                    pass
                self.game_time_tick_after_id = None

    def schedule_game_time_read(self) -> None:
        if not self.auto_game_time.get():
            return
        try:
            interval = int(self.game_time_poll_ms_text.get())
        except ValueError:
            interval = 50
        interval = max(50, min(2000, interval))
        self.game_time_auto_after_id = self.after(interval, self.poll_game_time_read)

    def poll_game_time_read(self) -> None:
        self.game_time_auto_after_id = None
        if not self.auto_game_time.get():
            return
        self.read_selected_game_time(log_success=False)
        self.schedule_game_time_read()

    def parse_target_time_ms(self, text: str) -> int | None:
        compact = re.fullmatch(r"\s*(\d{3,6}|\d{8,9})\s*", text)
        if compact:
            digits = compact.group(1)
            milli = 0
            if len(digits) <= 4:
                hour = int(digits[:-2])
                minute = int(digits[-2:])
                second = 0
            elif len(digits) <= 6:
                hour = int(digits[:-4])
                minute = int(digits[-4:-2])
                second = int(digits[-2:])
            else:
                main = digits[:-3]
                milli = int(digits[-3:])
                hour = int(main[:-4])
                minute = int(main[-4:-2])
                second = int(main[-2:])
            if hour > 23 or minute > 59 or second > 59:
                return None
            return ((hour * 60 + minute) * 60 + second) * 1000 + milli

        match = re.fullmatch(
            r"\s*(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?\s*",
            text,
        )
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3) or 0)
        milli_text = (match.group(4) or "0").ljust(3, "0")[:3]
        milli = int(milli_text)
        if hour > 23 or minute > 59 or second > 59:
            return None
        return ((hour * 60 + minute) * 60 + second) * 1000 + milli

    def capture_timed_click_point(self) -> None:
        self._countdown(
            "請把滑鼠移到要定時按下的按鈕上，3 秒後抓取。",
            self._set_timed_click_point_from_cursor,
        )

    def _set_timed_click_point_from_cursor(self) -> None:
        hwnd = get_window_under_cursor()
        if not hwnd or int(hwnd) == int(self.winfo_id()):
            self.write_log("沒有抓到有效按鈕窗口，請再試一次。")
            return
        pos = get_cursor_pos()
        inside, x, y = point_in_client(hwnd, pos.x, pos.y)
        if not inside:
            self.write_log("按鈕位置沒有落在窗口內，請再試一次。")
            return
        self.timed_click_hwnd = hwnd
        self.timed_click_point = (x, y)
        self.timed_click_point_text.set(f"按鈕位置：X={x}, Y={y}")
        self.write_log(f"已設定定時按下位置：{window_summary(hwnd)} X={x}, Y={y}")

    def toggle_timed_click(self) -> None:
        if self.timed_click_enabled.get():
            self.enable_timed_click()
        else:
            self.cancel_timed_click()

    def enable_timed_click(self) -> None:
        target_ms = self.parse_target_time_ms(self.timed_click_target_text.get())
        if target_ms is None:
            self.timed_click_enabled.set(False)
            messagebox.showwarning("目標時間格式錯誤", "請輸入 21:37、21:37:00.120，或直接輸入 2137。")
            return
        if not self.timed_click_hwnd or not self.timed_click_point:
            self.timed_click_enabled.set(False)
            messagebox.showwarning("缺少按鈕位置", "請先設定要按下的按鈕位置。")
            return
        if self.estimated_game_time_ms() is None:
            self.timed_click_enabled.set(False)
            messagebox.showwarning("缺少遊戲時間", "請先讀取遊戲時間，並開啟自動更新。")
            return
        if not self.auto_game_time.get():
            self.auto_game_time.set(True)
            self.toggle_auto_game_time()
        self.timed_click_fired = False
        self.write_log("定時按下已啟用。")
        self.schedule_timed_click_poll()

    def cancel_timed_click(self) -> None:
        self.timed_click_enabled.set(False)
        if self.timed_click_after_id:
            try:
                self.after_cancel(self.timed_click_after_id)
            except Exception:
                pass
            self.timed_click_after_id = None
        self.timed_click_status_text.set("定時按下：未啟用")

    def schedule_timed_click_poll(self) -> None:
        if not self.timed_click_enabled.get():
            return
        if self.timed_click_after_id:
            try:
                self.after_cancel(self.timed_click_after_id)
            except Exception:
                pass
        self.timed_click_after_id = self.after(5, self.poll_timed_click)

    def poll_timed_click(self) -> None:
        self.timed_click_after_id = None
        if not self.timed_click_enabled.get() or self.timed_click_fired:
            return
        now_ms = self.estimated_game_time_ms()
        target_ms = self.parse_target_time_ms(self.timed_click_target_text.get())
        if now_ms is None or target_ms is None:
            self.schedule_timed_click_poll()
            return
        try:
            lead_ms = int(self.timed_click_lead_ms_text.get())
        except ValueError:
            lead_ms = 120
        lead_ms = max(0, min(5000, lead_ms))
        day_ms = 24 * 60 * 60000
        click_ms = (target_ms - lead_ms) % day_ms
        remaining = (click_ms - now_ms + day_ms) % day_ms
        if remaining > day_ms // 2:
            self.timed_click_status_text.set("定時按下：目標已過")
            self.timed_click_enabled.set(False)
            return
        if remaining <= 8:
            self.fire_timed_click(now_ms, target_ms, lead_ms)
            return
        self.timed_click_status_text.set(f"定時按下：剩 {remaining} ms")
        self.schedule_timed_click_poll()

    def fire_timed_click(self, now_ms: int, target_ms: int, lead_ms: int) -> None:
        if not self.timed_click_hwnd or not self.timed_click_point:
            self.cancel_timed_click()
            return
        if not user32.IsWindow(self.timed_click_hwnd):
            self.write_log("定時按下失敗：目標窗口不存在。")
            self.cancel_timed_click()
            return
        repeat_count = self.int_from_text(
            self.timed_click_repeat_count_text.get(), 2, 1, 10
        )
        repeat_interval = self.int_from_text(
            self.timed_click_repeat_interval_ms_text.get(), 250, 50, 3000
        )
        for index in range(repeat_count):
            self.after(
                index * repeat_interval,
                lambda h=self.timed_click_hwnd, p=self.timed_click_point: self.send_timed_click_once(h, p),
            )
        self.timed_click_fired = True
        self.timed_click_enabled.set(False)
        delta = now_ms - target_ms
        self.timed_click_status_text.set(f"定時按下：已連點 {repeat_count} 次")
        self.write_log(
            f"定時按下已送出連點；次數 {repeat_count}，間隔 {repeat_interval} ms，提前設定 {lead_ms} ms，目前差值 {delta} ms。"
        )

    def send_timed_click_once(self, hwnd: int, point: tuple[int, int]) -> None:
        if not hwnd or not user32.IsWindow(hwnd):
            return
        x, y = point
        target, local_x, local_y = child_at_client_point(hwnd, x, y)
        user32.PostMessageW(
            target,
            WM_LBUTTONDOWN,
            MK_LBUTTON,
            make_lparam(local_x, local_y),
        )
        user32.PostMessageW(
            target,
            WM_MOUSEMOVE,
            MK_LBUTTON,
            make_lparam(local_x, local_y),
        )
        self.after(
            35,
            lambda h=target, lx=local_x, ly=local_y: user32.PostMessageW(
                h, WM_LBUTTONUP, 0, make_lparam(lx, ly)
            ),
        )

    def int_from_text(self, text: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(text)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    def autoclick_interval_ms(self) -> int:
        return self.int_from_text(self.autoclick_interval_ms_text.get(), 100, 1, 600000)

    def autoclick_repeat_count(self) -> int:
        return self.int_from_text(self.autoclick_repeat_count_text.get(), 1, 1, 999999)

    def toggle_autoclick(self) -> None:
        if self.autoclick_running:
            self.stop_autoclick()
        else:
            self.start_autoclick()

    def start_autoclick(self) -> None:
        if self.autoclick_running:
            return
        self.autoclick_running = True
        self.autoclick_sent_count = 0
        self.autoclick_status_text.set("自動點擊：跟隨滑鼠")
        self.write_log("自動點擊已開始：跟隨滑鼠移動。")
        self.poll_autoclick()

    def stop_autoclick(self) -> None:
        was_running = self.autoclick_running
        self.autoclick_running = False
        if self.autoclick_after_id:
            try:
                self.after_cancel(self.autoclick_after_id)
            except Exception:
                pass
            self.autoclick_after_id = None
        self.autoclick_status_text.set("自動點擊：未啟用")
        if was_running:
            self.write_log("自動點擊已停止。")

    def schedule_autoclick(self, delay_ms: int | None = None) -> None:
        if not self.autoclick_running:
            return
        if self.autoclick_after_id:
            try:
                self.after_cancel(self.autoclick_after_id)
            except Exception:
                pass
        delay = self.autoclick_interval_ms() if delay_ms is None else max(0, delay_ms)
        self.autoclick_after_id = self.after(delay, self.poll_autoclick)

    def poll_autoclick(self) -> None:
        self.autoclick_after_id = None
        if not self.autoclick_running:
            return
        self.perform_autoclick()
        self.autoclick_sent_count += 1
        if (
            not self.autoclick_repeat_forever.get()
            and self.autoclick_sent_count >= self.autoclick_repeat_count()
        ):
            self.stop_autoclick()
            return
        self.autoclick_status_text.set(f"自動點擊：跟隨滑鼠 {self.autoclick_sent_count}")
        self.schedule_autoclick()

    def perform_autoclick(self) -> None:
        if self.autoclick_button_text.get() == "右鍵":
            down_flag = MOUSEEVENTF_RIGHTDOWN
            up_flag = MOUSEEVENTF_RIGHTUP
        else:
            down_flag = MOUSEEVENTF_LEFTDOWN
            up_flag = MOUSEEVENTF_LEFTUP
        user32.mouse_event(down_flag, 0, 0, 0, None)
        user32.mouse_event(up_flag, 0, 0, 0, None)

    def read_selected_game_time_old(self) -> None:
        hwnd = self.game_time_hwnd()
        if not hwnd:
            messagebox.showwarning("缺少窗口", "請先抓取主窗口，或在列表選取一個同步窗口。")
            return
        if not self.game_time_rect:
            messagebox.showwarning("缺少時間區域", "請先設定遊戲時間的左上角與右下角。")
            return

        folder = os.path.dirname(__file__)
        image_path = os.path.join(folder, "game_time_capture.bmp")
        try:
            capture_client_region_to_bmp(hwnd, self.game_time_rect, image_path)
        except Exception as exc:
            self.write_log(f"截取遊戲時間失敗：{exc}")
            return

        value, detail = read_time_text_from_image(image_path)
        if value:
            self.game_time_text.set(f"遊戲時間：{value}")
            self.write_log(f"讀取遊戲時間：{value}")
            if detail and detail != value:
                self.write_log(f"OCR 原文：{detail}")
        else:
            self.game_time_text.set("遊戲時間：讀取失敗")
            self.write_log(f"讀取遊戲時間失敗：{detail} 截圖：{image_path}")

    def any_group_running(self) -> bool:
        return any(group.running for group in self.groups)

    def running_groups(self) -> list[SyncGroup]:
        return [group for group in self.groups if group.running]

    def status_display_parts(self) -> tuple[str, str, SyncGroup]:
        current = self.current_group()
        running = self.running_groups()
        if current.running:
            return current.name, "同步中", current
        if len(running) == 1:
            return running[0].name, "同步中", running[0]
        if len(running) > 1:
            return f"{len(running)}組", "同步中", running[0]
        return current.name, "未開啟", current

    def prune_group_followers(self, group: SyncGroup) -> None:
        valid = [hwnd for hwnd in group.followers if user32.IsWindow(hwnd)]
        for hwnd in list(group.offsets):
            if hwnd not in valid:
                group.offsets.pop(hwnd, None)
        group.followers = valid

    def sync_state_label_for_group(self, group: SyncGroup) -> str:
        return "同步中" if group.running else "未開啟"

    def update_window_title(self) -> None:
        group_name, state, _group = self.status_display_parts()
        title = f"{APP_DISPLAY_NAME} - {group_name} - {state}"
        self.title(title)
        if hasattr(self, "title_status_text"):
            self.title_status_text.set(f"{group_name} - {state}")
        self.update_floating_status()

    def start_sync(
        self,
        group_index: int | None = None,
        skip_prepare: bool = False,
        quiet: bool = False,
    ) -> None:
        group_index = int(self.active_group_index.get()) if group_index is None else group_index
        group = self.groups[group_index]
        if group.running:
            self.update_sync_state_text()
            return
        if group_index in self.pending_sync_start_groups:
            return
        if group.launch_entries:
            self.bind_existing_launch_windows_for_sync(group_index)
        if not group.master_hwnd or not user32.IsWindow(group.master_hwnd):
            if group.launch_entries:
                message = "本組主窗口尚未開啟或尚未配對成功，請先按整理本組。"
                self.write_log(f"{group.name}同步未啟動：{message}")
            else:
                message = "請先抓取主窗口。"
                if quiet:
                    self.write_log(f"{group.name}同步未啟動：{message}")
                else:
                    messagebox.showwarning("缺少主窗口", message)
            self.update_sync_state_text()
            return
        self.prune_group_followers(group)
        if group is self.current_group():
            self.refresh_followers()
        if not group.followers:
            if group.launch_entries:
                message = "本組同步窗口尚未開啟或尚未配對成功，請先按整理本組。"
                self.write_log(f"{group.name}同步未啟動：{message}")
            else:
                message = "請至少新增一個同步窗口。"
                if quiet:
                    self.write_log(f"{group.name}同步未啟動：{message}")
                else:
                    messagebox.showwarning("缺少同步窗口", message)
            self.update_sync_state_text()
            return

        group.running = True
        group.active_buttons.clear()
        group.last_button_pos.clear()
        group.button_state = {
            name: self.is_button_down(vk) for name, vk, _, _, _ in self.enabled_buttons(group)
        }
        group.keyboard_state = {
            custom.display: self.is_button_down(custom.value)
            for custom in self.keyboard_sync_inputs(group)
        }
        self.update_sync_state_text()
        self.write_log(f"{group.name}同步已開始。")
        self.install_mouse_wheel_hook()
        if self.poll_after_id is None:
            self.schedule_poll()

    def stop_sync(self, group_index: int | None = None) -> None:
        group_index = int(self.active_group_index.get()) if group_index is None else group_index
        group = self.groups[group_index]
        was_running = group.running
        group.running = False
        if self.poll_after_id and not self.any_group_running():
            try:
                self.after_cancel(self.poll_after_id)
            except Exception:
                pass
            self.poll_after_id = None
        group.active_buttons.clear()
        group.keyboard_state.clear()
        if not self.any_group_running():
            self.uninstall_mouse_wheel_hook()
        self.update_sync_state_text()
        if was_running:
            self.write_log(f"{group.name}同步已停止。")

    def update_sync_state_text(self) -> None:
        current = self.current_group()
        group_name, state, group = self.status_display_parts()
        if current.running:
            self.status_text.set(f"{current.name}同步狀態：已開啟")
        elif group.running:
            self.status_text.set(f"{group_name}同步狀態：已開啟")
        else:
            self.status_text.set(f"{current.name}同步狀態：{state}")
        self.update_window_title()

    def enabled_buttons(self, group: SyncGroup | None = None) -> list[tuple[str, int, int, int, int]]:
        group = self.current_group() if group is None else group
        if not group.sync_left_enabled:
            return []
        return [("left", VK_LBUTTON, WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON)]

    def keyboard_sync_inputs(self, group: SyncGroup) -> list[CustomInput]:
        if not group.sync_keyboard_enabled:
            return []
        inputs: list[CustomInput] = []
        for display in group.keyboard_key_displays:
            try:
                custom = parse_custom_input(display)
            except ValueError:
                continue
            if custom.kind == "key":
                inputs.append(custom)
        return inputs

    def is_button_down(self, vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def schedule_poll(self) -> None:
        if self.poll_after_id is None:
            self.poll_after_id = self.after(12, self.poll_input)

    def poll_input(self) -> None:
        self.poll_after_id = None
        if not self.any_group_running():
            return
        try:
            self.check_mouse_buttons()
            self.check_keyboard_keys()
        finally:
            if self.any_group_running():
                self.schedule_poll()

    def check_mouse_buttons(self) -> None:
        for group_index, group in enumerate(self.groups):
            if not group.running:
                continue
            if not group.master_hwnd or not user32.IsWindow(group.master_hwnd):
                self.stop_sync(group_index)
                self.write_log(f"{group.name}主窗口已不存在，已停止同步。")
                continue
            inside, client_x, client_y = cursor_point_in_client(group.master_hwnd)
            enabled_buttons = self.enabled_buttons(group)
            if inside:
                for name, *_ in enabled_buttons:
                    group.last_button_pos[name] = (client_x, client_y)

            for name, vk, down_msg, up_msg, _ in enabled_buttons:
                is_down = self.is_button_down(vk)
                was_down = group.button_state.get(name, is_down)
                group.button_state[name] = is_down

                if is_down == was_down:
                    continue

                if is_down:
                    if inside:
                        group.active_buttons.add(name)
                        group.last_button_pos[name] = (client_x, client_y)
                        self.events.put(
                            MouseMirrorEvent(group_index, down_msg, client_x, client_y)
                        )
                else:
                    if name in group.active_buttons:
                        x, y = group.last_button_pos.get(name, (client_x, client_y))
                        self.events.put(MouseMirrorEvent(group_index, up_msg, x, y))
                        group.active_buttons.discard(name)

    def check_keyboard_keys(self) -> None:
        for group_index, group in enumerate(self.groups):
            if not group.running or not group.sync_keyboard_enabled:
                continue
            if not group.master_hwnd or not user32.IsWindow(group.master_hwnd):
                continue
            inputs = self.keyboard_sync_inputs(group)
            if not inputs:
                continue
            for custom in inputs:
                is_down = self.is_button_down(custom.value)
                was_down = group.keyboard_state.get(custom.display, is_down)
                group.keyboard_state[custom.display] = is_down
                if is_down == was_down:
                    continue
                message = WM_KEYDOWN if is_down else WM_KEYUP
                self.events.put(KeyboardMirrorEvent(group_index, message, custom.value))

    def _start_worker(self) -> None:
        def worker() -> None:
            while True:
                event = self.events.get()
                if event is None:
                    return
                try:
                    if isinstance(event, KeyboardMirrorEvent):
                        self.replay_keyboard_background(event)
                    elif isinstance(event, MouseWheelMirrorEvent):
                        self.replay_wheel_background(event)
                    else:
                        self.replay_mouse_background(event)
                except Exception as exc:
                    self.after(0, lambda e=exc: self.write_log(f"同步失敗：{e}"))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def live_followers(self, group: SyncGroup | None = None) -> list[int]:
        group = self.current_group() if group is None else group
        return [h for h in group.followers if user32.IsWindow(h)]

    def adjusted_click_point(
        self, group: SyncGroup, hwnd: int, x: int, y: int
    ) -> tuple[bool, int, int]:
        setting = group.offsets.get(hwnd)
        if setting and setting.enabled:
            x += setting.dx
            y += setting.dy
        rect = get_client_rect(hwnd)
        inside = 0 <= x < rect.right and 0 <= y < rect.bottom
        return inside, x, y

    def run_sync_action(self, delay_ms: int, callback) -> None:
        if delay_ms <= 0:
            callback()
            return
        timer = threading.Timer(delay_ms / 1000.0, callback)
        timer.daemon = True
        timer.start()

    def post_mouse_event_to_follower(
        self,
        group: SyncGroup,
        hwnd: int,
        event: MouseMirrorEvent,
    ) -> None:
        inside, click_x, click_y = self.adjusted_click_point(group, hwnd, event.x, event.y)
        if not inside:
            return
        target, local_x, local_y = child_at_client_point(hwnd, click_x, click_y)
        button_wparam = MK_LBUTTON if event.message == WM_LBUTTONDOWN else 0
        user32.PostMessageW(
            target,
            event.message,
            button_wparam,
            make_lparam(local_x, local_y),
        )
        if event.message == WM_LBUTTONDOWN:
            user32.PostMessageW(
                target,
                WM_MOUSEMOVE,
                MK_LBUTTON,
                make_lparam(local_x, local_y),
            )

    def replay_mouse_background(self, event: MouseMirrorEvent) -> None:
        if event.group_index < 0 or event.group_index >= len(self.groups):
            return
        group = self.groups[event.group_index]
        for hwnd in self.live_followers(group):
            delay_ms = self.window_delay_ms(group, hwnd)
            self.run_sync_action(
                delay_ms,
                lambda g=group, h=hwnd, e=event: self.post_mouse_event_to_follower(g, h, e),
            )

    def post_keyboard_event_to_follower(
        self,
        hwnd: int,
        event: KeyboardMirrorEvent,
    ) -> None:
        key_up = event.message == WM_KEYUP
        lparam = make_key_lparam(event.vk, key_up=key_up)
        targets = [hwnd]
        try:
            rect = get_client_rect(hwnd)
            target, _local_x, _local_y = child_at_client_point(
                hwnd,
                max(1, int(rect.right // 2)),
                max(1, int(rect.bottom // 2)),
            )
            if target and target not in targets:
                targets.append(target)
        except Exception:
            pass
        for target in targets:
            user32.PostMessageW(target, event.message, event.vk, lparam)

    def replay_keyboard_background(self, event: KeyboardMirrorEvent) -> None:
        if event.group_index < 0 or event.group_index >= len(self.groups):
            return
        group = self.groups[event.group_index]
        for hwnd in self.live_followers(group):
            delay_ms = self.window_delay_ms(group, hwnd)
            self.run_sync_action(
                delay_ms,
                lambda h=hwnd, e=event: self.post_keyboard_event_to_follower(h, e),
            )

    def post_wheel_event_to_follower(
        self,
        group: SyncGroup,
        hwnd: int,
        event: MouseWheelMirrorEvent,
    ) -> None:
        inside, wheel_x, wheel_y = self.adjusted_click_point(group, hwnd, event.x, event.y)
        if not inside:
            return
        target, _local_x, _local_y = child_at_client_point(hwnd, wheel_x, wheel_y)
        screen_point = client_to_screen(hwnd, wheel_x, wheel_y)
        user32.PostMessageW(
            target,
            WM_MOUSEWHEEL,
            make_wparam(0, event.delta),
            make_lparam(screen_point.x, screen_point.y),
        )

    def replay_wheel_background(self, event: MouseWheelMirrorEvent) -> None:
        if event.group_index < 0 or event.group_index >= len(self.groups):
            return
        group = self.groups[event.group_index]
        for hwnd in self.live_followers(group):
            delay_ms = self.window_delay_ms(group, hwnd)
            self.run_sync_action(
                delay_ms,
                lambda g=group, h=hwnd, e=event: self.post_wheel_event_to_follower(g, h, e),
            )

    def install_mouse_wheel_hook(self) -> None:
        if self.mouse_hook:
            return
        self.mouse_hook_proc = LowLevelMouseProc(self.low_level_mouse_proc)
        module_handle = kernel32.GetModuleHandleW(None)
        self.mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL,
            self.mouse_hook_proc,
            module_handle,
            0,
        )
        if not self.mouse_hook:
            self.write_log("滾輪同步常駐監聽啟動失敗。")
        else:
            self.write_log("滾輪同步常駐已啟動。")

    def uninstall_mouse_wheel_hook(self) -> None:
        if self.mouse_hook:
            try:
                user32.UnhookWindowsHookEx(self.mouse_hook)
            except Exception:
                pass
            self.mouse_hook = None
        self.mouse_hook_proc = None

    def low_level_mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code == HC_ACTION and int(w_param) == WM_MOUSEWHEEL:
                info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                delta = ctypes.c_short((int(info.mouseData) >> 16) & 0xFFFF).value
                self.queue_wheel_sync_at_point(int(info.pt.x), int(info.pt.y), int(delta))
        except Exception:
            pass
        return int(user32.CallNextHookEx(self.mouse_hook, n_code, w_param, l_param))

    def queue_wheel_sync_at_point(self, screen_x: int, screen_y: int, delta: int) -> None:
        if not delta:
            return
        for group_index, group in enumerate(self.groups):
            if not group.running:
                continue
            if not group.master_hwnd or not user32.IsWindow(group.master_hwnd):
                continue
            inside, client_x, client_y = point_in_client(group.master_hwnd, screen_x, screen_y)
            if not inside:
                try:
                    local = screen_to_client_raw(group.master_hwnd, screen_x, screen_y)
                    rect = get_client_rect(group.master_hwnd)
                    if 0 <= local.x < rect.right and 0 <= local.y < rect.bottom:
                        inside, client_x, client_y = True, int(local.x), int(local.y)
                except Exception:
                    pass
            if not inside:
                continue
            self.events.put(MouseWheelMirrorEvent(group_index, client_x, client_y, delta))
            return

    def broadcast_windows(self) -> list[int]:
        windows: list[int] = []
        candidates = []
        if self.master_hwnd:
            candidates.append(self.master_hwnd)
        candidates.extend(self.followers)
        for hwnd in candidates:
            if hwnd and user32.IsWindow(hwnd) and hwnd not in windows:
                windows.append(hwnd)
        return windows

    def managed_flash_windows(self) -> list[int]:
        windows: list[int] = []
        for group in self.groups:
            candidates = []
            if group.master_hwnd:
                candidates.append(group.master_hwnd)
            candidates.extend(group.followers)
            for hwnd in candidates:
                if hwnd and user32.IsWindow(hwnd) and is_flash_window(hwnd) and hwnd not in windows:
                    windows.append(hwnd)
        return windows

    def refresh_current_group_monitor_bindings(self) -> None:
        group = self.current_group()
        if not group.launch_entries:
            return
        try:
            self.live_launch_hwnd_matches(
                group,
                group.launch_entries,
                allow_locked_master_identity_match=True,
                allow_locked_master_position_match=True,
            )
        except Exception as exc:
            self.write_log(f"偵測清單更新失敗：{exc}")

    def current_group_monitor_windows(self, refresh_matches: bool = False) -> list[int]:
        group = self.current_group()
        if refresh_matches:
            self.refresh_current_group_monitor_bindings()
        windows: list[int] = []
        candidates = []
        if group.launch_entries:
            for index in range(len(group.launch_entries)):
                hwnd = group.launch_hwnds.get(index)
                if hwnd:
                    candidates.append(hwnd)
            for _index, hwnd in sorted(group.launch_hwnds.items()):
                if hwnd:
                    candidates.append(hwnd)
        if group.master_hwnd:
            candidates.append(group.master_hwnd)
        candidates.extend(group.followers)
        for hwnd in candidates:
            if hwnd and user32.IsWindow(hwnd) and is_flash_window(hwnd) and hwnd not in windows:
                windows.append(hwnd)
        return windows

    def monitor_window_display_name(self, hwnd: int) -> str:
        group = self.current_group()
        entry = self.launch_entry_for_hwnd(group, hwnd)
        if entry and entry.path:
            name = os.path.basename(entry.path).strip()
            if name:
                return name
        name = self.window_display_name_for_group(group, hwnd)
        if name and name not in ("未讀取", "未校正"):
            return name
        return self.master_display_name(hwnd, group)

    def group_for_hwnd(self, hwnd: int) -> SyncGroup:
        group_index = self.group_index_for_hwnd(hwnd)
        if group_index is not None:
            return self.groups[group_index]
        return self.current_group()

    def group_index_for_hwnd(self, hwnd: int) -> int | None:
        current_index = int(self.active_group_index.get())
        if 0 <= current_index < len(self.groups):
            current = self.groups[current_index]
            if current.master_hwnd == hwnd:
                return current_index
            if hwnd in current.followers:
                return current_index
        for index, group in enumerate(self.groups):
            if group.master_hwnd == hwnd:
                return index
        for index, group in enumerate(self.groups):
            if hwnd in group.followers:
                return index
        return None

    def fishing_command_for_hwnd(self, hwnd: int) -> tuple[str, str]:
        group = self.group_for_hwnd(hwnd)
        route_name = group.fishing_route_name
        if route_name not in FISHING_ROUTES:
            route_name = "東郊"
            group.fishing_route_name = route_name
        return route_name, FISHING_ROUTES[route_name]

    def current_group_index_value(self) -> int:
        return max(0, min(len(self.groups) - 1, int(self.active_group_index.get())))

    def reset_disconnect_detected_names(self) -> None:
        self.disconnect_detected_names.clear()
        self.disconnect_last_detect.clear()
        self.disconnect_detected_group_index = self.current_group_index_value()

    def ensure_disconnect_group_context(self) -> None:
        group_index = self.current_group_index_value()
        if self.disconnect_detected_group_index != group_index:
            self.disconnect_detected_names.clear()
            self.disconnect_scan_index = 0
            self.disconnect_detected_group_index = group_index

    def remember_disconnect_detected_window(self, hwnd: int) -> str:
        name = self.monitor_window_display_name(hwnd)
        if name not in self.disconnect_detected_names:
            self.disconnect_detected_names.append(name)
        return name

    def disconnect_detected_summary(self) -> str:
        names = self.disconnect_detected_names
        if not names:
            return ""
        summary = "、".join(names[:3])
        if len(names) > 3:
            summary += f"、+{len(names) - 3}"
        return f"斷線偵測：偵測到 {len(names)} 個｜{summary}"

    def set_disconnect_scan_status(self, detail: str) -> None:
        summary = self.disconnect_detected_summary()
        if summary:
            self.disconnect_detect_status_text.set(f"{summary}｜{detail}" if detail else summary)
        else:
            self.disconnect_detect_status_text.set(f"斷線偵測：持續中｜{detail}" if detail else "斷線偵測：持續中")

    def disconnect_detect_interval_ms(self) -> int:
        return self.int_from_text(
            self.disconnect_detect_interval_ms_text.get(), 3000, 1000, 30000
        )

    def apply_disconnect_restore_minimized(self) -> None:
        self.save_launch_config()
        state = "允許" if self.disconnect_restore_minimized.get() else "略過"
        self.disconnect_detect_status_text.set(f"斷線偵測：縮小視窗{state}")
        self.write_log(f"斷線偵測設定：縮小視窗{state}。")

    def toggle_disconnect_detect(self) -> None:
        self.save_launch_config()
        if self.disconnect_detect_enabled.get():
            self.reset_disconnect_detected_names()
            interval_ms = self.disconnect_detect_interval_ms()
            self.disconnect_detect_status_text.set(
                f"斷線偵測：持續中｜每 {interval_ms}ms｜等待掃描"
            )
            self.refresh_current_group_monitor_bindings()
            self.write_log("斷線偵測已開啟：只偵測並顯示斷線視窗，不自動處理。")
            self.schedule_disconnect_detect(delay_ms=250)
        else:
            if self.disconnect_detect_after_id:
                try:
                    self.after_cancel(self.disconnect_detect_after_id)
                except Exception:
                    pass
                self.disconnect_detect_after_id = None
            self.reset_disconnect_detected_names()
            self.disconnect_detect_status_text.set("斷線偵測：未啟用")
            self.write_log("斷線偵測已關閉。")

    def scan_disconnect_once_visible(self) -> None:
        self.scan_disconnect_once(restore_minimized=False)

    def scan_disconnect_once_restore(self) -> None:
        self.scan_disconnect_once(restore_minimized=True)

    def scan_disconnect_once(self, restore_minimized: bool) -> None:
        self.reset_disconnect_detected_names()
        windows = self.current_group_monitor_windows(refresh_matches=True)
        if not windows:
            self.disconnect_detect_status_text.set("斷線偵測：單次完成｜掃描 0 個｜偵測到 0 個")
            return
        skipped = 0
        detected = 0
        detected_names: list[str] = []
        for hwnd in windows:
            if not hwnd or not user32.IsWindow(hwnd):
                continue
            if user32.IsIconic(hwnd) and not restore_minimized:
                skipped += 1
                continue
            if self.detect_disconnect_prompt(
                hwnd, restore_minimized=restore_minimized
            ):
                detected += 1
                detected_names.append(self.remember_disconnect_detected_window(hwnd))
        status = f"斷線偵測：單次完成｜掃描 {len(windows)} 個｜偵測到 {detected} 個"
        if detected_names:
            status += f"｜{'、'.join(detected_names[:3])}"
            if len(detected_names) > 3:
                status += f"、+{len(detected_names) - 3}"
        if skipped:
            status += f"｜縮小略過 {skipped}"
        self.disconnect_detect_status_text.set(status)
        action = "打開縮小並掃描" if restore_minimized else "單次掃描"
        self.write_log(
            f"斷線偵測：{self.current_group().name}{action}完成，偵測到 {detected} 個，略過縮小 {skipped} 個。"
        )

    def schedule_disconnect_detect(self, delay_ms: int | None = None) -> None:
        if self.disconnect_detect_enabled.get():
            self.disconnect_detect_after_id = self.after(
                delay_ms if delay_ms is not None else self.disconnect_detect_interval_ms(),
                self.poll_disconnect_detect,
            )

    def poll_disconnect_detect(self) -> None:
        self.disconnect_detect_after_id = None
        if not self.disconnect_detect_enabled.get():
            return
        self.ensure_disconnect_group_context()
        windows = self.current_group_monitor_windows()
        interval_ms = self.disconnect_detect_interval_ms()
        if not windows:
            self.disconnect_scan_index = 0
            self.set_disconnect_scan_status(f"每 {interval_ms}ms｜掃描 0 個")
        else:
            if self.disconnect_scan_index >= len(windows):
                self.disconnect_scan_index = 0
            scan_index = self.disconnect_scan_index
            hwnd = windows[scan_index]
            self.disconnect_scan_index = (scan_index + 1) % len(windows)
            restore_minimized = bool(self.disconnect_restore_minimized.get())
            if (
                hwnd
                and user32.IsWindow(hwnd)
                and user32.IsIconic(hwnd)
                and not restore_minimized
            ):
                self.set_disconnect_scan_status(
                    f"每 {interval_ms}ms｜掃描 {scan_index + 1}/{len(windows)}｜縮小略過"
                )
                self.schedule_disconnect_detect()
                return
            detected = self.detect_disconnect_prompt(
                hwnd, restore_minimized=restore_minimized
            )
            if detected:
                self.remember_disconnect_detected_window(hwnd)
                self.set_disconnect_scan_status(
                    f"每 {interval_ms}ms｜掃描 {scan_index + 1}/{len(windows)}"
                )
            else:
                self.set_disconnect_scan_status(
                    f"每 {interval_ms}ms｜掃描 {scan_index + 1}/{len(windows)}"
                )
        self.schedule_disconnect_detect()

    def detect_disconnect_prompt(
        self, hwnd: int, restore_minimized: bool = False
    ) -> bool:
        now = time.perf_counter()
        if now - self.disconnect_last_detect.get(hwnd, 0.0) < DISCONNECT_DETECT_COOLDOWN_SECONDS:
            return False
        if not hwnd or not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            if not restore_minimized:
                return False
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.15)
        client_width, client_height = get_client_size(hwnd)
        if client_width <= 0 or client_height <= 0:
            return False
        region_width = max(420, min(client_width, int(client_width * 0.74)))
        region_height = max(240, min(client_height, int(client_height * 0.55)))
        left = max(0, (client_width - region_width) // 2)
        top = max(0, (client_height - region_height) // 2)
        rect = (left, top, min(client_width, left + region_width), min(client_height, top + region_height))
        path = app_writable_path("disconnect_capture.bmp")
        try:
            capture_client_region_to_bmp(hwnd, rect, path)
            width, height, pixels = read_bmp_pixels(path)
            button = find_disconnect_confirm_button(width, height, pixels)
        except Exception as exc:
            self.write_log(f"斷線偵測截圖失敗：{exc}")
            return False
        if not button:
            return False
        self.disconnect_last_detect[hwnd] = now
        self.write_log(f"斷線偵測：偵測到中斷提示：{self.monitor_window_display_name(hwnd)}")
        return True

    def click_flash_client_point(self, hwnd: int, x: int, y: int) -> None:
        target, local_x, local_y = child_at_client_point(hwnd, x, y)
        user32.PostMessageW(
            target,
            WM_LBUTTONDOWN,
            MK_LBUTTON,
            make_lparam(local_x, local_y),
        )
        user32.PostMessageW(
            target,
            WM_MOUSEMOVE,
            MK_LBUTTON,
            make_lparam(local_x, local_y),
        )
        self.after(
            35,
            lambda h=target, lx=local_x, ly=local_y: user32.PostMessageW(
                h, WM_LBUTTONUP, 0, make_lparam(lx, ly)
            ),
        )

    def toggle_relogin_auto(self) -> None:
        if self.relogin_auto_enabled.get():
            self.relogin_status_text.set("重登流程：等待斷線")
            self.write_log("斷線後自動重登已開啟。")
        else:
            for hwnd in list(self.relogin_after_ids):
                self.cancel_relogin_flow(hwnd)
            self.relogin_status_text.set("重登流程：未啟用")
            self.write_log("斷線後自動重登已關閉。")

    def cancel_relogin_flow(self, hwnd: int) -> None:
        for after_id in self.relogin_after_ids.pop(hwnd, []):
            try:
                self.after_cancel(after_id)
            except Exception:
                pass

    def schedule_relogin_after(self, hwnd: int, delay_ms: int, callback) -> None:
        after_id = self.after(delay_ms, lambda h=hwnd: callback(h))
        self.relogin_after_ids.setdefault(hwnd, []).append(after_id)

    def schedule_relogin_flow(self, hwnd: int) -> None:
        self.cancel_relogin_flow(hwnd)
        self.relogin_status_text.set("重登流程：等待登入畫面")
        self.schedule_relogin_after(hwnd, 5000, self.relogin_click_start_game)
        self.schedule_relogin_after(hwnd, 10000, self.relogin_click_server_line)
        self.schedule_relogin_after(hwnd, 15000, self.relogin_click_enter_game)
        if self.restore_fishing_enabled.get():
            self.schedule_relogin_after(hwnd, 20000, self.restore_fishing_click_current_channel)
            self.schedule_relogin_after(hwnd, 25000, self.restore_fishing_send_command)
            self.schedule_relogin_after(hwnd, 30000, self.restore_fishing_click_chat_path)
            self.schedule_relogin_after(hwnd, 35000, self.check_restore_fishing_status)

    def current_group_master_for_test(self) -> int | None:
        hwnd = self.current_group().master_hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            messagebox.showwarning("缺少主窗口", "請先點選目前同步組的主窗口。")
            return None
        return hwnd

    def test_relogin_flow_current_group(self) -> None:
        hwnd = self.current_group_master_for_test()
        if not hwnd:
            return
        self.write_log("測試重登流程：直接從目前主窗口開始跑。")
        self.schedule_relogin_flow(hwnd)

    def test_restore_fishing_current_group(self) -> None:
        hwnd = self.current_group_master_for_test()
        if not hwnd:
            return
        self.cancel_relogin_flow(hwnd)
        self.relogin_status_text.set("重登流程：測試恢復釣魚")
        self.restore_fishing_click_current_channel(hwnd)
        self.schedule_relogin_after(hwnd, 500, self.restore_fishing_send_command)
        self.schedule_relogin_after(hwnd, 2000, self.restore_fishing_click_chat_path)
        self.schedule_relogin_after(hwnd, 7500, self.check_restore_fishing_status)

    def capture_full_client_pixels(
        self, hwnd: int, filename: str
    ) -> tuple[int, int, list[list[tuple[int, int, int]]]] | None:
        client_width, client_height = get_client_size(hwnd)
        if client_width <= 0 or client_height <= 0:
            return None
        path = app_writable_path(filename)
        try:
            capture_client_region_to_bmp(hwnd, (0, 0, client_width, client_height), path)
            return read_bmp_pixels(path)
        except Exception as exc:
            self.write_log(f"重登流程截圖失敗：{exc}")
            return None

    def choose_button_in_region(
        self,
        hwnd: int,
        region_ratio: tuple[float, float, float, float],
        filename: str,
        fallback_ratio: tuple[float, float],
        mode: str = "largest",
    ) -> tuple[int, int]:
        image = self.capture_full_client_pixels(hwnd, filename)
        client_width, client_height = get_client_size(hwnd)
        if image:
            width, height, pixels = image
            region = (
                int(width * region_ratio[0]),
                int(height * region_ratio[1]),
                int(width * region_ratio[2]),
                int(height * region_ratio[3]),
            )
            components = find_button_like_components(width, height, pixels, region)
            if components:
                if mode == "top":
                    picked = sorted(components, key=lambda box: (box[1], -box[4]))[0]
                elif mode == "left":
                    picked = sorted(components, key=lambda box: (box[0], -box[4]))[0]
                else:
                    picked = max(components, key=lambda box: box[4])
                return button_center(picked)
        return int(client_width * fallback_ratio[0]), int(client_height * fallback_ratio[1])

    def relogin_click_start_game(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        x, y = self.choose_button_in_region(
            hwnd,
            (0.35, 0.62, 0.65, 0.90),
            "relogin_start_capture.bmp",
            (0.50, 0.77),
            "largest",
        )
        self.click_flash_client_point(hwnd, x, y)
        self.relogin_status_text.set("重登流程：已按開始遊戲")
        self.write_log(f"重登流程：已按開始遊戲：{self.master_display_name(hwnd)}")

    def relogin_click_server_line(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        x, y = self.server_line_click_point(hwnd)
        box_width, box_height = self.server_line_overlay_size()
        self.show_click_overlay(hwnd, x, y, box_width, box_height, "麻布老虎1線點擊框線", 2500)
        self.click_flash_client_point(hwnd, x, y)
        self.relogin_status_text.set("重登流程：已選麻布老虎1線")
        self.write_log(f"重登流程：已選麻布老虎1線：X={x}, Y={y} {self.master_display_name(hwnd)}")

    def relogin_click_enter_game(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        x, y = self.choose_button_in_region(
            hwnd,
            (0.20, 0.81, 0.50, 0.92),
            "relogin_enter_capture.bmp",
            (0.34, 0.85),
            "largest",
        )
        self.click_flash_client_point(hwnd, x, y)
        self.relogin_status_text.set("重登流程：已按進入遊戲")
        self.write_log(f"重登流程：已按進入遊戲：{self.master_display_name(hwnd)}")
        self.after(10000, lambda h=hwnd: self.resume_sync_after_relogin(h))

    def resume_sync_after_relogin(self, hwnd: int) -> None:
        group_index = self.relogin_resume_groups.pop(hwnd, None)
        if group_index is None or group_index < 0 or group_index >= len(self.groups):
            return
        group = self.groups[group_index]
        if group.running:
            return
        self.relogin_status_text.set(f"重登流程：恢復同步 {group.name}")
        self.write_log(f"重登流程：嘗試恢復 {group.name} 同步。")
        self.start_sync(group_index, skip_prepare=True)

    def post_text_to_flash(self, hwnd: int, text: str, x: int, y: int) -> None:
        target, _local_x, _local_y = child_at_client_point(hwnd, x, y)
        for char in text:
            user32.PostMessageW(target, WM_CHAR, ord(char), 0)
        user32.PostMessageW(target, WM_KEYDOWN, VK_RETURN, 0)
        user32.PostMessageW(target, WM_KEYUP, VK_RETURN, 0)

    def restore_fishing_current_channel_point(self, hwnd: int) -> tuple[int, int]:
        client_width, client_height = get_client_size(hwnd)
        return int(client_width * 0.13), int(client_height * 0.905)

    def restore_fishing_chat_input_point(self, hwnd: int) -> tuple[int, int]:
        client_width, client_height = get_client_size(hwnd)
        return int(client_width * 0.22), int(client_height * 0.955)

    def restore_fishing_chat_path_point(self, hwnd: int) -> tuple[int, int]:
        client_width, client_height = get_client_size(hwnd)
        return int(client_width * 0.18), int(client_height * 0.61)

    def server_line_click_point(self, hwnd: int) -> tuple[int, int]:
        return 450, 195

    def server_line_overlay_size(self) -> tuple[int, int]:
        return 180, 36

    def clear_restore_fishing_overlay(self) -> None:
        for window in self.restore_fishing_overlay_windows:
            try:
                window.destroy()
            except Exception:
                pass
        self.restore_fishing_overlay_windows = []

    def show_click_overlay(
        self,
        hwnd: int,
        center_x: int,
        center_y: int,
        width: int,
        height: int,
        label: str,
        duration_ms: int = 4000,
    ) -> None:
        self.clear_restore_fishing_overlay()
        if not hwnd or not user32.IsWindow(hwnd):
            return
        box_half_width = max(1, width // 2)
        box_half_height = max(1, height // 2)
        left = max(0, center_x - box_half_width)
        top = max(0, center_y - box_half_height)
        right = center_x + box_half_width
        bottom = center_y + box_half_height
        screen_left_top = client_to_screen(hwnd, left, top)
        screen_right_bottom = client_to_screen(hwnd, right, bottom)
        x1, y1 = screen_left_top.x, screen_left_top.y
        x2, y2 = screen_right_bottom.x, screen_right_bottom.y
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        thickness = 3
        parts = [
            (x1, y1, width, thickness),
            (x1, y2 - thickness, width, thickness),
            (x1, y1, thickness, height),
            (x2 - thickness, y1, thickness, height),
        ]
        for part_x, part_y, part_width, part_height in parts:
            overlay = tk.Toplevel(self)
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            try:
                overlay.attributes("-toolwindow", True)
            except Exception:
                pass
            overlay.configure(bg="#ff2020")
            overlay.geometry(f"{part_width}x{part_height}{part_x:+d}{part_y:+d}")
            self.restore_fishing_overlay_windows.append(overlay)
        self.write_log(
            f"{label}：X={left}, Y={top}, 寬={right-left}, 高={bottom-top}"
        )
        self.after(duration_ms, self.clear_restore_fishing_overlay)

    def show_restore_fishing_click_overlay(
        self, hwnd: int | None = None, duration_ms: int = 4000
    ) -> None:
        if hwnd is None:
            hwnd = self.current_group_master_for_test()
        if not hwnd or not user32.IsWindow(hwnd):
            return
        path_x, path_y = self.restore_fishing_chat_path_point(hwnd)
        self.show_click_overlay(
            hwnd, path_x, path_y, 120, 32, "恢復釣魚點擊框線", duration_ms
        )

    def restore_fishing_click_current_channel(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        x, y = self.restore_fishing_current_channel_point(hwnd)
        self.click_flash_client_point(hwnd, x, y)
        self.write_log(f"恢復釣魚：已按左下角目前頻道：{self.master_display_name(hwnd)}")

    def restore_fishing_send_command(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        chat_x, chat_y = self.restore_fishing_chat_input_point(hwnd)
        route_name, command = self.fishing_command_for_hwnd(hwnd)
        if not command:
            self.write_log("恢復釣魚：路徑指令空白，略過。")
            return
        self.click_flash_client_point(hwnd, chat_x, chat_y)
        self.after(120, lambda h=hwnd, t=command, x=chat_x, y=chat_y: self.post_text_to_flash(h, t, x, y))
        self.relogin_status_text.set("重登流程：已送釣魚路徑")
        self.write_log(f"恢復釣魚：已送出{route_name}路徑：{command}")

    def restore_fishing_click_chat_path(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        path_x, path_y = self.restore_fishing_chat_path_point(hwnd)
        self.show_restore_fishing_click_overlay(hwnd=hwnd, duration_ms=2500)
        self.click_flash_client_point(hwnd, path_x, path_y)
        self.relogin_status_text.set("重登流程：已點聊天路徑")
        self.write_log(f"恢復釣魚：已點擊聊天路徑：{self.master_display_name(hwnd)}")

    def is_fishing_status_visible(self, hwnd: int) -> bool:
        image = self.capture_full_client_pixels(hwnd, "fishing_status_capture.bmp")
        if not image:
            return False
        width, height, pixels = image
        box = (
            int(width * 0.32),
            int(height * 0.42),
            int(width * 0.58),
            int(height * 0.58),
        )
        green_count = count_pixels_in_box(
            pixels,
            box,
            lambda r, g, b: g >= 150 and r <= 100 and b <= 120 and g >= r + 70,
        )
        return green_count >= 80

    def check_restore_fishing_status(self, hwnd: int) -> None:
        if not user32.IsWindow(hwnd):
            return
        if self.is_fishing_status_visible(hwnd):
            self.relogin_status_text.set("重登流程：釣魚中")
            self.write_log(f"恢復釣魚：已偵測到正在釣魚：{self.master_display_name(hwnd)}")
        else:
            self.relogin_status_text.set("重登流程：未確認釣魚")
            self.write_log(f"恢復釣魚：尚未偵測到正在釣魚：{self.master_display_name(hwnd)}")

    def start_capture_custom_input(self) -> None:
        group = self.current_group()
        if (not group.master_hwnd or not user32.IsWindow(group.master_hwnd)) and not group.launch_entries:
            self.custom_key_text.set("未設定")
            messagebox.showwarning("缺少主窗口", "請先點選主窗口，或先在本組加入啟動檔案。")
            return
        self.begin_capture_input("sync", "等待設定同步快捷鍵：請按鍵盤鍵，或按滑鼠側鍵。")

    def clear_custom_hotkey(self) -> None:
        self.cancel_capture_custom_input(log=False)
        group = self.current_group()
        group.custom_key_display = ""
        group.hotkey_state = False
        self.custom_key_text.set("")
        self.save_launch_config()
        self.update_sync_state_text()
        self.write_log(f"{group.name}快捷鍵已清除。")

    def start_capture_launch_input(self) -> None:
        group = self.current_group()
        if not group.launch_entries:
            messagebox.showwarning("缺少啟動清單", "請先在本組加入啟動檔案。")
            return
        self.begin_capture_input("launch", "等待設定整理本組快捷鍵：請按鍵盤鍵，或按滑鼠側鍵。")

    def clear_launch_hotkey(self) -> None:
        self.cancel_capture_custom_input(log=False)
        group = self.current_group()
        group.launch_hotkey_display = ""
        group.launch_hotkey_state = False
        self.launch_hotkey_text.set("")
        self.save_launch_config()
        self.write_log(f"{group.name}整理本組快捷鍵已清除。")

    def start_capture_autoclick_input(self) -> None:
        self.begin_capture_input("autoclick", "等待設定自動點擊快捷鍵：請按鍵盤鍵，或按滑鼠側鍵。")

    def begin_capture_input(self, target: str, message: str) -> None:
        self.cancel_capture_custom_input(log=False)
        self.capture_custom_input = True
        self.capture_input_target = target
        self.capture_mouse_state = {
            VK_XBUTTON1: self.is_button_down(VK_XBUTTON1),
            VK_XBUTTON2: self.is_button_down(VK_XBUTTON2),
        }
        self.focus_force()
        self.write_log(message)
        self.schedule_capture_poll()

    def cancel_capture_custom_input(self, log: bool = True) -> None:
        self.capture_custom_input = False
        self.capture_input_target = None
        self.capture_mouse_state = {}
        if self.capture_after_id:
            try:
                self.after_cancel(self.capture_after_id)
            except Exception:
                pass
            self.capture_after_id = None
        if log:
            self.update_sync_state_text()
            self.write_log("已取消快捷鍵設定。")

    def schedule_capture_poll(self) -> None:
        if self.capture_custom_input:
            self.capture_after_id = self.after(15, self.poll_capture_custom_input)

    def poll_capture_custom_input(self) -> None:
        self.capture_after_id = None
        if not self.capture_custom_input:
            return
        for vk, button_number in ((VK_XBUTTON1, 1), (VK_XBUTTON2, 2)):
            is_down = self.is_button_down(vk)
            was_down = self.capture_mouse_state.get(vk, is_down)
            self.capture_mouse_state[vk] = is_down
            if is_down and not was_down:
                self.finish_capture_input(
                    CustomInput("xbutton", button_number, f"XBUTTON{button_number}")
                )
                return
        self.schedule_capture_poll()

    def on_capture_key(self, event) -> str | None:
        if self.capture_follower_click and (event.keysym or "").lower() == "escape":
            self.cancel_capture_follower_click(log=True)
            return "break"
        if not self.capture_custom_input:
            return None
        key_name = event.keysym or event.char
        if event.char and len(event.char) == 1 and event.char.isprintable():
            key_name = event.char
        try:
            custom = parse_custom_input(key_name)
        except ValueError:
            self.write_log(f"不支援這個按鍵：{key_name}")
            return "break"
        self.finish_capture_input(custom)
        return "break"

    def finish_capture_input(self, custom: CustomInput) -> None:
        if self.capture_input_target == "autoclick":
            self.finish_capture_autoclick_input(custom)
        elif self.capture_input_target == "launch":
            self.finish_capture_launch_input(custom)
        else:
            self.finish_capture_custom_input(custom)

    def hotkey_matches_custom(self, display: str, custom: CustomInput) -> bool:
        try:
            parsed = parse_custom_input(display)
        except ValueError:
            return False
        return parsed.kind == custom.kind and int(parsed.value) == int(custom.value)

    def clear_conflicting_group_hotkeys(
        self, custom: CustomInput, owner_group_index: int, owner_kind: str
    ) -> list[str]:
        cleared: list[str] = []
        for index, other_group in enumerate(self.groups):
            if not (owner_kind == "sync" and index == owner_group_index):
                if self.hotkey_matches_custom(other_group.custom_key_display, custom):
                    other_group.custom_key_display = ""
                    other_group.hotkey_state = False
                    cleared.append(f"{other_group.name}同步鍵")
            if not (owner_kind == "launch" and index == owner_group_index):
                if self.hotkey_matches_custom(other_group.launch_hotkey_display, custom):
                    other_group.launch_hotkey_display = ""
                    other_group.launch_hotkey_state = False
                    cleared.append(f"{other_group.name}整理鍵")
        return cleared

    def normalize_loaded_hotkey_conflicts(self) -> list[str]:
        notes: list[str] = []
        sync_hotkeys: list[tuple[int, SyncGroup, CustomInput]] = []
        for index, group in enumerate(self.groups):
            try:
                sync_hotkeys.append((index, group, parse_custom_input(group.custom_key_display)))
            except ValueError:
                continue
        for _sync_index, sync_group, sync_hotkey in sync_hotkeys:
            for launch_group in self.groups:
                if self.hotkey_matches_custom(launch_group.launch_hotkey_display, sync_hotkey):
                    launch_group.launch_hotkey_display = ""
                    launch_group.launch_hotkey_state = False
                    notes.append(
                        f"已清空同鍵設定：{launch_group.name}整理鍵，保留 {sync_group.name}同步鍵。"
                    )
        return notes

    def apply_pending_hotkey_conflict_notes(self) -> None:
        notes = getattr(self, "pending_hotkey_conflict_notes", [])
        if not notes:
            return
        self.pending_hotkey_conflict_notes = []
        for note in notes:
            self.write_log(note)
        self.launch_hotkey_text.set(self.current_group().launch_hotkey_display)
        self.custom_key_text.set(self.current_group().custom_key_display)
        self.save_launch_config()

    def finish_capture_custom_input(self, custom: CustomInput) -> None:
        self.cancel_capture_custom_input(log=False)
        group = self.current_group()
        group_index = int(self.active_group_index.get())
        if (not group.master_hwnd or not user32.IsWindow(group.master_hwnd)) and not group.launch_entries:
            self.custom_key_text.set("未設定")
            messagebox.showwarning("缺少主窗口", "請先點選主窗口，或先在本組加入啟動檔案。")
            return
        cleared_hotkeys = self.clear_conflicting_group_hotkeys(custom, group_index, "sync")
        group.custom_key_display = custom.display
        self.custom_key_text.set(custom.display)
        self.launch_hotkey_text.set(group.launch_hotkey_display)
        group.hotkey_state = self.is_custom_input_down(custom)
        self.save_launch_config()
        self.update_sync_state_text()
        self.write_log(f"{group.name}快捷鍵已設定：{custom.display}")
        if cleared_hotkeys:
            self.write_log(
                f"已清空同鍵設定：{'、'.join(cleared_hotkeys)}，目前以 {group.name}同步鍵為主。"
            )

    def finish_capture_launch_input(self, custom: CustomInput) -> None:
        self.cancel_capture_custom_input(log=False)
        group = self.current_group()
        group_index = int(self.active_group_index.get())
        if not group.launch_entries:
            messagebox.showwarning("缺少啟動清單", "請先在本組加入啟動檔案。")
            return
        cleared_hotkeys = self.clear_conflicting_group_hotkeys(custom, group_index, "launch")
        group.launch_hotkey_display = custom.display
        self.launch_hotkey_text.set(custom.display)
        self.custom_key_text.set(group.custom_key_display)
        group.launch_hotkey_state = self.is_custom_input_down(custom)
        self.save_launch_config()
        self.write_log(f"{group.name}整理本組快捷鍵已設定：{custom.display}")
        if cleared_hotkeys:
            self.write_log(
                f"已清空同鍵設定：{'、'.join(cleared_hotkeys)}，目前以 {group.name}整理鍵為主。"
            )

    def finish_capture_autoclick_input(self, custom: CustomInput) -> None:
        self.cancel_capture_custom_input(log=False)
        self.autoclick_hotkey = custom
        self.autoclick_hotkey_text.set(custom.display)
        self.autoclick_hotkey_state = self.is_custom_input_down(custom)
        self.write_log(f"自動點擊快捷鍵已設定：{custom.display}")
        if any(group.custom_key_display == custom.display for group in self.groups):
            self.write_log("提醒：自動點擊快捷鍵與同步組快捷鍵相同時，按下會一起觸發。")

    def current_hotkey(self) -> CustomInput | None:
        return self.group_hotkey(self.current_group())

    def group_hotkey(self, group: SyncGroup) -> CustomInput | None:
        if (not group.master_hwnd or not user32.IsWindow(group.master_hwnd)) and not group.launch_entries:
            return None
        try:
            return parse_custom_input(group.custom_key_display)
        except ValueError:
            return None

    def group_launch_hotkey(self, group: SyncGroup) -> CustomInput | None:
        try:
            return parse_custom_input(group.launch_hotkey_display)
        except ValueError:
            return None

    def custom_input_key(self, custom: CustomInput) -> tuple[str, int]:
        return custom.kind, int(custom.value)

    def is_custom_input_down(self, custom: CustomInput) -> bool:
        if custom.kind == "xbutton":
            vk = VK_XBUTTON1 if custom.value == 1 else VK_XBUTTON2
            return self.is_button_down(vk)
        if custom.kind == "key":
            return self.is_button_down(custom.value)
        return False

    def is_custom_input_used_for_running_keyboard_sync(self, custom: CustomInput) -> bool:
        if custom.kind != "key":
            return False
        for group in self.groups:
            if not group.running or not group.sync_keyboard_enabled:
                continue
            for keyboard_input in self.keyboard_sync_inputs(group):
                if keyboard_input.value == custom.value:
                    return True
        return False

    def schedule_hotkey_poll(self) -> None:
        self.hotkey_after_id = self.after(25, self.poll_hotkey)

    def hotkey_group_order(self) -> list[int]:
        if not self.groups:
            return []
        active = max(0, min(len(self.groups) - 1, int(self.active_group_index.get())))
        return [active] + [index for index in range(len(self.groups)) if index != active]

    def poll_hotkey(self) -> None:
        self.hotkey_after_id = None
        try:
            if not self.capture_custom_input and not self.capture_follower_click:
                triggered_inputs: set[tuple[str, int]] = set()
                for group_index in self.hotkey_group_order():
                    group = self.groups[group_index]
                    launch_hotkey = self.group_launch_hotkey(group)
                    if not launch_hotkey:
                        continue
                    input_key = self.custom_input_key(launch_hotkey)
                    is_down = self.is_custom_input_down(launch_hotkey)
                    if (
                        is_down
                        and not group.launch_hotkey_state
                        and input_key not in triggered_inputs
                        and not self.is_custom_input_used_for_running_keyboard_sync(launch_hotkey)
                    ):
                        self.launch_group_by_hotkey(group_index, launch_hotkey)
                        triggered_inputs.add(input_key)
                    group.launch_hotkey_state = is_down
                for group_index in self.hotkey_group_order():
                    group = self.groups[group_index]
                    hotkey = self.group_hotkey(group)
                    if not hotkey:
                        continue
                    input_key = self.custom_input_key(hotkey)
                    is_down = self.is_custom_input_down(hotkey)
                    if (
                        is_down
                        and not group.hotkey_state
                        and input_key not in triggered_inputs
                        and not self.is_custom_input_used_for_running_keyboard_sync(hotkey)
                    ):
                        self.toggle_sync_by_hotkey(group_index, hotkey)
                        triggered_inputs.add(input_key)
                    group.hotkey_state = is_down
                autoclick_input_key = self.custom_input_key(self.autoclick_hotkey)
                autoclick_down = self.is_custom_input_down(self.autoclick_hotkey)
                if (
                    autoclick_down
                    and not self.autoclick_hotkey_state
                    and autoclick_input_key not in triggered_inputs
                    and not self.is_custom_input_used_for_running_keyboard_sync(self.autoclick_hotkey)
                ):
                    self.toggle_autoclick()
                    triggered_inputs.add(autoclick_input_key)
                self.autoclick_hotkey_state = autoclick_down
        finally:
            self.schedule_hotkey_poll()

    def toggle_sync_by_hotkey(self, group_index: int, hotkey: CustomInput) -> None:
        group = self.groups[group_index]
        if group.running:
            self.write_log(f"快捷鍵 {hotkey.display}：停止{group.name}同步。")
            self.stop_sync(group_index)
        else:
            self.write_log(f"快捷鍵 {hotkey.display}：開始{group.name}同步。")
            self.start_sync(group_index, skip_prepare=True, quiet=True)

    def on_close(self) -> None:
        self.closing_app = True
        self.close_tray_menu()
        if self.tray_restore_poll_after_id:
            try:
                self.after_cancel(self.tray_restore_poll_after_id)
            except Exception:
                pass
            self.tray_restore_poll_after_id = None
        if self.main_geometry_save_after_id:
            try:
                self.after_cancel(self.main_geometry_save_after_id)
            except Exception:
                pass
            self.main_geometry_save_after_id = None
        try:
            self.save_launch_config()
        except Exception:
            pass
        self.cancel_capture_custom_input(log=False)
        self.cancel_capture_follower_click(log=False)
        self.clear_role_id_overlay()
        self.clear_game_time_overlay()
        self.clear_restore_fishing_overlay()
        self.auto_game_time.set(False)
        if self.game_time_auto_after_id:
            try:
                self.after_cancel(self.game_time_auto_after_id)
            except Exception:
                pass
            self.game_time_auto_after_id = None
        if self.game_time_tick_after_id:
            try:
                self.after_cancel(self.game_time_tick_after_id)
            except Exception:
                pass
            self.game_time_tick_after_id = None
        if self.timed_click_after_id:
            try:
                self.after_cancel(self.timed_click_after_id)
            except Exception:
                pass
            self.timed_click_after_id = None
        if self.auto_resize_after_id:
            try:
                self.after_cancel(self.auto_resize_after_id)
            except Exception:
                pass
            self.auto_resize_after_id = None
        if self.launch_wait_after_id:
            try:
                self.after_cancel(self.launch_wait_after_id)
            except Exception:
                pass
            self.launch_wait_after_id = None
        self.disconnect_detect_enabled.set(False)
        if self.disconnect_detect_after_id:
            try:
                self.after_cancel(self.disconnect_detect_after_id)
            except Exception:
                pass
            self.disconnect_detect_after_id = None
        self.relogin_auto_enabled.set(False)
        for hwnd in list(self.relogin_after_ids):
            self.cancel_relogin_flow(hwnd)
        self.relogin_resume_groups.clear()
        self.stop_autoclick()
        if self.hotkey_after_id:
            try:
                self.after_cancel(self.hotkey_after_id)
            except Exception:
                pass
            self.hotkey_after_id = None
        for group_index in range(len(self.groups)):
            self.stop_sync(group_index)
        self.uninstall_mouse_wheel_hook()
        self.remove_tray_icon()
        if self.floating_status_window is not None:
            try:
                self.floating_status_window.destroy()
            except Exception:
                pass
            self.floating_status_window = None
        self.events.put(None)
        self.destroy()


def acquire_single_instance_lock() -> bool:
    global SINGLE_INSTANCE_MUTEX_HANDLE
    handle = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX_NAME)
    if not handle:
        return True
    if int(kernel32.GetLastError()) == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    SINGLE_INSTANCE_MUTEX_HANDLE = handle
    return True


def notify_existing_instance() -> None:
    if not SINGLE_INSTANCE_RESTORE_MESSAGE:
        return
    for _ in range(3):
        user32.PostMessageW(
            wintypes.HWND(HWND_BROADCAST),
            SINGLE_INSTANCE_RESTORE_MESSAGE,
            0,
            0,
        )
        time.sleep(0.08)


def release_single_instance_lock() -> None:
    global SINGLE_INSTANCE_MUTEX_HANDLE
    if SINGLE_INSTANCE_MUTEX_HANDLE:
        try:
            kernel32.CloseHandle(SINGLE_INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass
        SINGLE_INSTANCE_MUTEX_HANDLE = None


def main() -> int:
    if not acquire_single_instance_lock():
        notify_existing_instance()
        return 0
    app = FlashSyncApp()
    try:
        app.mainloop()
        return 0
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    import faulthandler

    log_path = os.path.join(os.path.dirname(__file__), "flash_sync_error.log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        faulthandler.enable(file=log_file, all_threads=True)
        try:
            sys.exit(main())
        except Exception:
            traceback.print_exc(file=log_file)
            raise
