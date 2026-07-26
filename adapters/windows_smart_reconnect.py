"""Safe multi-window smart reconnect for the confirmed Flash login flow."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Iterable, Protocol

from adapters.game_screen_recognizer import (
    FORCE_LOGIN_CLICK_POINT,
    NormalizedPoint,
    ReferenceScreenRecognizer,
    ScreenRecognition,
)
from adapters.windows_background_capture import (
    Win32RecoveringPrintWindowProvider,
    WindowCaptureProvider,
)
from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import Win32WindowBackend, WindowBackend, WindowInfo
from core.reconnect_policy import (
    ReconnectAction,
    ReconnectPolicy,
    ReconnectScreenState,
)
from core.sp1_boundaries import OperationResult, ReconnectState, SmartReconnectBoundary


ACTIONABLE_RECONNECT_ACTIONS = frozenset(
    {
        ReconnectAction.CONFIRM_DISCONNECT,
        ReconnectAction.START_GAME,
        ReconnectAction.FORCE_LOGIN,
        ReconnectAction.SELECT_DEFAULT_LINE,
        ReconnectAction.ENTER_GAME,
        ReconnectAction.CLOSE_ANNOUNCEMENT,
    }
)
POST_LOGIN_AUTOMATION_GRACE_SECONDS = 180.0


class ScreenRecognizer(Protocol):
    def recognize_capture(self, sample) -> ScreenRecognition:
        """Recognize a capture without changing or persisting it."""


class MouseMessageBackend(Protocol):
    def is_window(self, handle: int) -> bool:
        """Return whether the supplied top-level window still exists."""

    def probe_responsive(self, handle: int, timeout_ms: int) -> bool:
        """Perform a no-op responsiveness check."""

    def click_relative(self, handle: int, point: NormalizedPoint) -> bool:
        """Send one client-relative left click to an already validated window."""


class Win32MouseMessageBackend:
    """Pure-ctypes mouse-message delivery that does not move the real cursor."""

    WM_NULL = 0x0000
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001
    SMTO_ABORTIFHUNG = 0x0002
    SW_SHOWNOACTIVATE = 4
    SW_SHOWMINNOACTIVE = 7
    MINIMIZED_PAINT_SETTLE_SECONDS = 0.05

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
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
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

    def click_relative(self, handle: int, point: NormalizedPoint) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        rect = wintypes.RECT()
        hwnd = wintypes.HWND(handle)
        was_minimized = bool(user32.IsIconic(hwnd))
        temporarily_restored = False
        try:
            if was_minimized:
                user32.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
                temporarily_restored = not bool(user32.IsIconic(hwnd))
                if temporarily_restored:
                    time.sleep(self.MINIMIZED_PAINT_SETTLE_SECONDS)
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return False
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width <= 1 or height <= 1:
                return False
            relative_x, relative_y = point
            if not (0.0 <= relative_x <= 1.0 and 0.0 <= relative_y <= 1.0):
                return False
            x = max(0, min(width - 1, round((width - 1) * relative_x)))
            y = max(0, min(height - 1, round((height - 1) * relative_y)))
            lparam = (y << 16) | (x & 0xFFFF)
            moved = bool(
                user32.PostMessageW(hwnd, self.WM_MOUSEMOVE, 0, lparam)
            )
            pressed = bool(
                user32.PostMessageW(
                    hwnd,
                    self.WM_LBUTTONDOWN,
                    self.MK_LBUTTON,
                    lparam,
                )
            )
            released = bool(
                user32.PostMessageW(hwnd, self.WM_LBUTTONUP, 0, lparam)
            )
            return moved and pressed and released
        finally:
            if temporarily_restored and not user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, self.SW_SHOWMINNOACTIVE)


@dataclass(frozen=True, slots=True)
class ReconnectBatchResult:
    expected_windows: int
    discovered_windows: int
    validated_windows: int
    captured_windows: int
    recognized_windows: int
    connected_windows: int
    actionable_windows: int
    clicked_windows: int
    unknown_windows: int
    execution_requested: bool
    next_check_seconds: int
    state_counts: tuple[tuple[str, int], ...]
    failure_codes: tuple[str, ...]

    @property
    def all_connected(self) -> bool:
        return (
            self.validated_windows == self.expected_windows
            and self.connected_windows == self.expected_windows
            and self.unknown_windows == 0
            and not self.failure_codes
        )

    @property
    def progressed(self) -> bool:
        return self.execution_requested and self.clicked_windows > 0

    def to_dict(self) -> dict[str, object]:
        """Return aggregate evidence without identity, pixels, or click coordinates."""
        return {
            "all_connected": self.all_connected,
            "progressed": self.progressed,
            "expected_windows": self.expected_windows,
            "discovered_windows": self.discovered_windows,
            "validated_windows": self.validated_windows,
            "captured_windows": self.captured_windows,
            "recognized_windows": self.recognized_windows,
            "connected_windows": self.connected_windows,
            "actionable_windows": self.actionable_windows,
            "clicked_windows": self.clicked_windows,
            "unknown_windows": self.unknown_windows,
            "execution_requested": self.execution_requested,
            "next_check_seconds": self.next_check_seconds,
            "state_counts": dict(self.state_counts),
            "failure_codes": list(self.failure_codes),
            "raw_arguments_emitted": False,
            "fingerprints_emitted": False,
            "captured_pixels_persisted": False,
            "click_coordinates_emitted": False,
        }


@dataclass(slots=True)
class ReconnectRuntimeState:
    pending_fingerprints: set[str]
    active_fingerprints: set[str]
    active_until: dict[str, float]
    retry_after: dict[str, tuple[ReconnectScreenState, float]]


class ReconnectRuntimeStateStore:
    """Persist only anonymous fingerprints and reconnect timing state."""

    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    @staticmethod
    def _empty() -> ReconnectRuntimeState:
        return ReconnectRuntimeState(set(), set(), {}, {})

    @staticmethod
    def _fingerprints(values: object) -> set[str]:
        if not isinstance(values, list):
            raise ValueError("Fingerprint collection must be a list")
        normalized = {
            fingerprint
            for value in values
            if (fingerprint := normalize_launch_fingerprint(value)) is not None
        }
        if len(normalized) != len(values):
            raise ValueError("Fingerprint collection contains invalid values")
        return normalized

    def load(self) -> ReconnectRuntimeState:
        if not self.path.is_file():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
                raise ValueError("Unsupported reconnect state version")
            pending = self._fingerprints(payload.get("pending_fingerprints", []))
            active = self._fingerprints(payload.get("active_fingerprints", []))
            raw_active_until = payload.get("active_until", {})
            if not isinstance(raw_active_until, dict):
                raise ValueError("active_until must be an object")
            active_until: dict[str, float] = {}
            for raw_fingerprint, raw_deadline in raw_active_until.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                deadline = float(raw_deadline)
                if fingerprint is None or deadline < 0:
                    raise ValueError("Invalid active automation deadline")
                active.add(fingerprint)
                active_until[fingerprint] = deadline
            raw_retries = payload.get("retry_after", {})
            if not isinstance(raw_retries, dict):
                raise ValueError("retry_after must be an object")
            retries: dict[str, tuple[ReconnectScreenState, float]] = {}
            for raw_fingerprint, raw_retry in raw_retries.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                if (
                    fingerprint is None
                    or not isinstance(raw_retry, dict)
                    or not isinstance(raw_retry.get("state"), str)
                ):
                    raise ValueError("Invalid retry entry")
                retry_at = float(raw_retry.get("retry_at"))
                if retry_at < 0:
                    raise ValueError("Invalid retry time")
                retries[fingerprint] = (
                    ReconnectScreenState(raw_retry["state"]),
                    retry_at,
                )
            return ReconnectRuntimeState(
                pending,
                active,
                active_until,
                retries,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self.recovered_from_corruption = True
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time())}"
            )
            try:
                os.replace(self.path, backup)
                self.corrupt_backup = backup
            except OSError:
                self.corrupt_backup = None
            return self._empty()

    def save(self, state: ReconnectRuntimeState) -> bool:
        payload = {
            "version": self.VERSION,
            "pending_fingerprints": sorted(state.pending_fingerprints),
            "active_fingerprints": sorted(state.active_fingerprints),
            "active_until": {
                fingerprint: state.active_until[fingerprint]
                for fingerprint in sorted(state.active_fingerprints)
                if fingerprint in state.active_until
            },
            "retry_after": {
                fingerprint: {
                    "state": screen_state.value,
                    "retry_at": retry_at,
                }
                for fingerprint, (screen_state, retry_at)
                in sorted(state.retry_after.items())
            },
        }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
            return True
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False


class WindowsSmartReconnectController(SmartReconnectBoundary):
    """Recognize and advance each Flash window without changing focus."""

    def __init__(
        self,
        *,
        expected_windows: int,
        title_keywords: Iterable[str],
        window_backend: WindowBackend,
        capture_provider: WindowCaptureProvider,
        recognizer: ScreenRecognizer,
        mouse_backend: MouseMessageBackend,
        policy: ReconnectPolicy | None = None,
        preflight_timeout_ms: int = 1000,
        monotonic_clock: Callable[[], float] = time.time,
        state_path: Path | None = None,
        execution_enabled: bool = False,
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
        self._capture_provider = capture_provider
        self._recognizer = recognizer
        self._mouse_backend = mouse_backend
        self._policy = policy or ReconnectPolicy()
        self._preflight_timeout_ms = max(1, int(preflight_timeout_ms))
        self._monotonic_clock = monotonic_clock
        self._state = ReconnectState.DISCONNECTED
        self._last_result: ReconnectBatchResult | None = None
        self._execution_enabled = threading.Event()
        if execution_enabled:
            self._execution_enabled.set()
        self._runtime_state_store = (
            ReconnectRuntimeStateStore(state_path)
            if state_path is not None
            else None
        )
        runtime_state = (
            self._runtime_state_store.load()
            if self._runtime_state_store is not None
            else ReconnectRuntimeState(set(), set(), {}, {})
        )
        self._pending_reconnect_fingerprints = (
            runtime_state.pending_fingerprints
        )
        self._active_automation_fingerprints = (
            runtime_state.active_fingerprints
        )
        self._active_automation_until = runtime_state.active_until
        now = self._monotonic_clock()
        for fingerprint in self._active_automation_fingerprints:
            self._active_automation_until.setdefault(
                fingerprint,
                now + POST_LOGIN_AUTOMATION_GRACE_SECONDS,
            )
        self._action_retry_after: dict[
            str,
            tuple[ReconnectScreenState, float],
        ] = runtime_state.retry_after
        self._character_selection_pending: set[str] = set()

    @classmethod
    def for_real_windows(
        cls,
        *,
        reference_dir: Path,
        expected_windows: int = 14,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        state_path: Path | None = None,
    ) -> "WindowsSmartReconnectController":
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=Win32WindowBackend(
                PowerShellLaunchFingerprintResolver()
            ),
            capture_provider=Win32RecoveringPrintWindowProvider(),
            recognizer=ReferenceScreenRecognizer(reference_dir),
            mouse_backend=Win32MouseMessageBackend(),
            state_path=state_path,
        )

    @property
    def state(self) -> ReconnectState:
        return self._state

    @property
    def last_result(self) -> ReconnectBatchResult | None:
        return self._last_result

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

    def set_execution_enabled(self, enabled: bool) -> None:
        """Allow an active scan to stop before its next game-changing click."""
        if enabled:
            self._execution_enabled.set()
        else:
            self._execution_enabled.clear()

    def _matching_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
        )

    def _group_failures(self, windows: tuple[WindowInfo, ...]) -> list[str]:
        failures: list[str] = []
        if len(windows) != self._expected_windows:
            failures.append("window_count_mismatch")
        handles = [window.handle for window in windows if window.handle]
        if len(handles) != len(windows) or len(set(handles)) != len(handles):
            failures.append("window_handle_missing_or_duplicate")
        process_ids = [
            window.process_id
            for window in windows
            if isinstance(window.process_id, int) and window.process_id > 0
        ]
        if (
            len(process_ids) != len(windows)
            or len(set(process_ids)) != len(process_ids)
        ):
            failures.append("process_identity_missing_or_duplicate")
        fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        if (
            any(fingerprint is None for fingerprint in fingerprints)
            or len(set(fingerprints)) != len(fingerprints)
        ):
            failures.append("fingerprint_missing_or_duplicate")
        return failures

    def _runtime_state(self) -> ReconnectRuntimeState:
        return ReconnectRuntimeState(
            self._pending_reconnect_fingerprints,
            self._active_automation_fingerprints,
            self._active_automation_until,
            self._action_retry_after,
        )

    def _runtime_state_signature(self) -> tuple[object, ...]:
        return (
            tuple(sorted(self._pending_reconnect_fingerprints)),
            tuple(sorted(self._active_automation_fingerprints)),
            tuple(sorted(self._active_automation_until.items())),
            tuple(
                sorted(
                    (
                        fingerprint,
                        screen_state.value,
                        retry_at,
                    )
                    for fingerprint, (screen_state, retry_at)
                    in self._action_retry_after.items()
                )
            ),
        )

    def _scan(self, *, execute: bool) -> ReconnectBatchResult:
        windows = self._matching_windows()
        failures = self._group_failures(windows)
        if failures:
            result = ReconnectBatchResult(
                expected_windows=self._expected_windows,
                discovered_windows=len(windows),
                validated_windows=0,
                captured_windows=0,
                recognized_windows=0,
                connected_windows=0,
                actionable_windows=0,
                clicked_windows=0,
                unknown_windows=0,
                execution_requested=execute,
                next_check_seconds=self._policy.retry_interval_seconds,
                state_counts=(),
                failure_codes=tuple(dict.fromkeys(failures)),
            )
            self._last_result = result
            self._state = ReconnectState.FAILED
            return result

        state_before = self._runtime_state_signature()
        now = self._monotonic_clock()
        expired_automation = {
            fingerprint
            for fingerprint, deadline in self._active_automation_until.items()
            if deadline <= now
        }
        self._active_automation_fingerprints.difference_update(
            expired_automation
        )
        for fingerprint in expired_automation:
            self._active_automation_until.pop(fingerprint, None)
        live_fingerprints = {
            fingerprint
            for window in windows
            if (
                fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
            )
            is not None
        }
        self._action_retry_after = {
            fingerprint: retry
            for fingerprint, retry in self._action_retry_after.items()
            if fingerprint in live_fingerprints
        }
        recognized: list[tuple[WindowInfo, str, ScreenRecognition]] = []
        captured_windows = 0
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is None:
                continue
            try:
                sample = self._capture_provider.capture(window.handle)
            except OSError:
                sample = None
            recognition = self._recognizer.recognize_capture(sample)
            if sample is not None and sample.api_succeeded:
                captured_windows += 1
            if recognition.state is ReconnectScreenState.DISCONNECTED:
                self._pending_reconnect_fingerprints.add(fingerprint)
            elif recognition.state is ReconnectScreenState.CONNECTED:
                self._pending_reconnect_fingerprints.discard(fingerprint)
            elif (
                recognition.state is ReconnectScreenState.LOGIN_START
                and fingerprint in self._pending_reconnect_fingerprints
            ):
                recognition = replace(
                    recognition,
                    state=ReconnectScreenState.FORCE_LOGIN_START,
                    click_point=FORCE_LOGIN_CLICK_POINT,
                )
            retry = self._action_retry_after.get(fingerprint)
            if retry is not None and retry[0] is not recognition.state:
                self._action_retry_after.pop(fingerprint, None)
            if recognition.state is not ReconnectScreenState.CHARACTER_SELECTION:
                self._character_selection_pending.discard(fingerprint)
            elif (
                recognition.character_slot_selected is True
                and fingerprint in self._character_selection_pending
            ):
                # Selecting the preferred character does not leave this
                # screen. Permit the next distinct step ("進入遊戲")
                # without waiting for the one-minute retry window.
                self._action_retry_after.pop(fingerprint, None)
            recognized.append((window, fingerprint, recognition))

        state_counts = Counter(
            item.state.value
            for _window, _fingerprint, item in recognized
        )
        unknown_windows = state_counts.get(ReconnectScreenState.UNKNOWN.value, 0)
        if captured_windows != len(windows):
            failures.append("capture_failed")
        if unknown_windows:
            failures.append("screen_unknown")

        actionable = [
            (window, fingerprint, item)
            for window, fingerprint, item in recognized
            if self._policy.decide(item.state).action
            in ACTIONABLE_RECONNECT_ACTIONS
            and item.click_point is not None
            and (
                item.state
                not in {
                    ReconnectScreenState.POST_LOGIN_ACTIVITY,
                    ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
                    ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
                }
                or fingerprint in self._active_automation_fingerprints
            )
        ]
        clicked_windows = 0
        invalid_targets = 0
        unresponsive_targets = 0
        delivery_failures = 0
        if execute:
            for window, fingerprint, item in actionable:
                if not self._execution_enabled.is_set():
                    break
                retry = self._action_retry_after.get(fingerprint)
                if (
                    retry is not None
                    and retry[0] is item.state
                    and now < retry[1]
                ):
                    continue
                self._action_retry_after[fingerprint] = (
                    item.state,
                    now + self._policy.retry_interval_seconds,
                )
                if not self._mouse_backend.is_window(window.handle):
                    invalid_targets += 1
                    continue
                if not self._mouse_backend.probe_responsive(
                    window.handle,
                    self._preflight_timeout_ms,
                ):
                    unresponsive_targets += 1
                    continue
                try:
                    delivered = self._mouse_backend.click_relative(
                        window.handle,
                        item.click_point,
                    )
                except OSError:
                    delivered = False
                if delivered:
                    clicked_windows += 1
                    if item.state is ReconnectScreenState.CHARACTER_SELECTION:
                        if item.character_slot_selected is True:
                            self._character_selection_pending.discard(
                                fingerprint
                            )
                        elif item.character_slot_selected is False:
                            self._character_selection_pending.add(fingerprint)
                    self._active_automation_fingerprints.add(fingerprint)
                    self._active_automation_until[fingerprint] = (
                        now + POST_LOGIN_AUTOMATION_GRACE_SECONDS
                    )
                else:
                    delivery_failures += 1
        if (
            self._runtime_state_store is not None
            and state_before != self._runtime_state_signature()
            and not self._runtime_state_store.save(self._runtime_state())
        ):
            failures.append("reconnect_state_persistence_failed")
        if invalid_targets:
            failures.append("input_target_invalid")
        if unresponsive_targets:
            failures.append("input_target_unresponsive")
        if delivery_failures:
            failures.append("click_delivery_failed")

        decisions = [
            self._policy.decide(item.state)
            for _window, _fingerprint, item in recognized
        ]
        if actionable:
            next_check_seconds = min(
                self._policy.decide(item.state).delay_seconds
                for _window, _fingerprint, item in actionable
            )
        elif unknown_windows or failures:
            next_check_seconds = self._policy.retry_interval_seconds
        elif decisions:
            non_connected_decisions = [
                self._policy.decide(item.state)
                for _window, _fingerprint, item in recognized
                if item.state is not ReconnectScreenState.CONNECTED
            ]
            next_check_seconds = min(
                decision.delay_seconds
                for decision in (non_connected_decisions or decisions)
            )
        else:
            next_check_seconds = self._policy.retry_interval_seconds
        result = ReconnectBatchResult(
            expected_windows=self._expected_windows,
            discovered_windows=len(windows),
            validated_windows=len(windows),
            captured_windows=captured_windows,
            recognized_windows=len(windows) - unknown_windows,
            connected_windows=state_counts.get(
                ReconnectScreenState.CONNECTED.value,
                0,
            ),
            actionable_windows=len(actionable),
            clicked_windows=clicked_windows,
            unknown_windows=unknown_windows,
            execution_requested=execute,
            next_check_seconds=next_check_seconds,
            state_counts=tuple(sorted(state_counts.items())),
            failure_codes=tuple(dict.fromkeys(failures)),
        )
        self._last_result = result
        if result.all_connected:
            self._state = ReconnectState.CONNECTED
        elif result.progressed:
            self._state = ReconnectState.RECONNECTING
        elif actionable:
            self._state = ReconnectState.DISCONNECTED
        else:
            self._state = ReconnectState.FAILED
        return result

    def check_connection(self) -> OperationResult:
        result = self._scan(execute=False)
        if result.all_connected:
            return OperationResult(
                True,
                "reconnect.connected",
                "All validated Flash windows are connected.",
                result.to_dict(),
            )
        if result.actionable_windows:
            return OperationResult(
                False,
                "reconnect.required",
                "One or more validated Flash windows require reconnect progress.",
                result.to_dict(),
            )
        return OperationResult(
            False,
            "reconnect.waiting",
            "Reconnect is waiting for a known screen or a complete window group.",
            result.to_dict(),
        )

    def reconnect(self) -> OperationResult:
        result = self._scan(execute=True)
        if result.all_connected:
            return OperationResult(
                True,
                "reconnect.connected",
                "All validated Flash windows are connected.",
                result.to_dict(),
            )
        if result.progressed:
            code = (
                "reconnect.progressed"
                if not result.failure_codes
                else "reconnect.progressed_with_isolation"
            )
            return OperationResult(
                True,
                code,
                "Known reconnect screens were advanced without changing focus.",
                result.to_dict(),
            )
        return OperationResult(
            False,
            "reconnect.waiting",
            "No safe reconnect click was available; the monitor will recheck.",
            result.to_dict(),
        )
