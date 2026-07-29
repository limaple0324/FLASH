"""Safe multi-window smart reconnect for the confirmed Flash login flow."""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Iterable, Protocol

from adapters.game_screen_recognizer import (
    CharacterSelectionCandidate,
    FORCE_LOGIN_CLICK_POINT,
    NormalizedPoint,
    ReferenceScreenRecognizer,
    ScreenRecognition,
)
from adapters.windows_background_capture import (
    Win32PrintWindowProvider,
    WindowCaptureProvider,
)
from adapters.windows_battle_restart import (
    Win32WindowCloseBackend,
    WindowsBattleWindowRestarter,
    WindowsShortcutOpenBackend,
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
from services.group_launch_service import GroupLaunchPlan
from services.game_operation_gate import GameOperationGate
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.target_window_contract_service import ResolvedTargetWindows


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
ACTION_CONFIRMATION_FRAMES = 2
_ROLE_LEVEL_PREFIX = re.compile(r"^\s*(\d{2,3})(?!\d)")
_SESSION_ONLY_STATES = frozenset(
    {
        ReconnectScreenState.LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_START,
        ReconnectScreenState.LINE_SELECTION,
        ReconnectScreenState.CHARACTER_SELECTION,
        ReconnectScreenState.POST_LOGIN_ACTIVITY,
        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
    }
)


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
    restarted_windows: int
    unknown_windows: int
    execution_requested: bool
    next_check_seconds: int
    state_counts: tuple[tuple[str, int], ...]
    failure_codes: tuple[str, ...]

    @property
    def all_connected(self) -> bool:
        return (
            self.discovered_windows > 0
            and self.validated_windows == self.discovered_windows
            and self.connected_windows == self.discovered_windows
            and self.unknown_windows == 0
            and not self.failure_codes
        )

    @property
    def progressed(self) -> bool:
        return self.execution_requested and (
            self.clicked_windows > 0 or self.restarted_windows > 0
        )

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
            "restarted_windows": self.restarted_windows,
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
    pending_reopen_fingerprints: set[str]
    reopen_retry_after: dict[str, float]


class ReconnectRuntimeStateStore:
    """Persist only anonymous fingerprints and reconnect timing state."""

    VERSION = 4
    LEGACY_VERSIONS = frozenset({1, 2, 3})

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    @staticmethod
    def _empty() -> ReconnectRuntimeState:
        return ReconnectRuntimeState(set(), set(), {}, {}, set(), {})

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
            if (
                not isinstance(payload, dict)
                or payload.get("version")
                not in self.LEGACY_VERSIONS | {self.VERSION}
            ):
                raise ValueError("Unsupported reconnect state version")
            # Versions 1-3 can contain reconnect authorization created before
            # the current two-frame disconnect gate.  Never carry that
            # authorization into a newer executable: migrate to a clean
            # current-version state before the controller can observe or act.
            if payload.get("version") != self.VERSION:
                empty = self._empty()
                self.save(empty)
                return empty
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
            pending_reopens: set[str] = set()
            reopen_retries: dict[str, float] = {}
            pending_reopens = self._fingerprints(
                payload.get("pending_reopen_fingerprints", [])
            )
            raw_reopen_retries = payload.get("reopen_retry_after", {})
            if not isinstance(raw_reopen_retries, dict):
                raise ValueError("reopen_retry_after must be an object")
            for raw_fingerprint, raw_retry_at in raw_reopen_retries.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                retry_at = float(raw_retry_at)
                if fingerprint is None or retry_at < 0:
                    raise ValueError("Invalid reopen retry entry")
                pending_reopens.add(fingerprint)
                reopen_retries[fingerprint] = retry_at
            return ReconnectRuntimeState(
                pending,
                active,
                active_until,
                retries,
                pending_reopens,
                reopen_retries,
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
            "pending_reopen_fingerprints": sorted(
                state.pending_reopen_fingerprints
            ),
            "reopen_retry_after": {
                fingerprint: state.reopen_retry_after[fingerprint]
                for fingerprint in sorted(state.pending_reopen_fingerprints)
                if fingerprint in state.reopen_retry_after
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
        allowed_fingerprints: Iterable[str] | None = None,
        battle_restarter: WindowsBattleWindowRestarter | None = None,
        failure_status_service: ReconnectFailureStatusService | None = None,
        failure_record_callback: (
            Callable[[str, str], object] | None
        ) = None,
        target_windows_provider: (
            Callable[[], Iterable[WindowInfo] | ResolvedTargetWindows] | None
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
        self._screen_state_lock = threading.RLock()
        self._runtime_state_store = (
            ReconnectRuntimeStateStore(state_path)
            if state_path is not None
            else None
        )
        runtime_state = (
            self._runtime_state_store.load()
            if self._runtime_state_store is not None
            else ReconnectRuntimeState(set(), set(), {}, {}, set(), {})
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
        self._pending_reopen_fingerprints = (
            runtime_state.pending_reopen_fingerprints
        )
        self._reopen_retry_after = runtime_state.reopen_retry_after
        self._character_selection_pending: set[str] = set()
        self._action_state_since: dict[
            str,
            tuple[ReconnectScreenState, float],
        ] = {}
        self._flow_pause_until: dict[str, float] = {}
        self._allowed_fingerprints: frozenset[str] | None = None
        self.set_allowed_fingerprints(allowed_fingerprints)
        self._battle_restarter = battle_restarter
        self._group_launch_plan: GroupLaunchPlan | None = None
        self._failure_status_service = failure_status_service
        self._failure_record_callback = failure_record_callback
        self._target_windows_provider = target_windows_provider
        self._operation_gate = operation_gate
        self._last_screen_states: dict[str, ReconnectScreenState] = {}
        self._action_confirmations: dict[
            str,
            tuple[tuple[object, ...], int],
        ] = {}
        if self._expire_active_automation(now):
            self._persist_runtime_state()

    @classmethod
    def for_real_windows(
        cls,
        *,
        reference_dir: Path,
        expected_windows: int = 14,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        state_path: Path | None = None,
        window_backend: WindowBackend | None = None,
        failure_status_service: ReconnectFailureStatusService | None = None,
        failure_record_callback: (
            Callable[[str, str], object] | None
        ) = None,
        target_windows_provider: (
            Callable[[], Iterable[WindowInfo] | ResolvedTargetWindows] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
    ) -> "WindowsSmartReconnectController":
        window_backend = (
            window_backend
            or Win32WindowBackend(PowerShellLaunchFingerprintResolver())
        )
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=window_backend,
            # Observation must never restore or activate a minimized game.
            # If a passive PrintWindow frame is stale, the controller fails
            # closed instead of disturbing the player to refresh it.
            capture_provider=Win32PrintWindowProvider(),
            recognizer=ReferenceScreenRecognizer(reference_dir),
            mouse_backend=Win32MouseMessageBackend(),
            state_path=state_path,
            battle_restarter=WindowsBattleWindowRestarter(
                window_backend,
                Win32WindowCloseBackend(),
                WindowsShortcutOpenBackend(),
            ),
            failure_status_service=failure_status_service,
            failure_record_callback=failure_record_callback,
            target_windows_provider=target_windows_provider,
            operation_gate=operation_gate,
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

    def role_screen_states(self) -> dict[str, ReconnectScreenState]:
        with self._screen_state_lock:
            return dict(self._last_screen_states)

    def observe_screen_states(
        self,
        fingerprints: Iterable[str],
    ) -> dict[str, ReconnectScreenState]:
        requested = {
            fingerprint
            for item in fingerprints
            if (fingerprint := normalize_launch_fingerprint(item)) is not None
        }
        if not requested:
            return {}
        candidates = self._candidate_windows()
        by_fingerprint: dict[str, list[WindowInfo]] = {}
        for window in candidates:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint in requested:
                by_fingerprint.setdefault(fingerprint, []).append(window)
        observed: dict[str, ReconnectScreenState] = {}
        for fingerprint in requested:
            matches = by_fingerprint.get(fingerprint, ())
            if len(matches) != 1:
                continue
            try:
                sample = self._capture_provider.capture(matches[0].handle)
            except OSError:
                sample = None
            recognition = self._recognizer.recognize_capture(sample)
            if recognition.state is not ReconnectScreenState.UNKNOWN:
                observed[fingerprint] = recognition.state
        if observed:
            with self._screen_state_lock:
                self._last_screen_states.update(observed)
        return observed

    def reconnecting_fingerprints(self) -> frozenset[str]:
        if self._expire_active_automation(self._monotonic_clock()):
            self._persist_runtime_state()
        with self._screen_state_lock:
            return frozenset(
                self._pending_reconnect_fingerprints
                | self._pending_reopen_fingerprints
                | self._active_automation_fingerprints
            )

    def _expire_active_automation(self, now: float) -> bool:
        with self._screen_state_lock:
            expired = {
                fingerprint
                for fingerprint, deadline
                in tuple(self._active_automation_until.items())
                if deadline <= now
            }
            if not expired:
                return False
            self._active_automation_fingerprints.difference_update(expired)
            for fingerprint in expired:
                self._active_automation_until.pop(fingerprint, None)
            return True

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
        self._allowed_fingerprints = frozenset(normalized)

    def set_group_launch_plan(self, plan: GroupLaunchPlan | None) -> None:
        previous_plan = self._group_launch_plan
        previous_scope = self._allowed_fingerprints
        if plan is None:
            self._group_launch_plan = None
            self.set_allowed_fingerprints(None)
            if previous_plan is not None or previous_scope is not None:
                self._retain_runtime_scope(frozenset())
            return
        if not isinstance(plan, GroupLaunchPlan) or not plan.ready:
            raise ValueError("plan must be a ready GroupLaunchPlan.")
        self._group_launch_plan = plan
        self.set_allowed_fingerprints(plan.fingerprints)
        if (
            previous_plan is not None
            and previous_plan.group_name != plan.group_name
        ):
            # A group switch is a new reconnect context even when the two
            # groups share one role or the entire fingerprint set.
            self._retain_runtime_scope(frozenset())
        elif previous_scope != self._allowed_fingerprints:
            self._retain_runtime_scope(self._allowed_fingerprints)

    def _retain_runtime_scope(self, fingerprints: frozenset[str]) -> None:
        """Revoke reconnect authority that belongs to a previous group."""
        with self._screen_state_lock:
            tracked = (
                self._pending_reconnect_fingerprints
                | self._active_automation_fingerprints
                | self._pending_reopen_fingerprints
                | self._character_selection_pending
                | set(self._active_automation_until)
                | set(self._action_retry_after)
                | set(self._reopen_retry_after)
                | set(self._action_state_since)
                | set(self._flow_pause_until)
                | set(self._action_confirmations)
                | set(self._last_screen_states)
            )
            removed = tracked - fingerprints
            if not removed:
                return
            self._pending_reconnect_fingerprints.intersection_update(fingerprints)
            self._active_automation_fingerprints.intersection_update(fingerprints)
            self._pending_reopen_fingerprints.intersection_update(fingerprints)
            self._character_selection_pending.intersection_update(fingerprints)
            for mapping in (
                self._active_automation_until,
                self._action_retry_after,
                self._reopen_retry_after,
                self._action_state_since,
                self._flow_pause_until,
                self._action_confirmations,
                self._last_screen_states,
            ):
                for fingerprint in removed:
                    mapping.pop(fingerprint, None)
        for fingerprint in removed:
            self._clear_reconnect_failure(fingerprint)
        self._persist_runtime_state()

    def set_execution_enabled(self, enabled: bool) -> None:
        """Allow an active scan to stop before its next game-changing click."""
        if enabled:
            self._execution_enabled.set()
        else:
            self._execution_enabled.clear()

    def _execution_allowed(self) -> bool:
        """Read the stop gate immediately before every mutating backend call."""
        return self._execution_enabled.is_set()

    def _matching_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._candidate_windows()
            if (
                self._allowed_fingerprints is None
                or normalize_launch_fingerprint(window.launch_fingerprint)
                in self._allowed_fingerprints
            )
        )

    def _candidate_windows(self) -> tuple[WindowInfo, ...]:
        return self._candidate_window_set()[0]

    def _candidate_window_set(
        self,
    ) -> tuple[
        tuple[WindowInfo, ...],
        tuple[str, ...],
        frozenset[str],
    ]:
        if self._target_windows_provider is not None:
            try:
                provided = self._target_windows_provider()
                if isinstance(provided, ResolvedTargetWindows):
                    return (
                        provided.windows,
                        provided.failure_codes,
                        provided.blocked_fingerprints,
                    )
                return tuple(provided), (), frozenset()
            except Exception:
                return (
                    (),
                    ("target_window_provider_failed",),
                    frozenset(),
                )
        try:
            return (
                tuple(
                    window
                    for window in self._window_backend.list_windows()
                    if all(
                        keyword in window.title.casefold()
                        for keyword in self._keywords
                    )
                ),
                (),
                frozenset(),
            )
        except Exception:
            return (), ("window_enumeration_failed",), frozenset()

    def _target_for_fingerprint(self, fingerprint: str):
        plan = self._group_launch_plan
        return (
            plan.target_for_fingerprint(fingerprint)
            if plan is not None
            else None
        )

    def _has_reconnect_session(self, fingerprint: str) -> bool:
        return fingerprint in (
            self._pending_reconnect_fingerprints
            | self._pending_reopen_fingerprints
            | self._active_automation_fingerprints
        )

    @staticmethod
    def _action_signature(item: ScreenRecognition) -> tuple[object, ...]:
        return (
            item.state,
            item.click_point,
            item.line_number,
            item.character_level,
            item.character_slot_index,
            item.character_slot_selected,
            item.character_identity,
            item.character_candidates,
            item.battle_context,
        )

    def _clear_action_confirmation(self, fingerprint: str | None = None) -> None:
        if fingerprint is None:
            self._action_confirmations.clear()
            return
        self._action_confirmations.pop(fingerprint, None)

    def _clear_reconnect_session(self, fingerprint: str) -> None:
        """Revoke every permission granted by a completed reconnect flow."""
        self._pending_reconnect_fingerprints.discard(fingerprint)
        self._pending_reopen_fingerprints.discard(fingerprint)
        self._active_automation_fingerprints.discard(fingerprint)
        self._active_automation_until.pop(fingerprint, None)
        self._action_retry_after.pop(fingerprint, None)
        self._reopen_retry_after.pop(fingerprint, None)
        self._character_selection_pending.discard(fingerprint)
        self._action_state_since.pop(fingerprint, None)
        self._flow_pause_until.pop(fingerprint, None)
        self._clear_action_confirmation(fingerprint)

    def _action_is_confirmed(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> bool:
        signature = self._action_signature(item)
        previous_signature, previous_count = self._action_confirmations.get(
            fingerprint,
            ((), 0),
        )
        count = previous_count + 1 if previous_signature == signature else 1
        self._action_confirmations[fingerprint] = (signature, count)
        return count >= ACTION_CONFIRMATION_FRAMES

    def _action_wait_seconds(
        self,
        fingerprint: str,
        state: ReconnectScreenState,
        now: float,
    ) -> int:
        deadlines: list[float] = []
        first_seen = self._action_state_since.get(fingerprint)
        if state is ReconnectScreenState.DISCONNECTED and first_seen is not None:
            deadlines.append(
                first_seen[1]
                + self._policy.disconnect_confirmation_wait_seconds
            )
        pause_until = self._flow_pause_until.get(fingerprint)
        if pause_until is not None:
            if pause_until > now:
                deadlines.append(pause_until)
            else:
                self._flow_pause_until.pop(fingerprint, None)
        if not deadlines:
            return 0
        return max(0, int(math.ceil(max(deadlines) - now)))

    @staticmethod
    def _target_level(target) -> int | None:
        for label in (target.display_name, target.shortcut_path.stem):
            match = _ROLE_LEVEL_PREFIX.match(label)
            if match is not None:
                return int(match.group(1))
        return None

    def _character_target_is_safe(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> ScreenRecognition | None:
        """Choose the original shortcut role without guessing tied slots."""
        target = self._target_for_fingerprint(fingerprint)
        plan = self._group_launch_plan
        identity = (
            item.character_identity.strip().casefold()
            if isinstance(item.character_identity, str)
            and item.character_identity.strip()
            else None
        )
        if target is None or plan is None:
            return None
        expected_level = self._target_level(target)
        if identity is not None:
            if item.character_level is None:
                return None
            identity_matches = tuple(
                candidate
                for candidate in plan.targets
                if identity
                in {
                    candidate.display_name.strip().casefold(),
                    candidate.shortcut_path.stem.strip().casefold(),
                }
            )
            if (
                len(identity_matches) != 1
                or identity_matches[0].fingerprint != fingerprint
                or expected_level is None
                or expected_level != item.character_level
            ):
                return None
            return item

        candidates: tuple[CharacterSelectionCandidate, ...] = (
            item.character_candidates
        )
        if not candidates:
            return None
        if expected_level is not None:
            matching = tuple(
                candidate
                for candidate in candidates
                if candidate.level == expected_level
            )
        else:
            first = candidates[0]
            matching = tuple(
                candidate
                for candidate in candidates
                if (
                    candidate.importance is first.importance
                    and candidate.level == first.level
                )
            )
        if len(matching) != 1:
            return None
        selected = matching[0]
        return replace(
            item,
            click_point=selected.click_point,
            character_level=selected.level,
            character_importance=selected.importance,
            character_slot_index=selected.slot_index,
            character_slot_selected=selected.selected,
        )

    def _unknown_failure_key(self) -> str:
        group_name = (
            self._group_launch_plan.group_name
            if self._group_launch_plan is not None
            else "目前組別"
        )
        return f"group:{group_name}:unknown"

    def _report_reconnect_failure(self, fingerprint: str | None) -> None:
        service = self._failure_status_service
        if service is None:
            return
        target = (
            self._target_for_fingerprint(fingerprint)
            if fingerprint is not None
            else None
        )
        if target is not None:
            key = f"role:{target.fingerprint}"
            service.report(key, target.display_name)
            self._record_reconnect_failure(
                target.display_name,
                "重連失敗",
            )
            restart = self._restart_failed_role(
                target.fingerprint,
            )
            self._record_reconnect_failure(
                target.display_name,
                (
                    "已依原捷徑重新開啟"
                    if restart.success
                    else f"重新開啟失敗：{restart.failure_code}"
                ),
            )
            return
        group_name = (
            self._group_launch_plan.group_name
            if self._group_launch_plan is not None
            else "目前組別"
        )
        service.report(
            self._unknown_failure_key(),
            f"{group_name}中的未知角色",
        )

    def _record_reconnect_failure(
        self,
        role_name: str,
        detail: str,
    ) -> None:
        if self._failure_record_callback is None:
            return
        try:
            self._failure_record_callback(role_name, detail)
        except Exception:
            pass

    def _restart_failed_role(
        self,
        fingerprint: str,
    ) -> BattleRestartResult:
        target = self._target_for_fingerprint(fingerprint)
        restarter = self._battle_restarter
        if target is None or restarter is None:
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(
                False,
                "reconnect_restart_identity_unresolved",
            )
        candidates = self._candidate_windows()
        matches = tuple(
            window
            for window in candidates
            if normalize_launch_fingerprint(window.launch_fingerprint)
            == fingerprint
        )
        if len(matches) > 1:
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(
                False,
                "reconnect_restart_window_ambiguous",
            )
        if not self._execution_allowed():
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(False, "reconnect_stopped")
        if len(matches) == 1:
            result = restarter.restart(matches[0], target)
        else:
            reopen_missing = getattr(restarter, "reopen_missing", None)
            if not callable(reopen_missing):
                self._clear_action_confirmation(fingerprint)
                return BattleRestartResult(
                    False,
                    "reconnect_restart_unavailable",
                )
            if not self._execution_allowed():
                self._clear_action_confirmation(fingerprint)
                return BattleRestartResult(False, "reconnect_stopped")
            result = reopen_missing(target, candidates)
        if result.success:
            now = self._monotonic_clock()
            self._pending_reconnect_fingerprints.add(fingerprint)
            self._pending_reopen_fingerprints.add(fingerprint)
            self._reopen_retry_after[fingerprint] = (
                now + self._policy.progress_interval_seconds
            )
            self._persist_runtime_state()
        return result

    def _clear_reconnect_failure(self, fingerprint: str) -> None:
        service = self._failure_status_service
        if service is None:
            return
        service.clear(f"role:{fingerprint}")

    def _clear_unknown_reconnect_failure(self) -> None:
        service = self._failure_status_service
        if service is not None:
            service.clear(self._unknown_failure_key())

    def _group_failures(self, windows: tuple[WindowInfo, ...]) -> list[str]:
        failures: list[str] = []
        # Without an explicit group identity scope, completeness remains the
        # safety boundary.  With a ready group plan, reconnect monitors only
        # the uniquely resolved windows that are currently open.  A role that
        # is deliberately closed or belongs to another group must not block a
        # safe open sibling.
        if (
            self._allowed_fingerprints is None
            and len(windows) != self._expected_windows
        ):
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
        if self._allowed_fingerprints is not None and not set(
            fingerprints
        ).issubset(self._allowed_fingerprints):
            failures.append("group_identity_set_mismatch")
        return failures

    def _runtime_state(self) -> ReconnectRuntimeState:
        with self._screen_state_lock:
            return ReconnectRuntimeState(
                set(self._pending_reconnect_fingerprints),
                set(self._active_automation_fingerprints),
                dict(self._active_automation_until),
                dict(self._action_retry_after),
                set(self._pending_reopen_fingerprints),
                dict(self._reopen_retry_after),
            )

    def _persist_runtime_state(self) -> bool:
        return (
            self._runtime_state_store.save(self._runtime_state())
            if self._runtime_state_store is not None
            else True
        )

    def _runtime_state_signature(self) -> tuple[object, ...]:
        with self._screen_state_lock:
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
                tuple(sorted(self._pending_reopen_fingerprints)),
                tuple(sorted(self._reopen_retry_after.items())),
            )

    def _retry_pending_reopens(
        self,
        *,
        candidate_windows: tuple[WindowInfo, ...],
        blocked_fingerprints: frozenset[str],
        execute: bool,
        now: float,
    ) -> tuple[int, list[str], int | None]:
        live_fingerprints = {
            fingerprint
            for window in candidate_windows
            if (
                fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
            )
            is not None
        }
        appeared = (
            self._pending_reopen_fingerprints & live_fingerprints
        )
        self._pending_reopen_fingerprints.difference_update(appeared)
        for fingerprint in appeared:
            self._reopen_retry_after.pop(fingerprint, None)

        missing = tuple(sorted(self._pending_reopen_fingerprints))
        if not missing:
            return 0, [], None

        failures: list[str] = []
        reopened = 0
        next_delays: list[int] = []
        for fingerprint in missing:
            retry_at = self._reopen_retry_after.get(fingerprint, now)
            if fingerprint in blocked_fingerprints:
                failures.append("battle_reopen_identity_unsafe")
                next_delays.append(self._policy.retry_interval_seconds)
                continue
            if not execute or not self._execution_allowed():
                next_delays.append(
                    max(1, math.ceil(max(0.0, retry_at - now)))
                )
                continue
            if now < retry_at:
                next_delays.append(max(1, math.ceil(retry_at - now)))
                continue

            target = self._target_for_fingerprint(fingerprint)
            retry_open = getattr(
                self._battle_restarter,
                "reopen_missing",
                None,
            )
            self._reopen_retry_after[fingerprint] = (
                now + self._policy.progress_interval_seconds
            )
            next_delays.append(self._policy.progress_interval_seconds)
            if target is None or not callable(retry_open):
                self._clear_action_confirmation(fingerprint)
                failures.append("battle_restart_identity_unresolved")
                self._report_reconnect_failure(None)
                continue

            if not self._execution_allowed():
                self._clear_action_confirmation(fingerprint)
                next_delays.append(1)
                continue
            result = retry_open(target, candidate_windows)
            if result.success:
                reopened += 1
            else:
                failures.append(
                    result.failure_code or "battle_shortcut_open_failed"
                )
                # The role still failed. Record it and immediately retry only
                # this exact role instead of waiting for the old 60-second
                # interval.
                self._report_reconnect_failure(fingerprint)

        next_delay = min(next_delays) if next_delays else None
        return reopened, failures, next_delay

    def _scan(self, *, execute: bool) -> ReconnectBatchResult:
        (
            candidate_windows,
            source_failures,
            blocked_fingerprints,
        ) = self._candidate_window_set()
        windows = tuple(
            window
            for window in candidate_windows
            if (
                self._allowed_fingerprints is None
                or normalize_launch_fingerprint(window.launch_fingerprint)
                in self._allowed_fingerprints
            )
        )
        state_before = self._runtime_state_signature()
        now = self._monotonic_clock()
        retried_reopens, retry_failures, pending_reopen_delay = (
            self._retry_pending_reopens(
                candidate_windows=candidate_windows,
                blocked_fingerprints=blocked_fingerprints,
                execute=execute,
                now=now,
            )
        )
        group_failures = self._group_failures(windows)
        failures = [*group_failures, *source_failures, *retry_failures]
        if group_failures:
            # A partially validated group must never carry a first-frame
            # confirmation into a later, different group or identity set.
            self._clear_action_confirmation()
            if (
                self._runtime_state_store is not None
                and state_before != self._runtime_state_signature()
                and not self._runtime_state_store.save(self._runtime_state())
            ):
                failures.append("reconnect_state_persistence_failed")
            result = ReconnectBatchResult(
                expected_windows=self._expected_windows,
                discovered_windows=len(windows),
                validated_windows=0,
                captured_windows=0,
                recognized_windows=0,
                connected_windows=0,
                actionable_windows=0,
                clicked_windows=0,
                restarted_windows=retried_reopens,
                unknown_windows=0,
                execution_requested=execute,
                next_check_seconds=(
                    2
                    if retried_reopens
                    else (
                        pending_reopen_delay
                        or self._policy.retry_interval_seconds
                    )
                ),
                state_counts=(),
                failure_codes=tuple(dict.fromkeys(failures)),
            )
            self._last_result = result
            self._state = (
                ReconnectState.RECONNECTING
                if result.progressed
                else ReconnectState.FAILED
            )
            return result

        self._expire_active_automation(now)
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
        self._action_confirmations = {
            fingerprint: confirmation
            for fingerprint, confirmation in self._action_confirmations.items()
            if fingerprint in live_fingerprints
        }
        self._action_retry_after = {
            fingerprint: retry
            for fingerprint, retry in self._action_retry_after.items()
            if fingerprint in live_fingerprints
        }
        self._action_state_since = {
            fingerprint: state_and_time
            for fingerprint, state_and_time in self._action_state_since.items()
            if fingerprint in live_fingerprints
        }
        self._flow_pause_until = {
            fingerprint: deadline
            for fingerprint, deadline in self._flow_pause_until.items()
            if fingerprint in live_fingerprints
        }
        recognized: list[tuple[WindowInfo, str, ScreenRecognition]] = []
        confirmed_action_fingerprints: set[str] = set()
        pending_confirmation_delays: list[int] = []
        pending_action_wait_delays: list[int] = []
        captured_windows = 0
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is None:
                self._clear_action_confirmation()
                continue
            try:
                sample = self._capture_provider.capture(window.handle)
            except OSError:
                sample = None
            recognition = self._recognizer.recognize_capture(sample)
            if sample is not None and sample.api_succeeded:
                captured_windows += 1
            else:
                self._clear_action_confirmation(fingerprint)
            if recognition.state is ReconnectScreenState.DISCONNECTED:
                if (
                    recognition.click_point is not None
                    and self._action_is_confirmed(fingerprint, recognition)
                ):
                    confirmed_action_fingerprints.add(fingerprint)
            elif recognition.state is ReconnectScreenState.CONNECTED:
                # Normal gameplay is the terminal state. Revoke the entire
                # reconnect session immediately so a later manual login or
                # popup cannot inherit the previous 180-second authorization.
                self._clear_reconnect_session(fingerprint)
                self._clear_reconnect_failure(fingerprint)
            elif (
                recognition.state is ReconnectScreenState.LOGIN_START
                and self._has_reconnect_session(fingerprint)
            ):
                recognition = replace(
                    recognition,
                    state=ReconnectScreenState.FORCE_LOGIN_START,
                    click_point=FORCE_LOGIN_CLICK_POINT,
                )
            if recognition.state is ReconnectScreenState.CHARACTER_SELECTION:
                role_target = self._character_target_is_safe(
                    fingerprint,
                    recognition,
                )
                if role_target is None:
                    recognition = replace(recognition, click_point=None)
                    self._clear_action_confirmation(fingerprint)
                else:
                    recognition = role_target
            previous_state = self._action_state_since.get(fingerprint)
            if previous_state is None or previous_state[0] is not recognition.state:
                self._action_state_since[fingerprint] = (
                    recognition.state,
                    now,
                )
            action = self._policy.decide(recognition.state).action
            is_action_candidate = (
                action in ACTIONABLE_RECONNECT_ACTIONS
                and recognition.click_point is not None
                and (
                    recognition.state is ReconnectScreenState.DISCONNECTED
                    or (
                        recognition.state in _SESSION_ONLY_STATES
                        and self._has_reconnect_session(fingerprint)
                    )
                )
            )
            if recognition.state is not ReconnectScreenState.DISCONNECTED:
                if is_action_candidate and self._action_is_confirmed(
                    fingerprint,
                    recognition,
                ):
                    confirmed_action_fingerprints.add(fingerprint)
                elif not is_action_candidate:
                    self._clear_action_confirmation(fingerprint)
            if (
                is_action_candidate
                and fingerprint not in confirmed_action_fingerprints
            ):
                pending_confirmation_delays.append(
                    self._policy.progress_interval_seconds
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

        missing_session_targets = (
            self._pending_reconnect_fingerprints
            | self._pending_reopen_fingerprints
            | self._active_automation_fingerprints
        ) - live_fingerprints
        if missing_session_targets:
            failures.append("reconnect_target_missing")

        state_counts = Counter(
            item.state.value
            for _window, _fingerprint, item in recognized
        )
        unknown_windows = state_counts.get(ReconnectScreenState.UNKNOWN.value, 0)
        if captured_windows != len(windows):
            failures.append("capture_failed")
        if unknown_windows:
            failures.append("screen_unknown")
        # Identity and group completeness were already validated before any
        # capture.  A capture or recognition failure is local to that exact
        # window: it must never receive input, but it must not prevent another
        # uniquely identified window with two matching disconnected frames
        # from being recovered.
        if execute:
            self._pending_reconnect_fingerprints.update(
                fingerprint
                for _window, fingerprint, item in recognized
                if item.state is ReconnectScreenState.DISCONNECTED
                and fingerprint in confirmed_action_fingerprints
            )

        actionable_candidates = [
            (window, fingerprint, item)
            for window, fingerprint, item in recognized
            if self._policy.decide(item.state).action
            in ACTIONABLE_RECONNECT_ACTIONS
            and item.click_point is not None
            and fingerprint in confirmed_action_fingerprints
            and (
                item.state is ReconnectScreenState.DISCONNECTED
                or self._has_reconnect_session(fingerprint)
            )
            and not (
                item.state is ReconnectScreenState.DISCONNECTED
                and item.battle_context
            )
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
        actionable: list[tuple[WindowInfo, str, ScreenRecognition]] = []
        for window, fingerprint, item in actionable_candidates:
            wait_seconds = self._action_wait_seconds(
                fingerprint,
                item.state,
                now,
            )
            if wait_seconds:
                pending_action_wait_delays.append(wait_seconds)
            else:
                actionable.append((window, fingerprint, item))
        battle_actionable = [
            (window, fingerprint, item)
            for window, fingerprint, item in recognized
            if item.state is ReconnectScreenState.DISCONNECTED
            and item.battle_context
            and fingerprint in confirmed_action_fingerprints
        ]
        clicked_windows = 0
        restarted_windows = retried_reopens
        battle_restart_attempted = False
        invalid_targets = 0
        unresponsive_targets = 0
        delivery_failures = 0
        if execute:
            for window, fingerprint, item in battle_actionable:
                if not self._execution_allowed():
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
                    now + self._policy.progress_interval_seconds,
                )
                plan = self._group_launch_plan
                target = (
                    plan.target_for_fingerprint(fingerprint)
                    if plan is not None
                    else None
                )
                if self._battle_restarter is None or target is None:
                    self._clear_action_confirmation(fingerprint)
                    failures.append("battle_restart_identity_unresolved")
                    self._report_reconnect_failure(None)
                    continue
                battle_restart_attempted = True
                if not self._execution_allowed():
                    self._clear_action_confirmation(fingerprint)
                    break
                restart_result = self._battle_restarter.restart(
                    window,
                    target,
                )
                if not restart_result.success:
                    failures.append(
                        restart_result.failure_code
                        or "battle_restart_failed"
                    )
                    self._report_reconnect_failure(fingerprint)
                    if restart_result.window_closed:
                        self._pending_reopen_fingerprints.add(fingerprint)
                        self._reopen_retry_after[fingerprint] = (
                            now + self._policy.progress_interval_seconds
                        )
                    continue
                restarted_windows += 1
                self._pending_reopen_fingerprints.add(fingerprint)
                self._reopen_retry_after[fingerprint] = (
                    now + self._policy.progress_interval_seconds
                )
                self._active_automation_fingerprints.add(fingerprint)
                self._active_automation_until[fingerprint] = (
                    now + POST_LOGIN_AUTOMATION_GRACE_SECONDS
                )
            for window, fingerprint, item in actionable:
                if not self._execution_allowed():
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
                    self._clear_action_confirmation(fingerprint)
                    invalid_targets += 1
                    continue
                if not self._mouse_backend.probe_responsive(
                    window.handle,
                    self._preflight_timeout_ms,
                ):
                    self._clear_action_confirmation(fingerprint)
                    unresponsive_targets += 1
                    continue
                if not self._execution_allowed():
                    self._clear_action_confirmation(fingerprint)
                    break
                try:
                    delivered = self._mouse_backend.click_relative(
                        window.handle,
                        item.click_point,
                    )
                except OSError:
                    delivered = False
                if delivered:
                    clicked_windows += 1
                    self._action_confirmations.pop(fingerprint, None)
                    if item.state is ReconnectScreenState.DISCONNECTED:
                        self._flow_pause_until[fingerprint] = (
                            now + self._policy.force_login_wait_seconds
                        )
                    elif item.state is ReconnectScreenState.FORCE_LOGIN_START:
                        self._flow_pause_until[fingerprint] = (
                            now + self._policy.entry_transition_wait_seconds
                        )
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
        if battle_actionable and restarted_windows == 0 and execute:
            next_check_seconds = (
                self._policy.progress_interval_seconds
                if battle_restart_attempted
                else self._policy.retry_interval_seconds
            )
        elif battle_actionable:
            next_check_seconds = self._policy.progress_interval_seconds
        elif actionable:
            next_check_seconds = min(
                self._policy.decide(item.state).delay_seconds
                for _window, _fingerprint, item in (
                    actionable
                )
            )
        elif pending_action_wait_delays:
            next_check_seconds = min(pending_action_wait_delays)
        elif pending_confirmation_delays:
            # A first safe frame is not an unknown failure. Recheck at the
            # state-specific progress interval so the required second frame
            # is confirmed promptly even when other windows are unknown.
            next_check_seconds = min(pending_confirmation_delays)
        elif pending_reopen_delay is not None:
            next_check_seconds = pending_reopen_delay
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
            actionable_windows=len(actionable) + len(battle_actionable),
            clicked_windows=clicked_windows,
            restarted_windows=restarted_windows,
            unknown_windows=unknown_windows,
            execution_requested=execute,
            next_check_seconds=next_check_seconds,
            state_counts=tuple(sorted(state_counts.items())),
            failure_codes=tuple(dict.fromkeys(failures)),
        )
        with self._screen_state_lock:
            self._last_screen_states = {
                fingerprint: item.state
                for _window, fingerprint, item in recognized
            }
        if result.all_connected:
            self._clear_unknown_reconnect_failure()
        self._last_result = result
        if result.all_connected:
            self._state = ReconnectState.CONNECTED
        elif result.progressed:
            self._state = ReconnectState.RECONNECTING
        elif actionable or battle_actionable:
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
            "Reconnect is waiting for a known screen or a selected open target.",
            result.to_dict(),
        )

    def reconnect(self) -> OperationResult:
        lease = (
            self._operation_gate.acquire(
                "smart-reconnect",
                execution_guard=self._execution_allowed,
            )
            if self._operation_gate is not None
            else None
        )
        if self._operation_gate is not None and lease is None:
            return OperationResult(
                False,
                "reconnect.operation_paused",
                "Reconnect execution is paused while targets are rebinding.",
                {
                    "next_check_seconds": 1,
                    "failure_codes": ["operation_gate_closed"],
                },
            )
        try:
            result = self._scan(execute=True)
        finally:
            if lease is not None:
                lease.release()
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
