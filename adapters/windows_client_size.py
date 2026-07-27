"""Read and change one verified Windows game's client-area size."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol


class WindowClientSizeBackend(Protocol):
    def read(self, handle: int) -> tuple[int, int] | None:
        """Return the client width and height for one exact top-level window."""

    def resize(self, handle: int, width: int, height: int) -> bool:
        """Resize the client area without activating or moving the window."""


class Win32WindowClientSizeBackend:
    """Native client-area sizing compatible with the known legacy behavior."""

    SYSTEM_AWARE_DPI_CONTEXT = ctypes.c_void_p(-2)
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        user32.SetWindowPos.restype = wintypes.BOOL
        set_context = getattr(
            user32,
            "SetThreadDpiAwarenessContext",
            None,
        )
        if set_context is not None:
            set_context.argtypes = (ctypes.c_void_p,)
            set_context.restype = ctypes.c_void_p

    @classmethod
    def _enter_dpi_context(cls, user32):
        set_context = getattr(
            user32,
            "SetThreadDpiAwarenessContext",
            None,
        )
        if set_context is None:
            return None
        try:
            return set_context(cls.SYSTEM_AWARE_DPI_CONTEXT)
        except OSError:
            return None

    @staticmethod
    def _leave_dpi_context(user32, previous) -> None:
        if not previous:
            return
        set_context = getattr(
            user32,
            "SetThreadDpiAwarenessContext",
            None,
        )
        if set_context is None:
            return
        try:
            set_context(ctypes.c_void_p(previous))
        except OSError:
            pass

    @staticmethod
    def _read_client_size(
        user32,
        hwnd: wintypes.HWND,
    ) -> tuple[int, int] | None:
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        width = int(rect.right) - int(rect.left)
        height = int(rect.bottom) - int(rect.top)
        if width <= 0 or height <= 0:
            return None
        return width, height

    def read(self, handle: int) -> tuple[int, int] | None:
        user32 = self._user32()
        if user32 is None:
            return None
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return None
        previous = self._enter_dpi_context(user32)
        try:
            return self._read_client_size(user32, hwnd)
        finally:
            self._leave_dpi_context(user32, previous)

    def resize(self, handle: int, width: int, height: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False
        previous = self._enter_dpi_context(user32)
        try:
            window_rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
                return False
            client_size = self._read_client_size(user32, hwnd)
            if client_size is None:
                return False
            client_width, client_height = client_size
            outer_width = int(window_rect.right) - int(window_rect.left)
            outer_height = int(window_rect.bottom) - int(window_rect.top)
            frame_width = max(0, outer_width - client_width)
            frame_height = max(0, outer_height - client_height)
            target_outer_width = max(100, int(width) + frame_width)
            target_outer_height = max(100, int(height) + frame_height)
            return bool(
                user32.SetWindowPos(
                    hwnd,
                    wintypes.HWND(0),
                    int(window_rect.left),
                    int(window_rect.top),
                    target_outer_width,
                    target_outer_height,
                    self.SWP_NOZORDER | self.SWP_NOACTIVATE,
                )
            )
        finally:
            self._leave_dpi_context(user32, previous)
