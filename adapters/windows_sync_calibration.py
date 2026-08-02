"""Non-activating cursor and client-region reads for sync calibration."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from adapters.windows_background_capture import (
    CaptureSample,
    _BITMAPINFO,
    _configure_win32_capture_api,
)


class Win32SyncCalibrationBackend:
    SRCCOPY = 0x00CC0020

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

    def capture_client_region(
        self,
        window_handle: int,
        region: tuple[int, int, int, int],
    ) -> CaptureSample | None:
        user32, gdi32 = self._libraries()
        if user32 is None or gdi32 is None or not window_handle:
            return None
        left, top, right, bottom = region
        width = max(1, int(right) - int(left))
        height = max(1, int(bottom) - int(top))
        _configure_win32_capture_api(user32, gdi32)
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = wintypes.HDC
        gdi32.BitBlt.argtypes = (
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        )
        gdi32.BitBlt.restype = wintypes.BOOL
        hwnd = wintypes.HWND(window_handle)
        window_dc = user32.GetDC(hwnd)
        if not window_dc:
            return None
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        old_object = None
        bitmap_selected = False
        try:
            if not memory_dc or not bitmap:
                return None
            old_object = gdi32.SelectObject(memory_dc, bitmap)
            if not old_object:
                return None
            bitmap_selected = True
            api_succeeded = bool(
                gdi32.BitBlt(
                    memory_dc,
                    0,
                    0,
                    width,
                    height,
                    window_dc,
                    int(left),
                    int(top),
                    self.SRCCOPY,
                )
            )
            restored = gdi32.SelectObject(memory_dc, old_object)
            if not restored:
                return None
            bitmap_selected = False
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(info.bmiHeader)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0
            buffer = (ctypes.c_ubyte * (width * height * 4))()
            copied = gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                ctypes.byref(buffer),
                ctypes.byref(info),
                0,
            )
            if copied != height:
                return None
            return CaptureSample(
                width=width,
                height=height,
                pixels=bytes(buffer),
                api_succeeded=api_succeeded,
            )
        finally:
            if bitmap_selected and old_object and memory_dc:
                gdi32.SelectObject(memory_dc, old_object)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)
