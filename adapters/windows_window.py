"""Read-only Windows target-window detection for FLASH SP1.

This adapter never sends input. It only enumerates visible top-level windows and
reports whether a configured target can be identified safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Protocol

from core.sp1_boundaries import ExternalAdapter, OperationResult


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str
    visible: bool
    minimized: bool
    rect: tuple[int, int, int, int]


class WindowBackend(Protocol):
    def list_windows(self) -> Iterable[WindowInfo]:
        """Return visible top-level windows available to the current process."""

    def foreground_handle(self) -> int | None:
        """Return the current foreground top-level window handle."""

    def top_window_at(self, x: int, y: int) -> int | None:
        """Return the root window visible at the supplied screen point."""


class Win32WindowBackend:
    """ctypes-based backend with no third-party Windows dependency."""

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        import ctypes

        return ctypes.windll.user32

    def list_windows(self) -> list[WindowInfo]:
        user32 = self._user32()
        if user32 is None:
            return []

        import ctypes
        from ctypes import wintypes

        windows: list[WindowInfo] = []
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True

            windows.append(
                WindowInfo(
                    handle=int(hwnd),
                    title=title,
                    visible=True,
                    minimized=bool(user32.IsIconic(hwnd)),
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                )
            )
            return True

        user32.EnumWindows(enum_proc_type(callback), 0)
        return windows

    def foreground_handle(self) -> int | None:
        user32 = self._user32()
        if user32 is None:
            return None
        handle = int(user32.GetForegroundWindow())
        return handle or None

    def top_window_at(self, x: int, y: int) -> int | None:
        user32 = self._user32()
        if user32 is None:
            return None

        import ctypes
        from ctypes import wintypes

        point = wintypes.POINT(x, y)
        handle = int(user32.WindowFromPoint(point))
        if not handle:
            return None

        # WindowFromPoint may return a child control. GA_ROOT resolves the
        # containing top-level window used by the rest of the adapter.
        ga_root = 2
        root = int(user32.GetAncestor(ctypes.c_void_p(handle), ga_root))
        return root or handle


class WindowsWindowAdapter(ExternalAdapter):
    """Read-only target-window adapter used before any automation is allowed."""

    def __init__(self, title_keywords: Iterable[str], backend: WindowBackend | None = None):
        self._keywords = tuple(keyword.strip().casefold() for keyword in title_keywords if keyword.strip())
        self._backend = backend or Win32WindowBackend()
        self._last_match: WindowInfo | None = None

    @property
    def name(self) -> str:
        return "windows_target_window"

    @property
    def last_match(self) -> WindowInfo | None:
        return self._last_match

    @staticmethod
    def _sample_points(rect: tuple[int, int, int, int]) -> tuple[tuple[int, int], ...]:
        left, top, right, bottom = rect
        inset_x = max(1, min(24, (right - left) // 10))
        inset_y = max(1, min(24, (bottom - top) // 10))
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        return (
            (center_x, center_y),
            (left + inset_x, top + inset_y),
            (right - inset_x - 1, top + inset_y),
            (left + inset_x, bottom - inset_y - 1),
            (right - inset_x - 1, bottom - inset_y - 1),
        )

    def _check_focus_and_overlap(self, match: WindowInfo) -> OperationResult | None:
        foreground = self._backend.foreground_handle()
        if foreground is None:
            return OperationResult(
                success=False,
                code="window.focus_unknown",
                message="The foreground window could not be verified; input must remain disabled.",
                details={"title": match.title, "handle": match.handle},
            )
        if foreground != match.handle:
            return OperationResult(
                success=False,
                code="window.not_foreground",
                message="The target window is not in the foreground; input must remain disabled.",
                details={"title": match.title, "handle": match.handle, "foreground_handle": foreground},
            )

        covered_points: list[tuple[int, int, int | None]] = []
        for x, y in self._sample_points(match.rect):
            top_handle = self._backend.top_window_at(x, y)
            if top_handle != match.handle:
                covered_points.append((x, y, top_handle))

        if covered_points:
            return OperationResult(
                success=False,
                code="window.overlapped",
                message="Another window covers part of the target; input must remain disabled.",
                details={
                    "title": match.title,
                    "handle": match.handle,
                    "covered_points": tuple(covered_points),
                },
            )
        return None

    def find_target(self) -> OperationResult:
        self._last_match = None
        if not self._keywords:
            return OperationResult(
                success=False,
                code="window.not_configured",
                message="No target-window title keyword is configured.",
            )

        matches = [
            window
            for window in self._backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
        ]

        if not matches:
            return OperationResult(
                success=False,
                code="window.not_found",
                message="No visible window matched the configured title keywords.",
                details={"keywords": self._keywords},
            )

        if len(matches) > 1:
            return OperationResult(
                success=False,
                code="window.ambiguous",
                message="More than one visible window matched; input must remain disabled.",
                details={"count": len(matches), "titles": tuple(item.title for item in matches)},
            )

        match = matches[0]
        left, top, right, bottom = match.rect
        if right <= left or bottom <= top:
            return OperationResult(
                success=False,
                code="window.invalid_bounds",
                message="The target window has invalid screen bounds.",
                details={"title": match.title, "rect": match.rect},
            )

        if match.minimized:
            return OperationResult(
                success=False,
                code="window.minimized",
                message="The target window is minimized; input must remain disabled.",
                details={"title": match.title, "handle": match.handle},
            )

        safety_issue = self._check_focus_and_overlap(match)
        if safety_issue is not None:
            return safety_issue

        self._last_match = match
        return OperationResult(
            success=True,
            code="window.ready",
            message="The target window is uniquely identified, foreground, and unobstructed.",
            details={"title": match.title, "handle": match.handle, "rect": match.rect},
        )

    def health_check(self) -> OperationResult:
        return self.find_target()

    def shutdown(self) -> None:
        self._last_match = None
