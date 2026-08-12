"""Read-only Windows target-window detection for FLASH SP1.

This adapter never sends input. It identifies a target window and verifies only
explicit operation areas before future automation is allowed.
"""

from __future__ import annotations

import os
import hashlib
import json
from dataclasses import dataclass, replace
from threading import RLock
from typing import Callable, Iterable, Protocol

from adapters.windows_launch_fingerprint import (
    LaunchFingerprintResolver,
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
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
    launch_fingerprint: str | None = None
    thread_id: int | None = None
    process_lifecycle_token: int | None = None


def complete_window_instance_identity(
    window: WindowInfo,
) -> tuple[
    str,
    int,
    int,
    int,
    str,
    int,
    tuple[int, int, int, int],
    bool,
] | None:
    """Return the complete, current identity required for a safe window action.

    The launch fingerprint identifies an executable, not a top-level window.  A
    caller may therefore use the immutable portion to name one monitored
    instance while retaining the complete tuple for every dispatch recheck.
    """

    if not isinstance(window, WindowInfo):
        return None
    fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
    if (
        fingerprint is None
        or not isinstance(window.handle, int)
        or isinstance(window.handle, bool)
        or window.handle <= 0
        or not isinstance(window.process_id, int)
        or isinstance(window.process_id, bool)
        or window.process_id <= 0
        or not isinstance(window.thread_id, int)
        or isinstance(window.thread_id, bool)
        or window.thread_id <= 0
        or not isinstance(window.window_class, str)
        or not window.window_class.strip()
        or not isinstance(window.process_lifecycle_token, int)
        or isinstance(window.process_lifecycle_token, bool)
        or window.process_lifecycle_token <= 0
        or not isinstance(window.rect, tuple)
        or len(window.rect) != 4
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in window.rect
        )
        or window.rect[2] <= window.rect[0]
        or window.rect[3] <= window.rect[1]
        or type(window.minimized) is not bool
    ):
        return None
    return (
        fingerprint,
        window.handle,
        window.process_id,
        window.thread_id,
        window.window_class,
        window.process_lifecycle_token,
        window.rect,
        window.minimized,
    )


