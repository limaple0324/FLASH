from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time
from typing import Iterable
import threading

import numpy as np
from user_activity_guard import USER_ACTIVITY_GUARD


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
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
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_RESTORE = 9
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_NOSENDCHANGING = 0x0400
SWP_SHOWWINDOW = 0x0040
HWND_BOTTOM = 1
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002


def set_process_dpi_awareness() -> None:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    class_name: str
    minimized: bool

    @property
    def display(self) -> str:
        state = " [最小化]" if self.minimized else ""
        return f"{self.title}{state}  (PID {self.pid}, HWND 0x{self.hwnd:X})"


def get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def get_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def get_window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def enumerate_windows(visible_only: bool = True) -> list[WindowInfo]:
    result: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        title = get_window_title(hwnd).strip()
        if not title:
            return True
        result.append(
            WindowInfo(
                int(hwnd),
                title,
                get_window_pid(hwnd),
                get_class_name(hwnd),
                bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    user32.EnumWindows(callback, 0)
    return result


def is_window(hwnd: int) -> bool:
    return bool(hwnd and user32.IsWindow(hwnd))


def is_game_window(info: WindowInfo, previous_title: str = "") -> bool:
    title = info.title.casefold()
    previous = previous_title.casefold().strip()
    return (
        "adobe flash player" in title
        or "flash player" in title
        or (bool(previous) and previous == title)
    )


def get_client_size(hwnd: int) -> tuple[int, int]:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError(ctypes.get_last_error(), "GetClientRect failed")
    return max(0, rect.right - rect.left), max(0, rect.bottom - rect.top)


def get_window_rect(hwnd: int) -> RECT:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(ctypes.get_last_error(), "GetWindowRect failed")
    return rect


def _bitmap_buffer(width: int, height: int, compatible_dc: int):
    bitmap_info = BITMAPINFO()
    bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bitmap_info.bmiHeader.biWidth = width
    bitmap_info.bmiHeader.biHeight = -height
    bitmap_info.bmiHeader.biPlanes = 1
    bitmap_info.bmiHeader.biBitCount = 32
    bitmap_info.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        compatible_dc,
        ctypes.byref(bitmap_info),
        DIB_RGB_COLORS,
        ctypes.byref(bits),
        None,
        0,
    )
    if not bitmap or not bits:
        raise OSError(ctypes.get_last_error(), "CreateDIBSection failed")
    return bitmap, bits


def _is_useful_frame(frame: np.ndarray | None) -> bool:
    if frame is None or frame.size == 0 or min(frame.shape[:2]) < 100:
        return False
    central = frame[
        frame.shape[0] // 10 : frame.shape[0] * 9 // 10,
        frame.shape[1] // 10 : frame.shape[1] * 9 // 10,
    ]
    return float(central.std()) >= 8.0 and float(central.mean()) >= 4.0


def _capture_with_print_window(hwnd: int, flags: int) -> np.ndarray | None:
    width, height = get_client_size(hwnd)
    if width <= 0 or height <= 0:
        return None
    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = None
    old_object = None
    try:
        bitmap, bits = _bitmap_buffer(width, height, memory_dc)
        old_object = gdi32.SelectObject(memory_dc, bitmap)
        if not user32.PrintWindow(hwnd, memory_dc, flags):
            return None
        raw = ctypes.string_at(bits, width * height * 4)
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)


def _capture_with_bitblt(hwnd: int) -> np.ndarray | None:
    width, height = get_client_size(hwnd)
    if width <= 0 or height <= 0:
        return None
    source_dc = user32.GetDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(source_dc)
    bitmap = None
    old_object = None
    try:
        bitmap, bits = _bitmap_buffer(width, height, memory_dc)
        old_object = gdi32.SelectObject(memory_dc, bitmap)
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            source_dc,
            0,
            0,
            SRCCOPY | CAPTUREBLT,
        ):
            return None
        raw = ctypes.string_at(bits, width * height * 4)
        return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if source_dc:
            user32.ReleaseDC(hwnd, source_dc)


def capture_client(hwnd: int) -> tuple[np.ndarray | None, str]:
    for flags, label in (
        (PW_CLIENTONLY | PW_RENDERFULLCONTENT, "PrintWindow完整內容"),
        (PW_CLIENTONLY, "PrintWindow"),
    ):
        try:
            frame = _capture_with_print_window(hwnd, flags)
            if _is_useful_frame(frame):
                return frame, label
        except OSError:
            continue
    try:
        frame = _capture_with_bitblt(hwnd)
        if _is_useful_frame(frame):
            return frame, "BitBlt"
    except OSError:
        pass
    return None, "擷取失敗"


