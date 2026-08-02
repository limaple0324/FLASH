"""Confirmed keyboard synchronization for individually identified Flash windows.

Every batch fails closed before sending input unless the live desktop contains
the exact expected number of title-matched windows, process IDs, and anonymous
launcher fingerprints. Reports contain aggregate counts only.
"""

from __future__ import annotations

import os
import ctypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from ctypes import wintypes
from time import perf_counter_ns
from typing import Callable, Iterable, Mapping, Protocol

from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import Win32WindowBackend, WindowBackend, WindowInfo
from domain.game_shortcuts import GAME_SHORTCUT_BY_KEY
from domain.sync_target_settings import SyncTargetSettings
from services.sync_conflict_arbiter import SyncConflictArbiter
from services.sync_dispatch_scheduler import SyncDispatchScheduler
from services.deferred_sync_operation_service import (
    DeferredSyncOperationService,
)
from services.game_operation_gate import GameOperationGate
from core.reconnect_policy import ReconnectScreenState


APPROVED_SYNC_KEYS = frozenset(GAME_SHORTCUT_BY_KEY)
# Retained as a compatibility name for older callers; the value now contains
# the complete player-confirmed catalog rather than only B and C.
APPROVED_TEST_KEYS = APPROVED_SYNC_KEYS
_KEY_ALIASES = {
    "CTRL+UP": "CTRL+↑",
    "CTRL+DOWN": "CTRL+↓",
}
_SINGLE_VIRTUAL_KEYS = {
    **{letter: ord(letter) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    "TAB": 0x09,
    "ESC": 0x1B,
    "CTRL": 0x11,
    "SHIFT": 0x10,
}
VIRTUAL_KEY_SEQUENCES = {
    key: (_SINGLE_VIRTUAL_KEYS[key],)
    for key in APPROVED_SYNC_KEYS
    if key in _SINGLE_VIRTUAL_KEYS
}
VIRTUAL_KEY_SEQUENCES.update(
    {
        "CTRL+↑": (0x11, 0x26),
        "CTRL+↓": (0x11, 0x28),
    }
)


class WindowInputPolicy(str, Enum):
    """Which states are eligible to receive an approved synchronized input."""

    FOREGROUND_ONLY = "foreground_only"
    FOREGROUND_BACKGROUND = "foreground_background"
    ALL = "all"


def normalize_input_policy(value: object) -> WindowInputPolicy | None:
    if isinstance(value, WindowInputPolicy):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    try:
        return WindowInputPolicy(normalized)
    except ValueError:
        return None


def normalize_approved_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper().replace(" ", "")
    normalized = _KEY_ALIASES.get(normalized, normalized)
    return normalized if normalized in APPROVED_SYNC_KEYS else None


class KeyMessageBackend(Protocol):
    def is_window(self, handle: int) -> bool:
        """Return whether the supplied top-level window handle still exists."""

    def probe_responsive(self, handle: int, timeout_ms: int) -> bool:
        """Send a no-op probe without changing the game state."""

    def send_virtual_key(self, handle: int, virtual_key: int) -> bool:
        """Post one key-down/key-up pair to one already-validated window."""

    def send_key_chord(
        self,
        handle: int,
        virtual_keys: tuple[int, ...],
    ) -> bool:
        """Post a modifier chord while preserving down/up ordering."""


class Win32KeyMessageBackend:
    """Pure-ctypes Win32 keyboard-message backend."""

    WM_NULL = 0x0000
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    SMTO_ABORTIFHUNG = 0x0002
    MAPVK_VK_TO_VSC = 0
    EXTENDED_KEYS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28})

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        user32.MapVirtualKeyW.restype = wintypes.UINT
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wintypes.HWND
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

    def _post_key_event(
        self,
        handle: int,
        virtual_key: int,
        *,
        key_up: bool,
    ) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        scan_code = int(
            user32.MapVirtualKeyW(
                int(virtual_key),
                self.MAPVK_VK_TO_VSC,
            )
        )
        lparam = 1 | ((scan_code & 0xFF) << 16)
        if virtual_key in self.EXTENDED_KEYS:
            lparam |= 1 << 24
        if key_up:
            lparam |= (1 << 30) | (1 << 31)
        return bool(
            user32.PostMessageW(
                wintypes.HWND(handle),
                self.WM_KEYUP if key_up else self.WM_KEYDOWN,
                int(virtual_key),
                lparam,
            )
        )

    def send_virtual_key(self, handle: int, virtual_key: int) -> bool:
        key_down_ok = self._post_key_event(
            handle,
            virtual_key,
            key_up=False,
        )
        key_up_ok = self._post_key_event(
            handle,
            virtual_key,
            key_up=True,
        )
        return key_down_ok and key_up_ok

    def send_key_chord(
        self,
        handle: int,
        virtual_keys: tuple[int, ...],
    ) -> bool:
        if len(virtual_keys) < 2:
            return (
                self.send_virtual_key(handle, virtual_keys[0])
                if virtual_keys
                else False
            )
        results: list[bool] = []
        modifiers = virtual_keys[:-1]
        action_key = virtual_keys[-1]
        for modifier in modifiers:
            results.append(
                self._post_key_event(handle, modifier, key_up=False)
            )
        results.append(
            self._post_key_event(handle, action_key, key_up=False)
        )
        results.append(
            self._post_key_event(handle, action_key, key_up=True)
        )
        for modifier in reversed(modifiers):
            results.append(
                self._post_key_event(handle, modifier, key_up=True)
            )
        return all(results)

