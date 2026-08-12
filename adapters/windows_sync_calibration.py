"""Non-activating cursor and client-region reads for sync calibration."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

class Win32SyncCalibrationBackend:
    @staticmethod
    def _libraries():
        if os.name != "nt":
            return None, None
        return ctypes.windll.user32, ctypes.windll.gdi32

    @staticmethod
    def _configure_cursor_api(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.ScreenToClient.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        user32.ScreenToClient.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetClientRect.restype = wintypes.BOOL

    def cursor_client_point(
        self,
        window_handle: int,
    ) -> tuple[int, int] | None:
        user32, _ = self._libraries()
        if user32 is None or not window_handle:
            return None
        self._configure_cursor_api(user32)
        hwnd = wintypes.HWND(window_handle)
        point = wintypes.POINT()
        rect = wintypes.RECT()
        if (
            not user32.IsWindow(hwnd)
            or not user32.GetCursorPos(ctypes.byref(point))
            or not user32.ScreenToClient(hwnd, ctypes.byref(point))
            or not user32.GetClientRect(hwnd, ctypes.byref(rect))
        ):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        x, y = int(point.x), int(point.y)
        if not (0 <= x < width and 0 <= y < height):
            return None
        return x, y
