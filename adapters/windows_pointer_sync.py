"""Safe left-mouse synchronization for uniquely identified Flash windows."""

from __future__ import annotations

import ctypes
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from ctypes import wintypes
from threading import Lock
from time import perf_counter_ns
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
POINTER_OPERATIONS = POINTER_EVENTS | {"click"}


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
    controller_elapsed_ns: int = 0
    preflight_elapsed_ns: int = 0
    dispatch_spread_ns: int = 0
    queue_wait_ns: int = 0

    @property
    def passed(self) -> bool:
        return (
            not self.failure_codes
            and self.sent_windows == self.eligible_windows
        )

    def to_dict(self) -> dict[str, object]:
        """Return aggregate timing and delivery data without target identity."""
        return {
            "passed": self.passed,
            "expected_windows": self.expected_windows,
            "discovered_windows": self.discovered_windows,
            "eligible_windows": self.eligible_windows,
            "sent_windows": self.sent_windows,
            "event": self.event,
            "failure_codes": list(self.failure_codes),
            "partial_delivery": 0 < self.sent_windows < self.eligible_windows,
            "controller_elapsed_ns": self.controller_elapsed_ns,
            "preflight_elapsed_ns": self.preflight_elapsed_ns,
            "dispatch_spread_ns": self.dispatch_spread_ns,
            "queue_wait_ns": self.queue_wait_ns,
            "controller_elapsed_ms": self.controller_elapsed_ns / 1_000_000,
            "preflight_elapsed_ms": self.preflight_elapsed_ns / 1_000_000,
            "dispatch_spread_ms": self.dispatch_spread_ns / 1_000_000,
            "queue_wait_ms": self.queue_wait_ns / 1_000_000,
            "timing_scope": "controller_postmessage_scheduling_only",
            "game_receipt_verified": False,
            "raw_arguments_emitted": False,
            "fingerprints_emitted": False,
            "input_sent": self.sent_windows > 0,
        }


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
        self._allowed_fingerprints: tuple[str, ...] | None = None
        self._allowed_fingerprint_set: frozenset[str] | None = None
        self._conflict_arbiter = conflict_arbiter
        self._deferred_service = deferred_service
        self._reconnecting_provider = reconnecting_provider or (lambda: ())
        self._role_operation_callback = role_operation_callback
        self._screen_state_provider = screen_state_provider
        self._pressed_targets: dict[int, tuple[float, float]] = {}
        self._pressed_targets_lock = Lock()
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
            or event not in POINTER_OPERATIONS
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
        window_backend: WindowBackend | None = None,
    ) -> "WindowsPointerSyncController":
        return cls(
            expected_windows=14,
            title_keywords=("Adobe Flash Player",),
            window_backend=(
                window_backend
                or Win32WindowBackend(
                    PowerShellLaunchFingerprintResolver()
                )
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
            if event == "click":
                down_delivered = bool(
                    self._message_backend.send_pointer(
                        window.handle,
                        x_ratio,
                        y_ratio,
                        "left_down",
                    )
                )
                self._remember_pointer_delivery(
                    window.handle,
                    x_ratio,
                    y_ratio,
                    "left_down",
                    down_delivered,
                )
                try:
                    up_delivered = bool(
                        self._message_backend.send_pointer(
                            window.handle,
                            x_ratio,
                            y_ratio,
                            "left_up",
                        )
                    )
                except OSError:
                    up_delivered = False
                self._remember_pointer_delivery(
                    window.handle,
                    x_ratio,
                    y_ratio,
                    "left_up",
                    up_delivered,
                )
                if down_delivered and not up_delivered:
                    self._release_pressed_handles((window.handle,))
                delivered = down_delivered and up_delivered
            else:
                delivered = bool(
                    self._message_backend.send_pointer(
                        window.handle,
                        x_ratio,
                        y_ratio,
                        event,
                    )
                )
                self._remember_pointer_delivery(
                    window.handle,
                    x_ratio,
                    y_ratio,
                    event,
                    delivered,
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
            self._allowed_fingerprint_set = None
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
        ordered = (
            tuple(sorted(normalized))
            if isinstance(values, (set, frozenset))
            else normalized
        )
        self._allowed_fingerprints = ordered
        self._allowed_fingerprint_set = frozenset(ordered)

    def _windows(self) -> tuple[WindowInfo, ...]:
        windows = tuple(
            window
            for window in self._window_backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
            and (
                self._allowed_fingerprint_set is None
                or normalize_launch_fingerprint(window.launch_fingerprint)
                in self._allowed_fingerprint_set
            )
        )
        order = (
            {
                fingerprint: index
                for index, fingerprint in enumerate(
                    self._allowed_fingerprints
                )
            }
            if self._allowed_fingerprints is not None
            else None
        )
        return tuple(
            sorted(
                windows,
                key=lambda window: (
                    order.get(
                        normalize_launch_fingerprint(
                            window.launch_fingerprint
                        ),
                        len(order),
                    )
                    if order is not None
                    else 0,
                    normalize_launch_fingerprint(
                        window.launch_fingerprint
                    )
                    or "",
                    window.process_id or 0,
                ),
            )
        )

    def _responsive_targets(
        self,
        targets: tuple[WindowInfo, ...],
    ) -> tuple[WindowInfo, ...]:
        """Probe larger batches concurrently while retaining fixed order."""
        if len(targets) < 4:
            states = tuple(
                self._message_backend.probe_responsive(
                    window.handle,
                    self._preflight_timeout_ms,
                )
                for window in targets
            )
        else:
            with ThreadPoolExecutor(
                max_workers=min(14, len(targets)),
                thread_name_prefix="flash-pointer-preflight",
            ) as executor:
                states = tuple(
                    executor.map(
                        lambda window: self._message_backend.probe_responsive(
                            window.handle,
                            self._preflight_timeout_ms,
                        ),
                        targets,
                    )
                )
        return tuple(
            window
            for window, responsive in zip(targets, states)
            if responsive
        )

    def _remember_pointer_delivery(
        self,
        handle: int,
        x_ratio: float,
        y_ratio: float,
        event: str,
        delivered: bool,
    ) -> None:
        if not delivered:
            return
        with self._pressed_targets_lock:
            if event == "left_down":
                self._pressed_targets[handle] = (x_ratio, y_ratio)
            elif event == "move" and handle in self._pressed_targets:
                self._pressed_targets[handle] = (x_ratio, y_ratio)
            elif event == "left_up":
                self._pressed_targets.pop(handle, None)

    def _release_pressed_handles(self, handles: Iterable[int]) -> int:
        selected = frozenset(handles)
        with self._pressed_targets_lock:
            pending = tuple(
                (handle, position)
                for handle, position in self._pressed_targets.items()
                if handle in selected
            )
        released = 0
        for handle, (x_ratio, y_ratio) in pending:
            delivered = False
            try:
                delivered = bool(
                    self._message_backend.is_window(handle)
                    and self._message_backend.send_pointer(
                        handle,
                        x_ratio,
                        y_ratio,
                        "left_up",
                    )
                )
            except OSError:
                delivered = False
            if delivered:
                with self._pressed_targets_lock:
                    self._pressed_targets.pop(handle, None)
                released += 1
        return released

    def release_pressed_targets(self) -> int:
        """Release only targets that previously received a successful down."""
        with self._pressed_targets_lock:
            handles = tuple(self._pressed_targets)
        return self._release_pressed_handles(handles)

    def has_pressed_targets(self) -> bool:
        with self._pressed_targets_lock:
            return bool(self._pressed_targets)

    def _result(
        self,
        *,
        discovered_windows: int,
        eligible_windows: int,
        sent_windows: int,
        event: str | None,
        failures: Iterable[str] = (),
        controller_started_ns: int | None = None,
        preflight_elapsed_ns: int = 0,
        dispatch_spread_ns: int = 0,
        queue_wait_ns: int = 0,
    ) -> PointerSyncResult:
        return PointerSyncResult(
            expected_windows=self._expected_windows,
            discovered_windows=discovered_windows,
            eligible_windows=eligible_windows,
            sent_windows=sent_windows,
            event=event,
            failure_codes=tuple(dict.fromkeys(failures)),
            controller_elapsed_ns=(
                max(0, perf_counter_ns() - controller_started_ns)
                if controller_started_ns is not None
                else 0
            ),
            preflight_elapsed_ns=max(0, preflight_elapsed_ns),
            dispatch_spread_ns=max(0, dispatch_spread_ns),
            queue_wait_ns=max(0, queue_wait_ns),
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

    def source_is_eligible(self, source_handle: int) -> bool:
        """Check group identity and foreground ownership without sending input."""
        if self._allowed_fingerprint_set is None:
            return False
        windows = self._windows()
        if (
            len(windows) != self._expected_windows
            or self._window_backend.foreground_handle() != source_handle
        ):
            return False
        source_matches = tuple(
            window for window in windows if window.handle == source_handle
        )
        if len(source_matches) != 1:
            return False
        process_ids = tuple(window.process_id for window in windows)
        if (
            any(
                not isinstance(process_id, int) or process_id <= 0
                for process_id in process_ids
            )
            or len(process_ids) != len(set(process_ids))
        ):
            return False
        fingerprints = tuple(
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        )
        return (
            all(fingerprint is not None for fingerprint in fingerprints)
            and len(fingerprints) == len(set(fingerprints))
            and set(fingerprints) == self._allowed_fingerprint_set
            and normalize_launch_fingerprint(
                source_matches[0].launch_fingerprint
            )
            in self._allowed_fingerprint_set
        )

    def source_is_group_member(self, source_handle: int) -> bool:
        """Identify one foreground group member without requiring a full group."""
        if (
            self._allowed_fingerprint_set is None
            or self._window_backend.foreground_handle() != source_handle
        ):
            return False
        all_windows = self._all_title_matching_windows()
        source_matches = tuple(
            window
            for window in all_windows
            if window.handle == source_handle
        )
        if len(source_matches) != 1:
            return False
        fingerprint = normalize_launch_fingerprint(
            source_matches[0].launch_fingerprint
        )
        if fingerprint not in self._allowed_fingerprint_set:
            return False
        return (
            sum(
                normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
                == fingerprint
                for window in all_windows
            )
            == 1
        )

    def source_must_block_physical_fallback(
        self,
        source_handle: int,
    ) -> bool:
        """Fail closed for any foreground Flash source with uncertain identity."""
        if self._window_backend.foreground_handle() != source_handle:
            return False
        try:
            matches = tuple(
                window
                for window in self._all_title_matching_windows()
                if window.handle == source_handle
            )
        except Exception:
            return True
        # Exactly one title-matched Flash window is enough to prove a physical
        # fallback could interfere with the game. Identity may remain unknown.
        return len(matches) == 1

    def send(
        self,
        *,
        source_handle: int,
        x_ratio: float,
        y_ratio: float,
        event: object,
        policy: object,
        execute: bool = True,
        include_source: bool = False,
        execution_guard: Callable[[], bool] | None = None,
    ) -> PointerSyncResult:
        return self._send(
            source_handle=source_handle,
            x_ratio=x_ratio,
            y_ratio=y_ratio,
            event=event,
            policy=policy,
            execute=execute,
            include_source=include_source,
            execution_guard=execution_guard,
        )

    def send_click(
        self,
        *,
        source_handle: int,
        x_ratio: float,
        y_ratio: float,
        policy: object,
        execute: bool = True,
        include_source: bool = True,
        execution_guard: Callable[[], bool] | None = None,
    ) -> PointerSyncResult:
        """Send one atomic down/up pair per target after a single preflight."""
        return self._send(
            source_handle=source_handle,
            x_ratio=x_ratio,
            y_ratio=y_ratio,
            event="click",
            policy=policy,
            execute=execute,
            include_source=include_source,
            execution_guard=execution_guard,
        )

    def _send(
        self,
        *,
        source_handle: int,
        x_ratio: float,
        y_ratio: float,
        event: object,
        policy: object,
        execute: bool = True,
        include_source: bool = False,
        execution_guard: Callable[[], bool] | None = None,
    ) -> PointerSyncResult:
        controller_started_ns = perf_counter_ns()
        windows = self._windows()
        failures: list[str] = []
        normalized_policy = normalize_input_policy(policy)
        normalized_event = (
            event
            if isinstance(event, str) and event in POINTER_OPERATIONS
            else None
        )
        reconnecting = {
            fingerprint
            for value in self._reconnecting_provider()
            if (fingerprint := normalize_launch_fingerprint(value)) is not None
        }
        with self._pressed_targets_lock:
            pressed_handles = tuple(self._pressed_targets)
        if normalized_event == "left_up" and pressed_handles:
            if not execute:
                return self._result(
                    discovered_windows=len(windows),
                    eligible_windows=len(pressed_handles),
                    sent_windows=0,
                    event=normalized_event,
                    controller_started_ns=controller_started_ns,
                )
            if execution_guard is not None:
                try:
                    release_allowed = bool(execution_guard())
                except Exception:
                    release_allowed = False
                if not release_allowed:
                    return self._result(
                        discovered_windows=len(windows),
                        eligible_windows=len(pressed_handles),
                        sent_windows=0,
                        event=normalized_event,
                        failures=("execution_stopped",),
                        controller_started_ns=controller_started_ns,
                    )
            released = self._release_pressed_handles(pressed_handles)
            return self._result(
                discovered_windows=len(windows),
                eligible_windows=len(pressed_handles),
                sent_windows=released,
                event=normalized_event,
                failures=(
                    ()
                    if released == len(pressed_handles)
                    else ("input_delivery_failed",)
                ),
                controller_started_ns=controller_started_ns,
            )
        if normalized_policy is None:
            failures.append("input_policy_invalid")
        if normalized_event is None:
            failures.append("pointer_event_invalid")
        if not 0.0 <= x_ratio <= 1.0 or not 0.0 <= y_ratio <= 1.0:
            failures.append("pointer_position_invalid")
        handles = [window.handle for window in windows]
        if source_handle not in handles:
            failures.append("source_not_in_group")
        if self._window_backend.foreground_handle() != source_handle:
            failures.append("source_not_foreground")
        process_ids = [window.process_id for window in windows]
        process_identity_valid = not (
            any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in process_ids
            )
            or len(process_ids) != len(set(process_ids))
        )
        if not process_identity_valid:
            failures.append("process_identity_missing_or_duplicate")
        fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        fingerprint_identity_valid = not (
            any(value is None for value in fingerprints)
            or len(fingerprints) != len(set(fingerprints))
        )
        if not fingerprint_identity_valid:
            failures.append("fingerprint_missing_or_duplicate")
        visible_fingerprint_set = (
            set(fingerprints)
            if fingerprint_identity_valid
            else set()
        )
        missing_allowed = (
            tuple(
                fingerprint
                for fingerprint in self._allowed_fingerprints
                if fingerprint not in visible_fingerprint_set
            )
            if self._allowed_fingerprints is not None
            else ()
        )
        partial_reconnect_candidate = (
            process_identity_valid
            and fingerprint_identity_valid
            and self._allowed_fingerprints is not None
            and self._allowed_fingerprint_set is not None
            and len(self._allowed_fingerprints)
            == self._expected_windows
            and bool(missing_allowed)
            and len(windows) + len(missing_allowed)
            == self._expected_windows
            and visible_fingerprint_set
            <= self._allowed_fingerprint_set
            and set(missing_allowed) <= reconnecting
        )
        unresolved_title_identity = False
        if partial_reconnect_candidate:
            unresolved_title_identity = any(
                not isinstance(window.process_id, int)
                or isinstance(window.process_id, bool)
                or window.process_id <= 0
                or normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
                is None
                for window in self._all_title_matching_windows()
            )
        safe_partial_reconnect = (
            partial_reconnect_candidate
            and not unresolved_title_identity
        )
        if (
            len(windows) != self._expected_windows
            and not safe_partial_reconnect
        ):
            failures.append("window_count_mismatch")
        if (
            self._allowed_fingerprints is not None
            and visible_fingerprint_set
            != self._allowed_fingerprint_set
            and not safe_partial_reconnect
        ):
            failures.append("group_identity_set_mismatch")

        if normalized_policy is WindowInputPolicy.FOREGROUND_ONLY:
            eligible = tuple(
                window
                for window in windows
                if include_source and window.handle == source_handle
            )
        elif normalized_policy is WindowInputPolicy.FOREGROUND_BACKGROUND:
            eligible = tuple(
                window
                for window in windows
                if (
                    (include_source or window.handle != source_handle)
                    and not window.minimized
                )
            )
        else:
            eligible = tuple(
                window
                for window in windows
                if include_source or window.handle != source_handle
            )

        if failures or normalized_event is None or normalized_policy is None:
            return self._result(
                discovered_windows=len(windows),
                eligible_windows=len(eligible),
                sent_windows=0,
                event=normalized_event,
                failures=failures,
                controller_started_ns=controller_started_ns,
            )
        group_reconnecting = (
            bool(reconnecting & self._allowed_fingerprint_set)
            if self._allowed_fingerprint_set is not None
            else bool(reconnecting)
        )
        if (
            execute
            and group_reconnecting
            and self._deferred_service is not None
            and self._allowed_fingerprints is not None
        ):
            operation = (
                f"pointer:{normalized_event}:"
                f"{x_ratio:.4f}:{y_ratio:.4f}"
            )
            source_fingerprint = next(
                (
                    normalize_launch_fingerprint(
                        window.launch_fingerprint
                    )
                    for window in windows
                    if window.handle == source_handle
                ),
                None,
            )
            visible_background = {
                normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
                for window in windows
                if not window.minimized
            }
            if normalized_policy is WindowInputPolicy.FOREGROUND_ONLY:
                candidates = (
                    (source_fingerprint,)
                    if include_source and source_fingerprint is not None
                    else ()
                )
            elif (
                normalized_policy
                is WindowInputPolicy.FOREGROUND_BACKGROUND
            ):
                candidate_set = (
                    visible_background | set(missing_allowed)
                )
                candidates = tuple(
                    fingerprint
                    for fingerprint in self._allowed_fingerprints
                    if fingerprint in candidate_set
                )
            else:
                candidates = self._allowed_fingerprints
            target_fingerprints = tuple(
                fingerprint
                for fingerprint in candidates
                if include_source or fingerprint != source_fingerprint
            )
            if not target_fingerprints:
                return self._result(
                    discovered_windows=len(windows),
                    eligible_windows=0,
                    sent_windows=0,
                    event=normalized_event,
                    failures=("no_eligible_windows",),
                    controller_started_ns=controller_started_ns,
                )
            for fingerprint in target_fingerprints:
                self._deferred_service.enqueue(
                    fingerprint,
                    operation,
                    kind="pointer",
                    payload={
                        "x_ratio": x_ratio,
                        "y_ratio": y_ratio,
                        "event": normalized_event,
                        "policy": normalized_policy.value,
                        "source_eligible_at_capture": True,
                    },
                )
                if normalized_event != "move":
                    self._record_role_operation(
                        fingerprint,
                        "同步左鍵",
                        "全組等待重連後補做",
                    )
            return self._result(
                discovered_windows=len(windows),
                eligible_windows=len(target_fingerprints),
                sent_windows=0,
                event=normalized_event,
                failures=("sync_group_deferred_reconnect",),
                controller_started_ns=controller_started_ns,
            )
        if not eligible:
            return self._result(
                discovered_windows=len(windows),
                eligible_windows=0,
                sent_windows=0,
                event=normalized_event,
                failures=("no_eligible_windows",),
                controller_started_ns=controller_started_ns,
            )
        preflight_started_ns = perf_counter_ns()
        valid_windows = tuple(
            window
            for window in eligible
            if self._message_backend.is_window(window.handle)
        )
        valid = self._responsive_targets(valid_windows)
        preflight_elapsed_ns = max(
            0,
            perf_counter_ns() - preflight_started_ns,
        )
        if len(valid) != len(eligible):
            return self._result(
                discovered_windows=len(windows),
                eligible_windows=len(eligible),
                sent_windows=0,
                event=normalized_event,
                failures=("input_target_invalid_or_unresponsive",),
                controller_started_ns=controller_started_ns,
                preflight_elapsed_ns=preflight_elapsed_ns,
            )
        sent = 0
        deferred = 0
        reconnecting = reconnecting
        dispatch_first_ns: int | None = None
        dispatch_last_ns: int | None = None
        execution_stopped = False
        batch_down_handles: set[int] = set()
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
                            "policy": normalized_policy.value,
                            "source_eligible_at_capture": True,
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
                        if execution_guard is not None:
                            try:
                                execution_allowed = bool(execution_guard())
                            except Exception:
                                execution_allowed = False
                            if not execution_allowed:
                                failures.append("execution_stopped")
                                execution_stopped = True
                                break
                        dispatch_started_ns = perf_counter_ns()
                        if dispatch_first_ns is None:
                            dispatch_first_ns = dispatch_started_ns
                        dispatch_last_ns = dispatch_started_ns
                        if normalized_event == "click":
                            down_delivered = bool(
                                self._message_backend.send_pointer(
                                    window.handle,
                                    x_ratio,
                                    y_ratio,
                                    "left_down",
                                )
                            )
                            self._remember_pointer_delivery(
                                window.handle,
                                x_ratio,
                                y_ratio,
                                "left_down",
                                down_delivered,
                            )
                            if down_delivered:
                                batch_down_handles.add(window.handle)
                            dispatch_last_ns = perf_counter_ns()
                            up_delivered = bool(
                                self._message_backend.send_pointer(
                                    window.handle,
                                    x_ratio,
                                    y_ratio,
                                    "left_up",
                                )
                            )
                            self._remember_pointer_delivery(
                                window.handle,
                                x_ratio,
                                y_ratio,
                                "left_up",
                                up_delivered,
                            )
                            if up_delivered:
                                batch_down_handles.discard(window.handle)
                            delivered = down_delivered and up_delivered
                        else:
                            delivered = bool(
                                self._message_backend.send_pointer(
                                    window.handle,
                                    x_ratio,
                                    y_ratio,
                                    normalized_event,
                                )
                            )
                            self._remember_pointer_delivery(
                                window.handle,
                                x_ratio,
                                y_ratio,
                                normalized_event,
                                delivered,
                            )
                            if normalized_event == "left_down" and delivered:
                                batch_down_handles.add(window.handle)
                            elif normalized_event == "left_up" and delivered:
                                batch_down_handles.discard(window.handle)
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
        if execution_stopped:
            self.release_pressed_targets()
        elif (
            normalized_event in {"left_down", "click"}
            and sent + deferred != len(eligible)
            and batch_down_handles
        ):
            self._release_pressed_handles(batch_down_handles)
        dispatch_spread_ns = (
            max(0, dispatch_last_ns - dispatch_first_ns)
            if (
                dispatch_first_ns is not None
                and dispatch_last_ns is not None
            )
            else 0
        )
        return self._result(
            discovered_windows=len(windows),
            eligible_windows=len(eligible),
            sent_windows=sent,
            event=normalized_event,
            failures=failures,
            controller_started_ns=controller_started_ns,
            preflight_elapsed_ns=preflight_elapsed_ns,
            dispatch_spread_ns=dispatch_spread_ns,
        )
