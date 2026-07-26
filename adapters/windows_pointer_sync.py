"""Safe left-mouse synchronization for uniquely identified Flash windows."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from ctypes import wintypes
from typing import Iterable, Mapping, Protocol

from adapters.windows_input_sync import (
    WindowInputPolicy,
    normalize_input_policy,
)
from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import Win32WindowBackend, WindowBackend, WindowInfo
from services.sync_conflict_arbiter import SyncConflictArbiter
from services.deferred_sync_operation_service import (
    DeferredSyncOperationService,
)
from core.reconnect_policy import ReconnectScreenState
from collections.abc import Callable


POINTER_EVENTS = frozenset({"move", "left_down", "left_up"})


class PointerMessageBackend(Protocol):
    def is_window(self, handle: int) -> bool: ...

    def probe_responsive(self, handle: int, timeout_ms: int) -> bool: ...

    def send_pointer(
        self,
        handle: int,
        x_ratio: float,
        y_ratio: float,
        event: str,
    ) -> bool: ...


class Win32PointerMessageBackend:
    WM_NULL = 0x0000
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001
    SMTO_ABORTIFHUNG = 0x0002

    @staticmethod
    def _user32():
        return ctypes.windll.user32 if os.name == "nt" else None

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetClientRect.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
        user32.SendMessageTimeoutW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        )
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    def is_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        return bool(user32.IsWindow(wintypes.HWND(handle)))

    def probe_responsive(self, handle: int, timeout_ms: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        result = ctypes.c_size_t()
        return bool(
            user32.SendMessageTimeoutW(
                wintypes.HWND(handle),
                self.WM_NULL,
                0,
                0,
                self.SMTO_ABORTIFHUNG,
                max(1, int(timeout_ms)),
                ctypes.byref(result),
            )
        )

    def send_pointer(
        self,
        handle: int,
        x_ratio: float,
        y_ratio: float,
        event: str,
    ) -> bool:
        user32 = self._user32()
        if user32 is None or event not in POINTER_EVENTS:
            return False
        self._configure(user32)
        rect = wintypes.RECT()
        hwnd = wintypes.HWND(handle)
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return False
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        x = min(width - 1, max(0, round(float(x_ratio) * (width - 1))))
        y = min(height - 1, max(0, round(float(y_ratio) * (height - 1))))
        lparam = (int(y) << 16) | (int(x) & 0xFFFF)
        message, flags = {
            "move": (self.WM_MOUSEMOVE, self.MK_LBUTTON),
            "left_down": (self.WM_LBUTTONDOWN, self.MK_LBUTTON),
            "left_up": (self.WM_LBUTTONUP, 0),
        }[event]
        return bool(user32.PostMessageW(hwnd, message, flags, lparam))


@dataclass(frozen=True, slots=True)
class PointerSyncResult:
    expected_windows: int
    discovered_windows: int
    eligible_windows: int
    sent_windows: int
    event: str | None
    failure_codes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            not self.failure_codes
            and self.sent_windows == self.eligible_windows
        )


class WindowsPointerSyncController:
    def __init__(
        self,
        *,
        expected_windows: int,
        title_keywords: Iterable[str],
        window_backend: WindowBackend,
        message_backend: PointerMessageBackend,
        preflight_timeout_ms: int = 1000,
        conflict_arbiter: SyncConflictArbiter | None = None,
        deferred_service: DeferredSyncOperationService | None = None,
        reconnecting_provider: Callable[[], Iterable[str]] | None = None,
        role_operation_callback: (
            Callable[[str, str, str], object] | None
        ) = None,
        screen_state_provider: (
            Callable[[str], ReconnectScreenState | None] | None
        ) = None,
    ) -> None:
        self._expected_windows = max(1, int(expected_windows))
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        self._window_backend = window_backend
        self._message_backend = message_backend
        self._preflight_timeout_ms = max(1, int(preflight_timeout_ms))
        self._allowed_fingerprints: frozenset[str] | None = None
        self._conflict_arbiter = conflict_arbiter
        self._deferred_service = deferred_service
        self._reconnecting_provider = reconnecting_provider or (lambda: ())
        self._role_operation_callback = role_operation_callback
        self._screen_state_provider = screen_state_provider
        if self._deferred_service is not None:
            self._deferred_service.register_handler(
                "pointer",
                self._handle_deferred_pointer,
            )

    def _handle_deferred_pointer(
        self,
        fingerprint: str,
        payload: Mapping[str, object],
    ) -> bool:
        event = payload.get("event")
        x_ratio = payload.get("x_ratio")
        y_ratio = payload.get("y_ratio")
        if (
            not isinstance(event, str)
            or event not in POINTER_EVENTS
            or not isinstance(x_ratio, (int, float))
            or isinstance(x_ratio, bool)
            or not isinstance(y_ratio, (int, float))
            or isinstance(y_ratio, bool)
        ):
            return False
        return self._deliver_deferred_pointer(
            fingerprint,
            float(x_ratio),
            float(y_ratio),
            event,
        )

    @classmethod
    def for_real_windows(
        cls,
        *,
        conflict_arbiter: SyncConflictArbiter | None = None,
        deferred_service: DeferredSyncOperationService | None = None,
        reconnecting_provider: Callable[[], Iterable[str]] | None = None,
        role_operation_callback: (
            Callable[[str, str, str], object] | None
        ) = None,
        screen_state_provider: (
            Callable[[str], ReconnectScreenState | None] | None
        ) = None,
    ) -> "WindowsPointerSyncController":
        return cls(
            expected_windows=14,
            title_keywords=("Adobe Flash Player",),
            window_backend=Win32WindowBackend(
                PowerShellLaunchFingerprintResolver()
            ),
            message_backend=Win32PointerMessageBackend(),
            conflict_arbiter=conflict_arbiter,
            deferred_service=deferred_service,
            reconnecting_provider=reconnecting_provider,
            role_operation_callback=role_operation_callback,
            screen_state_provider=screen_state_provider,
        )

    def _record_role_operation(
        self,
        fingerprint: str,
        operation: str,
        outcome: str,
    ) -> None:
        if self._role_operation_callback is None:
            return
        try:
            self._role_operation_callback(
                fingerprint,
                operation,
                outcome,
            )
        except Exception:
            pass

    def _deliver_deferred_pointer(
        self,
        fingerprint: str,
        x_ratio: float,
        y_ratio: float,
        event: str,
    ) -> bool:
        if (
            self._screen_state_provider is None
            or self._screen_state_provider(fingerprint)
            is not ReconnectScreenState.CONNECTED
        ):
            return False
        matches = tuple(
            window
            for window in self._all_title_matching_windows()
            if normalize_launch_fingerprint(window.launch_fingerprint)
            == fingerprint
        )
        if len(matches) != 1:
            return False
        window = matches[0]
        if (
            not self._message_backend.is_window(window.handle)
            or not self._message_backend.probe_responsive(
                window.handle,
                self._preflight_timeout_ms,
            )
        ):
            return False
        operation = f"pointer:{event}:{x_ratio:.4f}:{y_ratio:.4f}"
        lease = (
            self._conflict_arbiter.try_begin(fingerprint, operation)
            if self._conflict_arbiter is not None
            else None
        )
        if self._conflict_arbiter is not None and lease is None:
            return False
        try:
            delivered = bool(
                self._message_backend.send_pointer(
                    window.handle,
                    x_ratio,
                    y_ratio,
                    event,
                )
            )
            if event != "move":
                self._record_role_operation(
                    fingerprint,
                    "同步左鍵",
                    "補做成功" if delivered else "補做失敗",
                )
            return delivered
        except OSError:
            return False
        finally:
            if lease is not None:
                lease.release()

    def set_expected_windows(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("expected_windows must be positive.")
        self._expected_windows = value

    def set_allowed_fingerprints(
        self,
        values: Iterable[str] | None,
    ) -> None:
        if values is None:
            self._allowed_fingerprints = None
            return
        normalized = tuple(
            normalize_launch_fingerprint(value)
            for value in values
        )
        if (
            not normalized
            or any(value is None for value in normalized)
            or len(normalized) != len(set(normalized))
        ):
            raise ValueError("fingerprints must be unique SHA-256 values.")
        self._allowed_fingerprints = frozenset(normalized)

    def _windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
            and (
                self._allowed_fingerprints is None
                or normalize_launch_fingerprint(window.launch_fingerprint)
                in self._allowed_fingerprints
            )
        )

    def _all_title_matching_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if all(
                keyword in window.title.casefold()
                for keyword in self._keywords
            )
        )

    def send(
        self,
        *,
        source_handle: int,
        x_ratio: float,
        y_ratio: float,
        event: object,
        policy: object,
        execute: bool = True,
    ) -> PointerSyncResult:
        windows = self._windows()
        failures: list[str] = []
        normalized_policy = normalize_input_policy(policy)
        normalized_event = (
            event
            if isinstance(event, str) and event in POINTER_EVENTS
            else None
        )
        reconnecting = {
            fingerprint
            for value in self._reconnecting_provider()
            if (fingerprint := normalize_launch_fingerprint(value)) is not None
        }
        group_reconnecting = (
            bool(reconnecting & self._allowed_fingerprints)
            if self._allowed_fingerprints is not None
            else bool(reconnecting)
        )
        if (
            execute
            and group_reconnecting
            and normalized_event is not None
            and normalized_policy is not None
            and self._deferred_service is not None
            and self._allowed_fingerprints is not None
        ):
            source_matches = tuple(
                window
                for window in windows
                if window.handle == source_handle
            )
            source_fingerprint = (
                normalize_launch_fingerprint(
                    source_matches[0].launch_fingerprint
                )
                if len(source_matches) == 1
                else None
            )
            source_failures: list[str] = []
            if (
                source_fingerprint is None
                or source_fingerprint not in self._allowed_fingerprints
            ):
                source_failures.append("source_not_in_group")
            if self._window_backend.foreground_handle() != source_handle:
                source_failures.append("source_not_foreground")
            if source_failures:
                return PointerSyncResult(
                    self._expected_windows,
                    len(windows),
                    0,
                    0,
                    normalized_event,
                    tuple(source_failures),
                )
            targets = tuple(
                fingerprint
                for fingerprint in self._allowed_fingerprints
                if fingerprint != source_fingerprint
            )
            operation = (
                f"pointer:{normalized_event}:"
                f"{x_ratio:.4f}:{y_ratio:.4f}"
            )
            for fingerprint in targets:
                self._deferred_service.enqueue(
                    fingerprint,
                    operation,
                    kind="pointer",
                    payload={
                        "x_ratio": x_ratio,
                        "y_ratio": y_ratio,
                        "event": normalized_event,
                    },
                )
                if normalized_event != "move":
                    self._record_role_operation(
                        fingerprint,
                        "同步左鍵",
                        "全組等待重連後補做",
                    )
            return PointerSyncResult(
                self._expected_windows,
                len(windows),
                len(targets),
                0,
                normalized_event,
                ("sync_group_deferred_reconnect",),
            )
        if normalized_policy is None:
            failures.append("input_policy_invalid")
        if normalized_event is None:
            failures.append("pointer_event_invalid")
        if not 0.0 <= x_ratio <= 1.0 or not 0.0 <= y_ratio <= 1.0:
            failures.append("pointer_position_invalid")
        if len(windows) != self._expected_windows:
            failures.append("window_count_mismatch")
        handles = [window.handle for window in windows]
        if source_handle not in handles:
            failures.append("source_not_in_group")
        if self._window_backend.foreground_handle() != source_handle:
            failures.append("source_not_foreground")
        process_ids = [window.process_id for window in windows]
        if (
            any(not isinstance(value, int) or value <= 0 for value in process_ids)
            or len(process_ids) != len(set(process_ids))
        ):
            failures.append("process_identity_missing_or_duplicate")
        fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        if (
            any(value is None for value in fingerprints)
            or len(fingerprints) != len(set(fingerprints))
        ):
            failures.append("fingerprint_missing_or_duplicate")
        if (
            self._allowed_fingerprints is not None
            and set(fingerprints) != set(self._allowed_fingerprints)
        ):
            failures.append("group_identity_set_mismatch")

        if normalized_policy is WindowInputPolicy.FOREGROUND_ONLY:
            eligible: tuple[WindowInfo, ...] = ()
        elif normalized_policy is WindowInputPolicy.FOREGROUND_BACKGROUND:
            eligible = tuple(
                window
                for window in windows
                if window.handle != source_handle and not window.minimized
            )
        else:
            eligible = tuple(
                window
                for window in windows
                if window.handle != source_handle
            )

        if failures or normalized_event is None or normalized_policy is None:
            return PointerSyncResult(
                self._expected_windows,
                len(windows),
                len(eligible),
                0,
                normalized_event,
                tuple(dict.fromkeys(failures)),
            )
        valid = [
            window
            for window in eligible
            if self._message_backend.is_window(window.handle)
            and self._message_backend.probe_responsive(
                window.handle,
                self._preflight_timeout_ms,
            )
        ]
        if len(valid) != len(eligible):
            return PointerSyncResult(
                self._expected_windows,
                len(windows),
                len(eligible),
                0,
                normalized_event,
                ("input_target_invalid_or_unresponsive",),
            )
        sent = 0
        deferred = 0
        reconnecting = reconnecting
        if execute:
            for window in valid:
                fingerprint = normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
                operation = (
                    f"pointer:{normalized_event}:"
                    f"{x_ratio:.4f}:{y_ratio:.4f}"
                )
                if (
                    fingerprint in reconnecting
                    and self._deferred_service is not None
                ):
                    self._deferred_service.enqueue(
                        fingerprint,
                        operation,
                        kind="pointer",
                        payload={
                            "x_ratio": x_ratio,
                            "y_ratio": y_ratio,
                            "event": normalized_event,
                        },
                    )
                    deferred += 1
                    if normalized_event != "move":
                        self._record_role_operation(
                            fingerprint,
                            "同步左鍵",
                            "等待重連後補做",
                        )
                    failures.append("sync_deferred_reconnect")
                    continue
                lease = (
                    self._conflict_arbiter.try_begin(
                        fingerprint,
                        operation,
                    )
                    if self._conflict_arbiter is not None
                    and fingerprint is not None
                    else None
                )
                if self._conflict_arbiter is not None and lease is None:
                    failures.append("sync_conflict_skipped")
                    continue
                try:
                    try:
                        delivered = self._message_backend.send_pointer(
                            window.handle,
                            x_ratio,
                            y_ratio,
                            normalized_event,
                        )
                    except OSError:
                        delivered = False
                finally:
                    if lease is not None:
                        lease.release()
                sent += int(bool(delivered))
                if normalized_event != "move":
                    self._record_role_operation(
                        fingerprint,
                        "同步左鍵",
                        "成功" if delivered else "失敗",
                    )
        if execute and sent + deferred != len(eligible):
            failures.append("input_delivery_failed")
        return PointerSyncResult(
            self._expected_windows,
            len(windows),
            len(eligible),
            sent,
            normalized_event,
            tuple(dict.fromkeys(failures)),
        )