def monitored_window_instance_fingerprint(window: WindowInfo) -> str | None:
    """Derive the stable anonymous identity for one complete live instance.

    Geometry and minimized state remain part of
    :func:`complete_window_instance_identity` and are rechecked before input.
    They are deliberately excluded from this name so a reversible move or
    restore cannot silently become a different monitored role.
    """

    identity = complete_window_instance_identity(window)
    if identity is None:
        return None
    encoded = json.dumps(
        (
            "smart-reconnect-activation-v1",
            *identity[:6],
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    def __init__(
        self,
        fingerprint_resolver: LaunchFingerprintResolver | None = None,
        *,
        process_lifecycle_provider: Callable[[int], int | None] | None = None,
    ):
        self._fingerprint_resolver = fingerprint_resolver
        self._process_lifecycle_provider = (
            process_lifecycle_provider
            or self._process_lifecycle_token
        )
        # A fingerprint (including an unresolved ``None``) is trusted only for
        # one PID creation-time token. Hot input paths reuse this snapshot and
        # never launch PowerShell again for the same process lifecycle.
        self._fingerprint_cache: dict[
            int,
            tuple[int | None, str | None],
        ] = {}
        self._fingerprint_cache_lock = RLock()

    @staticmethod
    def _process_lifecycle_token(process_id: int) -> int | None:
        """Return the process creation FILETIME without reading command lines."""
        if os.name != "nt" or process_id <= 0:
            return None
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(process_id),
        )
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return (
                int(created.dwHighDateTime) << 32
            ) | int(created.dwLowDateTime)
        except OSError:
            return None
        finally:
            kernel32.CloseHandle(handle)

    def _resolve_cached_fingerprints(
        self,
        process_ids: Iterable[int],
    ) -> dict[int, str]:
        if self._fingerprint_resolver is None:
            return {}
        normalized_ids = tuple(
            dict.fromkeys(
                process_id
                for process_id in process_ids
                if isinstance(process_id, int)
                and not isinstance(process_id, bool)
                and process_id > 0
            )
        )
        with self._fingerprint_cache_lock:
            unresolved: dict[int, int] = {}
            for process_id in normalized_ids:
                cached = self._fingerprint_cache.get(process_id)
                if cached is not None and cached[0] is None:
                    # A lifecycle token that could not be proven stays unknown
                    # until an explicit cache invalidation.
                    continue
                try:
                    lifecycle = self._process_lifecycle_provider(
                        process_id
                    )
                except Exception:
                    lifecycle = None
                if lifecycle is None:
                    self._fingerprint_cache[process_id] = (None, None)
                    continue
                if cached is not None and cached[0] == lifecycle:
                    continue
                unresolved[process_id] = lifecycle

            if unresolved:
                try:
                    resolved = self._fingerprint_resolver.resolve(
                        unresolved
                    )
                except Exception:
                    resolved = {}
                for process_id, lifecycle in unresolved.items():
                    self._fingerprint_cache[process_id] = (
                        lifecycle,
                        normalize_launch_fingerprint(
                            resolved.get(process_id)
                        ),
                    )

            return {
                process_id: fingerprint
                for process_id in normalized_ids
                if (
                    (cached := self._fingerprint_cache.get(process_id))
                    is not None
                    and (fingerprint := cached[1]) is not None
                )
            }

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
            window_thread_id = None
            try:
                process_id_value = wintypes.DWORD()
                thread_id = user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(process_id_value),
                )
                if thread_id and process_id_value.value:
                    process_id = int(process_id_value.value)
                    window_thread_id = int(thread_id)
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
                    thread_id=window_thread_id,
                )
            )
            return True

        if not user32.EnumWindows(enum_proc_type(callback), 0):
            return []
        lifecycle_tokens: dict[int, int] = {}
        if self._fingerprint_resolver is not None:
            fingerprints = self._resolve_cached_fingerprints(
                window.process_id
                for window in windows
                if window.process_id is not None
            )
            with self._fingerprint_cache_lock:
                lifecycle_tokens = {
                    process_id: lifecycle
                    for process_id in {
                        window.process_id
                        for window in windows
                        if window.process_id is not None
                    }
                    if (
                        (cached := self._fingerprint_cache.get(process_id))
                        is not None
                        and (lifecycle := cached[0]) is not None
                    )
                }
            windows = [
                replace(
                    window,
                    launch_fingerprint=fingerprints.get(window.process_id),
                    process_lifecycle_token=lifecycle_tokens.get(
                        window.process_id
                    ),
                )
                for window in windows
            ]
        else:
            for process_id in {
                window.process_id
                for window in windows
                if window.process_id is not None
            }:
                try:
                    lifecycle = self._process_lifecycle_provider(
                        process_id
                    )
                except Exception:
                    lifecycle = None
                if lifecycle is not None:
                    lifecycle_tokens[process_id] = lifecycle
            windows = [
                replace(
                    window,
                    process_lifecycle_token=lifecycle_tokens.get(
                        window.process_id
                    ),
                )
                for window in windows
            ]
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

    def __init__(
        self,
        title_keywords: Iterable[str],
        backend: WindowBackend | None = None,
        *,
        launch_fingerprint: object = None,
    ):
        self._keywords = tuple(keyword.strip().casefold() for keyword in title_keywords if keyword.strip())
        self._fingerprint_configured = (
            launch_fingerprint is not None
            and not (isinstance(launch_fingerprint, str) and not launch_fingerprint.strip())
        )
        self._launch_fingerprint = normalize_launch_fingerprint(launch_fingerprint)
        self._backend = backend or Win32WindowBackend(
            fingerprint_resolver=(
                PowerShellLaunchFingerprintResolver()
                if self._launch_fingerprint is not None
                else None
            )
        )
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
        if self._fingerprint_configured and self._launch_fingerprint is None:
            return OperationResult(
                False,
                "window.identity_invalid",
                "The configured anonymous window identity is invalid; input must remain disabled.",
            )

        title_matches = [
            window
            for window in self._backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
        ]

        if not title_matches:
            return OperationResult(
                False,
                "window.not_found",
                "No visible window matched the configured title keywords.",
                {"keywords": self._keywords},
            )
        matches = title_matches
        if self._launch_fingerprint is not None:
            matches = [
                window
                for window in title_matches
                if window.launch_fingerprint == self._launch_fingerprint
            ]
            if not matches:
                return OperationResult(
                    False,
                    "window.identity_not_found",
                    "No title-matched window had the configured anonymous identity; input must remain disabled.",
                    {"title_match_count": len(title_matches)},
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
                "identity_method": (
                    "launch_fingerprint"
                    if self._launch_fingerprint is not None
                    else "title_keywords"
                ),
                "input_enabled": False,
            },
        )

    def health_check(self) -> OperationResult:
        """Check target identity and focus without assuming any future click location."""
        return self.find_target()

    def shutdown(self) -> None:
        self._last_match = None