def _make_lparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def post_click(hwnd: int, x: int, y: int, hold_seconds: float = 0.06) -> bool:
    if not is_window(hwnd):
        return False
    lparam = _make_lparam(x, y)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lparam)
    time.sleep(0.025)
    down = user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    time.sleep(hold_seconds)
    up = user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    return bool(down and up)


def post_move(hwnd: int, x: int, y: int) -> bool:
    if not is_window(hwnd):
        return False
    return bool(user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, _make_lparam(x, y)))


def launch_shortcut(path: str) -> None:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(path)
    os.startfile(str(target))  # type: ignore[attr-defined]


def is_running_as_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


class BackgroundWindowSession:
    """Captures and clicks a window without taking foreground focus.

    It first uses PrintWindow while preserving the current state. If Flash stops
    painting while minimized, ``promote_offscreen`` restores the window outside
    the visible desktop without activation and restores the original placement
    on exit.
    """

    def __init__(self, hwnd: int, cancelled: threading.Event | None = None) -> None:
        self.hwnd = hwnd
        self.cancelled = cancelled
        self.capture_method = ""
        self.offscreen = False
        self.original_placement: WINDOWPLACEMENT | None = None
        self.original_rect: RECT | None = None
        self.started_minimized = bool(user32.IsIconic(hwnd)) if is_window(hwnd) else False

    def __enter__(self) -> "BackgroundWindowSession":
        if not is_window(self.hwnd):
            raise OSError("綁定的遊戲視窗已不存在")
        placement = WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(WINDOWPLACEMENT)
        if user32.GetWindowPlacement(self.hwnd, ctypes.byref(placement)):
            self.original_placement = placement
        self.original_rect = get_window_rect(self.hwnd)
        return self

    def capture(self) -> np.ndarray | None:
        frame, method = capture_client(self.hwnd)
        self.capture_method = method + ("＋螢幕外還原" if self.offscreen else "")
        return frame

    def click(self, x: int, y: int) -> bool:
        if not USER_ACTIVITY_GUARD.wait_until_allowed(self.hwnd, self.cancelled):
            return False
        return post_click(self.hwnd, x, y)

    def move(self, x: int, y: int) -> bool:
        if not USER_ACTIVITY_GUARD.wait_until_allowed(self.hwnd, self.cancelled):
            return False
        return post_move(self.hwnd, x, y)

    def promote_offscreen(self) -> bool:
        if self.offscreen or not is_window(self.hwnd):
            return self.offscreen
        if self.original_rect is None:
            return False
        size_rect = self.original_rect
        if self.original_placement is not None:
            normal = self.original_placement.rcNormalPosition
            if normal.right > normal.left and normal.bottom > normal.top:
                size_rect = normal
        width = max(320, size_rect.right - size_rect.left)
        height = max(240, size_rect.bottom - size_rect.top)
        virtual_left = user32.GetSystemMetrics(76)
        virtual_top = user32.GetSystemMetrics(77)
        virtual_height = user32.GetSystemMetrics(79)
        # Keep only a non-interactive edge outside the visible work area so the
        # legacy Flash renderer continues to paint without stealing focus.
        offscreen_x = virtual_left
        offscreen_y = virtual_top + virtual_height - 1
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        moved = user32.SetWindowPos(
            self.hwnd,
            HWND_BOTTOM,
            offscreen_x,
            offscreen_y,
            width,
            height,
            SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING | SWP_SHOWWINDOW,
        )
        self.offscreen = bool(moved)
        if self.offscreen:
            time.sleep(0.9)
        return self.offscreen

    def restore(self) -> None:
        if not self.offscreen or not is_window(self.hwnd):
            return
        if self.original_placement is not None:
            self.original_placement.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.SetWindowPlacement(self.hwnd, ctypes.byref(self.original_placement))
        elif self.original_rect is not None:
            width = self.original_rect.right - self.original_rect.left
            height = self.original_rect.bottom - self.original_rect.top
            user32.SetWindowPos(
                self.hwnd,
                HWND_BOTTOM,
                self.original_rect.left,
                self.original_rect.top,
                width,
                height,
                SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
            if self.started_minimized:
                user32.ShowWindow(self.hwnd, SW_MINIMIZE)
        self.offscreen = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.restore()
