"""Read-only Windows background capture probe for FLASH SP1.

This module never sends mouse or keyboard input. It uses the Windows PrintWindow
API to ask a target window to render into an off-screen bitmap, then performs a
small validity check on the captured pixels. Actual support still requires a
real target-desktop test because some legacy renderers return blank frames.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Protocol


class _BITMAPINFOHEADER(ctypes.Structure):
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


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def _configure_win32_capture_api(user32, gdi32) -> None:
    """Apply pointer-safe ctypes signatures to every Win32 capture call."""
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowPlacement.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(_WINDOWPLACEMENT),
    )
    user32.GetWindowPlacement.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.PrintWindow.restype = wintypes.BOOL
    if hasattr(user32, "RedrawWindow"):
        user32.RedrawWindow.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
            wintypes.HRGN,
            wintypes.UINT,
        )
        user32.RedrawWindow.restype = wintypes.BOOL
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int

    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = (
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    )
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL


@dataclass(frozen=True, slots=True)
class CaptureSample:
    width: int
    height: int
    pixels: bytes
    api_succeeded: bool


class WindowCaptureProvider(Protocol):
    def capture(self, window_handle: int) -> CaptureSample | None:
        """Capture a target window without changing focus or sending input."""


class Win32PrintWindowProvider:
    """ctypes implementation of an off-screen PrintWindow capture."""

    RDW_INVALIDATE = 0x0001
    RDW_UPDATENOW = 0x0100
    RDW_ALLCHILDREN = 0x0080

    @staticmethod
    def _libraries():
        return ctypes.windll.user32, ctypes.windll.gdi32

    @staticmethod
    def _capture_rect(user32, hwnd) -> wintypes.RECT | None:
        if user32.IsIconic(hwnd):
            placement = _WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
            if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                return None
            return placement.rcNormalPosition

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect

    def capture(self, window_handle: int) -> CaptureSample | None:
        if os.name != "nt" or not window_handle:
            return None

        user32, gdi32 = self._libraries()
        _configure_win32_capture_api(user32, gdi32)
        hwnd = wintypes.HWND(window_handle)

        rect = self._capture_rect(user32, hwnd)
        if rect is None:
            return None

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None

        window_dc = user32.GetWindowDC(hwnd)
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

            # Legacy Flash projectors can leave an off-screen frame stale after
            # a modal appears. Ask the target to process a paint cycle without
            # activating it or moving the user's pointer.
            redraw_window = getattr(user32, "RedrawWindow", None)
            if redraw_window is not None:
                redraw_window(
                    hwnd,
                    None,
                    None,
                    self.RDW_INVALIDATE
                    | self.RDW_UPDATENOW
                    | self.RDW_ALLCHILDREN,
                )

            # Legacy Flash projectors render more reliably with the documented
            # whole-window mode (flags=0); PW_RENDERFULLCONTENT can return a
            # stale pre-modal frame for background instances.
            api_succeeded = bool(user32.PrintWindow(hwnd, memory_dc, 0))
            restored_object = gdi32.SelectObject(memory_dc, old_object)
            if not restored_object:
                return None
            bitmap_selected = False

            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down buffer
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB

            buffer_size = width * height * 4
            buffer = (ctypes.c_ubyte * buffer_size)()
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
                if gdi32.SelectObject(memory_dc, old_object):
                    bitmap_selected = False
            if bitmap_selected:
                # A selected bitmap cannot be deleted. Releasing its memory DC
                # first makes the bitmap deletable even when restoration failed.
                if memory_dc:
                    gdi32.DeleteDC(memory_dc)
                    memory_dc = None
                if bitmap:
                    gdi32.DeleteObject(bitmap)
            else:
                if bitmap:
                    gdi32.DeleteObject(bitmap)
                if memory_dc:
                    gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)


class Win32RecoveringPrintWindowProvider(Win32PrintWindowProvider):
    """Refresh minimized Flash windows without taking foreground focus.

    A minimized legacy projector can keep returning its pre-modal PrintWindow
    frame. This provider briefly restores it without activation, captures the
    fresh frame, and returns it to the minimized state in ``finally``.
    """

    SW_SHOWNOACTIVATE = 4
    SW_SHOWMINNOACTIVE = 7

    def __init__(
        self,
        *,
        paint_settle_seconds: float = 0.75,
    ) -> None:
        self._paint_settle_seconds = max(0.0, float(paint_settle_seconds))

    def capture(self, window_handle: int) -> CaptureSample | None:
        if os.name != "nt" or not window_handle:
            return None

        user32, _gdi32 = self._libraries()
        hwnd = wintypes.HWND(window_handle)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL

        was_minimized = bool(user32.IsIconic(hwnd))
        temporarily_restored = False
        try:
            if was_minimized:
                user32.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
                temporarily_restored = not bool(user32.IsIconic(hwnd))
                if temporarily_restored and self._paint_settle_seconds:
                    time.sleep(self._paint_settle_seconds)
            return super().capture(window_handle)
        finally:
            if temporarily_restored and not user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, self.SW_SHOWMINNOACTIVE)


class WindowsBackgroundCaptureBackend:
    """Conservative background capability backend.

    Input probes intentionally remain undetermined. They require a user-approved,
    game-specific harmless action and are not performed by this read-only backend.
    """

    def __init__(self, provider: WindowCaptureProvider | None = None):
        self._provider = provider or Win32PrintWindowProvider()
        self.last_sample: CaptureSample | None = None

    @staticmethod
    def _looks_non_blank(sample: CaptureSample) -> bool:
        if sample.width < 2 or sample.height < 2 or len(sample.pixels) < 16:
            return False

        pixels = sample.pixels
        # Sample the buffer instead of constructing a large set for full-HD windows.
        stride = max(4, (len(pixels) // 512) // 4 * 4)
        sampled = pixels[0::stride]
        if not sampled:
            return False

        minimum = min(sampled)
        maximum = max(sampled)
        return maximum - minimum >= 8

    def probe_background_capture(self, window_handle: int) -> bool | None:
        self.last_sample = self._provider.capture(window_handle)
        if self.last_sample is None:
            return None
        if not self.last_sample.api_succeeded:
            return False
        return self._looks_non_blank(self.last_sample)

    def probe_background_input(self, window_handle: int) -> bool | None:
        return None

    def probe_minimized_input(self, window_handle: int) -> bool | None:
        return None