@dataclass(frozen=True, slots=True)
class InputSyncResult:
    approved_key: str | None
    policy: str | None
    expected_windows: int
    discovered_windows: int
    eligible_windows: int
    responsive_windows: int
    sent_windows: int
    minimized_windows: int
    background_windows: int
    skipped_windows: int
    execution_requested: bool
    failure_codes: tuple[str, ...]
    controller_elapsed_ns: int = 0
    preflight_elapsed_ns: int = 0
    dispatch_spread_ns: int = 0
    queue_wait_ns: int = 0
    scheduled_windows: int = 0

    @property
    def passed(self) -> bool:
        return (
            self.execution_requested
            and not self.failure_codes
            and self.eligible_windows > 0
            and self.sent_windows + self.scheduled_windows
            == self.eligible_windows
        )

    @property
    def ready(self) -> bool:
        return (
            not self.failure_codes
            and self.eligible_windows > 0
            and self.responsive_windows == self.eligible_windows
        )

    def to_dict(self) -> dict[str, object]:
        """Return a report without handles, PIDs, fingerprints, or launcher data."""
        return {
            "passed": self.passed,
            "ready": self.ready,
            "approved_key": self.approved_key,
            "policy": self.policy,
            "expected_windows": self.expected_windows,
            "discovered_windows": self.discovered_windows,
            "eligible_windows": self.eligible_windows,
            "responsive_windows": self.responsive_windows,
            "sent_windows": self.sent_windows,
            "scheduled_windows": self.scheduled_windows,
            "minimized_windows": self.minimized_windows,
            "background_windows": self.background_windows,
            "skipped_windows": self.skipped_windows,
            "execution_requested": self.execution_requested,
            "partial_delivery": (
                0
                < self.sent_windows + self.scheduled_windows
                < self.eligible_windows
            ),
            "failure_codes": list(self.failure_codes),
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
            "input_scheduled": self.scheduled_windows > 0,
        }


