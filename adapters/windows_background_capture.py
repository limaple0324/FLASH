"""Read-only Windows background capture probe for FLASH SP1.

This module never sends mouse or keyboard input. It uses the Windows PrintWindow
API to ask a target window to render into an off-screen bitmap, then performs a
small validity check on the captured pixels. Actual support still requires a
real target-desktop test because some legacy renderers return blank frames.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


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

    def capture(self, window_handle: int) -> CaptureSample | None:
        if os.name != "nt" or not window_handle:
            return None

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(window_handle), ctypes.byref(rect)):
            return None

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None

        window_dc = user32.GetWindowDC(wintypes.HWND(window_handle))
        if not window_dc:
            return None

        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        old_object = None
        try:
            if not memory_dc or not bitmap:
                return None

            old_object = gdi32.SelectObject(memory_dc, bitmap)
            # PW_RENDERFULLCONTENT improves capture for some modern and legacy windows.
            api_succeeded = bool(user32.PrintWindow(wintypes.HWND(window_handle), memory_dc, 0x00000002))

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
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
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
            if old_object and memory_dc:
                gdi32.SelectObject(memory_dc, old_object)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(wintypes.HWND(window_handle), window_dc)


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
