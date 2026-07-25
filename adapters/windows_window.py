"""Read-only Windows target-window detection for FLASH SP1.

This adapter never sends input. It identifies a target window and verifies only
explicit operation areas before future automation is allowed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Protocol

from core.sp1_boundaries import ExternalAdapter, OperationResult


def _configure_user32_window_api(user32):
    """Apply pointer-safe ctypes signatures to the user32 calls used here."""
    import ctypes
    from ctypes import wintypes

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.EnumWindows.argtypes = (enum_proc_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND

    return enum_proc_type


def _handle_value(handle) -> int:
    """Normalize an HWND returned by ctypes or a test double without narrowing it."""
    value = getattr(handle, "value", handle)
    return int(value) if value else 0


@dataclass(frozen=True, slots=True)
class WindowInfo:
    handle: int
    title: str
    visible: bool
    minimized: bool
    rect: tuple[int, int, int, int]
    process_id: int | None = None
    window_class: str | None = None


@dataclass(frozen=True, slots=True)
class OperationArea:
    """A named rectangle relative to the target window, using values from 0 to 1."""

    name: str
    rect: tuple[float, float, float, float]

    def validate(self) -> None:
        left, top, right, bottom = self.rect
        if not self.name.strip():
            raise ValueError("Operation area name must not be empty.")
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            raise ValueError(f"Invalid relative operation area: {self.rect}")


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
        enum_proc_type = _configure_user32_window_api(user32)

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

            process_id = None
            try:
                process_id_value = wintypes.DWORD()
                thread_id = user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(process_id_value),
                )
                if thread_id and process_id_value.value:
                    process_id = int(process_id_value.value)
            except OSError:
                pass

            window_class = None
            try:
                class_buffer = ctypes.create_unicode_buffer(256)
                class_length = user32.GetClassNameW(
                    hwnd,
                    class_buffer,
                    len(class_buffer),
                )
                if class_length > 0:
                    window_class = class_buffer.value.strip() or None
            except OSError:
                pass

            windows.append(
                WindowInfo(
                    handle=_handle_value(hwnd),
                    title=title,
                    visible=True,
                    minimized=bool(user32.IsIconic(hwnd)),
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    process_id=process_id,
                    window_class=window_class,
                )
            )
            return True

        if not user32.EnumWindows(enum_proc_type(callback), 0):
            return []
        return windows

    def foreground_handle(self) -> int | None:
        user32 = self._user32()
        if user32 is None:
            return None
        _configure_user32_window_api(user32)
        handle = _handle_value(user32.GetForegroundWindow())
        return handle or None

    def top_window_at(self, x: int, y: int) -> int | None:
        user32 = self._user32()
        if user32 is None:
            return None

        from ctypes import wintypes

        _configure_user32_window_api(user32)
        point = wintypes.POINT(x, y)
        handle = _handle_value(user32.WindowFromPoint(point))
        if not handle:
            return None

        ga_root = 2
        root = _handle_value(user32.GetAncestor(wintypes.HWND(handle), ga_root))
        return root or None


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
    def _area_sample_points(
        window_rect: tuple[int, int, int, int], area: OperationArea
    ) -> tuple[tuple[int, int], ...]:
        area.validate()
        win_left, win_top, win_right, win_bottom = window_rect
        width = win_right - win_left
        height = win_bottom - win_top
        rel_left, rel_top, rel_right, rel_bottom = area.rect

        left = win_left + int(width * rel_left)
        top = win_top + int(height * rel_top)
        right = win_left + int(width * rel_right)
        bottom = win_top + int(height * rel_bottom)
        inset_x = max(1, min(8, (right - left) // 5))
        inset_y = max(1, min(8, (bottom - top) // 5))

        return (
            ((left + right) // 2, (top + bottom) // 2),
            (left + inset_x, top + inset_y),
            (right - inset_x - 1, top + inset_y),
            (left + inset_x, bottom - inset_y - 1),
            (right - inset_x - 1, bottom - inset_y - 1),
        )

    def _check_foreground(self, match: WindowInfo) -> OperationResult | None:
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
        return None

    def _check_operation_areas(
        self, match: WindowInfo, operation_areas: Iterable[OperationArea]
    ) -> OperationResult | None:
        covered: list[dict[str, object]] = []
        checked_names: list[str] = []

        for area in operation_areas:
            area.validate()
            checked_names.append(area.name)
            for x, y in self._area_sample_points(match.rect, area):
                top_handle = self._backend.top_window_at(x, y)
                if top_handle != match.handle:
                    covered.append(
                        {
                            "area": area.name,
                            "point": (x, y),
                            "covering_handle": top_handle,
                        }
                    )

        if covered:
            return OperationResult(
                success=False,
                code="operation_area.overlapped",
                message="A required operation area is covered; input must remain disabled.",
                details={
                    "title": match.title,
                    "handle": match.handle,
                    "covered": tuple(covered),
                    "checked_areas": tuple(checked_names),
                },
            )
        return None

    def find_target(self, operation_areas: Iterable[OperationArea] = ()) -> OperationResult:
        self._last_match = None
        if not self._keywords:
            return OperationResult(False, "window.not_configured", "No target-window title keyword is configured.")

        matches = [
            window
            for window in self._backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
        ]

        if not matches:
            return OperationResult(
                False,
                "window.not_found",
                "No visible window matched the configured title keywords.",
                {"keywords": self._keywords},
            )
        if len(matches) > 1:
            return OperationResult(
                False,
                "window.ambiguous",
                "More than one visible window matched; input must remain disabled.",
                {"count": len(matches), "titles": tuple(item.title for item in matches)},
            )

        match = matches[0]
        left, top, right, bottom = match.rect
        if right <= left or bottom <= top:
            return OperationResult(
                False,
                "window.invalid_bounds",
                "The target window has invalid screen bounds.",
                {"title": match.title, "rect": match.rect},
            )
        if match.minimized:
            return OperationResult(
                False,
                "window.minimized",
                "The target window is minimized; input must remain disabled.",
                {"title": match.title, "handle": match.handle},
            )

        focus_issue = self._check_foreground(match)
        if focus_issue is not None:
            return focus_issue

        areas = tuple(operation_areas)
        area_issue = self._check_operation_areas(match, areas)
        if area_issue is not None:
            return area_issue

        self._last_match = match
        return OperationResult(
            success=True,
            code="window.ready",
            message=(
                "The target window is ready and all requested operation areas are unobstructed."
                if areas
                else "The target window is ready; no operation area was requested."
            ),
            details={
                "title": match.title,
                "handle": match.handle,
                "rect": match.rect,
                "checked_areas": tuple(area.name for area in areas),
                "input_enabled": False,
            },
        )

    def health_check(self) -> OperationResult:
        """Check target identity and focus without assuming any future click location."""
        return self.find_target()

    def shutdown(self) -> None:
        self._last_match = None
