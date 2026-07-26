"""Place one already verified Windows game window at a saved position."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol

from services.group_launch_service import SavedWindowPlacement


class WindowPlacementBackend(Protocol):
    def place(
        self,
        handle: int,
        placement: SavedWindowPlacement,
    ) -> bool:
        """Restore, move, and resize one exact top-level window."""


class Win32WindowPlacementBackend:
    SW_RESTORE = 9
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindowAsync.restype = wintypes.BOOL
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

    def place(
        self,
        handle: int,
        placement: SavedWindowPlacement,
    ) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindowAsync(hwnd, self.SW_RESTORE)
        return bool(
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(0),
                placement.x,
                placement.y,
                placement.width,
                placement.height,
                self.SWP_NOZORDER
                | self.SWP_NOACTIVATE
                | self.SWP_SHOWWINDOW,
            )
        )
