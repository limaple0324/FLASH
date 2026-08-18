"""Safe Windows target capture and background timed-click delivery."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from threading import Timer
from typing import Protocol

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_pointer_sync import PointerMessageBackend
from adapters.windows_window import (
    Win32WindowBackend,
    WindowBackend,
    WindowInfo,
    complete_window_instance_identity,
)
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


class TargetMarkerBackend(Protocol):
    """Draw and erase a native marker without sending window input."""

    def draw(
        self,
        window: WindowInfo,
        target: TimedClickTarget,
    ) -> object | None: ...

    def erase(self, token: object) -> bool: ...


class Win32FocusMarkerBackend:
    """Use the native reversible focus frame on an existing client surface."""

    _MARKER_HALF_SIZE = 6

    @staticmethod
    def _user32():
        return ctypes.windll.user32 if os.name == "nt" else None

    @staticmethod
    def _configure(user32) -> None:
        user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = (wintypes.HWND, ctypes.c_void_p)
        user32.ReleaseDC.restype = ctypes.c_int
        user32.DrawFocusRect.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.DrawFocusRect.restype = wintypes.BOOL

    @classmethod
    def _marker_rect(
        cls,
        client: wintypes.RECT,
        target: TimedClickTarget,
    ) -> wintypes.RECT | None:
        width = int(client.right - client.left)
        height = int(client.bottom - client.top)
        if width <= 2 * cls._MARKER_HALF_SIZE or height <= 2 * cls._MARKER_HALF_SIZE:
            return None
        x = round(target.x_ratio * (width - 1))
        y = round(target.y_ratio * (height - 1))
        left = max(0, min(width - 2 * cls._MARKER_HALF_SIZE, x - cls._MARKER_HALF_SIZE))
        top = max(0, min(height - 2 * cls._MARKER_HALF_SIZE, y - cls._MARKER_HALF_SIZE))
        return wintypes.RECT(
            left,
            top,
            left + 2 * cls._MARKER_HALF_SIZE,
            top + 2 * cls._MARKER_HALF_SIZE,
        )

    def draw(
        self,
        window: WindowInfo,
        target: TimedClickTarget,
    ) -> object | None:
        user32 = self._user32()
        if user32 is None or not isinstance(window.handle, int):
            return None
        self._configure(user32)
        hwnd = wintypes.HWND(window.handle)
        client = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client)):
            return None
        marker = self._marker_rect(client, target)
        if marker is None:
            return None
        device_context = user32.GetDC(hwnd)
        if not device_context:
            return None
        try:
            if not user32.DrawFocusRect(
                device_context,
                ctypes.byref(marker),
            ):
                return None
        finally:
            user32.ReleaseDC(hwnd, device_context)
        return (window.handle, tuple(int(value) for value in marker))

    def erase(self, token: object) -> bool:
        if (
            not isinstance(token, tuple)
            or len(token) != 2
            or not isinstance(token[0], int)
            or not isinstance(token[1], tuple)
            or len(token[1]) != 4
        ):
            return False
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(token[0])
        marker = wintypes.RECT(*token[1])
        device_context = user32.GetDC(hwnd)
        if not device_context:
            return False
        try:
            return bool(user32.DrawFocusRect(device_context, ctypes.byref(marker)))
        finally:
            user32.ReleaseDC(hwnd, device_context)


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


def legacy_sync_group_from_title(title: object) -> str | None:
    """Read the running legacy group from its public top-level window title."""

    if not isinstance(title, str):
        return None
    prefix = "輔V0.2 - "
    suffix = " - 同步中"
    if not title.startswith(prefix) or not title.endswith(suffix):
        return None
    group_name = title[len(prefix) : -len(suffix)].strip()
    return group_name or None


class Win32LegacySyncStatusProvider:
    """Observe an active legacy sync session without controlling that process."""

    def __init__(
        self,
        window_backend: WindowBackend | None = None,
        process_path_provider: Callable[[int], str | None] | None = None,
    ) -> None:
        self._window_backend = window_backend or Win32WindowBackend()
        self._process_path_provider = (
            process_path_provider or self._process_path
        )

    @staticmethod
    def _process_path(process_id: int) -> str | None:
        if os.name != "nt" or process_id <= 0:
            return None
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, int(process_id))
        if not process:
            return None
        try:
            path_buffer = ctypes.create_unicode_buffer(32768)
            path_length = wintypes.DWORD(len(path_buffer))
            if not kernel32.QueryFullProcessImageNameW(
                process,
                0,
                path_buffer,
                ctypes.byref(path_length),
            ):
                return None
            return path_buffer.value
        finally:
            kernel32.CloseHandle(process)

    def active_group_names(self) -> tuple[str, ...]:
        try:
            windows = tuple(self._window_backend.list_windows())
        except Exception:
            return ()
        groups = []
        for window in windows:
            group_name = legacy_sync_group_from_title(window.title)
            if (
                group_name is None
                or window.window_class != "TkTopLevel"
                or not isinstance(window.process_id, int)
                or window.process_id <= 0
            ):
                continue
            try:
                executable_path = self._process_path_provider(window.process_id)
            except Exception:
                continue
            if (
                isinstance(executable_path, str)
                and Path(executable_path).name.casefold()
                == "輔v0.2.exe".casefold()
            ):
                groups.append(group_name)
        return tuple(dict.fromkeys(groups))


class WindowsTimedClickBackend:
    """Resolve a configured fingerprint on every operation and never guess."""

    def __init__(
        self,
        window_backend: WindowBackend,
        message_backend: PointerMessageBackend,
        *,
        point_reader: CursorClientPointReader | None = None,
        marker_backend: TargetMarkerBackend | None = None,
        synchronized_windows_provider: (
            Callable[[], Iterable[WindowInfo]] | None
        ) = None,
        synchronization_active_provider: (
            Callable[[], bool | None] | None
        ) = None,
    ) -> None:
        self._window_backend = window_backend
        self._message_backend = message_backend
        self._point_reader = point_reader or Win32CursorClientPointReader()
        self._marker_backend = marker_backend
        self._synchronized_windows_provider = synchronized_windows_provider
        self._synchronization_active_provider = synchronization_active_provider

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

    def _synchronization_active(self) -> bool | None:
        if self._synchronization_active_provider is None:
            return None
        try:
            active = self._synchronization_active_provider()
        except Exception:
            return False
        return active if active is None or type(active) is bool else False

    def _synchronized_windows(
        self,
        target: TimedClickTarget,
        allowed_fingerprints: tuple[str, ...],
    ) -> tuple[WindowInfo, ...]:
        if self._synchronized_windows_provider is None:
            return ()
        try:
            windows = tuple(self._synchronized_windows_provider())
        except Exception:
            return ()
        identities = tuple(
            complete_window_instance_identity(window) for window in windows
        )
        if len(windows) < 2 or any(identity is None for identity in identities):
            return ()
        complete_identities = tuple(
            identity for identity in identities if identity is not None
        )
        fingerprints = tuple(identity[0] for identity in complete_identities)
        handles = tuple(identity[1] for identity in complete_identities)
        process_ids = tuple(identity[2] for identity in complete_identities)
        allowed_order = {
            fingerprint: index
            for index, fingerprint in enumerate(allowed_fingerprints)
        }
        positions = tuple(
            allowed_order.get(fingerprint, -1) for fingerprint in fingerprints
        )
        if (
            target.fingerprint != allowed_fingerprints[0]
            or fingerprints[0] != target.fingerprint
            or any(position < 0 for position in positions)
            or positions != tuple(sorted(positions))
            or len(fingerprints) != len(set(fingerprints))
            or len(handles) != len(set(handles))
            or len(process_ids) != len(set(process_ids))
            or len(complete_identities) != len(set(complete_identities))
        ):
            return ()
        return windows

    def _marker_windows(
        self,
        target: TimedClickTarget,
        allowed_fingerprints: tuple[str, ...],
    ) -> tuple[WindowInfo, ...]:
        if self._synchronized_windows_provider is None:
            return ()
        if not allowed_fingerprints or target.fingerprint != allowed_fingerprints[0]:
            return ()
        try:
            windows = tuple(self._synchronized_windows_provider())
        except Exception:
            return ()
        if not 1 <= len(windows) <= 14 or len(windows) != len(allowed_fingerprints):
            return ()
        identities = tuple(
            complete_window_instance_identity(window) for window in windows
        )
        if any(identity is None for identity in identities):
            return ()
        fingerprints = tuple(identity[0] for identity in identities if identity is not None)
        if fingerprints != allowed_fingerprints or len(set(fingerprints)) != len(fingerprints):
            return ()
        return windows

    def show_target_markers(
        self,
        target: TimedClickTarget,
        allowed_fingerprints: Iterable[str],
        *,
        duration_seconds: float = 3.0,
    ) -> bool:
        """Show one native marker per current sync target, then erase them."""
        marker_backend = self._marker_backend
        if marker_backend is None:
            return False
        allowed = self._allowed(allowed_fingerprints)
        windows = self._marker_windows(target, allowed)
        if not windows:
            return False
        identities = tuple(self._dispatch_identity(window) for window in windows)
        if any(identity is None for identity in identities):
            return False
        complete_identities = tuple(
            identity for identity in identities if identity is not None
        )
        drawn: list[tuple[tuple[str, int, int, int, str, int], object]] = []

        def draw(
            item: tuple[
                WindowInfo,
                tuple[str, int, int, int, str, int] | None,
            ],
        ) -> tuple[tuple[str, int, int, int, str, int], object] | None:
            window, identity = item
            if identity is None or not self._instance_is_current(identity):
                return None
            token = marker_backend.draw(window, target)
            return None if token is None else (identity, token)

        with ThreadPoolExecutor(
            max_workers=min(14, len(windows)),
            thread_name_prefix="timed-click-marker",
        ) as executor:
            results = tuple(executor.map(draw, zip(windows, identities)))
        if any(result is None for result in results):
            for result in results:
                if result is not None:
                    marker_backend.erase(result[1])
            return False
        drawn = [result for result in results if result is not None]

        def erase() -> None:
            def erase_one(
                item: tuple[
                    tuple[str, int, int, int, str, int],
                    object,
                ],
            ) -> None:
                identity, token = item
                if self._instance_is_current(identity):
                    marker_backend.erase(token)

            with ThreadPoolExecutor(
                max_workers=min(14, len(drawn)),
                thread_name_prefix="timed-click-marker-erase",
            ) as executor:
                tuple(executor.map(erase_one, tuple(drawn)))

        timer = Timer(max(0.0, float(duration_seconds)), erase)
        timer.daemon = True
        timer.start()
        return True

    def _ready(self, window: WindowInfo) -> bool:
        return bool(
            self._message_backend.is_window(window.handle)
            and self._message_backend.probe_responsive(window.handle, 1_000)
        )

    @staticmethod
    def _dispatch_identity(
        window: WindowInfo,
    ) -> tuple[str, int, int, int, str, int] | None:
        identity = complete_window_instance_identity(window)
        return None if identity is None else identity[:6]

    def _instance_is_current(
        self,
        expected: tuple[str, int, int, int, str, int],
    ) -> bool:
        try:
            matches = tuple(
                window
                for window in self._window_backend.list_windows()
                if window.handle == expected[1]
            )
        except Exception:
            return False
        return bool(
            len(matches) == 1
            and self._dispatch_identity(matches[0]) == expected
        )

    def _send_if_current(
        self,
        expected: tuple[str, int, int, int, str, int],
        target: TimedClickTarget,
        event: str,
    ) -> bool:
        handle = expected[1]
        return bool(
            self._instance_is_current(expected)
            and self._message_backend.is_window(handle)
            and self._message_backend.send_pointer(
                handle,
                target.x_ratio,
                target.y_ratio,
                event,
            )
        )

    def _send_press(
        self,
        expected: tuple[str, int, int, int, str, int],
        target: TimedClickTarget,
    ) -> bool:
        down = self._send_if_current(expected, target, "left_down")
        move = bool(down and self._send_if_current(expected, target, "move"))
        if down and not move:
            self._send_if_current(expected, target, "left_up")
        return bool(down and move)

    def _release_instances(
        self,
        identities: tuple[tuple[str, int, int, int, str, int], ...],
        x_ratio: float,
        y_ratio: float,
    ) -> bool:
        target = TimedClickTarget(
            identities[0][0],
            x_ratio,
            y_ratio,
        )

        def release(
            identity: tuple[str, int, int, int, str, int],
        ) -> bool:
            return self._send_if_current(
                identity,
                target,
                "left_up",
            )

        with ThreadPoolExecutor(
            max_workers=min(14, len(identities)),
            thread_name_prefix="timed-click-release",
        ) as executor:
            released = tuple(executor.map(release, identities))
        return all(released)

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
        synchronization_active = self._synchronization_active()
        if synchronization_active is False:
            return None
        if synchronization_active is True:
            windows = self._synchronized_windows(target, allowed)
            if not windows:
                return None
            identities = tuple(
                self._dispatch_identity(window) for window in windows
            )
            if any(identity is None for identity in identities):
                return None
            complete_identities = tuple(
                identity for identity in identities if identity is not None
            )
            with ThreadPoolExecutor(
                max_workers=min(14, len(windows)),
                thread_name_prefix="timed-click-preflight",
            ) as executor:
                ready = tuple(executor.map(self._ready, windows))
            if not all(ready):
                return None
            handles = tuple(window.handle for window in windows)
            with ThreadPoolExecutor(
                max_workers=min(14, len(complete_identities)),
                thread_name_prefix="timed-click-press",
            ) as executor:
                pressed = tuple(
                    executor.map(
                        lambda identity: self._send_press(identity, target),
                        complete_identities,
                    )
                )
            successful_identities = tuple(
                identity
                for identity, delivered in zip(complete_identities, pressed)
                if delivered
            )
            if not all(pressed):
                if successful_identities:
                    self._release_instances(
                        successful_identities,
                        target.x_ratio,
                        target.y_ratio,
                    )
                return None
            return TimedClickPressReceipt(
                handles[0],
                target.x_ratio,
                target.y_ratio,
                handles,
                complete_identities,
            )
        windows = self._configured_windows(allowed)
        matches = tuple(
            window
            for window in windows
            if normalize_launch_fingerprint(window.launch_fingerprint)
            == target.fingerprint
        )
        if len(matches) != 1:
            return None
        identity = self._dispatch_identity(matches[0])
        if identity is None:
            return None
        handle = identity[1]
        if (
            not self._message_backend.is_window(handle)
            or not self._message_backend.probe_responsive(
                handle,
                1_000,
            )
        ):
            return None
        if not self._send_press(identity, target):
            return None
        return TimedClickPressReceipt(
            handle,
            target.x_ratio,
            target.y_ratio,
            instance_identities=(identity,),
        )

    def release(self, receipt: TimedClickPressReceipt) -> bool:
        if not receipt.instance_identities:
            return False
        return self._release_instances(
            receipt.instance_identities,
            receipt.x_ratio,
            receipt.y_ratio,
        )
