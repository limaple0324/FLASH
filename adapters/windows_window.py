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


class Win32WindowBackend:
    """ctypes-based backend with no third-party Windows dependency."""

    def list_windows(self) -> list[WindowInfo]:
        if os.name != "nt":
            return []

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
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

        self._last_match = match
        return OperationResult(
            success=True,
            code="window.ready",
            message="Exactly one visible target window was identified safely.",
            details={"title": match.title, "handle": match.handle, "rect": match.rect},
        )

    def health_check(self) -> OperationResult:
        return self.find_target()

    def shutdown(self) -> None:
        self._last_match = None
