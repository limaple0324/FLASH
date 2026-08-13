"""Safe Windows target capture and background timed-click delivery."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Iterable
from ctypes import wintypes
from typing import Protocol

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_pointer_sync import PointerMessageBackend
from adapters.windows_window import WindowBackend, WindowInfo
from services.game_time_timed_click_service import (
    TimedClickPressReceipt,
    TimedClickTarget,
)


class CursorClientPointReader(Protocol):
    def screen_position(self) -> tuple[int, int] | None: ...

    def read(
        self,
        handle: int,
        screen_position: tuple[int, int] | None = None,
    ) -> tuple[float, float] | None: ...


class Win32CursorClientPointReader:
    """Read one cursor point without activating, moving, or clicking a window."""

    @staticmethod
    def _user32():
        return ctypes.windll.user32 if os.name == "nt" else None

    @staticmethod
    def _configure(user32) -> None:
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

    def screen_position(self) -> tuple[int, int] | None:
        user32 = self._user32()
        if user32 is None:
            return None
        self._configure(user32)
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def read(
        self,
        handle: int,
        screen_position: tuple[int, int] | None = None,
    ) -> tuple[float, float] | None:
        user32 = self._user32()
        if user32 is None or not isinstance(handle, int) or handle <= 0:
            return None
        self._configure(user32)
        position = screen_position or self.screen_position()
        if position is None:
            return None
        point = wintypes.POINT(*position)
        rect = wintypes.RECT()
        hwnd = wintypes.HWND(handle)
        if not user32.ScreenToClient(hwnd, ctypes.byref(point)):
            return None
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 1 or height <= 1:
            return None
        if not 0 <= point.x < width or not 0 <= point.y < height:
            return None
        return (
            point.x / (width - 1),
            point.y / (height - 1),
        )


class WindowsTimedClickBackend:
    """Resolve a configured fingerprint on every operation and never guess."""

    def __init__(
        self,
        window_backend: WindowBackend,
        message_backend: PointerMessageBackend,
        *,
        point_reader: CursorClientPointReader | None = None,
    ) -> None:
        self._window_backend = window_backend
        self._message_backend = message_backend
        self._point_reader = point_reader or Win32CursorClientPointReader()

    @staticmethod
    def _allowed(values: Iterable[str]) -> tuple[str, ...]:
        normalized = tuple(
            fingerprint
            for value in values
            if (fingerprint := normalize_launch_fingerprint(value)) is not None
        )
        if not normalized or len(normalized) != len(set(normalized)):
            return ()
        return normalized

    def _configured_windows(
        self,
        allowed_fingerprints: Iterable[str],
    ) -> tuple[WindowInfo, ...]:
        allowed = frozenset(self._allowed(allowed_fingerprints))
        if not allowed:
            return ()
        windows = tuple(
            window
            for window in self._window_backend.list_windows()
            if normalize_launch_fingerprint(window.launch_fingerprint) in allowed
        )
        fingerprints = tuple(
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        )
        process_ids = tuple(window.process_id for window in windows)
        if (
            any(value is None for value in fingerprints)
            or len(fingerprints) != len(set(fingerprints))
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in process_ids
            )
            or len(process_ids) != len(set(process_ids))
        ):
            return ()
        return windows

    def capture_target(
        self,
        allowed_fingerprints: Iterable[str],
    ) -> TimedClickTarget | None:
        allowed = self._allowed(allowed_fingerprints)
        if not allowed:
            return None
        screen_position = self._point_reader.screen_position()
        if screen_position is None:
            return None
        handle = self._window_backend.top_window_at(*screen_position)
        if not isinstance(handle, int) or handle <= 0:
            return None
        windows = self._configured_windows(allowed)
        matches = tuple(window for window in windows if window.handle == handle)
        if len(matches) != 1:
            return None
        ratios = self._point_reader.read(handle, screen_position)
        if ratios is None:
            return None
        fingerprint = normalize_launch_fingerprint(matches[0].launch_fingerprint)
        if fingerprint is None:
            return None
        return TimedClickTarget(
            fingerprint,
            ratios[0],
            ratios[1],
            matches[0].title,
        )

    def press(
        self,
        target: TimedClickTarget,
        allowed_fingerprints: Iterable[str],
    ) -> TimedClickPressReceipt | None:
        allowed = self._allowed(allowed_fingerprints)
        if target.fingerprint not in allowed:
            return None
        windows = self._configured_windows(allowed)
        matches = tuple(
            window
            for window in windows
            if normalize_launch_fingerprint(window.launch_fingerprint)
            == target.fingerprint
        )
        if len(matches) != 1:
            return None
        handle = matches[0].handle
        if (
            not self._message_backend.is_window(handle)
            or not self._message_backend.probe_responsive(
                handle,
                1_000,
            )
        ):
            return None
        down = self._message_backend.send_pointer(
            handle,
            target.x_ratio,
            target.y_ratio,
            "left_down",
        )
        move = self._message_backend.send_pointer(
            handle,
            target.x_ratio,
            target.y_ratio,
            "move",
        )
        if not down or not move:
            if down:
                self._message_backend.send_pointer(
                    handle,
                    target.x_ratio,
                    target.y_ratio,
                    "left_up",
                )
            return None
        return TimedClickPressReceipt(
            handle,
            target.x_ratio,
            target.y_ratio,
        )

    def release(self, receipt: TimedClickPressReceipt) -> bool:
        return bool(
            self._message_backend.is_window(receipt.handle)
            and self._message_backend.send_pointer(
                receipt.handle,
                receipt.x_ratio,
                receipt.y_ratio,
                "left_up",
            )
        )