class WindowsInputSyncController:
    """Validate one-to-one identity, preflight, then send an approved key batch."""

    def __init__(
        self,
        *,
        expected_windows: int,
        title_keywords: Iterable[str],
        window_backend: WindowBackend,
        message_backend: KeyMessageBackend,
        preflight_timeout_ms: int = 1000,
        allowed_fingerprints: Iterable[str] | None = None,
        conflict_arbiter: SyncConflictArbiter | None = None,
        deferred_service: DeferredSyncOperationService | None = None,
        reconnecting_provider: Callable[[], Iterable[str]] | None = None,
        role_operation_callback: (
            Callable[[str, str, str], object] | None
        ) = None,
        screen_state_provider: (
            Callable[[str], ReconnectScreenState | None] | None
        ) = None,
        target_windows_provider: (
            Callable[[], Iterable[WindowInfo]] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
    ):
        if expected_windows <= 0:
            raise ValueError("expected_windows must be positive")
        self._expected_windows = expected_windows
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._keywords:
            raise ValueError("At least one title keyword is required")
        self._window_backend = window_backend
        self._message_backend = message_backend
        self._preflight_timeout_ms = max(1, int(preflight_timeout_ms))
        self._allowed_fingerprints: tuple[str, ...] | None = None
        self._allowed_fingerprint_set: frozenset[str] | None = None
        self._controller_fingerprint: str | None = None
        self._conflict_arbiter = conflict_arbiter
        self._deferred_service = deferred_service
        self._reconnecting_provider = reconnecting_provider or (lambda: ())
        self._role_operation_callback = role_operation_callback
        self._screen_state_provider = screen_state_provider
        self._target_windows_provider = target_windows_provider
        self._operation_gate = operation_gate
        self._target_settings: dict[str, SyncTargetSettings] = {}
        self._dispatch_scheduler = SyncDispatchScheduler(
            thread_name="flash-key-delay",
        )
        if self._deferred_service is not None:
            self._deferred_service.register_handler(
                "keyboard",
                self._handle_deferred_key,
            )
        self.set_allowed_fingerprints(allowed_fingerprints)

    def _handle_deferred_key(
        self,
        fingerprint: str,
        payload: Mapping[str, object],
    ) -> bool:
        key = normalize_approved_key(payload.get("key"))
        if key is None:
            return False
        lease = (
            self._operation_gate.acquire("keyboard-deferred")
            if self._operation_gate is not None
            else None
        )
        if self._operation_gate is not None and lease is None:
            return False
        try:
            return self._deliver_deferred_key(
                fingerprint,
                key,
                VIRTUAL_KEY_SEQUENCES[key],
            )
        finally:
            if lease is not None:
                lease.release()
    @property
    def expected_windows(self) -> int:
        return self._expected_windows

    def set_expected_windows(self, expected_windows: int) -> None:
        if (
            isinstance(expected_windows, bool)
            or not isinstance(expected_windows, int)
            or expected_windows <= 0
        ):
            raise ValueError("expected_windows must be positive")
        self._expected_windows = expected_windows

    def set_allowed_fingerprints(
        self,
        fingerprints: Iterable[str] | None,
    ) -> None:
        if fingerprints is None:
            self._allowed_fingerprints = None
            self._allowed_fingerprint_set = None
            self._controller_fingerprint = None
            self._target_settings = {}
            self.invalidate_scheduled()
            return
        normalized = tuple(
            normalize_launch_fingerprint(item)
            for item in fingerprints
        )
        if (
            not normalized
            or any(item is None for item in normalized)
            or len(normalized) != len(set(normalized))
        ):
            raise ValueError(
                "fingerprints must contain unique complete SHA-256 digests"
            )
        ordered = (
            tuple(sorted(normalized))
            if isinstance(fingerprints, (set, frozenset))
            else normalized
        )
        self._allowed_fingerprints = ordered
        self._allowed_fingerprint_set = frozenset(ordered)
        if self._controller_fingerprint not in self._allowed_fingerprint_set:
            self._controller_fingerprint = None
        self._target_settings = {
            fingerprint: settings
            for fingerprint, settings in self._target_settings.items()
            if fingerprint in self._allowed_fingerprint_set
        }
        self.invalidate_scheduled()

    def set_target_settings(
        self,
        values: Mapping[str, SyncTargetSettings] | None,
    ) -> None:
        normalized: dict[str, SyncTargetSettings] = {}
        if values is not None:
            for raw_fingerprint, raw_settings in values.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                if (
                    fingerprint is None
                    or not isinstance(raw_settings, SyncTargetSettings)
                    or (
                        self._allowed_fingerprint_set is not None
                        and fingerprint not in self._allowed_fingerprint_set
                    )
                ):
                    raise ValueError("target settings contain an invalid role.")
                normalized[fingerprint] = raw_settings
        self._target_settings = normalized
        self.invalidate_scheduled()

    def invalidate_scheduled(self) -> None:
        self._dispatch_scheduler.invalidate()

    def close(self, timeout_seconds: float = 5.0) -> bool:
        self.set_allowed_fingerprints(None)
        return self._dispatch_scheduler.close(timeout_seconds)

    def _settings_for(self, fingerprint: str | None) -> SyncTargetSettings:
        if fingerprint is None:
            return SyncTargetSettings()
        return self._target_settings.get(
            fingerprint,
            SyncTargetSettings(),
        )

    def set_controller_fingerprint(self, fingerprint: object) -> None:
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            raise ValueError(
                "controller fingerprint must be a complete SHA-256 digest"
            )
        if (
            self._allowed_fingerprint_set is None
            or normalized not in self._allowed_fingerprint_set
        ):
            raise ValueError(
                "controller fingerprint must belong to the configured scope"
            )
        self._controller_fingerprint = normalized

    @classmethod
    def for_real_windows(
        cls,
        *,
        expected_windows: int = 14,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
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
        target_windows_provider: (
            Callable[[], Iterable[WindowInfo]] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
    ) -> "WindowsInputSyncController":
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=(
                window_backend
                or Win32WindowBackend(
                    PowerShellLaunchFingerprintResolver()
                )
            ),
            message_backend=Win32KeyMessageBackend(),
            conflict_arbiter=conflict_arbiter,
            deferred_service=deferred_service,
            reconnecting_provider=reconnecting_provider,
            role_operation_callback=role_operation_callback,
            screen_state_provider=screen_state_provider,
            target_windows_provider=target_windows_provider,
            operation_gate=operation_gate,
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

    def _deliver_deferred_key(
        self,
        fingerprint: str,
        normalized_key: str,
        virtual_keys: tuple[int, ...],
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
        lease = (
            self._conflict_arbiter.try_begin(
                fingerprint,
                f"key:{normalized_key}",
            )
            if self._conflict_arbiter is not None
            else None
        )
        if self._conflict_arbiter is not None and lease is None:
            return False
        try:
            if len(virtual_keys) == 1:
                delivered = bool(
                    self._message_backend.send_virtual_key(
                        window.handle,
                        virtual_keys[0],
                    )
                )
            else:
                delivered = bool(
                    self._message_backend.send_key_chord(
                    window.handle,
                    virtual_keys,
                    )
                )
            self._record_role_operation(
                fingerprint,
                f"快捷鍵 {normalized_key}",
                "補做成功" if delivered else "補做失敗",
            )
            return delivered
        except OSError:
            return False
        finally:
            if lease is not None:
                lease.release()

    def _run_scheduled_key(
        self,
        fingerprint: str,
        normalized_key: str,
        virtual_keys: tuple[int, ...],
        execution_guard: Callable[[], bool] | None,
    ) -> None:
        lease = (
            self._operation_gate.acquire(
                "keyboard-scheduled",
                execution_guard=execution_guard,
            )
            if self._operation_gate is not None
            else None
        )
        if self._operation_gate is not None and lease is None:
            return
        try:
            self._run_scheduled_key_without_gate(
                fingerprint,
                normalized_key,
                virtual_keys,
                execution_guard,
            )
        finally:
            if lease is not None:
                lease.release()

    def _run_scheduled_key_without_gate(
        self,
        fingerprint: str,
        normalized_key: str,
        virtual_keys: tuple[int, ...],
        execution_guard: Callable[[], bool] | None,
    ) -> None:
        if execution_guard is not None:
            try:
                if not bool(execution_guard()):
                    return
            except Exception:
                return
        reconnecting = {
            normalized
            for value in self._reconnecting_provider()
            if (
                normalized := normalize_launch_fingerprint(value)
            )
            is not None
        }
        if (
            fingerprint in reconnecting
            and self._deferred_service is not None
        ):
            self._deferred_service.enqueue(
                fingerprint,
                f"key:{normalized_key}",
                kind="keyboard",
                payload={
                    "key": normalized_key,
                    "delay_already_applied": True,
                },
            )
            self._record_role_operation(
                fingerprint,
                f"快捷鍵 {normalized_key}",
                "延遲到期時斷線，等待重連後補做",
            )
            return
        matches = tuple(
            window
            for window in self._all_title_matching_windows()
            if normalize_launch_fingerprint(window.launch_fingerprint)
            == fingerprint
        )
        if len(matches) != 1:
            self._record_role_operation(
                fingerprint,
                f"快捷鍵 {normalized_key}",
                "延遲送出失敗",
            )
            return
        window = matches[0]
        if (
            not self._message_backend.is_window(window.handle)
            or not self._message_backend.probe_responsive(
                window.handle,
                self._preflight_timeout_ms,
            )
        ):
            self._record_role_operation(
                fingerprint,
                f"快捷鍵 {normalized_key}",
                "延遲送出失敗",
            )
            return
        lease = (
            self._conflict_arbiter.try_begin(
                fingerprint,
                f"key:{normalized_key}",
            )
            if self._conflict_arbiter is not None
            else None
        )
        if self._conflict_arbiter is not None and lease is None:
            return
        try:
            if len(virtual_keys) == 1:
                delivered = bool(
                    self._message_backend.send_virtual_key(
                        window.handle,
                        virtual_keys[0],
                    )
                )
            else:
                delivered = bool(
                    self._message_backend.send_key_chord(
                        window.handle,
                        virtual_keys,
                    )
                )
            self._record_role_operation(
                fingerprint,
                f"快捷鍵 {normalized_key}",
                "延遲送出成功" if delivered else "延遲送出失敗",
            )
        except OSError:
            self._record_role_operation(
                fingerprint,
                f"快捷鍵 {normalized_key}",
                "延遲送出失敗",
            )
        finally:
            if lease is not None:
                lease.release()

    def _matching_windows(self) -> tuple[WindowInfo, ...]:
        windows = tuple(
            window
            for window in self._candidate_windows()
            if (
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

    def _candidate_windows(self) -> tuple[WindowInfo, ...]:
        if self._target_windows_provider is not None:
            try:
                return tuple(self._target_windows_provider())
            except Exception:
                return ()
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if all(
                keyword in window.title.casefold()
                for keyword in self._keywords
            )
        )

    def _responsive_targets(
        self,
        targets: tuple[WindowInfo, ...],
    ) -> tuple[WindowInfo, ...]:
        """Probe larger batches concurrently while preserving target order."""
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
                thread_name_prefix="flash-key-preflight",
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

    def _all_title_matching_windows(self) -> tuple[WindowInfo, ...]:
        return self._candidate_windows()

    def _base_result(
        self,
        *,
        key: str | None,
        policy: WindowInputPolicy | None,
        windows: tuple[WindowInfo, ...],
        eligible: tuple[WindowInfo, ...] = (),
        responsive: int = 0,
        sent: int = 0,
        execute: bool,
        failures: Iterable[str] = (),
        controller_started_ns: int | None = None,
        preflight_elapsed_ns: int = 0,
        dispatch_spread_ns: int = 0,
        queue_wait_ns: int = 0,
        eligible_count: int | None = None,
        scheduled: int = 0,
    ) -> InputSyncResult:
        foreground = self._window_backend.foreground_handle()
        controller_elapsed_ns = (
            max(0, perf_counter_ns() - controller_started_ns)
            if controller_started_ns is not None
            else 0
        )
        return InputSyncResult(
            approved_key=key,
            policy=policy.value if policy is not None else None,
            expected_windows=self._expected_windows,
            discovered_windows=len(windows),
            eligible_windows=(
                len(eligible)
                if eligible_count is None
                else max(0, int(eligible_count))
            ),
            responsive_windows=responsive,
            sent_windows=sent,
            minimized_windows=sum(window.minimized for window in windows),
            background_windows=sum(
                window.handle != foreground for window in windows
            ),
            skipped_windows=max(
                0,
                len(windows)
                - (
                    len(eligible)
                    if eligible_count is None
                    else max(0, int(eligible_count))
                ),
            ),
            execution_requested=execute,
            failure_codes=tuple(dict.fromkeys(failures)),
            controller_elapsed_ns=controller_elapsed_ns,
            preflight_elapsed_ns=max(0, preflight_elapsed_ns),
            dispatch_spread_ns=max(0, dispatch_spread_ns),
            queue_wait_ns=max(0, queue_wait_ns),
            scheduled_windows=max(0, scheduled),
        )

    def send_approved_key(
        self,
        key: object,
        *,
        policy: object,
        execute: bool = False,
        exclude_foreground: bool = False,
        source_handle: int | None = None,
        execution_guard: Callable[[], bool] | None = None,
    ) -> InputSyncResult:
        if not execute or self._operation_gate is None:
            return self._send_approved_key_without_gate(
                key,
                policy=policy,
                execute=execute,
                exclude_foreground=exclude_foreground,
                source_handle=source_handle,
                execution_guard=execution_guard,
            )
        lease = self._operation_gate.acquire(
            "keyboard-sync",
            execution_guard=execution_guard,
        )
        if lease is None:
            started_ns = perf_counter_ns()
            return self._base_result(
                key=normalize_approved_key(key),
                policy=normalize_input_policy(policy),
                windows=self._matching_windows(),
                execute=True,
                failures=("operation_gate_closed",),
                controller_started_ns=started_ns,
            )
        try:
            return self._send_approved_key_without_gate(
                key,
                policy=policy,
                execute=True,
                exclude_foreground=exclude_foreground,
                source_handle=source_handle,
                execution_guard=execution_guard,
            )
        finally:
            lease.release()

    def _send_approved_key_without_gate(
        self,
        key: object,
        *,
        policy: object,
        execute: bool = False,
        exclude_foreground: bool = False,
        source_handle: int | None = None,
        execution_guard: Callable[[], bool] | None = None,
    ) -> InputSyncResult:
        controller_started_ns = perf_counter_ns()
        normalized_key = normalize_approved_key(key)
        normalized_policy = normalize_input_policy(policy)
        windows = self._matching_windows()
        failures: list[str] = []

        reconnecting = {
            fingerprint
            for value in self._reconnecting_provider()
            if (fingerprint := normalize_launch_fingerprint(value)) is not None
        }
        foreground = self._window_backend.foreground_handle()
        captured_source = (
            source_handle
            if (
                isinstance(source_handle, int)
                and not isinstance(source_handle, bool)
                and source_handle > 0
            )
            else foreground
        )
        matching_handles = {window.handle for window in windows}
        if execute and captured_source not in matching_handles:
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                execute=True,
                failures=("foreground_not_in_group",),
                controller_started_ns=controller_started_ns,
            )
        source_fingerprint = next(
            (
                normalize_launch_fingerprint(window.launch_fingerprint)
                for window in windows
                if window.handle == captured_source
            ),
            None,
        )
        if (
            execute
            and self._controller_fingerprint is not None
            and source_fingerprint != self._controller_fingerprint
        ):
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                execute=True,
                failures=("source_not_controller",),
                controller_started_ns=controller_started_ns,
            )
        if normalized_key is None:
            failures.append("key_not_approved")
        if normalized_policy is None:
            failures.append("input_policy_invalid")
        process_ids = [
            window.process_id
            for window in windows
            if isinstance(window.process_id, int)
            and not isinstance(window.process_id, bool)
            and window.process_id > 0
        ]
        process_identity_valid = not (
            len(process_ids) != len(windows)
            or len(set(process_ids)) != len(process_ids)
        )
        if not process_identity_valid:
            failures.append("process_identity_missing_or_duplicate")

        fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        fingerprint_identity_valid = not (
            any(fingerprint is None for fingerprint in fingerprints)
            or len(set(fingerprints)) != len(fingerprints)
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

        if failures or normalized_key is None or normalized_policy is None:
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                execute=execute,
                failures=failures,
                controller_started_ns=controller_started_ns,
            )

        if exclude_foreground and captured_source not in matching_handles:
            failures.append("foreground_not_in_group")
        if normalized_policy is WindowInputPolicy.FOREGROUND_ONLY:
            eligible = tuple(
                window
                for window in windows
                if window.handle == captured_source
            )
            if not eligible:
                failures.append("foreground_not_in_group")
        elif normalized_policy is WindowInputPolicy.FOREGROUND_BACKGROUND:
            eligible = tuple(window for window in windows if not window.minimized)
        else:
            eligible = windows

        if exclude_foreground:
            eligible = tuple(
                window
                for window in eligible
                if window.handle != captured_source
            )

        group_reconnecting = (
            bool(reconnecting & self._allowed_fingerprint_set)
            if self._allowed_fingerprint_set is not None
            else bool(reconnecting)
        )
        if (
            not failures
            and execute
            and group_reconnecting
            and normalized_key is not None
            and normalized_policy is not None
            and self._deferred_service is not None
            and self._allowed_fingerprints is not None
        ):
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
                    if source_fingerprint is not None
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
            targets = tuple(
                fingerprint
                for fingerprint in candidates
                if not (
                    exclude_foreground
                    and fingerprint == source_fingerprint
                )
            )
            if not targets:
                return self._base_result(
                    key=normalized_key,
                    policy=normalized_policy,
                    windows=windows,
                    eligible=eligible,
                    execute=True,
                    failures=("no_eligible_windows",),
                    controller_started_ns=controller_started_ns,
                )
            for fingerprint in targets:
                self._deferred_service.enqueue(
                    fingerprint,
                    f"key:{normalized_key}",
                    kind="keyboard",
                    payload={
                        "key": normalized_key,
                        "policy": normalized_policy.value,
                        "source_eligible_at_capture": True,
                    },
                )
                self._record_role_operation(
                    fingerprint,
                    f"快捷鍵 {normalized_key}",
                    "全組等待重連後補做",
                )
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                eligible=eligible,
                execute=True,
                failures=("sync_group_deferred_reconnect",),
                controller_started_ns=controller_started_ns,
                eligible_count=len(targets),
            )

        if not eligible:
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                eligible=eligible,
                execute=execute,
                failures=("no_eligible_windows",),
                controller_started_ns=controller_started_ns,
            )

        preflight_started_ns = perf_counter_ns()
        valid_targets = tuple(
            window
            for window in eligible
            if self._message_backend.is_window(window.handle)
        )
        if len(valid_targets) != len(eligible):
            failures.append("input_target_invalid")

        responsive_targets = self._responsive_targets(valid_targets)
        preflight_elapsed_ns = max(
            0,
            perf_counter_ns() - preflight_started_ns,
        )
        if len(responsive_targets) != len(eligible):
            failures.append("input_target_unresponsive")

        if failures:
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                eligible=eligible,
                responsive=len(responsive_targets),
                execute=execute,
                failures=failures,
                controller_started_ns=controller_started_ns,
                preflight_elapsed_ns=preflight_elapsed_ns,
            )

        if not execute:
            return self._base_result(
                key=normalized_key,
                policy=normalized_policy,
                windows=windows,
                eligible=eligible,
                responsive=len(responsive_targets),
                execute=False,
                controller_started_ns=controller_started_ns,
                preflight_elapsed_ns=preflight_elapsed_ns,
            )

        virtual_keys = VIRTUAL_KEY_SEQUENCES[normalized_key]
        sent = 0
        scheduled = 0
        deferred = 0
        reconnecting = reconnecting
        dispatch_first_ns: int | None = None
        dispatch_last_ns: int | None = None
        for window in responsive_targets:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if (
                fingerprint in reconnecting
                and self._deferred_service is not None
            ):
                self._deferred_service.enqueue(
                    fingerprint,
                    f"key:{normalized_key}",
                    kind="keyboard",
                    payload={"key": normalized_key},
                )
                deferred += 1
                self._record_role_operation(
                    fingerprint,
                    f"快捷鍵 {normalized_key}",
                    "等待重連後補做",
                )
                failures.append("sync_deferred_reconnect")
                continue
            settings = self._settings_for(fingerprint)
            if settings.delay_ms > 0 and fingerprint is not None:
                scheduled_ok = self._dispatch_scheduler.schedule(
                    settings.delay_ms,
                    lambda role_fingerprint=fingerprint,
                    delayed_key=normalized_key,
                    delayed_virtual_keys=virtual_keys,
                    delayed_guard=execution_guard: self._run_scheduled_key(
                        role_fingerprint,
                        delayed_key,
                        delayed_virtual_keys,
                        delayed_guard,
                    ),
                )
                if scheduled_ok:
                    scheduled += 1
                    self._record_role_operation(
                        fingerprint,
                        f"快捷鍵 {normalized_key}",
                        f"已排程延遲 {settings.delay_ms} 毫秒",
                    )
                else:
                    failures.append("input_schedule_failed")
                continue
            lease = (
                self._conflict_arbiter.try_begin(
                    fingerprint,
                    f"key:{normalized_key}",
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
                            break
                    dispatch_started_ns = perf_counter_ns()
                    if dispatch_first_ns is None:
                        dispatch_first_ns = dispatch_started_ns
                    dispatch_last_ns = dispatch_started_ns
                    if len(virtual_keys) == 1:
                        delivered = self._message_backend.send_virtual_key(
                            window.handle,
                            virtual_keys[0],
                        )
                    else:
                        delivered = self._message_backend.send_key_chord(
                            window.handle,
                            virtual_keys,
                        )
                    if delivered:
                        sent += 1
                    self._record_role_operation(
                        fingerprint,
                        f"快捷鍵 {normalized_key}",
                        "成功" if delivered else "失敗",
                    )
                except OSError:
                    continue
            except OSError:
                continue
            finally:
                if lease is not None:
                    lease.release()
        if sent + scheduled + deferred != len(eligible):
            failures.append("input_delivery_failed")
        dispatch_spread_ns = (
            max(0, dispatch_last_ns - dispatch_first_ns)
            if (
                dispatch_first_ns is not None
                and dispatch_last_ns is not None
            )
            else 0
        )

        return self._base_result(
            key=normalized_key,
            policy=normalized_policy,
            windows=windows,
            eligible=eligible,
            responsive=len(responsive_targets),
            sent=sent,
            execute=True,
            failures=failures,
            controller_started_ns=controller_started_ns,
            preflight_elapsed_ns=preflight_elapsed_ns,
            dispatch_spread_ns=dispatch_spread_ns,
            scheduled=scheduled,
        )
