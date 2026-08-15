"""Safe multi-window smart reconnect for the confirmed Flash login flow."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from ctypes import wintypes
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Protocol

from adapters.game_screen_recognizer import (
    CharacterSelectionCandidate,
    FORCE_LOGIN_CLICK_POINT,
    CHARACTER_ENTER_CLICK_POINT,
    LINE_LIST_SCROLL_POINT,
    LINE_ROUTE_CLICK_POINTS,
    NormalizedPoint,
    ReferenceScreenRecognizer,
    ScreenRecognition,
)
from adapters.windows_background_capture import (
    CaptureSample,
    _WindowInstanceCredential,
    _WINDOWPLACEMENT,
    Win32PrintWindowProvider,
    Win32RecoveringPrintWindowProvider,
    Win32TemporarilyRevealedCaptureProvider,
    Win32VisibleRegionCaptureProvider,
    WindowsGraphicsCaptureProvider,
    WindowCaptureProvider,
)
from adapters.windows_auto_battle import AutoBattleEvidence, AutoBattleRecognizer
from adapters.windows_battle_restart import (
    BattleReopenStage,
    BattleReopenStageEvidence,
    BattleRestartResult,
    Win32WindowCloseBackend,
    WindowsBattleWindowRestarter,
    WindowsShortcutOpenBackend,
)
from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import (
    Win32WindowBackend,
    WindowBackend,
    WindowInfo,
    complete_window_instance_identity,
    monitored_window_instance_fingerprint,
)
from core.reconnect_policy import (
    ReconnectAction,
    ReconnectPolicy,
    ReconnectScreenState,
)
from core.sp1_boundaries import OperationResult, ReconnectState, SmartReconnectBoundary
from domain.character import CharacterImportance, character_importance_rank
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.game_operation_gate import GameOperationGate
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.smart_reconnect_capture_settings_service import (
    SmartReconnectCaptureSettings,
)
from services.smart_reconnect_evidence_store import (
    SmartReconnectEvidenceRecorder,
)
from services.target_window_contract_service import ResolvedTargetWindows


ACTIONABLE_RECONNECT_ACTIONS = frozenset(
    {
        ReconnectAction.CONFIRM_DISCONNECT,
        ReconnectAction.START_GAME,
        ReconnectAction.FORCE_LOGIN,
        ReconnectAction.CONFIRM_FORCE_LOGIN_TIMEOUT,
        ReconnectAction.SELECT_DEFAULT_LINE,
        ReconnectAction.ENTER_GAME,
        ReconnectAction.CLOSE_ANNOUNCEMENT,
    }
)
POST_LOGIN_AUTOMATION_GRACE_SECONDS = 180.0
INITIAL_LOGIN_AUTHORIZATION_SECONDS = 180.0
ACTION_CONFIRMATION_FRAMES = 2
TERMINAL_CONFIRMATION_FRAMES = 3
TERMINAL_CONFIRMATION_SECONDS = 4.0
TRUSTED_CONNECTED_EVIDENCE_MAX_AGE_SECONDS = 10.0
CAPTURE_ROUTE_VISIBLE = "visible"
CAPTURE_ROUTE_OBSCURED = "obscured"
CAPTURE_ROUTE_MINIMIZED = "minimized"
_ROLE_LEVEL_PREFIX = re.compile(r"^\s*(\d{2,3})(?!\d)")
_POST = frozenset({ReconnectScreenState.POST_LOGIN_ACTIVITY,
                   ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
                   ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON})
_SESSION_ONLY_STATES = frozenset(
    {
        ReconnectScreenState.LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
        ReconnectScreenState.LINE_SELECTION,
        ReconnectScreenState.CHARACTER_SELECTION,
    }
) | _POST
_AUTO_BATTLE_GENERAL_STATES = frozenset(
    {
        ReconnectScreenState.CONNECTED,
        ReconnectScreenState.UNKNOWN,
    }
)
AUTO_BATTLE_BATTLE_WINDOW_SECONDS = 24.0
AUTO_BATTLE_RECHECK_SECONDS = 2
RECONNECT_TOTAL_BUDGET_SECONDS = 60.0
START_GAME_BUDGET_SECONDS = 60.0
TIMING_DIAGNOSTIC_LIMIT = 256
_TCP_N = 3
_TCP_T = 7
_ISOLATABLE_TARGET_WINDOW_FAILURE_CODES = frozenset(
    {
        "window_offline",
        "shortcut_identity_unresolved",
        "window_identity_duplicate",
        "window_instance_incomplete",
    }
)


class _TcpR(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "state", "local_address", "local_port", "remote_address", "remote_port", "process_id")]


def _ipv4_established_counts_by_pid(ids: frozenset[int]) -> dict[int, int] | None:
    pids = {pid for pid in ids if pid > 0}
    if not pids:
        return {}
    try:
        q = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
        size = wintypes.ULONG()
        if q(None, ctypes.byref(size), False, 2, 5, 0) not in (0, 122) or not size.value:
            return None
        data = ctypes.create_string_buffer(size.value)
        if q(data, ctypes.byref(size), False, 2, 5, 0):
            return None
        rows = ctypes.cast(
            ctypes.byref(data, ctypes.sizeof(wintypes.DWORD)),
            ctypes.POINTER(_TcpR))
        out = dict.fromkeys(pids, 0)
        count = ctypes.cast(data, ctypes.POINTER(wintypes.DWORD)).contents.value
        for index in range(count):
            row = rows[index]
            if (
                row.process_id in out
                and row.state == 5
                and row.remote_address
                and row.remote_address & 0xFF != 127
            ):
                out[row.process_id] += 1
        return out
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class ScreenRecognizer(Protocol):
    def recognize_capture(self, sample) -> ScreenRecognition:
        """Recognize a capture without changing or persisting it."""


@dataclass(frozen=True, slots=True)
class WindowInstanceToken:
    handle: int
    process_id: int
    thread_id: int
    window_class: str
    rect: tuple[int, int, int, int]
    minimized: bool
    process_lifecycle_token: int

    @classmethod
    def from_window(
        cls,
        window: WindowInfo,
    ) -> "WindowInstanceToken | None":
        if (
            not isinstance(window.handle, int)
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
        return cls(
            window.handle,
            window.process_id,
            window.thread_id,
            window.window_class,
            window.rect,
            window.minimized,
            window.process_lifecycle_token,
        )


@dataclass(frozen=True, slots=True)
class _TrustedConnectedEvidence:
    instance: WindowInstanceToken
    capture_route: str
    capture_settings_revision: int
    observed_at: float


@dataclass(slots=True)
class _TcpState:
    instance: WindowInstanceToken
    entry_id: str | None = None
    online: bool = False
    zero_since: float | None = None
    zero_count: int = 0
    gen: int = 0


@dataclass(frozen=True, slots=True)
class _TcpObservation:
    """Anonymous evidence for the most recent real TCP provider call."""

    generation: int = 0
    observed_at_monotonic: float | None = None
    query_succeeded: bool = False
    observed_window_count: int = 0
    zero_window_count: int = 0
    confirmed_window_count: int = 0

    def items(self) -> tuple[tuple[str, object], ...]:
        return (
            ("generation", self.generation),
            ("observed_at_monotonic", self.observed_at_monotonic),
            ("query_succeeded", self.query_succeeded),
            ("observed_window_count", self.observed_window_count),
            ("zero_window_count", self.zero_window_count),
            ("confirmed_window_count", self.confirmed_window_count),
        )


class _TcpRecoveryStage(Enum):
    TCP_CONFIRMED_OWNER = "tcp_confirmed_owner"
    CLOSE_VERIFIED = "close_verified"
    REOPEN_PENDING = "reopen_pending"
    SHORTCUT_REQUESTED = "shortcut_requested"
    WAITING_NEW_INSTANCE = "waiting_new_instance"
    NEW_INSTANCE_BOUND = "new_instance_bound"
    SCREEN_RECOVERY = "screen_recovery"
    LOGIN = "login"
    LINE = "line"
    ROLE = "role"
    ENTER = "enter"
    CONNECTED = "connected"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(slots=True)
class _TcpRecoveryAuthority:
    """The only mutable authority for one TCP recovery owner."""

    fingerprint: str
    entry_id: str
    stage: _TcpRecoveryStage
    old_instance: WindowInstanceToken
    activation_instance: WindowInstanceToken
    target_fingerprint: str
    shortcut_path: str
    plan_signature: tuple[tuple[object, ...], ...]
    peer_signature: tuple[tuple[object, ...], ...]
    source_state_generation: int
    scope_token: str | None
    deadline: float
    retry_at: float
    shortcut_consumed: bool = False
    new_instance: WindowInstanceToken | None = None
    restored_tombstone: bool = False
    reopen_stage_evidence: tuple[BattleReopenStageEvidence, ...] = ()
    reopen_intent_sequence: int | None = None
    reopen_intent_signature: str | None = None
    reopen_intent_finished: bool = False
    reopen_worker_unreaped: bool = False

    @property
    def terminal(self) -> bool:
        return self.stage in {
            _TcpRecoveryStage.CONNECTED,
            _TcpRecoveryStage.CANCELLED,
            _TcpRecoveryStage.TIMED_OUT,
        }


@dataclass(frozen=True, slots=True)
class RegisteredReconnectRole:
    """One saved game ID used only to resolve a same-level role tie."""

    role_id: str
    importance: CharacterImportance

    def __post_init__(self) -> None:
        role_id = "".join(
            character
            for character in self.role_id.strip()
            if not character.isspace() and ord(character) >= 32
        )
        if not role_id:
            raise ValueError("role_id must not be empty")
        if not isinstance(self.importance, CharacterImportance):
            raise TypeError("importance must be CharacterImportance")
        object.__setattr__(self, "role_id", role_id)


@dataclass(frozen=True, slots=True)
class MouseClickResult:
    delivered: bool
    restored: bool
    delivery_uncertain: bool
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class _MessageAttempt:
    attempted: bool
    confirmed: bool


class MouseMessageBackend(Protocol):
    def is_window(self, handle: int) -> bool:
        """Return whether the supplied top-level window still exists."""

    def probe_responsive(self, handle: int, timeout_ms: int) -> bool:
        """Perform a no-op responsiveness check."""

    def click_relative(
        self,
        handle: int,
        point: NormalizedPoint,
        expected_process_id: int,
        instance_token: WindowInstanceToken,
    ) -> MouseClickResult:
        """Send one client-relative left click to an already validated window."""

    def scroll_relative(
        self,
        handle: int,
        point: NormalizedPoint,
        delta: int,
        expected_process_id: int,
        instance_token: WindowInstanceToken,
    ) -> MouseClickResult:
        """Send one guarded wheel step inside the validated client area."""


class Win32MouseMessageBackend:
    """Pure-ctypes mouse-message delivery that does not move the real cursor."""

    WM_NULL = 0x0000
    WM_MOUSEMOVE = 0x0200
    WM_MOUSEWHEEL = 0x020A
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    MK_LBUTTON = 0x0001
    SMTO_BLOCK = 0x0001
    SMTO_ABORTIFHUNG = 0x0002
    SMTO_ERRORONEXIT = 0x0020
    SW_SHOWNOACTIVATE = 4
    SW_SHOWMINNOACTIVE = 7
    MINIMIZED_PAINT_SETTLE_SECONDS = 0.05
    MESSAGE_TIMEOUT_MS = 1000
    UP_COMPENSATION_ATTEMPTS = 2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    _window_state_lock = (
        Win32RecoveringPrintWindowProvider._window_state_lock
    )

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _kernel32():
        if os.name != "nt":
            return None
        return ctypes.windll.kernel32

    @staticmethod
    def _configure(user32) -> None:
        Win32TemporarilyRevealedCaptureProvider._configure(user32)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        user32.ClientToScreen.restype = wintypes.BOOL
        user32.GetWindowPlacement.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_WINDOWPLACEMENT),
        )
        user32.GetWindowPlacement.restype = wintypes.BOOL
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
        user32.GetClassNameW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        user32.GetClassNameW.restype = ctypes.c_int

    @staticmethod
    def _configure_kernel32(kernel32) -> None:
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
        kernel32.WaitForSingleObject.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
        )
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def _handle_value(handle) -> int:
        return Win32TemporarilyRevealedCaptureProvider._handle_value(
            handle
        )

    @staticmethod
    def _process_lifecycle_from_handle(
        kernel32,
        process_handle,
    ) -> int | None:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                process_handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
        except OSError:
            return None
        return (
            int(created.dwHighDateTime) << 32
        ) | int(created.dwLowDateTime)

    @classmethod
    def _live_process_lifecycle_token(
        cls,
        kernel32,
        process_id: int,
    ) -> int | None:
        """Read one neighbouring process lifecycle without trusting its PID."""
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id <= 0
        ):
            return None
        try:
            process_handle = kernel32.OpenProcess(
                cls.PROCESS_QUERY_LIMITED_INFORMATION | cls.SYNCHRONIZE,
                False,
                process_id,
            )
        except OSError:
            return None
        if not process_handle:
            return None
        try:
            if (
                kernel32.WaitForSingleObject(process_handle, 0)
                != cls.WAIT_TIMEOUT
            ):
                return None
            return cls._process_lifecycle_from_handle(
                kernel32,
                process_handle,
            )
        except OSError:
            return None
        finally:
            try:
                kernel32.CloseHandle(process_handle)
            except OSError:
                pass

    @classmethod
    def _state_instance_credential(
        cls,
        user32,
        kernel32,
        handle: int,
    ) -> _WindowInstanceCredential | None:
        if not handle:
            return None
        return (
            Win32TemporarilyRevealedCaptureProvider
            ._window_instance_credential(
                user32,
                wintypes.HWND(handle),
                lambda process_id: cls._live_process_lifecycle_token(
                    kernel32,
                    process_id,
                ),
            )
        )

    @classmethod
    def _open_expected_process(
        cls,
        kernel32,
        token: WindowInstanceToken,
    ):
        try:
            process_handle = kernel32.OpenProcess(
                cls.PROCESS_QUERY_LIMITED_INFORMATION | cls.SYNCHRONIZE,
                False,
                token.process_id,
            )
        except OSError:
            return None
        if not process_handle:
            return None
        lifecycle = cls._process_lifecycle_from_handle(
            kernel32,
            process_handle,
        )
        if lifecycle != token.process_lifecycle_token:
            try:
                kernel32.CloseHandle(process_handle)
            except OSError:
                pass
            return None
        return process_handle

    @classmethod
    def _static_instance_is_current(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
    ) -> bool:
        try:
            if (
                cls._handle_value(hwnd) != token.handle
                or not user32.IsWindow(hwnd)
                or kernel32.WaitForSingleObject(process_handle, 0)
                != cls.WAIT_TIMEOUT
            ):
                return False
            process_id = wintypes.DWORD()
            thread_id = int(
                user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(process_id),
                )
                or 0
            )
            if (
                int(process_id.value) != token.process_id
                or thread_id != token.thread_id
            ):
                return False
            class_buffer = ctypes.create_unicode_buffer(256)
            class_length = user32.GetClassNameW(
                hwnd,
                class_buffer,
                len(class_buffer),
            )
            return bool(
                class_length > 0
                and class_buffer.value == token.window_class
                and cls._process_lifecycle_from_handle(
                    kernel32,
                    process_handle,
                )
                == token.process_lifecycle_token
            )
        except OSError:
            return False

    @classmethod
    def _instance_is_current(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        expected_rect: tuple[int, int, int, int],
        expected_minimized: bool,
    ) -> bool:
        state = Win32TemporarilyRevealedCaptureProvider
        try:
            return bool(
                cls._static_instance_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                )
                and user32.IsWindowVisible(hwnd)
                and bool(user32.IsIconic(hwnd)) is expected_minimized
                and state._window_rect(user32, hwnd) == expected_rect
            )
        except OSError:
            return False

    @classmethod
    def _send_confirmed(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        message: int,
        wparam: int,
        lparam: int,
    ) -> _MessageAttempt:
        if not cls._static_instance_is_current(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
        ):
            return _MessageAttempt(False, False)
        result = ctypes.c_size_t()
        try:
            delivered = bool(
                user32.SendMessageTimeoutW(
                    hwnd,
                    message,
                    wparam,
                    lparam,
                    (
                        cls.SMTO_BLOCK
                        | cls.SMTO_ABORTIFHUNG
                        | cls.SMTO_ERRORONEXIT
                    ),
                    cls.MESSAGE_TIMEOUT_MS,
                    ctypes.byref(result),
                )
            )
        except OSError:
            return _MessageAttempt(True, False)
        return _MessageAttempt(
            True,
            bool(
                delivered
                and cls._static_instance_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                )
            ),
        )

    @classmethod
    def _release_after_down_attempt(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        lparam: int,
    ) -> bool:
        for _attempt in range(1 + cls.UP_COMPENSATION_ATTEMPTS):
            released = cls._send_confirmed(
                user32,
                kernel32,
                hwnd,
                token=token,
                process_handle=process_handle,
                message=cls.WM_LBUTTONUP,
                wparam=0,
                lparam=lparam,
            )
            if released.confirmed:
                return True
            if not released.attempted:
                break
        return False

    @classmethod
    def _deliver_click_messages(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        lparam: int,
        action_state_is_current: Callable[[], bool],
    ) -> MouseClickResult:
        if not action_state_is_current():
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_changed_before_move",
            )
        moved = cls._send_confirmed(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            message=cls.WM_MOUSEMOVE,
            wparam=0,
            lparam=lparam,
        )
        if not moved.confirmed:
            return MouseClickResult(
                False,
                True,
                False,
                (
                    "input_instance_changed_before_move"
                    if not moved.attempted
                    else "mouse_move_delivery_failed"
                ),
            )
        if not action_state_is_current():
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_changed_before_down",
            )
        pressed = cls._send_confirmed(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            message=cls.WM_LBUTTONDOWN,
            wparam=cls.MK_LBUTTON,
            lparam=lparam,
        )
        if not pressed.attempted:
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_changed_before_down",
            )
        state_after_down_is_current = action_state_is_current()
        released = cls._release_after_down_attempt(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            lparam=lparam,
        )
        if not pressed.confirmed:
            return MouseClickResult(
                False,
                True,
                True,
                "mouse_down_delivery_uncertain",
            )
        if not released:
            return MouseClickResult(
                False,
                True,
                True,
                "mouse_up_delivery_uncertain",
            )
        if (
            not state_after_down_is_current
            or not action_state_is_current()
        ):
            return MouseClickResult(
                False,
                True,
                True,
                "input_window_state_changed_during_click",
            )
        return MouseClickResult(True, True, False, None)

    @classmethod
    def _deliver_wheel_message(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        client_lparam: int,
        screen_lparam: int,
        delta: int,
        action_state_is_current: Callable[[], bool],
    ) -> MouseClickResult:
        if not action_state_is_current():
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_changed_before_move",
            )
        moved = cls._send_confirmed(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            message=cls.WM_MOUSEMOVE,
            wparam=0,
            lparam=client_lparam,
        )
        if not moved.confirmed or not action_state_is_current():
            return MouseClickResult(
                False,
                True,
                False,
                (
                    "input_instance_changed_before_wheel"
                    if not moved.attempted
                    else "mouse_move_delivery_failed"
                ),
            )
        wheeled = cls._send_confirmed(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            message=cls.WM_MOUSEWHEEL,
            wparam=(int(delta) & 0xFFFF) << 16,
            lparam=screen_lparam,
        )
        if not wheeled.attempted:
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_changed_before_wheel",
            )
        if not wheeled.confirmed or not action_state_is_current():
            return MouseClickResult(
                False,
                True,
                True,
                "mouse_wheel_delivery_uncertain",
            )
        return MouseClickResult(True, True, False, None)

    @classmethod
    def _normal_action_state_is_current(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        expected_rect: tuple[int, int, int, int],
    ) -> bool:
        return cls._instance_is_current(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            expected_rect=expected_rect,
            expected_minimized=False,
        )

    @classmethod
    def _restored_action_state_is_current(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        expected_rect: tuple[int, int, int, int],
        placement_signature: tuple[
            int,
            int,
            int,
            int,
            int,
            int,
            int,
            int,
            int,
            int,
        ],
        was_topmost: bool,
        expected_foreground: int,
        expected_foreground_instance: _WindowInstanceCredential | None,
        previous_handle: int,
        next_handle: int,
        previous_instance: _WindowInstanceCredential | None,
        next_instance: _WindowInstanceCredential | None,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        state = Win32TemporarilyRevealedCaptureProvider
        recovery = Win32RecoveringPrintWindowProvider
        try:
            if not cls._static_instance_is_current(
                user32,
                kernel32,
                hwnd,
                token=token,
                process_handle=process_handle,
            ):
                return False
            placement = recovery._window_placement(user32, hwnd)
            foreground_is_current = bool(
                cls._handle_value(user32.GetForegroundWindow())
                == expected_foreground
                and state._optional_instance_is_current(
                    user32,
                    expected_foreground,
                    expected_foreground_instance,
                    lifecycle_provider,
                )
            )
            return bool(
                cls._normal_action_state_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                    expected_rect=expected_rect,
                )
                and placement is not None
                and recovery._placement_signature(placement)
                == placement_signature
                and state._is_topmost(user32, hwnd) is was_topmost
                and foreground_is_current
                and recovery._trusted_minimized_neighbor_restoration(
                    user32,
                    hwnd,
                    previous_handle=previous_handle,
                    next_handle=next_handle,
                    previous_instance=previous_instance,
                    next_instance=next_instance,
                    lifecycle_provider=lifecycle_provider,
                )
            )
        except OSError:
            return False

    @classmethod
    def _restore_original_minimized_state(
        cls,
        user32,
        kernel32,
        hwnd,
        *,
        token: WindowInstanceToken,
        process_handle,
        minimized_rect: tuple[int, int, int, int],
        placement_signature: tuple[
            int,
            int,
            int,
            int,
            int,
            int,
            int,
            int,
            int,
            int,
        ],
        was_topmost: bool,
        original_foreground: int,
        original_foreground_instance: _WindowInstanceCredential | None,
        original_previous_handle: int,
        original_next_handle: int,
        original_previous_instance: _WindowInstanceCredential | None,
        original_next_instance: _WindowInstanceCredential | None,
        transient_rect: tuple[int, int, int, int],
        transient_previous_handle: int,
        transient_next_handle: int,
        transient_previous_instance: _WindowInstanceCredential | None,
        transient_next_instance: _WindowInstanceCredential | None,
        target_state_instance: _WindowInstanceCredential,
        lifecycle_provider: Callable[[int], int | None],
    ) -> bool:
        state = Win32TemporarilyRevealedCaptureProvider
        recovery = Win32RecoveringPrintWindowProvider
        # A foreground, position, layer, or manual state change means the user
        # interacted while the target was temporarily restored. Do not
        # overwrite that new state merely to recreate the old snapshot.
        if not cls._restored_action_state_is_current(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            expected_rect=transient_rect,
            placement_signature=placement_signature,
            was_topmost=was_topmost,
            expected_foreground=original_foreground,
            expected_foreground_instance=original_foreground_instance,
            previous_handle=transient_previous_handle,
            next_handle=transient_next_handle,
            previous_instance=transient_previous_instance,
            next_instance=transient_next_instance,
            lifecycle_provider=lifecycle_provider,
        ):
            return False
        if not cls._static_instance_is_current(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
        ):
            return False
        try:
            user32.ShowWindow(hwnd, cls.SW_SHOWMINNOACTIVE)
        except OSError:
            return False
        if not cls._static_instance_is_current(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
        ):
            return False
        if not recovery._minimized_window_was_restored(
            user32,
            hwnd,
            process_id=token.process_id,
            minimized_rect=minimized_rect,
            placement_signature=placement_signature,
            was_topmost=was_topmost,
            expected_instance=target_state_instance,
            lifecycle_provider=lifecycle_provider,
        ):
            return False
        if not recovery._both_neighbor_relations_are_restored(
            user32,
            hwnd,
            previous_handle=original_previous_handle,
            next_handle=original_next_handle,
            previous_instance=original_previous_instance,
            next_instance=original_next_instance,
            lifecycle_provider=lifecycle_provider,
        ):
            try:
                restore_plan = (
                    state._restore_insert_after_for_instances(
                        user32,
                        hwnd,
                        previous_handle=original_previous_handle,
                        next_handle=original_next_handle,
                        previous_instance=original_previous_instance,
                        next_instance=original_next_instance,
                        was_topmost=was_topmost,
                        lifecycle_provider=lifecycle_provider,
                    )
                )
                if restore_plan is None:
                    return False
                insert_after, _verify_relation, anchor_instance = (
                    restore_plan
                )
                if not state._set_window_position_if_instances_current(
                    user32,
                    hwnd,
                    insert_after=insert_after,
                    target_instance=target_state_instance,
                    previous_handle=original_previous_handle,
                    next_handle=original_next_handle,
                    previous_instance=original_previous_instance,
                    next_instance=original_next_instance,
                    anchor_instance=anchor_instance,
                    lifecycle_provider=lifecycle_provider,
                ):
                    return False
                if not cls._static_instance_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                ):
                    return False
            except OSError:
                return False
        try:
            return bool(
                cls._static_instance_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                )
                and recovery._minimized_window_was_restored(
                    user32,
                    hwnd,
                    process_id=token.process_id,
                    minimized_rect=minimized_rect,
                    placement_signature=placement_signature,
                    was_topmost=was_topmost,
                    expected_instance=target_state_instance,
                    lifecycle_provider=lifecycle_provider,
                )
                and recovery._trusted_minimized_neighbor_restoration(
                    user32,
                    hwnd,
                    previous_handle=original_previous_handle,
                    next_handle=original_next_handle,
                    previous_instance=original_previous_instance,
                    next_instance=original_next_instance,
                    lifecycle_provider=lifecycle_provider,
                )
                and state._foreground_was_preserved(
                    user32,
                    hwnd,
                    original_foreground,
                    original_instance=original_foreground_instance,
                    lifecycle_provider=lifecycle_provider,
                )
            )
        except OSError:
            return False

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

    def click_relative(
        self,
        handle: int,
        point: NormalizedPoint,
        expected_process_id: int,
        instance_token: WindowInstanceToken,
    ) -> MouseClickResult:
        user32 = self._user32()
        kernel32 = self._kernel32()
        if (
            user32 is None
            or kernel32 is None
            or not isinstance(expected_process_id, int)
            or isinstance(expected_process_id, bool)
            or expected_process_id <= 0
            or not isinstance(instance_token, WindowInstanceToken)
            or instance_token.handle != handle
            or instance_token.process_id != expected_process_id
        ):
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_token_invalid",
            )
        self._configure(user32)
        self._configure_kernel32(kernel32)
        hwnd = wintypes.HWND(handle)
        process_handle = self._open_expected_process(
            kernel32,
            instance_token,
        )
        if not process_handle:
            return MouseClickResult(
                False,
                True,
                False,
                "input_process_lifecycle_mismatch",
            )
        try:
            with self._window_state_lock:
                return self._click_relative_locked(
                    user32,
                    kernel32,
                    hwnd,
                    point=point,
                    token=instance_token,
                    process_handle=process_handle,
                    wheel_delta=None,
                )
        finally:
            try:
                kernel32.CloseHandle(process_handle)
            except OSError:
                pass

    def scroll_relative(
        self,
        handle: int,
        point: NormalizedPoint,
        delta: int,
        expected_process_id: int,
        instance_token: WindowInstanceToken,
    ) -> MouseClickResult:
        if (
            not isinstance(delta, int)
            or isinstance(delta, bool)
            or delta == 0
            or abs(delta) > 120
        ):
            return MouseClickResult(
                False,
                True,
                False,
                "input_wheel_delta_invalid",
            )
        user32 = self._user32()
        kernel32 = self._kernel32()
        if (
            user32 is None
            or kernel32 is None
            or not isinstance(expected_process_id, int)
            or isinstance(expected_process_id, bool)
            or expected_process_id <= 0
            or not isinstance(instance_token, WindowInstanceToken)
            or instance_token.handle != handle
            or instance_token.process_id != expected_process_id
        ):
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_token_invalid",
            )
        self._configure(user32)
        self._configure_kernel32(kernel32)
        hwnd = wintypes.HWND(handle)
        process_handle = self._open_expected_process(
            kernel32,
            instance_token,
        )
        if not process_handle:
            return MouseClickResult(
                False,
                True,
                False,
                "input_process_lifecycle_mismatch",
            )
        try:
            with self._window_state_lock:
                return self._click_relative_locked(
                    user32,
                    kernel32,
                    hwnd,
                    point=point,
                    token=instance_token,
                    process_handle=process_handle,
                    wheel_delta=delta,
                )
        finally:
            try:
                kernel32.CloseHandle(process_handle)
            except OSError:
                pass

    def _click_relative_locked(
        self,
        user32,
        kernel32,
        hwnd,
        *,
        point: NormalizedPoint,
        token: WindowInstanceToken,
        process_handle,
        wheel_delta: int | None,
    ) -> MouseClickResult:
        state = Win32TemporarilyRevealedCaptureProvider
        recovery = Win32RecoveringPrintWindowProvider
        if not self._instance_is_current(
            user32,
            kernel32,
            hwnd,
            token=token,
            process_handle=process_handle,
            expected_rect=token.rect,
            expected_minimized=token.minimized,
        ):
            return MouseClickResult(
                False,
                True,
                False,
                "input_instance_token_mismatch",
            )
        was_minimized = token.minimized
        original_target_rect = token.rect

        original_placement_signature = None
        was_topmost = False
        original_foreground = 0
        original_foreground_instance = None
        original_previous_handle = 0
        original_next_handle = 0
        original_previous_instance = None
        original_next_instance = None
        transient_rect = original_target_rect
        transient_previous_handle = 0
        transient_next_handle = 0
        transient_previous_instance = None
        transient_next_instance = None
        target_state_instance = None
        temporarily_restored = False
        restoration_succeeded = True

        lifecycle_provider = lambda process_id: (
            self._live_process_lifecycle_token(
                kernel32,
                process_id,
            )
        )

        def perform_delivery() -> MouseClickResult:
            nonlocal original_placement_signature
            nonlocal was_topmost
            nonlocal original_foreground
            nonlocal original_foreground_instance
            nonlocal original_previous_handle
            nonlocal original_next_handle
            nonlocal original_previous_instance
            nonlocal original_next_instance
            nonlocal transient_rect
            nonlocal transient_previous_handle
            nonlocal transient_next_handle
            nonlocal transient_previous_instance
            nonlocal transient_next_instance
            nonlocal target_state_instance
            nonlocal temporarily_restored
            try:
                if was_minimized:
                    original_placement = recovery._window_placement(
                        user32,
                        hwnd,
                    )
                    if original_placement is None:
                        return MouseClickResult(
                            False,
                            True,
                            False,
                            "input_window_placement_unavailable",
                        )
                    original_placement_signature = (
                        recovery._placement_signature(
                            original_placement
                        )
                    )
                    was_topmost = state._is_topmost(user32, hwnd)
                    original_foreground = self._handle_value(
                        user32.GetForegroundWindow()
                    )
                    original_previous_handle = self._handle_value(
                        user32.GetWindow(hwnd, state.GW_HWNDPREV)
                    )
                    original_next_handle = self._handle_value(
                        user32.GetWindow(hwnd, state.GW_HWNDNEXT)
                    )
                    target_state_instance = self._state_instance_credential(
                        user32,
                        kernel32,
                        token.handle,
                    )
                    original_foreground_instance = (
                        self._state_instance_credential(
                            user32,
                            kernel32,
                            original_foreground,
                        )
                        if original_foreground
                        else None
                    )
                    original_previous_instance = (
                        self._state_instance_credential(
                            user32,
                            kernel32,
                            original_previous_handle,
                        )
                        if original_previous_handle
                        else None
                    )
                    original_next_instance = (
                        self._state_instance_credential(
                            user32,
                            kernel32,
                            original_next_handle,
                        )
                        if original_next_handle
                        else None
                    )
                    if (
                        target_state_instance is None
                        or target_state_instance.handle != token.handle
                        or target_state_instance.process_id
                        != token.process_id
                        or target_state_instance.thread_id
                        != token.thread_id
                        or target_state_instance.window_class
                        != token.window_class
                        or target_state_instance.process_lifecycle_token
                        != token.process_lifecycle_token
                        or (
                            original_foreground
                            and original_foreground_instance is None
                        )
                        or (
                            original_previous_handle
                            and original_previous_instance is None
                        )
                        or (
                            original_next_handle
                            and original_next_instance is None
                        )
                        or not state._reference_instances_are_current(
                            user32,
                            previous_handle=original_previous_handle,
                            next_handle=original_next_handle,
                            previous_instance=original_previous_instance,
                            next_instance=original_next_instance,
                            lifecycle_provider=lifecycle_provider,
                        )
                        or not state._optional_instance_is_current(
                            user32,
                            original_foreground,
                            original_foreground_instance,
                            lifecycle_provider,
                        )
                    ):
                        return MouseClickResult(
                            False,
                            True,
                            False,
                            "input_restore_reference_incomplete",
                        )
                    if not self._instance_is_current(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                        expected_rect=original_target_rect,
                        expected_minimized=True,
                    ):
                        return MouseClickResult(
                            False,
                            True,
                            False,
                            "input_instance_changed_before_restore",
                        )
                    user32.ShowWindow(
                        hwnd,
                        self.SW_SHOWNOACTIVATE,
                    )
                    temporarily_restored = not bool(
                        user32.IsIconic(hwnd)
                    )
                    if not temporarily_restored:
                        return MouseClickResult(
                            False,
                            True,
                            False,
                            "input_window_restore_failed",
                        )
                    if not self._static_instance_is_current(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                    ):
                        return MouseClickResult(
                            False,
                            False,
                            False,
                            "input_instance_changed_after_restore",
                        )
                    transient_rect = state._window_rect(user32, hwnd)
                    if transient_rect is None:
                        return MouseClickResult(
                            False,
                            False,
                            False,
                            "input_window_rect_unavailable",
                        )
                    transient_previous_handle = self._handle_value(
                        user32.GetWindow(hwnd, state.GW_HWNDPREV)
                    )
                    transient_next_handle = self._handle_value(
                        user32.GetWindow(hwnd, state.GW_HWNDNEXT)
                    )
                    transient_previous_instance = (
                        self._state_instance_credential(
                            user32,
                            kernel32,
                            transient_previous_handle,
                        )
                        if transient_previous_handle
                        else None
                    )
                    transient_next_instance = (
                        self._state_instance_credential(
                            user32,
                            kernel32,
                            transient_next_handle,
                        )
                        if transient_next_handle
                        else None
                    )
                    if (
                        (
                            transient_previous_handle
                            and transient_previous_instance is None
                        )
                        or (
                            transient_next_handle
                            and transient_next_instance is None
                        )
                    ):
                        return MouseClickResult(
                            False,
                            False,
                            False,
                            "input_restore_reference_incomplete",
                        )
                    if not self._restored_action_state_is_current(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                        expected_rect=transient_rect,
                        placement_signature=(
                            original_placement_signature
                        ),
                        was_topmost=was_topmost,
                        expected_foreground=original_foreground,
                        expected_foreground_instance=(
                            original_foreground_instance
                        ),
                        previous_handle=transient_previous_handle,
                        next_handle=transient_next_handle,
                        previous_instance=transient_previous_instance,
                        next_instance=transient_next_instance,
                        lifecycle_provider=lifecycle_provider,
                    ):
                        return MouseClickResult(
                            False,
                            False,
                            False,
                            "input_window_restore_race",
                        )
                    if self.MINIMIZED_PAINT_SETTLE_SECONDS:
                        time.sleep(
                            self.MINIMIZED_PAINT_SETTLE_SECONDS
                        )

                def action_state_is_current() -> bool:
                    if was_minimized:
                        if original_placement_signature is None:
                            return False
                        return self._restored_action_state_is_current(
                            user32,
                            kernel32,
                            hwnd,
                            token=token,
                            process_handle=process_handle,
                            expected_rect=transient_rect,
                            placement_signature=(
                                original_placement_signature
                            ),
                            was_topmost=was_topmost,
                            expected_foreground=original_foreground,
                            expected_foreground_instance=(
                                original_foreground_instance
                            ),
                            previous_handle=transient_previous_handle,
                            next_handle=transient_next_handle,
                            previous_instance=(
                                transient_previous_instance
                            ),
                            next_instance=transient_next_instance,
                            lifecycle_provider=lifecycle_provider,
                        )
                    return self._normal_action_state_is_current(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                        expected_rect=original_target_rect,
                    )

                if not action_state_is_current():
                    return MouseClickResult(
                        False,
                        not temporarily_restored,
                        False,
                        "input_instance_changed_before_coordinates",
                    )
                if not self._static_instance_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                ):
                    return MouseClickResult(
                        False,
                        not temporarily_restored,
                        False,
                        "input_instance_changed_before_coordinates",
                    )
                rect = wintypes.RECT()
                if not user32.GetClientRect(
                    hwnd,
                    ctypes.byref(rect),
                ):
                    return MouseClickResult(
                        False,
                        not temporarily_restored,
                        False,
                        "input_client_rect_unavailable",
                    )
                if (
                    not self._static_instance_is_current(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                    )
                    or not action_state_is_current()
                ):
                    return MouseClickResult(
                        False,
                        not temporarily_restored,
                        False,
                        "input_instance_changed_after_coordinates",
                    )
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width <= 1 or height <= 1:
                    return MouseClickResult(
                        False,
                        not temporarily_restored,
                        False,
                        "input_client_rect_invalid",
                    )
                relative_x, relative_y = point
                if not (
                    0.0 <= relative_x <= 1.0
                    and 0.0 <= relative_y <= 1.0
                ):
                    return MouseClickResult(
                        False,
                        not temporarily_restored,
                        False,
                        "input_click_point_invalid",
                    )
                x = max(
                    0,
                    min(width - 1, round((width - 1) * relative_x)),
                )
                y = max(
                    0,
                    min(height - 1, round((height - 1) * relative_y)),
                )
                lparam = (y << 16) | (x & 0xFFFF)
                if wheel_delta is not None:
                    screen_point = wintypes.POINT(x, y)
                    if (
                        not user32.ClientToScreen(
                            hwnd,
                            ctypes.byref(screen_point),
                        )
                        or not action_state_is_current()
                    ):
                        return MouseClickResult(
                            False,
                            not temporarily_restored,
                            False,
                            "input_wheel_coordinates_unavailable",
                        )
                    screen_lparam = (
                        ((int(screen_point.y) & 0xFFFF) << 16)
                        | (int(screen_point.x) & 0xFFFF)
                    )
                    return self._deliver_wheel_message(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                        client_lparam=lparam,
                        screen_lparam=screen_lparam,
                        delta=wheel_delta,
                        action_state_is_current=action_state_is_current,
                    )
                return self._deliver_click_messages(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                    lparam=lparam,
                    action_state_is_current=action_state_is_current,
                )
            except OSError:
                return MouseClickResult(
                    False,
                    not temporarily_restored,
                    False,
                    "input_backend_os_error",
                )

        try:
            delivery_result = perform_delivery()
        finally:
            if (
                temporarily_restored
                and original_placement_signature is not None
                and target_state_instance is not None
            ):
                restoration_succeeded = (
                    self._restore_original_minimized_state(
                        user32,
                        kernel32,
                        hwnd,
                        token=token,
                        process_handle=process_handle,
                        minimized_rect=original_target_rect,
                        placement_signature=original_placement_signature,
                        was_topmost=was_topmost,
                        original_foreground=original_foreground,
                        original_foreground_instance=(
                            original_foreground_instance
                        ),
                        original_previous_handle=original_previous_handle,
                        original_next_handle=original_next_handle,
                        original_previous_instance=(
                            original_previous_instance
                        ),
                        original_next_instance=original_next_instance,
                        transient_rect=transient_rect,
                        transient_previous_handle=(
                            transient_previous_handle
                        ),
                        transient_next_handle=transient_next_handle,
                        transient_previous_instance=(
                            transient_previous_instance
                        ),
                        transient_next_instance=transient_next_instance,
                        target_state_instance=target_state_instance,
                        lifecycle_provider=lifecycle_provider,
                    )
                )
        failure_code = delivery_result.failure_code
        if (
            delivery_result.delivered
            and not restoration_succeeded
        ):
            failure_code = "input_window_restore_failed"
        elif failure_code is None and not restoration_succeeded:
            failure_code = "input_window_restore_failed"
        return MouseClickResult(
            delivery_result.delivered,
            restoration_succeeded,
            delivery_result.delivery_uncertain,
            failure_code,
        )


@dataclass(frozen=True, slots=True)
class CaptureDiagnostic:
    """Anonymous, pixel-free evidence for one guarded capture attempt."""

    window_index: int
    stage: str
    capture_path: str
    width: int | None
    height: int | None
    sha256: str | None
    recognition_score: float | None
    rejection_gate: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "window_index": self.window_index,
            "stage": self.stage,
            "capture_path": self.capture_path,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "recognition_score": self.recognition_score,
            "rejection_gate": self.rejection_gate,
        }


@dataclass(frozen=True, slots=True)
class ReconnectTimingDiagnostic:
    """Pixel-free timing evidence for one anonymous recovery window."""

    recorded_at: float
    window_id: str
    lifecycle: str
    stage: str
    stage_seconds: float
    total_seconds: float
    cycle: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "recorded_at": self.recorded_at,
            "window_id": self.window_id,
            "lifecycle": self.lifecycle,
            "stage": self.stage,
            "stage_seconds": self.stage_seconds,
            "total_seconds": self.total_seconds,
            "cycle": self.cycle,
            "status": self.status,
        }


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
    source_missing_windows: int
    execution_requested: bool
    next_check_seconds: int
    state_counts: tuple[tuple[str, int], ...]
    failure_codes: tuple[str, ...]
    capture_diagnostics: tuple[CaptureDiagnostic, ...] = ()
    timing_diagnostics: tuple[ReconnectTimingDiagnostic, ...] = ()
    tcp_observation: tuple[tuple[str, object], ...] = ()

    @property
    def all_connected(self) -> bool:
        return (
            self.discovered_windows > 0
            and self.validated_windows == self.discovered_windows
            and self.connected_windows == self.discovered_windows
            and self.actionable_windows == 0
            and self.clicked_windows == 0
            and self.restarted_windows == 0
            and self.unknown_windows == 0
            and self.source_missing_windows == 0
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
            "source_missing_windows": self.source_missing_windows,
            "execution_requested": self.execution_requested,
            "next_check_seconds": self.next_check_seconds,
            "state_counts": dict(self.state_counts),
            "failure_codes": list(self.failure_codes),
            "capture_diagnostics": [
                item.to_dict() for item in self.capture_diagnostics
            ],
            "timing_diagnostics": [
                item.to_dict() for item in self.timing_diagnostics
            ],
            "tcp_observation": dict(self.tcp_observation),
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
    terminal_ready_after: dict[str, float]
    flow_pause_until: dict[str, float]
    scope_token: str | None
    preferred_line_numbers: dict[str, int]
    tcp_recovery_authority: dict[str, object] | None = None


@dataclass(slots=True)
class _TerminalEvidence:
    instance: WindowInstanceToken
    width: int
    height: int
    first_seen: float
    last_digest: bytes
    changing_frames: int


@dataclass(frozen=True, slots=True)
class _ActionConfirmation:
    instance: WindowInstanceToken
    capture_route: str
    capture_settings_revision: int
    source_state_generation: int
    signature: tuple[object, ...]
    consecutive_frames: int


@dataclass(frozen=True, slots=True)
class _BattleRestartEvent:
    handle: int
    process_id: int
    thread_id: int
    window_class: str
    process_lifecycle_token: int

    @classmethod
    def from_instance(
        cls,
        instance: WindowInstanceToken,
    ) -> "_BattleRestartEvent":
        """Identify one live window session without mutable capture evidence."""

        return cls(
            handle=instance.handle,
            process_id=instance.process_id,
            thread_id=instance.thread_id,
            window_class=instance.window_class,
            process_lifecycle_token=instance.process_lifecycle_token,
        )


@dataclass(frozen=True, slots=True)
class _InitialLoginAuthorization:
    """One activation-scoped grant for an already-open game window."""

    instance: WindowInstanceToken
    capture_settings_revision: int
    source_state_generation: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class _AutoBattleButtonWindow:
    instance: WindowInstanceToken
    capture_route: str
    capture_settings_revision: int
    source_state_generation: int
    started_at: float
    attempted: bool = False


@dataclass(slots=True)
class _ReconnectTimingFlow:
    lifecycle: str
    started_at: float
    stage: str
    stage_started_at: float
    cycle: int = 1


class ReconnectRuntimeStateStore:
    """Persist only anonymous fingerprints and reconnect timing state."""

    VERSION = 7
    LEGACY_VERSIONS = frozenset({1, 2, 3, 4, 5})
    MIGRATABLE_VERSIONS = frozenset({6})

    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovered_from_corruption = False
        self.corrupt_backup: Path | None = None

    @staticmethod
    def _empty() -> ReconnectRuntimeState:
        return ReconnectRuntimeState(
            set(),
            set(),
            {},
            {},
            set(),
            {},
            {},
            {},
            None,
            {},
        )

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
                not in self.LEGACY_VERSIONS
                | self.MIGRATABLE_VERSIONS
                | {self.VERSION}
            ):
                raise ValueError("Unsupported reconnect state version")
            # Versions 1-3 can contain reconnect authorization created before
            # the current two-frame disconnect gate. Versions 4-5 do not
            # persist the selected-group scope or the mandatory cross-screen
            # pauses. Never carry incomplete authorization into a newer
            # executable: migrate every legacy payload to a clean state.
            source_version = int(payload["version"])
            if source_version in self.LEGACY_VERSIONS:
                empty = self._empty()
                self.save(empty)
                return empty
            raw_scope_token = payload.get("scope_token")
            if raw_scope_token is None:
                scope_token = None
            else:
                scope_token = normalize_launch_fingerprint(raw_scope_token)
                if scope_token is None:
                    raise ValueError("Invalid reconnect scope token")
            pending = self._fingerprints(payload.get("pending_fingerprints", []))
            active = self._fingerprints(payload.get("active_fingerprints", []))
            raw_active_until = payload.get("active_until", {})
            if not isinstance(raw_active_until, dict):
                raise ValueError("active_until must be an object")
            active_until: dict[str, float] = {}
            for raw_fingerprint, raw_deadline in raw_active_until.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                deadline = float(raw_deadline)
                if (
                    fingerprint is None
                    or not math.isfinite(deadline)
                    or deadline < 0
                ):
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
                if not math.isfinite(retry_at) or retry_at < 0:
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
                if (
                    fingerprint is None
                    or not math.isfinite(retry_at)
                    or retry_at < 0
                ):
                    raise ValueError("Invalid reopen retry entry")
                pending_reopens.add(fingerprint)
                reopen_retries[fingerprint] = retry_at
            raw_terminal_ready = payload.get("terminal_ready_after", {})
            if not isinstance(raw_terminal_ready, dict):
                raise ValueError("terminal_ready_after must be an object")
            terminal_ready: dict[str, float] = {}
            for raw_fingerprint, raw_ready_at in raw_terminal_ready.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                ready_at = float(raw_ready_at)
                if (
                    fingerprint is None
                    or not math.isfinite(ready_at)
                    or ready_at < 0
                ):
                    raise ValueError("Invalid terminal ready entry")
                terminal_ready[fingerprint] = ready_at
            raw_flow_pauses = payload.get("flow_pause_until", {})
            if not isinstance(raw_flow_pauses, dict):
                raise ValueError("flow_pause_until must be an object")
            flow_pauses: dict[str, float] = {}
            for raw_fingerprint, raw_pause_until in raw_flow_pauses.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                pause_until = float(raw_pause_until)
                if (
                    fingerprint is None
                    or not math.isfinite(pause_until)
                    or pause_until < 0
                ):
                    raise ValueError("Invalid reconnect flow pause")
                flow_pauses[fingerprint] = pause_until
            raw_preferred_lines = payload.get("preferred_line_numbers", {})
            if not isinstance(raw_preferred_lines, dict):
                raise ValueError("preferred_line_numbers must be an object")
            preferred_line_numbers: dict[str, int] = {}
            for raw_fingerprint, raw_line_number in raw_preferred_lines.items():
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                if (
                    fingerprint is None
                    or isinstance(raw_line_number, bool)
                    or not isinstance(raw_line_number, int)
                    or raw_line_number not in LINE_ROUTE_CLICK_POINTS
                ):
                    raise ValueError("Invalid preferred reconnect line")
                preferred_line_numbers[fingerprint] = raw_line_number
            raw_tcp_authority = payload.get("tcp_recovery_authority")
            if raw_tcp_authority is not None:
                if (
                    source_version != self.VERSION
                    or not isinstance(raw_tcp_authority, dict)
                    or not isinstance(raw_tcp_authority.get("stage"), str)
                    or normalize_launch_fingerprint(
                        raw_tcp_authority.get("fingerprint")
                    )
                    is None
                    or type(
                        raw_tcp_authority.get("shortcut_consumed")
                    ) is not bool
                ):
                    raise ValueError("Invalid persisted TCP recovery authority")
                tcp_recovery_authority = dict(raw_tcp_authority)
            else:
                tcp_recovery_authority = None
            state = ReconnectRuntimeState(
                pending,
                active,
                active_until,
                retries,
                pending_reopens,
                reopen_retries,
                terminal_ready,
                flow_pauses,
                scope_token,
                preferred_line_numbers,
                tcp_recovery_authority,
            )
            if scope_token is None and (
                state.pending_fingerprints
                or state.active_fingerprints
                or state.pending_reopen_fingerprints
                or state.retry_after
                or state.reopen_retry_after
                or state.terminal_ready_after
                or state.flow_pause_until
            ):
                empty = self._empty()
                empty.preferred_line_numbers = (
                    state.preferred_line_numbers
                )
                self.save(empty)
                return empty
            return state
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
            "scope_token": state.scope_token,
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
            "terminal_ready_after": {
                fingerprint: state.terminal_ready_after[fingerprint]
                for fingerprint in sorted(state.terminal_ready_after)
            },
            "flow_pause_until": {
                fingerprint: state.flow_pause_until[fingerprint]
                for fingerprint in sorted(state.flow_pause_until)
            },
            "preferred_line_numbers": {
                fingerprint: state.preferred_line_numbers[fingerprint]
                for fingerprint in sorted(state.preferred_line_numbers)
            },
            "tcp_recovery_authority": state.tcp_recovery_authority,
        }
        temp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
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
        visible_capture_provider: WindowCaptureProvider | None = None,
        obscured_capture_provider: WindowCaptureProvider | None = None,
        active_refresh_capture_provider: WindowCaptureProvider | None = None,
        capture_access_preparer: Callable[[], bool] | None = None,
        primary_capture_is_trusted: bool = False,
        primary_capture_is_fresh_without_visibility: bool = False,
        capture_settings: SmartReconnectCaptureSettings | None = None,
        policy: ReconnectPolicy | None = None,
        preflight_timeout_ms: int = 1000,
        monotonic_clock: Callable[[], float] = time.time,
        state_path: Path | None = None,
        execution_enabled: bool = False,
        require_expected_window_count: bool = True,
        allowed_fingerprints: Iterable[str] | None = None,
        battle_restarter: WindowsBattleWindowRestarter | None = None,
        failure_status_service: ReconnectFailureStatusService | None = None,
        failure_record_callback: (
            Callable[[str, str], object] | None
        ) = None,
        target_windows_provider: (
            Callable[[], Iterable[WindowInfo] | ResolvedTargetWindows] | None
        ) = None,
        registered_role_provider: (
            Callable[[], Iterable[RegisteredReconnectRole]] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
        auto_battle_enabled: bool = True,
        auto_battle_recognizer: AutoBattleRecognizer | None = None,
        evidence_recorder: SmartReconnectEvidenceRecorder | None = None,
        evidence_required: bool = False,
        evidence_initialization_failed: bool = False,
        tcp_connection_count_provider: (
            Callable[[frozenset[int]], dict[int, int] | None] | None
        ) = None,
    ):
        if expected_windows <= 0:
            raise ValueError("expected_windows must be positive")
        self._expected_windows = expected_windows
        self._require_expected_window_count = bool(
            require_expected_window_count
        )
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._keywords:
            raise ValueError("At least one title keyword is required")
        self._window_backend = window_backend
        self._capture_provider = capture_provider
        self._visible_capture_provider = visible_capture_provider
        self._obscured_capture_provider = obscured_capture_provider
        self._active_refresh_capture_provider = active_refresh_capture_provider
        self._capture_access_preparer = capture_access_preparer
        self._primary_capture_is_trusted = bool(primary_capture_is_trusted)
        self._primary_capture_is_fresh_without_visibility = bool(
            primary_capture_is_fresh_without_visibility
        )
        self._capture_settings_lock = threading.RLock()
        self._capture_settings = (
            capture_settings
            if isinstance(capture_settings, SmartReconnectCaptureSettings)
            else SmartReconnectCaptureSettings()
        )
        self._capture_settings_revision = 0
        self._capture_diagnostic_lock = threading.RLock()
        self._capture_diagnostic_window_indices: dict[str, int] = {}
        self._capture_diagnostic_windows: tuple[WindowInfo, ...] = ()
        self._capture_diagnostic_foreground_handle: int | None = None
        self._capture_diagnostics: tuple[CaptureDiagnostic, ...] = ()
        self._evidence_recorder = evidence_recorder
        self._evidence_required = bool(evidence_required)
        self._evidence_recording_failed = bool(
            evidence_initialization_failed
        )
        self._recognizer = recognizer
        self._mouse_backend = mouse_backend
        self._policy = policy or ReconnectPolicy()
        self._preflight_timeout_ms = max(1, int(preflight_timeout_ms))
        self._monotonic_clock = monotonic_clock
        self._state = ReconnectState.DISCONNECTED
        self._last_result: ReconnectBatchResult | None = None
        self._execution_enabled = threading.Event()
        if execution_enabled:
            if self._record_evidence_monitoring_state(True):
                self._execution_enabled.set()
        # Source revocation must serialize with the actual backend delivery.
        # It intentionally has its own lock so the final delivery boundary
        # never nests the screen-state and capture-settings locks in opposite
        # directions.
        self._source_authority_lock = threading.RLock()
        self._screen_state_lock = threading.RLock()
        self._scan_lock = threading.RLock()
        self._runtime_persist_lock = threading.RLock()
        self._runtime_state_store = (
            ReconnectRuntimeStateStore(state_path)
            if state_path is not None
            else None
        )
        runtime_state = (
            self._runtime_state_store.load()
            if self._runtime_state_store is not None
            else ReconnectRuntimeState(
                set(),
                set(),
                {},
                {},
                set(),
                {},
                {},
                {},
                None,
                {},
            )
        )
        self._runtime_scope_token = runtime_state.scope_token
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
        self._terminal_ready_after = runtime_state.terminal_ready_after
        self._terminal_evidence: dict[str, _TerminalEvidence] = {}
        self._character_selection_pending: set[str] = set()
        self._character_selection_targets: dict[
            str,
            CharacterSelectionCandidate,
        ] = {}
        self._action_state_since: dict[
            str,
            tuple[WindowInstanceToken, ReconnectScreenState, float],
        ] = {}
        self._flow_pause_until = runtime_state.flow_pause_until
        self._preferred_line_numbers = runtime_state.preferred_line_numbers
        # Recent-login role text is corroborating evidence only and remains
        # memory-only; it is never written to anonymous runtime state.
        self._recent_login_role_ids: dict[str, str] = {}
        self._primary_entry_authorized: set[str] = set()
        self._primary_connected_fingerprints: set[str] = set()
        self._reconnect_entry_authorized: set[str] = set()
        self._reconnect_timing_flows: dict[
            tuple[str, str],
            _ReconnectTimingFlow,
        ] = {}
        self._reconnect_timing_diagnostics: list[
            ReconnectTimingDiagnostic
        ] = []
        self._allowed_fingerprints: frozenset[str] | None = None
        self.set_allowed_fingerprints(allowed_fingerprints)
        self._battle_restarter = battle_restarter
        self._group_launch_plan: GroupLaunchPlan | None = None
        self._activation_snapshot_instances: (
            dict[str, WindowInstanceToken] | None
        ) = None
        self._activation_snapshot_source_fingerprints: (
            dict[str, str] | None
        ) = None
        self._activation_snapshot_instance_index: (
            dict[tuple[str, int, int, int, str, int], str] | None
        ) = None
        self._activation_snapshot_direct_identity_collisions: (
            frozenset[str]
        ) = frozenset()
        self._detection_only_fingerprints: frozenset[str] = frozenset()
        self._initial_login_authorizations: dict[
            str,
            _InitialLoginAuthorization,
        ] = {}
        self._failure_status_service = failure_status_service
        self._failure_record_callback = failure_record_callback
        self._target_windows_provider = target_windows_provider
        if (
            registered_role_provider is not None
            and not callable(registered_role_provider)
        ):
            raise TypeError("registered_role_provider must be callable")
        self._registered_role_provider = registered_role_provider
        self._operation_gate = operation_gate
        # This is deliberately independent of execution permission.  Both
        # gates must be open before any future auto-battle mutation can occur.
        self._auto_battle_enabled = auto_battle_enabled is not False
        self._auto_battle_recognizer = auto_battle_recognizer
        self._auto_battle_evidence: dict[
            str, tuple[WindowInstanceToken, str, int, int, tuple[int, int, int, int]]
        ] = {}
        self._auto_battle_button_windows: dict[
            str,
            _AutoBattleButtonWindow,
        ] = {}
        self._auto_battle_attempted_actions: set[
            tuple[str, WindowInstanceToken, str]
        ] = set()
        self._auto_battle_confirmed_instances: dict[
            str,
            tuple[WindowInstanceToken, str, int, int],
        ] = {}
        self._last_screen_states: dict[str, ReconnectScreenState] = {}
        self._last_trusted_capture_routes: dict[str, str] = {}
        self._trusted_connected_evidence: dict[
            str,
            _TrustedConnectedEvidence,
        ] = {}
        self._tcp_counts = tcp_connection_count_provider
        self._tcp_gen = 0
        self._tcp_s: dict[tuple[str, WindowInstanceToken], _TcpState] = {}
        # Keep only the fact that one exact configured process instance was
        # previously observed online.  A temporary unsafe source snapshot must
        # break every zero-count sequence, but must not erase that prerequisite
        # for the same old instance after the source becomes unique again.
        # Rect/minimized are deliberately excluded because a player may
        # legitimately minimize the same HWND before its TCP connection drops.
        self._tcp_online_witnesses: dict[
            tuple[str, str],
            tuple[int, int, int, str, int],
        ] = {}
        self._tcp_observation = _TcpObservation()
        self._tcp_timeout_isolated: set[str] = set()
        self._tcp_v = None
        self._source_state_generation = 0
        # A continuing source failure is one revocation edge.  Keep its
        # identity until a complete authority source observes that identity
        # again, so repeated scans of the same missing role do not keep
        # resetting every safe role to its first action frame.
        self._source_revoked_fingerprints: set[str] = set()
        self._action_confirmations: dict[
            str,
            _ActionConfirmation,
        ] = {}
        self._battle_restart_attempts: dict[
            tuple[str, bool],
            _BattleRestartEvent,
        ] = {}
        # A role restarted through the formal reconnect chain may be restored
        # only through login and character selection.  It must not inherit
        # post-login or auto-battle authority from unrelated roles.
        self._login_only_recovery_fingerprints: set[str] = set()
        self._tcp_recovery_authority = (
            self._restore_tcp_recovery_tombstone(
                runtime_state.tcp_recovery_authority
            )
        )
        # One delivered timeout confirmation may not be repeated merely
        # because capture routing, settings, or source generation changes.
        # The event is intentionally bound only to immutable window-session
        # facts; geometry and capture details remain final-delivery gates.
        self._force_login_timeout_attempts: dict[
            str,
            _BattleRestartEvent,
        ] = {}
        if self._expire_active_automation(now):
            self._persist_runtime_state()
        self._published_reconnecting_fingerprints = frozenset()
        self._publish_reconnecting_fingerprints(now)

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
        registered_role_provider: (
            Callable[[], Iterable[RegisteredReconnectRole]] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
        capture_settings: SmartReconnectCaptureSettings | None = None,
        require_expected_window_count: bool = True,
        auto_battle_enabled: bool = True,
        evidence_recorder: SmartReconnectEvidenceRecorder | None = None,
    ) -> "WindowsSmartReconnectController":
        window_backend = (
            window_backend
            or Win32WindowBackend(PowerShellLaunchFingerprintResolver())
        )
        evidence_initialization_failed = False
        if evidence_recorder is None:
            evidence_root = (
                state_path.parent
                if state_path is not None
                else Path(tempfile.gettempdir()) / "flash_smart_reconnect_runtime"
            )
            try:
                evidence_recorder = SmartReconnectEvidenceRecorder.for_runtime(
                    evidence_root,
                    auto_battle_required=auto_battle_enabled is not False,
                )
            except (OSError, TypeError, ValueError):
                evidence_initialization_failed = True
        graphics_capture_provider = WindowsGraphicsCaptureProvider()
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=window_backend,
            # Smart reconnect capture must never change a game window's
            # minimized state or z-order.  A window that is not already fully
            # visible therefore remains UNKNOWN while TCP observation keeps
            # running independently.
            capture_provider=Win32PrintWindowProvider(),
            visible_capture_provider=Win32VisibleRegionCaptureProvider(),
            obscured_capture_provider=graphics_capture_provider,
            active_refresh_capture_provider=None,
            capture_access_preparer=(
                graphics_capture_provider.prepare_borderless_access
            ),
            primary_capture_is_trusted=True,
            primary_capture_is_fresh_without_visibility=False,
            recognizer=ReferenceScreenRecognizer(reference_dir),
            mouse_backend=Win32MouseMessageBackend(),
            capture_settings=capture_settings,
            state_path=state_path,
            require_expected_window_count=require_expected_window_count,
            battle_restarter=WindowsBattleWindowRestarter(
                window_backend,
                Win32WindowCloseBackend(),
                WindowsShortcutOpenBackend(),
            ),
            failure_status_service=failure_status_service,
            failure_record_callback=failure_record_callback,
            target_windows_provider=target_windows_provider,
            registered_role_provider=registered_role_provider,
            operation_gate=operation_gate,
            auto_battle_enabled=auto_battle_enabled,
            auto_battle_recognizer=AutoBattleRecognizer(
                reference_dir / "auto_battle"
            ),
            evidence_recorder=evidence_recorder,
            evidence_required=True,
            evidence_initialization_failed=evidence_initialization_failed,
            tcp_connection_count_provider=_ipv4_established_counts_by_pid,
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

    @property
    def capture_settings(self) -> SmartReconnectCaptureSettings:
        with self._capture_settings_lock:
            return self._capture_settings

    def _capture_settings_snapshot(
        self,
    ) -> tuple[SmartReconnectCaptureSettings, int]:
        with self._capture_settings_lock:
            return (
                self._capture_settings,
                self._capture_settings_revision,
            )

    @staticmethod
    def _capture_route_enabled(
        settings: SmartReconnectCaptureSettings,
        route: str | None,
    ) -> bool:
        if route == CAPTURE_ROUTE_VISIBLE:
            return settings.visible
        if route == CAPTURE_ROUTE_OBSCURED:
            return settings.obscured
        if route == CAPTURE_ROUTE_MINIMIZED:
            return settings.minimized
        # An older persisted pending action may not have a runtime route.
        # It is safe only while every capture route remains enabled.
        return all(settings.to_dict().values())

    @classmethod
    def _revoked_screen_state(
        cls,
        settings: SmartReconnectCaptureSettings,
        route: str | None,
    ) -> ReconnectScreenState:
        if route is not None and not cls._capture_route_enabled(
            settings,
            route,
        ):
            return ReconnectScreenState.CHECK_DISABLED
        return ReconnectScreenState.UNKNOWN

    def _remember_capture_route(
        self,
        fingerprint: str,
        route: str | None,
    ) -> None:
        if route is None:
            return
        with self._screen_state_lock:
            self._last_trusted_capture_routes[fingerprint] = route

    def _source_state_generation_snapshot(self) -> int:
        with self._source_authority_lock:
            return self._source_state_generation

    def _source_authority_is_current(
        self,
        expected_source_state_generation: int | None,
    ) -> bool:
        if expected_source_state_generation is None:
            return True
        with self._source_authority_lock:
            return (
                self._source_state_generation
                == expected_source_state_generation
            )

    def _remember_capture_route_if_source_current(
        self,
        fingerprint: str,
        route: str | None,
        expected_source_state_generation: int,
    ) -> bool:
        """Remember a route only while the capture source is still current."""
        with self._source_authority_lock:
            if (
                self._source_state_generation
                != expected_source_state_generation
            ):
                return False
            self._remember_capture_route(fingerprint, route)
            return True

    def _capture_authority_is_current(
        self,
        expected_revision: int | None,
        route: str | None,
    ) -> bool:
        settings, revision = self._capture_settings_snapshot()
        if expected_revision is not None and revision != expected_revision:
            return False
        return self._capture_route_enabled(settings, route)

    def _revoke_capture_authority(self) -> None:
        """Discard every pending mutation authorized by older screen evidence."""
        with self._screen_state_lock:
            self._pending_reconnect_fingerprints.clear()
            self._pending_reopen_fingerprints.clear()
            self._active_automation_fingerprints.clear()
            self._active_automation_until.clear()
            self._action_retry_after.clear()
            self._reopen_retry_after.clear()
            self._terminal_ready_after.clear()
            self._terminal_evidence.clear()
            self._character_selection_pending.clear()
            self._character_selection_targets.clear()
            self._action_state_since.clear()
            self._action_confirmations.clear()
            self._recent_login_role_ids.clear()
            self._auto_battle_button_windows.clear()
            self._auto_battle_attempted_actions.clear()
            self._auto_battle_confirmed_instances.clear()
            self._reconnect_entry_authorized.clear()
            self._login_only_recovery_fingerprints.clear()
            self._flow_pause_until.clear()
            self._last_trusted_capture_routes.clear()
            self._trusted_connected_evidence.clear()
        self._publish_reconnecting_fingerprints()
        self._persist_runtime_state()

    def set_capture_settings(
        self,
        settings: SmartReconnectCaptureSettings,
    ) -> None:
        if not isinstance(settings, SmartReconnectCaptureSettings):
            raise TypeError(
                "settings must be SmartReconnectCaptureSettings"
            )
        # Serialize external settings changes with a scan's final result. The
        # lock is re-entrant so a backend callback that changes a setting is
        # still detected by the scan revision checks below.
        with self._scan_lock:
            with self._screen_state_lock:
                previous_routes = dict(self._last_trusted_capture_routes)
                previous_states = set(self._last_screen_states)
            with self._capture_settings_lock:
                if settings == self._capture_settings:
                    return
                self._capture_settings = settings
                self._capture_settings_revision += 1
            # A setting can be switched off while an action is awaiting its
            # second confirmation frame. Revoke that evidence immediately; the
            # next action must be proven again by an enabled capture route.
            self._clear_action_confirmation()
            self._revoke_capture_authority()
            self._initial_login_authorizations.clear()
            allowed = self._allowed_fingerprints
            with self._screen_state_lock:
                tracked = previous_states | set(previous_routes)
                if allowed is not None:
                    tracked.update(allowed)
                self._last_screen_states = {
                    fingerprint: self._revoked_screen_state(
                        settings,
                        previous_routes.get(fingerprint),
                    )
                    for fingerprint in tracked
                }
                self._last_result = None
                self._state = ReconnectState.FAILED

    def role_screen_states(self) -> dict[str, ReconnectScreenState]:
        with self._screen_state_lock:
            return dict(self._last_screen_states)

    def _recent_trusted_connected_state(
        self,
        window: WindowInfo,
        fingerprint: str,
        capture_route: str | None,
        capture_settings_revision: int,
    ) -> bool:
        if capture_route not in {
            CAPTURE_ROUTE_OBSCURED,
            CAPTURE_ROUTE_MINIMIZED,
        }:
            return False
        instance = WindowInstanceToken.from_window(window)
        if instance is None:
            return False
        now = time.monotonic()
        with self._screen_state_lock:
            evidence = self._trusted_connected_evidence.get(fingerprint)
            return bool(
                evidence is not None
                and fingerprint
                not in self._published_reconnecting_fingerprints
                and evidence.instance == instance
                and evidence.capture_route == capture_route
                and evidence.capture_settings_revision
                == capture_settings_revision
                and 0.0
                <= now - evidence.observed_at
                <= TRUSTED_CONNECTED_EVIDENCE_MAX_AGE_SECONDS
            )

    @staticmethod
    def _unknown_capture_result(
        sample: object | None = None,
        route: str | None = None,
    ) -> tuple[object | None, ScreenRecognition, bool, str | None]:
        return (
            sample,
            ScreenRecognition(
                state=ReconnectScreenState.UNKNOWN,
                score=None,
                click_point=None,
                reference_name=None,
            ),
            False,
            route,
        )

    @staticmethod
    def _disabled_capture_result(
        route: str,
    ) -> tuple[None, ScreenRecognition, bool, str]:
        return (
            None,
            ScreenRecognition(
                state=ReconnectScreenState.CHECK_DISABLED,
                score=None,
                click_point=None,
                reference_name=None,
            ),
            False,
            route,
        )

    def _window_is_fully_visible_without_capture(
        self,
        window: WindowInfo,
    ) -> bool | None:
        """Classify a non-minimized window without reading any pixels."""
        if window.minimized or not window.visible:
            return None
        left, top, right, bottom = window.rect
        width = right - left
        height = bottom - top
        if width <= 1 or height <= 1:
            return None
        try:
            for relative_x, relative_y in (
                Win32VisibleRegionCaptureProvider.REQUIRED_VISIBLE_POINTS
            ):
                x = left + max(
                    0,
                    min(width - 1, round((width - 1) * relative_x)),
                )
                y = top + max(
                    0,
                    min(height - 1, round((height - 1) * relative_y)),
                )
                top_handle = self._window_backend.top_window_at(x, y)
                if top_handle is None:
                    return None
                if top_handle != window.handle:
                    return False
        except (AttributeError, OSError):
            return None
        return True

    def _begin_capture_diagnostics(
        self,
        windows: Iterable[WindowInfo],
    ) -> None:
        window_items = tuple(windows)
        indices: dict[str, int] = {}
        for index, window in enumerate(window_items, start=1):
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is not None and fingerprint not in indices:
                indices[fingerprint] = index
        with self._capture_diagnostic_lock:
            self._capture_diagnostic_window_indices = indices
            self._capture_diagnostic_windows = window_items
            try:
                self._capture_diagnostic_foreground_handle = (
                    self._window_backend.foreground_handle()
                )
            except (AttributeError, OSError):
                self._capture_diagnostic_foreground_handle = None
            self._capture_diagnostics = ()

    def anonymous_capture_diagnostics(
        self,
    ) -> tuple[CaptureDiagnostic, ...]:
        """Return only anonymous hashes and fail-closed capture stages."""

        with self._capture_diagnostic_lock:
            return tuple(self._capture_diagnostics)

    @staticmethod
    def _anonymous_timing_window_id(fingerprint: str) -> str:
        return hashlib.sha256(fingerprint.encode("ascii")).hexdigest()[:12]

    def anonymous_reconnect_timing_diagnostics(
        self,
    ) -> tuple[ReconnectTimingDiagnostic, ...]:
        with self._screen_state_lock:
            return tuple(self._reconnect_timing_diagnostics)

    def _record_reconnect_timing(
        self,
        fingerprint: str,
        flow: _ReconnectTimingFlow,
        now: float,
        status: str,
    ) -> None:
        diagnostic = ReconnectTimingDiagnostic(
            recorded_at=round(now, 3),
            window_id=self._anonymous_timing_window_id(fingerprint),
            lifecycle=flow.lifecycle,
            stage=flow.stage,
            stage_seconds=round(max(0.0, now - flow.stage_started_at), 3),
            total_seconds=round(max(0.0, now - flow.started_at), 3),
            cycle=flow.cycle,
            status=status,
        )
        with self._screen_state_lock:
            self._reconnect_timing_diagnostics.append(diagnostic)
            if (
                len(self._reconnect_timing_diagnostics)
                > TIMING_DIAGNOSTIC_LIMIT
            ):
                del self._reconnect_timing_diagnostics[
                    : len(self._reconnect_timing_diagnostics)
                    - TIMING_DIAGNOSTIC_LIMIT
                ]
        callback = self._failure_record_callback
        if callback is not None:
            try:
                callback(
                    f"視窗-{diagnostic.window_id}",
                    (
                        f"計時 {diagnostic.lifecycle}/{diagnostic.stage} "
                        f"狀態={diagnostic.status} "
                        f"階段={diagnostic.stage_seconds:.3f}秒 "
                        f"總計={diagnostic.total_seconds:.3f}秒 "
                        f"時間={diagnostic.recorded_at:.3f}"
                    ),
                )
            except Exception:
                pass

    def _start_reconnect_timing(
        self,
        fingerprint: str,
        lifecycle: str,
        stage: str,
        now: float,
    ) -> None:
        key = (fingerprint, lifecycle)
        if key in self._reconnect_timing_flows:
            return
        flow = _ReconnectTimingFlow(
            lifecycle=lifecycle,
            started_at=now,
            stage=stage,
            stage_started_at=now,
        )
        self._reconnect_timing_flows[key] = flow
        self._record_reconnect_timing(
            fingerprint,
            flow,
            now,
            "started",
        )

    def _advance_reconnect_timing(
        self,
        fingerprint: str,
        stage: str,
        now: float,
    ) -> None:
        for (flow_fingerprint, _lifecycle), flow in tuple(
            self._reconnect_timing_flows.items()
        ):
            if flow_fingerprint != fingerprint or flow.stage == stage:
                continue
            self._record_reconnect_timing(
                fingerprint,
                flow,
                now,
                "stage_complete",
            )
            flow.stage = stage
            flow.stage_started_at = now

    def _complete_reconnect_timing(
        self,
        fingerprint: str,
        lifecycle: str,
        now: float,
    ) -> bool:
        if not self._reconnect_budget_current(fingerprint, now):
            return False
        flow = self._reconnect_timing_flows.pop(
            (fingerprint, lifecycle),
            None,
        )
        if flow is not None:
            self._record_reconnect_timing(
                fingerprint,
                flow,
                now,
                "completed",
            )
        return True

    def _reconnect_budget_current(
        self,
        fingerprint: str,
        now: float | None = None,
    ) -> bool:
        """Fail closed at the final delivery boundary of a TCP owner budget."""

        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None or normalized in self._tcp_timeout_isolated:
            return False
        flow = self._reconnect_timing_flows.get(
            (normalized, "tcp_disconnect_to_connected")
        )
        if flow is None:
            return True
        fresh_now = self._monotonic_clock() if now is None else now
        if fresh_now - flow.started_at < RECONNECT_TOTAL_BUDGET_SECONDS:
            return True
        self._check_reconnect_timing_deadlines(fresh_now)
        return False

    def _reconnect_budget_deadline(
        self,
        fingerprint: str,
    ) -> float | None:
        flow = self._reconnect_timing_flows.get(
            (fingerprint, "tcp_disconnect_to_connected")
        )
        return (
            flow.started_at + RECONNECT_TOTAL_BUDGET_SECONDS
            if flow is not None
            else None
        )

    def _reset_recovery_context_after_timeout(
        self,
        fingerprint: str,
    ) -> None:
        self._clear_action_confirmation(fingerprint)
        self._action_state_since.pop(fingerprint, None)
        self._action_retry_after.pop(fingerprint, None)
        self._flow_pause_until.pop(fingerprint, None)
        self._character_selection_pending.discard(fingerprint)
        self._character_selection_targets.pop(fingerprint, None)
        self._terminal_ready_after.pop(fingerprint, None)
        self._terminal_evidence.pop(fingerprint, None)
        self._auto_battle_evidence.pop(fingerprint, None)
        self._auto_battle_button_windows.pop(fingerprint, None)
        self._auto_battle_confirmed_instances.pop(fingerprint, None)
        # This is an owner-local timeout.  Keep independently confirmed TCP
        # evidence for healthy peers so the next scan can advance the queue;
        # only the timed-out owner must lose its stale zero sequence.
        for (entry_id, _instance), state in self._tcp_s.items():
            target = self._target_for_fingerprint(fingerprint)
            if target is not None and entry_id == target.entry_id:
                state.zero_since = None
                state.zero_count = 0
        self._auto_battle_attempted_actions = {
            item
            for item in self._auto_battle_attempted_actions
            if item[0] != fingerprint
        }
        self._recent_login_role_ids.pop(fingerprint, None)
        self._initial_login_authorizations.pop(fingerprint, None)
        self._primary_entry_authorized.discard(fingerprint)
        self._primary_connected_fingerprints.discard(fingerprint)
        self._reconnect_entry_authorized.discard(fingerprint)

    def _check_reconnect_timing_deadlines(self, now: float) -> None:
        timed_out_fingerprints: set[str] = set()
        for (fingerprint, lifecycle), flow in tuple(
            self._reconnect_timing_flows.items()
        ):
            if fingerprint in timed_out_fingerprints:
                continue
            budget = (
                START_GAME_BUDGET_SECONDS
                if lifecycle == "start_game_to_primary_connected"
                else RECONNECT_TOTAL_BUDGET_SECONDS
            )
            if now - flow.started_at < budget:
                continue
            self._record_reconnect_timing(
                fingerprint,
                flow,
                now,
                "timeout",
            )
            if lifecycle == "tcp_disconnect_to_connected":
                # A TCP recovery owns only this one original role.  On the
                # fixed sixty-second budget it loses every reconnect grant,
                # keeps the immutable restart event dedupe, and cannot be
                # immediately selected again while another confirmed peer may
                # advance on the next monitor scan.
                self._reset_recovery_context_after_timeout(fingerprint)
                preserved_attempts = {
                    key: event
                    for key, event in self._battle_restart_attempts.items()
                    if key[0] == fingerprint
                }
                self._clear_reconnect_session(fingerprint)
                self._battle_restart_attempts.update(preserved_attempts)
                self._login_only_recovery_fingerprints.discard(fingerprint)
                self._tcp_timeout_isolated.add(fingerprint)
                for key in tuple(self._reconnect_timing_flows):
                    if key[0] == fingerprint:
                        self._reconnect_timing_flows.pop(key, None)
                timed_out_fingerprints.add(fingerprint)
                self._report_reconnect_failure(fingerprint)
                continue
            self._reset_recovery_context_after_timeout(fingerprint)
            flow.started_at = now
            flow.stage_started_at = now
            flow.cycle += 1

    def _evidence_available(self) -> bool:
        return bool(
            not self._evidence_recording_failed
            and (
                self._evidence_recorder is not None
                or not self._evidence_required
            )
        )

    def _mark_evidence_failure(self) -> None:
        self._evidence_recording_failed = True

    def _record_evidence_monitoring_state(self, enabled: bool) -> bool:
        recorder = self._evidence_recorder
        if recorder is None:
            return not self._evidence_required
        try:
            recorder.record_monitoring_state(enabled)
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()
            return False
        return True

    @staticmethod
    def _rectangles_overlap(
        first: tuple[int, int, int, int],
        second: tuple[int, int, int, int],
    ) -> bool:
        return bool(
            max(first[0], second[0]) < min(first[2], second[2])
            and max(first[1], second[1]) < min(first[3], second[3])
        )

    def _window_overlaps_monitored_window(self, window: WindowInfo) -> bool:
        with self._capture_diagnostic_lock:
            windows = self._capture_diagnostic_windows
        return any(
            item.handle != window.handle
            and self._rectangles_overlap(window.rect, item.rect)
            for item in windows
            if isinstance(item, WindowInfo)
        )

    def _evidence_presentation_state(
        self,
        window: WindowInfo,
        capture_route: str | None,
    ) -> str:
        if window.minimized:
            return "minimized"
        if capture_route == CAPTURE_ROUTE_VISIBLE:
            return "visible"
        if capture_route != CAPTURE_ROUTE_OBSCURED or not window.visible:
            return "unknown"
        left, top, right, bottom = window.rect
        width = right - left
        height = bottom - top
        if width <= 1 or height <= 1:
            return "unknown"
        visible_points = 0
        tested_points = 0
        try:
            for relative_x, relative_y in (
                (0.15, 0.15),
                (0.50, 0.15),
                (0.85, 0.15),
                (0.15, 0.50),
                (0.50, 0.50),
                (0.85, 0.50),
                (0.15, 0.85),
                (0.50, 0.85),
                (0.85, 0.85),
            ):
                x = left + max(0, min(width - 1, round(width * relative_x)))
                y = top + max(0, min(height - 1, round(height * relative_y)))
                top_handle = self._window_backend.top_window_at(x, y)
                if top_handle is None:
                    return "unknown"
                tested_points += 1
                if top_handle == window.handle:
                    visible_points += 1
        except (AttributeError, OSError):
            return "unknown"
        if tested_points <= 0:
            return "unknown"
        if visible_points == 0:
            return "fully_obscured"
        if visible_points < tested_points:
            return "partially_obscured"
        return "visible"

    @staticmethod
    def _recognition_evidence_basis(
        recognition: ScreenRecognition,
    ) -> str | None:
        if recognition.state is ReconnectScreenState.CONNECTED:
            if (
                recognition.reference_name
                == "anonymous_live_structure/general_hud.png"
            ):
                return "cross_map_fixed_ui"
            if isinstance(recognition.reference_name, str):
                return "legacy_or_map_specific"
            return "unresolved"
        if recognition.state is ReconnectScreenState.UNKNOWN:
            return "unresolved"
        return "workflow_screen"

    def _record_evidence_observation(
        self,
        *,
        window: WindowInfo,
        fingerprint: str,
        sample: object | None,
        recognition: ScreenRecognition,
        fresh_capture: bool,
        capture_route: str | None,
        rejection_gate: str | None,
        expected_source_state_generation: int | None,
    ) -> None:
        recorder = self._evidence_recorder
        if recorder is None or self._evidence_recording_failed:
            return
        pixels = (
            sample.pixels
            if isinstance(sample, CaptureSample) and sample.api_succeeded
            else None
        )
        width = (
            int(sample.width)
            if isinstance(sample, CaptureSample) and sample.api_succeeded
            else None
        )
        height = (
            int(sample.height)
            if isinstance(sample, CaptureSample) and sample.api_succeeded
            else None
        )
        instance = WindowInstanceToken.from_window(window)
        identity_verified = bool(
            instance is not None
            and self._source_authority_is_current(
                expected_source_state_generation
            )
        )
        with self._capture_diagnostic_lock:
            foreground_handle = self._capture_diagnostic_foreground_handle
        try:
            recorder.record_observation(
                raw_window_key=fingerprint,
                state=recognition.state.value,
                capture_method=capture_route,
                width=width,
                height=height,
                pixels=pixels,
                fresh=fresh_capture,
                identity_verified=identity_verified,
                score=recognition.score,
                reference_code=recognition.reference_name,
                failure_reason=rejection_gate,
                recognition_method="reference_screen_primary",
                fallback_capture_used=(
                    capture_route
                    in {CAPTURE_ROUTE_OBSCURED, CAPTURE_ROUTE_MINIMIZED}
                ),
                fallback_recognition_used=False,
                presentation_state=self._evidence_presentation_state(
                    window,
                    capture_route,
                ),
                scene_context=(
                    "battle"
                    if recognition.battle_context
                    else (
                        "general"
                        if recognition.state
                        in {
                            ReconnectScreenState.CONNECTED,
                            ReconnectScreenState.DISCONNECTED,
                        }
                        else "unknown"
                    )
                ),
                recognition_basis=self._recognition_evidence_basis(
                    recognition
                ),
                other_window_foreground=(
                    foreground_handle is not None
                    and foreground_handle != window.handle
                ),
                overlapped_by_game_window=(
                    self._window_overlaps_monitored_window(window)
                ),
            )
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()

    def _record_evidence_decision(
        self,
        *,
        fingerprint: str,
        item: ScreenRecognition,
        instance: WindowInstanceToken,
        capture_route: str,
        capture_settings_revision: int,
        source_state_generation: int,
    ) -> bool:
        recorder = self._evidence_recorder
        if recorder is None:
            return not self._evidence_required
        if self._evidence_recording_failed:
            return False
        identity_verified = bool(
            self._source_authority_is_current(source_state_generation)
            and self._current_action_window(instance, fingerprint) is not None
        )
        signature = hashlib.sha256(
            repr(self._action_signature(item)).encode("utf-8")
        ).hexdigest()
        authority_signature = self._evidence_authority_signature(
            instance,
            capture_route,
            capture_settings_revision,
            source_state_generation,
        )
        try:
            recorder.record_decision_evidence(
                raw_window_key=fingerprint,
                state=item.state.value,
                decision_signature=signature,
                capture_method=capture_route,
                identity_verified=identity_verified,
                authority_signature=authority_signature,
            )
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()
            return False
        return identity_verified

    def _record_reopen_absence_evidence(
        self,
        *,
        fingerprint: str,
        instance: WindowInstanceToken,
        target: GroupLaunchTarget,
        capture_route: str,
        capture_settings_revision: int,
        source_state_generation: int,
    ) -> tuple[bool, tuple[WindowInfo, ...], str | None]:
        recorder = self._evidence_recorder
        if recorder is None:
            return (not self._evidence_required, (), None)
        if self._evidence_recording_failed:
            return False, (), None
        authority_signature = self._evidence_authority_signature(
            instance,
            capture_route,
            capture_settings_revision,
            source_state_generation,
        )
        decision_signature = hashlib.sha256(
            repr(
                (
                    "reopen_window",
                    fingerprint,
                    instance,
                    target.fingerprint,
                )
            ).encode("utf-8")
        ).hexdigest()
        latest_candidates: tuple[WindowInfo, ...] = ()
        for _ in range(2):
            (
                latest_candidates,
                global_failures,
                target_failures,
            ) = self._candidate_window_set()
            matching = tuple(
                window
                for window in latest_candidates
                if normalize_launch_fingerprint(window.launch_fingerprint)
                == fingerprint
            )
            owner_offline = (
                not global_failures
                and set(target_failures.get(fingerprint, ()))
                == {"window_offline"}
            )
            identity_verified = bool(
                self._source_authority_is_current(source_state_generation)
                and owner_offline
                and not matching
                and target.fingerprint == fingerprint
                and self._activation_snapshot_instances is not None
                and self._activation_snapshot_instances.get(fingerprint)
                == instance
            )
            try:
                recorder.record_absence_evidence(
                    raw_window_key=fingerprint,
                    state=ReconnectScreenState.RECONNECTING.value,
                    decision_signature=decision_signature,
                    identity_verified=identity_verified,
                    shortcut_identity_verified=(
                        target.fingerprint == fingerprint
                    ),
                    target_absent=not matching,
                    authority_signature=authority_signature,
                )
            except (OSError, TypeError, ValueError):
                self._mark_evidence_failure()
                return False, (), None
            if not identity_verified:
                return False, latest_candidates, authority_signature
        return True, latest_candidates, authority_signature

    def _evidence_action_name(
        self,
        item: ScreenRecognition,
        *,
        line_scroll: bool = False,
    ) -> str:
        if line_scroll:
            return "scroll_line_list"
        if (
            item.state is ReconnectScreenState.CHARACTER_SELECTION
            and item.character_slot_selected is False
        ):
            return "select_role_slot"
        return self._policy.decide(item.state).action.value

    def _begin_evidence_action(
        self,
        *,
        fingerprint: str,
        item: ScreenRecognition,
        action: str,
        instance: WindowInstanceToken,
        capture_route: str,
        capture_settings_revision: int,
        source_state_generation: int,
        input_channel: str = "window_message",
        identity_verified_override: bool | None = None,
        allow_unknown: bool = False,
    ) -> tuple[bool, int | None, str | None]:
        if (
            item.state is ReconnectScreenState.CHECK_DISABLED
            or item.state is ReconnectScreenState.UNKNOWN
            and not allow_unknown
        ):
            return False, None, None
        recorder = self._evidence_recorder
        if recorder is None:
            return (not self._evidence_required, None, None)
        if self._evidence_recording_failed:
            return False, None, None
        identity_verified = (
            identity_verified_override
            if type(identity_verified_override) is bool
            else bool(
                self._current_action_window(instance, fingerprint) is not None
            )
        )
        authority_signature = self._evidence_authority_signature(
            instance,
            capture_route,
            capture_settings_revision,
            source_state_generation,
        )
        try:
            sequence = recorder.record_action_intent(
                raw_window_key=fingerprint,
                state=item.state.value,
                action=action,
                identity_verified=identity_verified,
                input_channel=input_channel,
                authority_signature=authority_signature,
            )
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()
            return False, None, None
        return identity_verified, sequence, authority_signature

    @staticmethod
    def _evidence_authority_signature(
        instance: WindowInstanceToken,
        capture_route: str,
        capture_settings_revision: int,
        source_state_generation: int,
    ) -> str:
        payload = (
            instance.handle,
            instance.process_id,
            instance.thread_id,
            instance.window_class,
            instance.rect,
            instance.minimized,
            instance.process_lifecycle_token,
            capture_route,
            capture_settings_revision,
            source_state_generation,
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()

    def _original_line_verified_for_evidence(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> bool | None:
        if item.state is not ReconnectScreenState.LINE_SELECTION:
            return None
        if item.line_scroll_delta:
            return None
        preferred = self._preferred_line_numbers.get(fingerprint)
        return bool(
            item.line_number in LINE_ROUTE_CLICK_POINTS
            and (
                item.line_number == preferred
                or item.recent_line_present is True
            )
        )

    def _original_role_verified_for_evidence(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> bool | None:
        if (
            item.state is not ReconnectScreenState.CHARACTER_SELECTION
            or item.character_slot_selected is not True
        ):
            return None
        target = self._target_for_fingerprint(fingerprint)
        if (
            target is None
            or not isinstance(item.character_target_key, str)
        ):
            return False
        return bool(
            item.character_target_key.strip().casefold()
            == target.role_id.strip().casefold()
        )

    def _finish_evidence_action(
        self,
        *,
        fingerprint: str,
        item: ScreenRecognition,
        action: str,
        intent_sequence: int | None,
        allowed: bool,
        performed: bool,
        clicked: bool,
        identity_verified: bool,
        restoration_verified: bool | None,
        failure_reason: str | None,
        auto_battle_panel_verified: bool | None = None,
        authority_signature: str | None = None,
        input_channel: str = "window_message",
    ) -> None:
        recorder = self._evidence_recorder
        if recorder is None or self._evidence_recording_failed:
            return
        try:
            recorder.record_action(
                raw_window_key=fingerprint,
                state=item.state.value,
                action=action,
                allowed=allowed,
                performed=performed,
                clicked=clicked,
                identity_verified=identity_verified,
                restoration_verified=restoration_verified,
                failure_reason=failure_reason,
                original_line_verified=(
                    self._original_line_verified_for_evidence(
                        fingerprint,
                        item,
                    )
                ),
                original_role_verified=(
                    self._original_role_verified_for_evidence(
                        fingerprint,
                        item,
                    )
                ),
                auto_battle_panel_verified=auto_battle_panel_verified,
                input_channel=input_channel,
                intent_sequence=intent_sequence,
                authority_signature=authority_signature,
            )
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()

    def _record_auto_battle_panel_verification(
        self,
        *,
        fingerprint: str,
        instance: WindowInstanceToken,
        sample: object | None,
    ) -> bool:
        recorder = self._evidence_recorder
        if recorder is None:
            return not self._evidence_required
        if self._evidence_recording_failed:
            return False
        if (
            not isinstance(sample, CaptureSample)
            or not sample.api_succeeded
            or not isinstance(sample.pixels, bytes)
            or self._current_action_window(instance, fingerprint) is None
        ):
            return False
        try:
            recorder.record_verification(
                raw_window_key=fingerprint,
                identity_verified=True,
                verification_basis="complete_auto_battle_panel",
                evidence_signature=hashlib.sha256(sample.pixels).hexdigest(),
                auto_battle_panel_verified=True,
            )
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()
            return False
        return True

    def _record_capture_diagnostic(
        self,
        *,
        window: WindowInfo,
        fingerprint: str,
        stage: str,
        sample: object | None,
        recognition: ScreenRecognition,
        fresh_capture: bool,
        capture_route: str | None,
        expected_source_state_generation: int | None,
    ) -> None:
        with self._capture_diagnostic_lock:
            window_index = self._capture_diagnostic_window_indices.get(
                fingerprint
            )
        if window_index is None:
            return

        width: int | None = None
        height: int | None = None
        digest: str | None = None
        if isinstance(sample, CaptureSample) and sample.api_succeeded:
            width = int(sample.width)
            height = int(sample.height)
            digest = hashlib.sha256(sample.pixels).hexdigest()

        rejection_gate: str | None = None
        if not self._source_authority_is_current(
            expected_source_state_generation
        ):
            rejection_gate = "source_generation_changed"
        elif recognition.state is ReconnectScreenState.CHECK_DISABLED:
            rejection_gate = "capture_path_disabled"
        elif sample is None or not getattr(sample, "api_succeeded", False):
            provider_stage = (
                getattr(
                    self._active_refresh_capture_provider,
                    "last_failure_stage",
                    None,
                )
                if capture_route == CAPTURE_ROUTE_MINIMIZED
                else None
            )
            rejection_gate = (
                provider_stage
                if isinstance(provider_stage, str) and provider_stage
                else "capture_failed"
            )
        elif not fresh_capture:
            rejection_gate = "capture_not_fresh"
        elif recognition.state is ReconnectScreenState.UNKNOWN:
            rejection_gate = "screen_unknown"

        score = recognition.score
        recognition_score = (
            float(score)
            if isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(float(score))
            else None
        )
        diagnostic = CaptureDiagnostic(
            window_index=window_index,
            stage=str(stage),
            capture_path=capture_route or "unresolved",
            width=width,
            height=height,
            sha256=digest,
            recognition_score=recognition_score,
            rejection_gate=rejection_gate,
        )
        with self._capture_diagnostic_lock:
            self._capture_diagnostics = (
                *self._capture_diagnostics[-63:],
                diagnostic,
            )
        self._record_evidence_observation(
            window=window,
            fingerprint=fingerprint,
            sample=sample,
            recognition=recognition,
            fresh_capture=fresh_capture,
            capture_route=capture_route,
            rejection_gate=rejection_gate,
            expected_source_state_generation=expected_source_state_generation,
        )

    def _capture_and_recognize(
        self,
        window: WindowInfo,
        fingerprint: str,
        *,
        execute: bool = False,
        expected_source_state_generation: int | None = None,
        diagnostic_stage: str = "scan",
    ) -> tuple[
        object | None,
        ScreenRecognition,
        bool,
        str | None,
    ]:
        result = self._capture_and_recognize_unobserved(
            window,
            fingerprint,
            execute=execute,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
        )
        sample, recognition, fresh_capture, capture_route = result
        self._record_capture_diagnostic(
            window=window,
            fingerprint=fingerprint,
            stage=diagnostic_stage,
            sample=sample,
            recognition=recognition,
            fresh_capture=fresh_capture,
            capture_route=capture_route,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
        )
        return result

    def _capture_and_recognize_unobserved(
        self,
        window: WindowInfo,
        fingerprint: str,
        *,
        execute: bool = False,
        expected_source_state_generation: int | None = None,
    ) -> tuple[
        object | None,
        ScreenRecognition,
        bool,
        str | None,
    ]:
        # Every capture route must start from one complete, immutable window
        # instance. Do this before reading settings or probing/capturing so
        # an incomplete source candidate cannot refresh online evidence.
        if WindowInstanceToken.from_window(window) is None:
            return self._unknown_capture_result()
        if expected_source_state_generation is None:
            expected_source_state_generation = (
                self._source_state_generation_snapshot()
            )
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            return self._unknown_capture_result()

        def remember_route(route: str | None) -> bool:
            return self._remember_capture_route_if_source_current(
                fingerprint,
                route,
                expected_source_state_generation,
            )

        settings = self.capture_settings
        if window.minimized:
            route = CAPTURE_ROUTE_MINIMIZED
            if not remember_route(route):
                return self._unknown_capture_result()
            if not settings.minimized:
                return self._disabled_capture_result(route)
            if (
                execute
                and self._execution_allowed()
                and self._active_refresh_capture_provider is not None
            ):
                try:
                    refreshed_sample = (
                        self._active_refresh_capture_provider.capture(
                            window.handle
                        )
                    )
                except OSError:
                    refreshed_sample = None
                if (
                    refreshed_sample is not None
                    and refreshed_sample.api_succeeded
                ):
                    return (
                        refreshed_sample,
                        self._recognizer.recognize_capture(
                            refreshed_sample
                        ),
                        True,
                        route,
                    )
            if self._primary_capture_is_fresh_without_visibility:
                try:
                    passive_sample = self._capture_provider.capture(
                        window.handle
                    )
                except OSError:
                    passive_sample = None
                if (
                    passive_sample is not None
                    and passive_sample.api_succeeded
                ):
                    return (
                        passive_sample,
                        self._recognizer.recognize_capture(passive_sample),
                        True,
                        route,
                    )
            return self._unknown_capture_result(route=route)

        fallback_provider = self._visible_capture_provider
        visible_sample = None
        if settings.visible and fallback_provider is not None:
            try:
                visible_sample = fallback_provider.capture(window.handle)
            except OSError:
                visible_sample = None
            if visible_sample is not None and visible_sample.api_succeeded:
                # This provider returns a frame only after proving that the
                # target is visible and unobscured at its guarded points.
                # Prefer the fresh desktop frame for every state, including
                # UNKNOWN, so a stale background frame can never authorize a
                # click or report a false disconnect.
                route = CAPTURE_ROUTE_VISIBLE
                if not remember_route(route):
                    return self._unknown_capture_result()
                return (
                    visible_sample,
                    self._recognizer.recognize_capture(visible_sample),
                    True,
                    route,
                )

        if (
            fallback_provider is None
            and settings.visible
            and self._primary_capture_is_trusted
        ):
            try:
                primary_sample = self._capture_provider.capture(window.handle)
            except OSError:
                primary_sample = None
            if primary_sample is not None and primary_sample.api_succeeded:
                if not remember_route(CAPTURE_ROUTE_VISIBLE):
                    return self._unknown_capture_result()
                return (
                    primary_sample,
                    self._recognizer.recognize_capture(primary_sample),
                    True,
                    CAPTURE_ROUTE_VISIBLE,
                )
            visibly_unobscured = (
                self._window_is_fully_visible_without_capture(window)
            )
            route = (
                CAPTURE_ROUTE_VISIBLE
                if visibly_unobscured is True
                else (
                    CAPTURE_ROUTE_OBSCURED
                    if visibly_unobscured is False
                    else None
                )
            )
            if not remember_route(route):
                return self._unknown_capture_result()
            return self._unknown_capture_result(primary_sample, route)

        visibly_unobscured = self._window_is_fully_visible_without_capture(
            window
        )
        if visibly_unobscured is True:
            route = CAPTURE_ROUTE_VISIBLE
            if not remember_route(route):
                return self._unknown_capture_result()
            if not settings.visible:
                return self._disabled_capture_result(route)
            return self._unknown_capture_result(visible_sample, route)
        if visibly_unobscured is None:
            # Classification itself was not trustworthy. It cannot be
            # assigned to either the enabled or disabled route, and an old
            # online/disconnected state must not survive this observation.
            return self._unknown_capture_result(visible_sample)

        route = CAPTURE_ROUTE_OBSCURED
        if not remember_route(route):
            return self._unknown_capture_result()
        if not settings.obscured:
            return self._disabled_capture_result(route)

        if self._primary_capture_is_fresh_without_visibility:
            try:
                passive_sample = self._capture_provider.capture(window.handle)
            except OSError:
                passive_sample = None
            if (
                passive_sample is not None
                and passive_sample.api_succeeded
            ):
                return (
                    passive_sample,
                    self._recognizer.recognize_capture(passive_sample),
                    True,
                    route,
                )

        if not execute or not self._execution_allowed():
            # Passive observers may inspect already visible desktop pixels,
            # but must never restore, reveal, reorder, or otherwise change a
            # game window merely to obtain a frame.
            return self._unknown_capture_result(route=route)

        if self._obscured_capture_provider is not None:
            try:
                obscured_sample = self._obscured_capture_provider.capture(
                    window.handle
                )
            except OSError:
                obscured_sample = None
            if (
                obscured_sample is not None
                and obscured_sample.api_succeeded
            ):
                return (
                    obscured_sample,
                    self._recognizer.recognize_capture(obscured_sample),
                    True,
                    route,
                )
            return self._unknown_capture_result(route=route)

        # Legacy injected controllers without a guarded obscured provider may
        # still expose passive pixels for anonymous diagnostics. Production
        # always has the guarded provider and never reads a frame merely to
        # discard it as non-fresh.
        try:
            primary_sample = self._capture_provider.capture(window.handle)
        except OSError:
            primary_sample = None
        return self._unknown_capture_result(primary_sample, route)

    def _arm_terminal_completion(
        self,
        fingerprint: str,
        now: float,
    ) -> None:
        self._terminal_ready_after[fingerprint] = now
        self._terminal_evidence.pop(fingerprint, None)

    def _terminal_completion_confirmed(
        self,
        fingerprint: str,
        window: WindowInfo,
        sample,
        now: float,
    ) -> bool:
        instance = WindowInstanceToken.from_window(window)
        ready_at = self._terminal_ready_after.get(fingerprint)
        if (
            instance is None
            or
            ready_at is None
            or sample is None
            or not sample.api_succeeded
            or sample.width <= 0
            or sample.height <= 0
            or len(sample.pixels) != sample.width * sample.height * 4
            or not self._mouse_backend.is_window(window.handle)
            or not self._mouse_backend.probe_responsive(
                window.handle,
                self._preflight_timeout_ms,
            )
        ):
            self._terminal_evidence.pop(fingerprint, None)
            return False
        digest = hashlib.blake2b(
            sample.pixels,
            digest_size=16,
        ).digest()
        previous = self._terminal_evidence.get(fingerprint)
        same_instance = (
            previous is not None
            and previous.instance == instance
            and previous.width == sample.width
            and previous.height == sample.height
        )
        if (
            same_instance
            and previous is not None
            and previous.last_digest != digest
        ):
            evidence = _TerminalEvidence(
                instance=instance,
                width=sample.width,
                height=sample.height,
                first_seen=previous.first_seen,
                last_digest=digest,
                changing_frames=previous.changing_frames + 1,
            )
        else:
            evidence = _TerminalEvidence(
                instance=instance,
                width=sample.width,
                height=sample.height,
                first_seen=now,
                last_digest=digest,
                changing_frames=1,
            )
        self._terminal_evidence[fingerprint] = evidence
        # Three fresh, changing frames from the same responsive instance prove
        # that the reconnect completed. A wall-clock delay adds no evidence and
        # can keep a finished login flow in a false waiting state.
        return (
            evidence.changing_frames >= TERMINAL_CONFIRMATION_FRAMES
            and now - evidence.first_seen >= TERMINAL_CONFIRMATION_SECONDS
            and now - ready_at >= TERMINAL_CONFIRMATION_SECONDS
        )

    @staticmethod
    def _unique_complete_candidate_instances(
        windows: Iterable[WindowInfo],
        *,
        instance_bound: bool = False,
    ) -> dict[str, tuple[WindowInfo, WindowInstanceToken]] | None:
        """Return a fail-closed, one-role-per-live-instance collection."""
        resolved: dict[str, tuple[WindowInfo, WindowInstanceToken]] = {}
        handles: set[int] = set()
        process_ids: set[int] = set()
        for window in windows:
            if not isinstance(window, WindowInfo):
                return None
            instance = WindowInstanceToken.from_window(window)
            fingerprint = (
                monitored_window_instance_fingerprint(window)
                if instance_bound
                else normalize_launch_fingerprint(window.launch_fingerprint)
            )
            if fingerprint is None or instance is None:
                return None
            if not instance_bound:
                if instance.process_id in process_ids:
                    return None
                process_ids.add(instance.process_id)
            if (
                fingerprint in resolved
                or instance.handle in handles
            ):
                return None
            resolved[fingerprint] = (
                replace(window, launch_fingerprint=fingerprint)
                if instance_bound
                else window,
                instance,
            )
            handles.add(instance.handle)
        return resolved

    @staticmethod
    def _activation_monitor_fingerprint(
        source_fingerprint: str,
        instance: WindowInstanceToken,
    ) -> str:
        """Derive one activation-local identity from immutable window facts."""

        fingerprint = monitored_window_instance_fingerprint(
            WindowInfo(
                handle=instance.handle,
                title="",
                visible=True,
                minimized=instance.minimized,
                rect=instance.rect,
                process_id=instance.process_id,
                launch_fingerprint=source_fingerprint,
                thread_id=instance.thread_id,
                window_class=instance.window_class,
                process_lifecycle_token=instance.process_lifecycle_token,
            )
        )
        if fingerprint is None:
            raise ValueError("complete window instance required")
        return fingerprint

    @staticmethod
    def _activation_instance_key(
        source_fingerprint: object,
        instance: WindowInstanceToken,
    ) -> tuple[str, int, int, int, str, int] | None:
        source = normalize_launch_fingerprint(source_fingerprint)
        if source is None:
            return None
        return (
            source,
            instance.handle,
            instance.process_id,
            instance.thread_id,
            instance.window_class,
            instance.process_lifecycle_token,
        )

    @classmethod
    def _activation_snapshot_candidate_instances(
        cls,
        windows: Iterable[WindowInfo],
    ) -> tuple[
        dict[str, tuple[WindowInfo, WindowInstanceToken]],
        dict[str, str],
        frozenset[str],
        int,
    ]:
        """Isolate incomplete instances and disambiguate shared executables."""

        candidates = tuple(windows)
        parsed: list[tuple[WindowInfo, str, WindowInstanceToken]] = []
        for window in candidates:
            if not isinstance(window, WindowInfo) or not window.visible:
                continue
            source_fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            instance = WindowInstanceToken.from_window(window)
            if source_fingerprint is None or instance is None:
                continue
            parsed.append((window, source_fingerprint, instance))

        handle_counts = Counter(item[2].handle for item in parsed)
        safe = tuple(
            item
            for item in parsed
            if handle_counts[item[2].handle] == 1
        )
        source_counts = Counter(item[1] for item in safe)
        provisional = tuple(
            (
                (
                    source_fingerprint
                    if source_counts[source_fingerprint] == 1
                    else cls._activation_monitor_fingerprint(
                        source_fingerprint,
                        instance,
                    )
                ),
                window,
                source_fingerprint,
                instance,
            )
            for window, source_fingerprint, instance in safe
        )
        monitor_counts = Counter(item[0] for item in provisional)
        resolved: dict[
            str,
            tuple[WindowInfo, WindowInstanceToken],
        ] = {}
        source_fingerprints: dict[str, str] = {}
        for monitor_fingerprint, window, source_fingerprint, instance in provisional:
            if monitor_counts[monitor_fingerprint] != 1:
                continue
            resolved[monitor_fingerprint] = (
                replace(
                    window,
                    launch_fingerprint=monitor_fingerprint,
                ),
                instance,
            )
            source_fingerprints[monitor_fingerprint] = source_fingerprint
        return resolved, source_fingerprints, len(candidates) - len(resolved)

    @staticmethod
    def _candidate_collections_overlap(
        supplied: dict[str, tuple[WindowInfo, WindowInstanceToken]],
        authority_windows: Iterable[WindowInfo],
    ) -> bool:
        """Identify whether an explicit observation refers to live authority."""
        supplied_fingerprints = set(supplied)
        supplied_handles = {
            instance.handle for _window, instance in supplied.values()
        }
        for window in authority_windows:
            fingerprint = normalize_launch_fingerprint(
                getattr(window, "launch_fingerprint", None)
            )
            if fingerprint in supplied_fingerprints:
                return True
            handle = getattr(window, "handle", None)
            if handle in supplied_handles:
                return True
        return False

    def observe_screen_states(
        self,
        fingerprints: Iterable[str],
        *,
        candidate_windows: Iterable[WindowInfo] | None = None,
        instance_bound: bool = False,
    ) -> dict[str, ReconnectScreenState]:
        requested = {
            fingerprint
            for item in fingerprints
            if (fingerprint := normalize_launch_fingerprint(item)) is not None
        }
        if not requested:
            return {}
        _settings, observation_revision = self._capture_settings_snapshot()
        supplied = (
            tuple(candidate_windows)
            if candidate_windows is not None
            else None
        )
        source_windows, global_failures, target_failures = (
            self._candidate_window_set()
        )
        source_instances = self._unique_complete_candidate_instances(
            source_windows,
            instance_bound=instance_bound,
        )
        if supplied is None:
            candidates = source_windows
            by_fingerprint = source_instances
            recheck_live_authority = True
            unsafe_candidate_set = bool(
                global_failures
                or requested & set(target_failures)
                or by_fingerprint is None
            )
        else:
            candidates = supplied
            by_fingerprint = self._unique_complete_candidate_instances(
                candidates,
                instance_bound=instance_bound,
            )
            overlap = self._candidate_collections_overlap(
                by_fingerprint or {},
                source_windows,
            )
            # Explicit candidates are used by the main-thread target contract.
            # Preserve its existing ungrouped read-only path when the live
            # enumerator knows nothing about that unrelated explicit instance.
            recheck_live_authority = overlap
            unsafe_candidate_set = by_fingerprint is None
            if overlap and (
                global_failures
                or requested & set(target_failures)
                or source_instances is None
                or any(
                    source_instances.get(fingerprint, (None, None))[1]
                    != instance
                    for fingerprint, (_window, instance) in (
                        (by_fingerprint or {}).items()
                    )
                    if fingerprint in requested
                )
            ):
                unsafe_candidate_set = True
        if (
            unsafe_candidate_set
            or by_fingerprint is None
            or not requested.issubset(by_fingerprint)
        ):
            # Missing, ambiguous, incomplete, or conflicting requested roles
            # invalidate this entire passive source before its first capture.
            # Advancing the generation prevents an older observer from later
            # publishing any stale state over this UNKNOWN result.
            self._revoke_source_failure_evidence(
                frozenset(requested),
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            return {
                fingerprint: ReconnectScreenState.UNKNOWN
                for fingerprint in requested
            }
        observation_source_generation = (
            self._source_state_generation_snapshot()
        )
        observed: dict[str, ReconnectScreenState] = {}
        observed_routes: dict[str, str | None] = {}
        fresh_instances: dict[
            str,
            tuple[WindowInstanceToken, str],
        ] = {}
        for fingerprint in requested:
            window, initial_instance = by_fingerprint[fingerprint]
            (
                sample,
                recognition,
                fresh_capture,
                route,
            ) = self._capture_and_recognize(
                window,
                fingerprint,
                expected_source_state_generation=(
                    observation_source_generation
                ),
            )
            observed_routes[fingerprint] = route
            if (
                fresh_capture
                and initial_instance is not None
                and route is not None
            ):
                fresh_instances[fingerprint] = (initial_instance, route)
            _, current_revision = self._capture_settings_snapshot()
            if current_revision != observation_revision:
                observed[fingerprint] = ReconnectScreenState.UNKNOWN
            elif recognition.state is ReconnectScreenState.CHECK_DISABLED:
                observed[fingerprint] = recognition.state
            elif (
                fresh_capture
                and recognition.state is not ReconnectScreenState.UNKNOWN
            ):
                observed[fingerprint] = recognition.state
            elif self._recent_trusted_connected_state(
                window,
                fingerprint,
                route,
                observation_revision,
            ):
                observed[fingerprint] = ReconnectScreenState.CONNECTED
            else:
                observed[fingerprint] = ReconnectScreenState.UNKNOWN
        if recheck_live_authority:
            final_windows, final_global_failures, final_target_failures = (
                self._candidate_window_set()
            )
            final_instances = self._unique_complete_candidate_instances(
                final_windows,
                instance_bound=instance_bound,
            )
            if (
                final_global_failures
                or requested & set(final_target_failures)
                or final_instances is None
                or any(
                    final_instances.get(fingerprint, (None, None))[1]
                    != instance
                    for fingerprint, (_window, instance) in by_fingerprint.items()
                    if fingerprint in requested
                )
            ):
                self._revoke_source_failure_evidence(
                    frozenset(requested),
                    revoke_runtime_authority=True,
                    refresh_source_generation=True,
                )
                return {
                    fingerprint: ReconnectScreenState.UNKNOWN
                    for fingerprint in requested
                }
        final_settings, final_revision = self._capture_settings_snapshot()
        # Keep the source generation and the published observation in one
        # source-authority -> screen-state order.  A completed revocation
        # cannot be followed by an older observer publishing its stale frame.
        with self._source_authority_lock:
            source_generation_changed = (
                self._source_state_generation
                != observation_source_generation
            )
            with self._screen_state_lock:
                if final_revision != observation_revision:
                    observed = {
                        # The capture result belongs to an older setting
                        # revision. A newly disabled route cannot describe
                        # that stale capture as currently disabled.
                        fingerprint: ReconnectScreenState.UNKNOWN
                        for fingerprint in observed
                    }
                    for fingerprint in fresh_instances:
                        self._trusted_connected_evidence.pop(
                            fingerprint,
                            None,
                        )
                elif source_generation_changed:
                    # A source-generation change invalidates the complete
                    # observation premise, not only online evidence.  The
                    # sole exception is a current, explicit disabled setting.
                    observed = {
                        fingerprint: (
                            ReconnectScreenState.CHECK_DISABLED
                            if state is ReconnectScreenState.CHECK_DISABLED
                            else ReconnectScreenState.UNKNOWN
                        )
                        for fingerprint, state in observed.items()
                    }
                    for fingerprint in fresh_instances:
                        self._trusted_connected_evidence.pop(
                            fingerprint,
                            None,
                        )
                else:
                    observed_at = time.monotonic()
                    for fingerprint, state in observed.items():
                        if state is not ReconnectScreenState.CONNECTED:
                            self._trusted_connected_evidence.pop(
                                fingerprint,
                                None,
                            )
                    for fingerprint, (
                        instance,
                        route,
                    ) in fresh_instances.items():
                        if (
                            observed.get(fingerprint)
                            is ReconnectScreenState.CONNECTED
                        ):
                            self._trusted_connected_evidence[fingerprint] = (
                                _TrustedConnectedEvidence(
                                    instance,
                                    route,
                                    observation_revision,
                                    observed_at,
                                )
                            )
                        else:
                            self._trusted_connected_evidence.pop(
                                fingerprint,
                                None,
                            )
                self._last_screen_states.update(observed)
        return observed

    def observe_window_instance_states(
        self,
        candidate_windows: Iterable[WindowInfo],
    ) -> dict[str, ReconnectScreenState]:
        """Observe contract-resolved instances without collapsing shared images."""

        candidates = tuple(candidate_windows)
        requested = tuple(
            fingerprint
            for window in candidates
            if isinstance(window, WindowInfo)
            and (
                fingerprint := monitored_window_instance_fingerprint(window)
            ) is not None
        )
        if (
            len(requested) != len(candidates)
            or len(requested) != len(set(requested))
        ):
            return {
                fingerprint: ReconnectScreenState.UNKNOWN
                for fingerprint in requested
            }
        return self.observe_screen_states(
            requested,
            candidate_windows=candidates,
            instance_bound=True,
        )

    def reconnecting_fingerprints(self) -> frozenset[str]:
        # This path is queried by high-frequency input synchronizers. Return
        # an immutable snapshot published at scan/group boundaries so it never
        # waits for 14-window recognition or iterates a concurrently mutated
        # set.
        with self._screen_state_lock:
            return self._published_reconnecting_fingerprints

    def _publish_reconnecting_fingerprints(
        self,
        now: float | None = None,
        *,
        observed_fingerprints: frozenset[str] | None = None,
    ) -> None:
        with self._screen_state_lock:
            current = self._monotonic_clock() if now is None else now
            active = {
                fingerprint
                for fingerprint in self._active_automation_fingerprints
                if self._active_automation_until.get(fingerprint, current)
                > current
            }
            self._published_reconnecting_fingerprints = frozenset(
                self._pending_reconnect_fingerprints
                | self._pending_reopen_fingerprints
                | active
                | (observed_fingerprints or frozenset())
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
        with self._scan_lock:
            self._expected_windows = expected_windows

    def set_allowed_fingerprints(
        self,
        fingerprints: Iterable[str] | None,
    ) -> None:
        if fingerprints is None:
            with self._scan_lock:
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
        with self._scan_lock:
            self._allowed_fingerprints = frozenset(normalized)

    @staticmethod
    def _snapshot_failure(
        code: str,
        message: str,
        failure_code: str,
    ) -> OperationResult:
        return OperationResult(
            False,
            code,
            message,
            {"failure_codes": [failure_code], "window_count": 0},
        )

    @staticmethod
    def _same_live_instance_identity(
        first: WindowInstanceToken,
        second: WindowInstanceToken,
    ) -> bool:
        """Compare immutable identity while allowing safe geometry changes."""
        return (
            first.handle == second.handle
            and first.process_id == second.process_id
            and first.thread_id == second.thread_id
            and first.window_class == second.window_class
            and first.process_lifecycle_token
            == second.process_lifecycle_token
        )

    def _verified_group_activation_snapshot(
        self,
        resolved: object,
        complete_instances: dict[
            str,
            tuple[WindowInfo, WindowInstanceToken],
        ],
        source_fingerprints: dict[str, str],
    ) -> tuple[
        dict[str, tuple[WindowInfo, WindowInstanceToken]],
        dict[str, str],
        int,
    ] | None:
        """Prove each required plan entry without comparing fingerprint sets."""

        plan = self._group_launch_plan
        if (
            not isinstance(resolved, ResolvedTargetWindows)
            or plan is None
            or not plan.targets
            or resolved.global_failure_codes
        ):
            return None

        targets = tuple(plan.targets)
        targets_by_entry = {}
        for target in targets:
            if (
                not target.entry_id
                or target.entry_id in targets_by_entry
            ):
                return None
            targets_by_entry[target.entry_id] = target

        evidence_by_entry = {}
        for evidence in resolved.target_failure_evidence:
            target = targets_by_entry.get(evidence.entry_id)
            if (
                target is None
                or evidence.entry_id in evidence_by_entry
                or evidence.failure_codes != ("window_offline",)
                or evidence.candidate_windows != ()
                or normalize_launch_fingerprint(evidence.fingerprint)
                != target.fingerprint
            ):
                return None
            evidence_by_entry[evidence.entry_id] = evidence

        plan_entry_ids = tuple(target.entry_id for target in targets)
        required_targets = tuple(
            target
            for target in targets
            if target.entry_id not in evidence_by_entry
        )
        required_entry_ids = tuple(
            target.entry_id for target in required_targets
        )
        if (
            tuple(resolved.sync_scope_entry_ids) != plan_entry_ids
            or tuple(resolved.sync_entry_ids) != required_entry_ids
            or len(resolved.windows) != len(required_targets)
            or len(complete_instances)
            != len(required_targets) + len(resolved.detection_only_windows)
            or len(source_fingerprints) != len(complete_instances)
        ):
            return None

        verified_instances = {}
        verified_sources: dict[str, str] = {}
        detection_only: set[str] = set()
        for target in required_targets:
            matches = tuple(
                (monitor_fingerprint, candidate)
                for monitor_fingerprint, candidate
                in complete_instances.items()
                if (
                    source_fingerprints.get(monitor_fingerprint)
                    == target.fingerprint
                    and self._tcp_id(
                        resolved,
                        target.fingerprint,
                        candidate[1],
                    ) == target.entry_id
                )
            )
            if len(matches) != 1 or matches[0][0] in verified_instances:
                return None
            monitor_fingerprint, candidate = matches[0]
            verified_instances[monitor_fingerprint] = candidate
            verified_sources[monitor_fingerprint] = target.fingerprint
            if not target.role_id:
                detection_only.add(monitor_fingerprint)
        for window in resolved.detection_only_windows:
            source_fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            instance = WindowInstanceToken.from_window(window)
            if source_fingerprint is None or instance is None:
                return None
            matches = tuple(
                (monitor_fingerprint, candidate)
                for monitor_fingerprint, candidate in complete_instances.items()
                if (
                    monitor_fingerprint not in verified_instances
                    and source_fingerprints.get(monitor_fingerprint)
                    == source_fingerprint
                    and candidate[1] == instance
                )
            )
            if len(matches) != 1:
                return None
            monitor_fingerprint, candidate = matches[0]
            detection_only.add(monitor_fingerprint)
            verified_instances[monitor_fingerprint] = candidate
            verified_sources[monitor_fingerprint] = source_fingerprint
        if len(verified_instances) != len(complete_instances):
            return None
        return (
            verified_instances,
            verified_sources,
            frozenset(detection_only),
            len(evidence_by_entry),
        )

    def prepare_execution_snapshot(self) -> OperationResult:
        """Lock this activation to the complete game windows open right now."""
        with self._scan_lock:
            if self._capture_access_preparer is not None:
                try:
                    capture_access_ready = (
                        self._capture_access_preparer() is True
                    )
                except Exception:
                    capture_access_ready = False
                if not capture_access_ready:
                    return self._snapshot_failure(
                        "reconnect.snapshot_capture_access_denied",
                        "Windows 無彩框背景擷取權限未允許，智慧重連未啟用。",
                        "borderless_capture_access_denied",
                    )
            if not self._evidence_available():
                return self._snapshot_failure(
                    "reconnect.snapshot_evidence_unavailable",
                    "智慧重連匿名紀錄無法使用，已停止所有自動操作。",
                    "evidence_recording_unavailable",
                )
            if self._execution_allowed():
                return self._snapshot_failure(
                    "reconnect.snapshot_execution_open",
                    "智慧重連仍在執行，無法建立新的視窗快照。",
                    "execution_gate_open",
                )
            (
                candidate_windows,
                global_failures,
                target_failures,
            ) = self._candidate_window_set()
            if global_failures:
                return self._snapshot_failure(
                    "reconnect.snapshot_source_failed",
                    "目前無法安全讀取遊戲視窗，沒有啟用智慧重連。",
                    global_failures[0],
                )
            if not candidate_windows:
                return self._snapshot_failure(
                    "reconnect.snapshot_empty",
                    "目前沒有可安全監看的遊戲視窗，沒有啟用智慧重連。",
                    "snapshot_empty",
                )
            (
                complete_instances,
                source_fingerprints,
                isolated_window_count,
            ) = self._activation_snapshot_candidate_instances(
                candidate_windows
            )
            if not complete_instances:
                return self._snapshot_failure(
                    "reconnect.snapshot_identity_unsafe",
                    "目前遊戲視窗不完整或身分重複，沒有啟用智慧重連。",
                    "window_identity_unsafe",
                )
            plan = self._group_launch_plan
            contract_isolated_window_count = len(target_failures)
            if plan is not None:
                verified = self._verified_group_activation_snapshot(
                    self._tcp_v,
                    complete_instances,
                    source_fingerprints,
                )
                if verified is None:
                    return self._snapshot_failure(
                        "reconnect.snapshot_identity_unsafe",
                        "The selected group does not match the safe target contract.",
                        "window_identity_unsafe",
                    )
                (
                    complete_instances,
                    source_fingerprints,
                    detection_only_fingerprints,
                    contract_isolated_window_count,
                ) = verified
            else:
                detection_only_fingerprints = frozenset()
            instance_index: dict[
                tuple[str, int, int, int, str, int], str
            ] = {}
            for monitor_fingerprint, (_window, instance) in (
                complete_instances.items()
            ):
                instance_key = self._activation_instance_key(
                    source_fingerprints.get(monitor_fingerprint),
                    instance,
                )
                if (
                    instance_key is None
                    or instance_key in instance_index
                ):
                    return self._snapshot_failure(
                        "reconnect.snapshot_identity_unsafe",
                        "?桀??閬?銝??湔?頨怠???嚗????冽?折????",
                        "window_identity_unsafe",
                    )
                instance_index[instance_key] = monitor_fingerprint

            self._revoke_capture_authority()
            self._tcp_s.clear()
            self._tcp_online_witnesses.clear()
            self._tcp_timeout_isolated.clear()
            if plan is None:
                self._runtime_scope_token = None
            self._allowed_fingerprints = frozenset(complete_instances)
            self._activation_snapshot_instances = {
                fingerprint: instance
                for fingerprint, (_window, instance)
                in complete_instances.items()
            }
            self._activation_snapshot_source_fingerprints = (
                source_fingerprints
            )
            self._activation_snapshot_instance_index = instance_index
            self._activation_snapshot_direct_identity_collisions = frozenset()
            self._detection_only_fingerprints = detection_only_fingerprints
            self._pending_reopen_fingerprints.clear()
            self._reopen_retry_after.clear()
            self._auto_battle_evidence.clear()
            with self._source_authority_lock:
                self._source_state_generation += 1
                source_state_generation = self._source_state_generation
                self._source_revoked_fingerprints.clear()
            _settings, capture_settings_revision = (
                self._capture_settings_snapshot()
            )
            expires_at = (
                self._monotonic_clock()
                + INITIAL_LOGIN_AUTHORIZATION_SECONDS
            )
            self._initial_login_authorizations = {
                fingerprint: _InitialLoginAuthorization(
                    instance,
                    capture_settings_revision,
                    source_state_generation,
                    expires_at,
                )
                for fingerprint, instance
                in self._activation_snapshot_instances.items()
                if fingerprint not in self._detection_only_fingerprints
            }
            retired_tombstone = self._retirable_tcp_recovery_tombstone(
                complete_instances,
                source_fingerprints,
            )
            if retired_tombstone is not None:
                self._tcp_recovery_authority = None
            if (
                not self._persist_runtime_state()
                and retired_tombstone is not None
            ):
                # Memory must remain at least as restrictive as the durable
                # consumed-launch tombstone when its retirement cannot be
                # committed atomically.
                self._tcp_recovery_authority = retired_tombstone
            return OperationResult(
                True,
                "reconnect.snapshot_ready",
                "智慧重連已鎖定目前開啟的安全遊戲視窗。",
                {
                    "failure_codes": [],
                    "window_count": len(complete_instances),
                    "isolated_window_count": (
                        isolated_window_count
                        + contract_isolated_window_count
                    ),
                },
            )

    def _retirable_tcp_recovery_tombstone(
        self,
        complete_instances: dict[
            str,
            tuple[WindowInfo, WindowInstanceToken],
        ],
        source_fingerprints: dict[str, str],
    ) -> _TcpRecoveryAuthority | None:
        """Prove a consumed reboot tombstone obsolete from one full activation."""

        authority = self._tcp_recovery_authority
        plan = self._group_launch_plan
        resolved = self._tcp_v
        if (
            authority is None
            or authority.stage is not _TcpRecoveryStage.CANCELLED
            or not authority.shortcut_consumed
            or not authority.restored_tombstone
            or authority.reopen_worker_unreaped
            or plan is None
            or not plan.ready
            or not isinstance(resolved, ResolvedTargetWindows)
        ):
            return None
        target = self._target_for_entry(authority.entry_id)
        if target is None:
            return None
        matches = tuple(
            instance
            for monitor_fingerprint, (_window, instance)
            in complete_instances.items()
            if (
                source_fingerprints.get(monitor_fingerprint)
                == target.fingerprint
                and self._tcp_id(
                    resolved,
                    target.fingerprint,
                    instance,
                )
                == authority.entry_id
            )
        )
        if len(matches) != 1 or matches[0] == authority.old_instance:
            return None
        return authority

    def _initial_login_authorization_is_current(
        self,
        fingerprint: str,
        instance: WindowInstanceToken | None,
        capture_settings_revision: int,
        source_state_generation: int,
        now: float | None = None,
    ) -> bool:
        authorization = self._initial_login_authorizations.get(fingerprint)
        if authorization is None or instance is None:
            return False
        current_time = self._monotonic_clock() if now is None else now
        if current_time >= authorization.expires_at:
            self._initial_login_authorizations.pop(fingerprint, None)
            self._clear_action_confirmation(fingerprint)
            return False
        return (
            self._execution_allowed()
            and authorization.instance == instance
            and authorization.capture_settings_revision
            == capture_settings_revision
            and authorization.source_state_generation
            == source_state_generation
            and self._source_authority_is_current(
                authorization.source_state_generation
            )
        )

    def set_group_launch_plan(self, plan: GroupLaunchPlan | None) -> None:
        with self._scan_lock:
            previous_plan = self._group_launch_plan
            previous_scope = self._allowed_fingerprints
            previous_token = self._runtime_scope_token
            if plan is None:
                # Clearing the selected-group identity is a hard revocation,
                # not an ordinary monitor stop.  No old snapshot or TCP
                # observation may remain executable after the outer gate is
                # reopened.
                self.set_execution_enabled(False)
                self._group_launch_plan = None
                self.set_allowed_fingerprints(None)
                self._runtime_scope_token = None
                if (
                    previous_plan is not None
                    or previous_scope is not None
                    or previous_token is not None
                ):
                    self._retain_runtime_scope(frozenset())
                return
            if not isinstance(plan, GroupLaunchPlan) or not plan.ready:
                raise ValueError("plan must be a ready GroupLaunchPlan.")
            scope_token = self._group_scope_token(plan)
            self._group_launch_plan = plan
            self._detection_only_fingerprints = frozenset()
            self.set_allowed_fingerprints(plan.fingerprints)
            self._runtime_scope_token = scope_token
            if previous_token != scope_token:
                # A group switch is a new reconnect context even when the two
                # groups share one role or the entire fingerprint set.
                self._retain_runtime_scope(frozenset())
                self._initial_login_authorizations.clear()
                self._tcp_s.clear()
                self._tcp_online_witnesses.clear()
                self._tcp_timeout_isolated.clear()
            elif previous_scope != self._allowed_fingerprints:
                self._retain_runtime_scope(self._allowed_fingerprints)

    @staticmethod
    def _group_scope_token(plan: GroupLaunchPlan) -> str:
        anonymous_scope = {
            "group": plan.group_name.strip().casefold(),
            "targets": [
                {
                    "fingerprint": target.fingerprint,
                    "entry": target.entry_id.strip().casefold(),
                    "role": target.role_id.strip().casefold(),
                }
                for target in plan.targets
            ],
        }
        encoded = json.dumps(
            anonymous_scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _retain_runtime_scope(self, fingerprints: frozenset[str]) -> None:
        """Revoke reconnect authority that belongs to a previous group."""
        with self._screen_state_lock:
            tracked = (
                self._pending_reconnect_fingerprints
                | self._active_automation_fingerprints
                | self._pending_reopen_fingerprints
                | self._character_selection_pending
                | set(self._character_selection_targets)
                | set(self._active_automation_until)
                | set(self._action_retry_after)
                | set(self._reopen_retry_after)
                | set(self._action_state_since)
                | set(self._flow_pause_until)
                | set(self._action_confirmations)
                | set(self._terminal_ready_after)
                | set(self._terminal_evidence)
                | set(self._last_screen_states)
                | set(self._last_trusted_capture_routes)
                | set(self._trusted_connected_evidence)
                | set(self._recent_login_role_ids)
                | set(self._auto_battle_confirmed_instances)
                | set(self._force_login_timeout_attempts)
                | self._primary_entry_authorized
                | self._primary_connected_fingerprints
                | self._reconnect_entry_authorized
                | self._login_only_recovery_fingerprints
                | {
                    fingerprint
                    for fingerprint, _lifecycle
                    in self._reconnect_timing_flows
                }
            )
            removed = tracked - fingerprints
            self._pending_reconnect_fingerprints.intersection_update(fingerprints)
            self._active_automation_fingerprints.intersection_update(fingerprints)
            self._pending_reopen_fingerprints.intersection_update(fingerprints)
            self._character_selection_pending.intersection_update(fingerprints)
            self._primary_entry_authorized.intersection_update(fingerprints)
            self._primary_connected_fingerprints.intersection_update(
                fingerprints
            )
            self._reconnect_entry_authorized.intersection_update(
                fingerprints
            )
            self._login_only_recovery_fingerprints.intersection_update(
                fingerprints
            )
            self._tcp_timeout_isolated.intersection_update(fingerprints)
            for key in tuple(self._tcp_online_witnesses):
                if key[1] not in fingerprints:
                    self._tcp_online_witnesses.pop(key, None)
            for mapping in (
                self._active_automation_until,
                self._action_retry_after,
                self._reopen_retry_after,
                self._action_state_since,
                self._flow_pause_until,
                self._action_confirmations,
                self._terminal_ready_after,
                self._terminal_evidence,
                self._character_selection_targets,
                self._last_screen_states,
                self._last_trusted_capture_routes,
                self._trusted_connected_evidence,
                self._recent_login_role_ids,
                self._auto_battle_confirmed_instances,
                self._force_login_timeout_attempts,
            ):
                for fingerprint in removed:
                    mapping.pop(fingerprint, None)
            for key in tuple(self._reconnect_timing_flows):
                if key[0] in removed:
                    self._reconnect_timing_flows.pop(key, None)
        for fingerprint in removed:
            self._clear_reconnect_failure(fingerprint)
        self._publish_reconnecting_fingerprints()
        self._persist_runtime_state()

    def set_execution_enabled(self, enabled: bool) -> None:
        """Allow an active scan to stop before its next game-changing click."""
        if enabled:
            if not self._record_evidence_monitoring_state(True):
                self._execution_enabled.clear()
                return
            self._execution_enabled.set()
            return
        # Close the gate before waiting for an active scan.  Its next final
        # authorization check must fail even while the remaining revocation is
        # waiting for the scan's read-only work to finish.
        self._execution_enabled.clear()
        self._record_evidence_monitoring_state(False)
        with self._scan_lock:
            # A later enable is a new session, never permission to resume an
            # old click, reopen, flow pause, terminal, or capture-route grant.
            self._revoke_capture_authority()
            self._auto_battle_evidence.clear()
            self._auto_battle_button_windows.clear()
            self._auto_battle_attempted_actions.clear()
            self._auto_battle_confirmed_instances.clear()
            self._recent_login_role_ids.clear()
            self._primary_entry_authorized.clear()
            self._primary_connected_fingerprints.clear()
            self._reconnect_entry_authorized.clear()
            self._reconnect_timing_flows.clear()
            self._initial_login_authorizations.clear()
            self._force_login_timeout_attempts.clear()
            self._tcp_s.clear()
            self._tcp_online_witnesses.clear()
            self._tcp_timeout_isolated.clear()
            self._activation_snapshot_instances = None
            self._activation_snapshot_source_fingerprints = None
            self._activation_snapshot_instance_index = None
            self._activation_snapshot_direct_identity_collisions = frozenset()
            self._detection_only_fingerprints = frozenset()
            plan = self._group_launch_plan
            self._allowed_fingerprints = (
                plan.fingerprints if plan is not None else None
            )
            with self._source_authority_lock:
                self._source_state_generation += 1
                self._source_revoked_fingerprints.clear()

    @property
    def auto_battle_enabled(self) -> bool:
        return self._auto_battle_enabled

    def set_auto_battle_enabled(self, enabled: bool) -> None:
        """關閉子開關立即撤銷任何尚未完成的畫面證據。"""
        self._auto_battle_enabled = enabled is True
        if not self._auto_battle_enabled:
            with self._scan_lock:
                self._auto_battle_evidence.clear()
                self._auto_battle_button_windows.clear()
                self._auto_battle_attempted_actions.clear()
                self._auto_battle_confirmed_instances.clear()
                now = self._monotonic_clock()
                for fingerprint in tuple(
                    self._primary_connected_fingerprints
                ):
                    self._complete_reconnect_timing(
                        fingerprint,
                        "disconnect_to_primary_auto",
                        now,
                    )
                    self._primary_entry_authorized.discard(fingerprint)
                    self._primary_connected_fingerprints.discard(
                        fingerprint
                    )

    def auto_battle_execution_allowed(self) -> bool:
        """自動戰鬥只能在智慧重連與子開關均開啟時處理。"""
        return self._execution_allowed() and self._auto_battle_enabled

    @staticmethod
    def _auto_battle_image(sample: object | None):
        """只接受完整、未裁切的擷取，格式不符即拒絕。"""
        if not isinstance(sample, CaptureSample) or not sample.api_succeeded:
            return None
        if sample.width <= 0 or sample.height <= 0:
            return None
        if len(sample.pixels) != sample.width * sample.height * 4:
            return None
        try:
            from PIL import Image
            return Image.frombytes(
                "RGB", (sample.width, sample.height), sample.pixels, "raw", "BGRX"
            )
        except (ValueError, OSError):
            return None

    def _auto_battle_evidence_for_sample(
        self, sample: object | None
    ) -> AutoBattleEvidence:
        recognizer = self._auto_battle_recognizer
        image = self._auto_battle_image(sample)
        if recognizer is None or image is None:
            return AutoBattleEvidence(False, False, False)
        return recognizer.read(image)

    def _auto_battle_snapshot_is_current(
        self,
        fingerprint: str,
        instance: WindowInstanceToken,
    ) -> bool:
        """Require the exact complete instance locked at this activation."""

        snapshot = self._activation_snapshot_instances
        return snapshot is not None and snapshot.get(fingerprint) == instance

    @staticmethod
    def _auto_battle_box_point(
        sample: object | None,
        box: tuple[int, int, int, int],
    ) -> NormalizedPoint | None:
        if (
            not isinstance(sample, CaptureSample)
            or not sample.api_succeeded
            or not isinstance(box, tuple)
            or len(box) != 4
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in box
            )
        ):
            return None
        left, top, right, bottom = box
        if (
            sample.width <= 0
            or sample.height <= 0
            or left < 0
            or top < 0
            or right <= left
            or bottom <= top
            or right > sample.width
            or bottom > sample.height
        ):
            return None
        return (
            (left + right) / 2 / sample.width,
            (top + bottom) / 2 / sample.height,
        )

    @staticmethod
    def _auto_battle_screen_is_allowed(
        item: ScreenRecognition,
        action_kind: str,
    ) -> bool:
        if action_kind == "normal-red-x":
            return bool(
                item.state in _AUTO_BATTLE_GENERAL_STATES
                and not item.battle_context
            )
        if action_kind == "battle-button":
            return bool(
                item.state is ReconnectScreenState.CONNECTED
                and item.battle_context
            )
        return False

    def _run_auto_battle_transaction(
        self,
        *,
        window: WindowInfo,
        fingerprint: str,
        instance: WindowInstanceToken,
        first_sample: object | None,
        first_box: tuple[int, int, int, int],
        capture_route: str,
        capture_settings_revision: int,
        source_state_generation: int,
        action_kind: str,
        first_screen_state: ReconnectScreenState,
        first_battle_context: bool,
    ) -> tuple[bool, bool]:
        first_item = ScreenRecognition(
            state=first_screen_state,
            score=1.0,
            click_point=None,
            reference_name=f"auto_battle:{action_kind}",
            battle_context=first_battle_context,
        )
        if not self._auto_battle_screen_is_allowed(first_item, action_kind):
            return False, False
        point = self._auto_battle_box_point(first_sample, first_box)
        attempt_key = (fingerprint, instance, action_kind)
        if point is None or attempt_key in self._auto_battle_attempted_actions:
            return False, False
        action_item = replace(first_item, click_point=point)
        self._action_is_confirmed(
            fingerprint,
            action_item,
            instance=instance,
            capture_route=capture_route,
            capture_settings_revision=capture_settings_revision,
            source_state_generation=source_state_generation,
        )

        current = self._current_action_window(instance, fingerprint)
        if current is None:
            return False, False
        (
            second_sample,
            second_state,
            second_fresh,
            second_route,
        ) = self._capture_and_recognize(
            current,
            fingerprint,
            execute=True,
            expected_source_state_generation=source_state_generation,
            diagnostic_stage=f"auto_battle_{action_kind}_second_frame",
        )
        second_evidence = self._auto_battle_evidence_for_sample(second_sample)
        second_box = (
            second_evidence.red_x_box
            if action_kind == "normal-red-x"
            else second_evidence.battle_button_box
        )
        if (
            not second_fresh
            or second_route != capture_route
            or second_box != first_box
            or not self._auto_battle_screen_is_allowed(
                second_state,
                action_kind,
            )
            or not self.auto_battle_execution_allowed()
            or self._capture_settings_snapshot()[1]
            != capture_settings_revision
            or not self._auto_battle_snapshot_is_current(
                fingerprint,
                instance,
            )
            or self._current_action_window(instance, fingerprint) is None
            or not self._source_authority_is_current(
                source_state_generation
            )
        ):
            self._clear_action_confirmation(fingerprint)
            return False, False
        second_point = self._auto_battle_box_point(
            second_sample,
            second_box,
        )
        if second_point is None:
            self._clear_action_confirmation(fingerprint)
            return False, False
        second_item = replace(
            second_state,
            click_point=second_point,
            reference_name=f"auto_battle:{action_kind}",
        )
        if not self._action_is_confirmed(
            fingerprint,
            second_item,
            instance=instance,
            capture_route=capture_route,
            capture_settings_revision=capture_settings_revision,
            source_state_generation=source_state_generation,
        ):
            return False, False

        evidence = (
            instance,
            capture_route,
            capture_settings_revision,
            source_state_generation,
            first_box,
        )
        self._auto_battle_evidence[fingerprint] = evidence

        def deliver() -> MouseClickResult | None:
            current_window = self._current_action_window(
                instance,
                fingerprint,
            )
            if (
                not self.auto_battle_execution_allowed()
                or attempt_key in self._auto_battle_attempted_actions
                or self._auto_battle_evidence.get(fingerprint) != evidence
                or not self._auto_battle_snapshot_is_current(
                    fingerprint,
                    instance,
                )
                or current_window is None
                or current_window.minimized
            ):
                return None

            def send_once() -> MouseClickResult:
                if attempt_key in self._auto_battle_attempted_actions:
                    return MouseClickResult(
                        False,
                        True,
                        False,
                        "auto_battle_already_attempted",
                    )
                self._auto_battle_attempted_actions.add(attempt_key)
                return self._mouse_backend.click_relative(
                    instance.handle,
                    second_point,
                    instance.process_id,
                    instance,
                )

            permitted, result = self._run_authorized_backend_call(
                send_once,
                expected_capture_settings_revision=(
                    capture_settings_revision
                ),
                capture_route=capture_route,
                expected_source_state_generation=(
                    source_state_generation
                ),
                additional_authorization_check=(
                    lambda: (
                        self._auto_battle_enabled is True
                        and self._auto_battle_evidence.get(fingerprint)
                        == evidence
                        and self._auto_battle_snapshot_is_current(
                            fingerprint,
                            instance,
                        )
                        and self._reconnect_budget_current(
                            fingerprint,
                            self._monotonic_clock(),
                        )
                    )
                ),
            )
            return (
                result
                if permitted and isinstance(result, MouseClickResult)
                else None
            )

        (
            evidence_ready,
            evidence_intent,
            evidence_authority,
        ) = self._begin_evidence_action(
            fingerprint=fingerprint,
            item=second_item,
            action="auto_battle_click",
            instance=instance,
            capture_route=capture_route,
            capture_settings_revision=capture_settings_revision,
            source_state_generation=source_state_generation,
        )
        if not evidence_ready:
            self._auto_battle_evidence.pop(fingerprint, None)
            return False, False
        permitted, delivered = self._run_game_mutation(
            "auto-battle-click",
            deliver,
            expected_capture_settings_revision=capture_settings_revision,
            capture_route=capture_route,
            expected_source_state_generation=source_state_generation,
        )
        attempted = bool(
            isinstance(delivered, MouseClickResult)
            and (
                delivered.delivered
                or delivered.delivery_uncertain
            )
        )
        if (
            not attempted
            and isinstance(delivered, MouseClickResult)
            and not delivered.delivery_uncertain
        ):
            self._auto_battle_attempted_actions.discard(attempt_key)
        if (
            not permitted
            or not isinstance(delivered, MouseClickResult)
            or not delivered.delivered
        ):
            self._finish_evidence_action(
                fingerprint=fingerprint,
                item=second_item,
                action="auto_battle_click",
                intent_sequence=evidence_intent,
                allowed=permitted,
                performed=bool(permitted and attempted),
                clicked=bool(
                    isinstance(delivered, MouseClickResult)
                    and delivered.delivered
                ),
                identity_verified=True,
                restoration_verified=(
                    delivered.restored
                    if isinstance(delivered, MouseClickResult)
                    else None
                ),
                failure_reason=(
                    delivered.failure_code
                    if isinstance(delivered, MouseClickResult)
                    and delivered.failure_code is not None
                    else "auto_battle_delivery_failed"
                ),
                auto_battle_panel_verified=False,
                authority_signature=evidence_authority,
            )
            self._auto_battle_evidence.pop(fingerprint, None)
            return False, attempted

        current = self._current_action_window(instance, fingerprint)
        if current is None:
            self._finish_evidence_action(
                fingerprint=fingerprint,
                item=second_item,
                action="auto_battle_click",
                intent_sequence=evidence_intent,
                allowed=True,
                performed=True,
                clicked=True,
                identity_verified=False,
                restoration_verified=delivered.restored,
                failure_reason="post_action_identity_changed",
                auto_battle_panel_verified=False,
                authority_signature=evidence_authority,
            )
            self._auto_battle_evidence.pop(fingerprint, None)
            return False, True
        (
            third_sample,
            third_state,
            third_fresh,
            third_route,
        ) = self._capture_and_recognize(
            current,
            fingerprint,
            execute=True,
            expected_source_state_generation=source_state_generation,
            diagnostic_stage=f"auto_battle_{action_kind}_third_frame",
        )
        confirmed = bool(
            third_fresh
            and third_route == capture_route
            and self._auto_battle_screen_is_allowed(
                third_state,
                action_kind,
            )
            and self.auto_battle_execution_allowed()
            and self._capture_settings_snapshot()[1]
            == capture_settings_revision
            and self._auto_battle_evidence_for_sample(
                third_sample
            ).enabled
            and self._auto_battle_snapshot_is_current(
                fingerprint,
                instance,
            )
            and self._current_action_window(instance, fingerprint)
            is not None
            and self._source_authority_is_current(
                source_state_generation
            )
        )
        self._auto_battle_evidence.pop(fingerprint, None)
        self._finish_evidence_action(
            fingerprint=fingerprint,
            item=second_item,
            action="auto_battle_click",
            intent_sequence=evidence_intent,
            allowed=True,
            performed=True,
            clicked=True,
            identity_verified=bool(
                self._current_action_window(instance, fingerprint)
                is not None
            ),
            restoration_verified=delivered.restored,
            failure_reason=(
                None if confirmed else "auto_battle_panel_not_confirmed"
            ),
            auto_battle_panel_verified=confirmed,
            authority_signature=evidence_authority,
        )
        return confirmed, True

    def _run_auto_battle_for_connected(
        self,
        window: WindowInfo,
        fingerprint: str,
        instance: WindowInstanceToken,
        first_sample: object | None,
        capture_route: str | None,
        capture_settings_revision: int,
        source_state_generation: int,
        *,
        first_screen_state: ReconnectScreenState = (
            ReconnectScreenState.CONNECTED
        ),
        first_battle_context: bool = False,
    ) -> bool:
        """Prefer the normal entry, then supplement from battle for 24 seconds."""
        self._auto_battle_evidence.pop(fingerprint, None)
        confirmation_key = (
            instance,
            capture_route,
            capture_settings_revision,
            source_state_generation,
        )
        if (
            first_screen_state not in _AUTO_BATTLE_GENERAL_STATES
            or not self.auto_battle_execution_allowed()
            or capture_route is None
            or not self._capture_authority_is_current(
                capture_settings_revision,
                capture_route,
            )
            or not self._source_authority_is_current(
                source_state_generation
            )
            or not self._auto_battle_snapshot_is_current(
                fingerprint,
                instance,
            )
            or self._current_action_window(instance, fingerprint) is None
        ):
            self._auto_battle_button_windows.pop(fingerprint, None)
            self._auto_battle_confirmed_instances.pop(fingerprint, None)
            return False
        if (
            self._auto_battle_confirmed_instances.get(fingerprint)
            != confirmation_key
        ):
            self._auto_battle_confirmed_instances.pop(fingerprint, None)
        first = self._auto_battle_evidence_for_sample(first_sample)
        if first.enabled:
            if not self._record_auto_battle_panel_verification(
                fingerprint=fingerprint,
                instance=instance,
                sample=first_sample,
            ):
                self._auto_battle_button_windows.pop(fingerprint, None)
                self._auto_battle_confirmed_instances.pop(fingerprint, None)
                return False
            self._auto_battle_button_windows.pop(fingerprint, None)
            self._auto_battle_confirmed_instances[fingerprint] = (
                confirmation_key
            )
            return True

        if (
            first.disabled
            and first.red_x_box is not None
            and not first_battle_context
        ):
            self._auto_battle_confirmed_instances.pop(fingerprint, None)
            self._auto_battle_button_windows.pop(fingerprint, None)
            confirmed, _attempted = self._run_auto_battle_transaction(
                window=window,
                fingerprint=fingerprint,
                instance=instance,
                first_sample=first_sample,
                first_box=first.red_x_box,
                capture_route=capture_route,
                capture_settings_revision=capture_settings_revision,
                source_state_generation=source_state_generation,
                action_kind="normal-red-x",
                first_screen_state=first_screen_state,
                first_battle_context=False,
            )
            if confirmed:
                self._auto_battle_confirmed_instances[fingerprint] = (
                    confirmation_key
                )
            return confirmed

        if (
            first_screen_state is not ReconnectScreenState.CONNECTED
            or not first_battle_context
        ):
            self._auto_battle_button_windows.pop(fingerprint, None)
            return False

        if (
            self._auto_battle_confirmed_instances.get(fingerprint)
            == confirmation_key
        ):
            self._auto_battle_button_windows.pop(fingerprint, None)
            return True

        now = self._monotonic_clock()
        current_window = self._auto_battle_button_windows.get(fingerprint)
        current_evidence = _AutoBattleButtonWindow(
            instance,
            capture_route,
            capture_settings_revision,
            source_state_generation,
            now,
            False,
        )
        if (
            current_window is None
            or current_window.instance != instance
            or current_window.capture_route != capture_route
            or current_window.capture_settings_revision
            != capture_settings_revision
            or current_window.source_state_generation
            != source_state_generation
        ):
            current_window = current_evidence
            self._auto_battle_button_windows[fingerprint] = current_window
        if now - current_window.started_at >= AUTO_BATTLE_BATTLE_WINDOW_SECONDS:
            self._auto_battle_button_windows.pop(fingerprint, None)
            return False
        if (
            current_window.attempted
            or first.battle_button_box is None
        ):
            return False

        confirmed, attempted = self._run_auto_battle_transaction(
            window=window,
            fingerprint=fingerprint,
            instance=instance,
            first_sample=first_sample,
            first_box=first.battle_button_box,
            capture_route=capture_route,
            capture_settings_revision=capture_settings_revision,
            source_state_generation=source_state_generation,
            action_kind="battle-button",
            first_screen_state=first_screen_state,
            first_battle_context=True,
        )
        if attempted:
            self._auto_battle_button_windows[fingerprint] = replace(
                current_window,
                attempted=True,
            )
        if confirmed:
            self._auto_battle_button_windows.pop(fingerprint, None)
            self._auto_battle_confirmed_instances[fingerprint] = (
                confirmation_key
            )
        return confirmed


    def _execution_allowed(self) -> bool:
        """Read the stop gate immediately before every mutating backend call."""
        return bool(
            self._execution_enabled.is_set()
            and self._evidence_available()
        )

    def _run_game_mutation(
        self,
        operation: str,
        callback: Callable[[], object],
        *,
        expected_capture_settings_revision: int | None = None,
        capture_route: str | None = None,
        expected_source_state_generation: int | None = None,
    ) -> tuple[bool, object | None]:
        """Run one game-change transaction under the shared exclusive gate.

        Capture settings are deliberately not held while ``callback`` performs
        read-only preflight work.  Capture routes remember screen evidence,
        so holding that lock here used to invert the screen-state lock order.
        The tiny final backend operation is instead linearized by
        ``_run_authorized_backend_call`` immediately before it is sent.
        """
        if not self._execution_allowed():
            return False, None
        if not self._capture_authority_is_current(
            expected_capture_settings_revision,
            capture_route,
        ):
            return False, None
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            return False, None
        gate = self._operation_gate
        lease = (
            gate.acquire(
                operation,
                execution_guard=self._execution_allowed,
                timeout_seconds=0,
            )
            if gate is not None
            else None
        )
        if gate is not None and lease is None:
            return False, None
        try:
            if not self._execution_allowed():
                return False, None
            return True, callback()
        finally:
            if lease is not None:
                lease.release()

    def _run_authorized_backend_call(
        self,
        callback: Callable[[], object],
        *,
        expected_capture_settings_revision: int | None,
        capture_route: str | None,
        expected_source_state_generation: int | None,
        additional_authorization_check: Callable[[], bool] | None = None,
    ) -> tuple[bool, object | None]:
        """Linearize one final backend mutation with capture settings.

        Callers must do all enumeration, capture, and screen-state checks
        before this method.  Keeping this critical section free of those
        operations gives the module one lock direction: capture settings are
        never held while the screen-state lock can be acquired.
        """
        # Hold the source authority generation across the backend call.  A
        # completed source revocation therefore linearizes before an older
        # scan can reach this final mutation boundary.  The only nested lock
        # is source-authority -> capture-settings; no path takes the reverse
        # order or involves the screen-state lock here.
        with self._source_authority_lock:
            if (
                expected_source_state_generation is not None
                and self._source_state_generation
                != expected_source_state_generation
            ):
                return False, None
            with self._capture_settings_lock:
                if (
                    expected_capture_settings_revision is not None
                    and self._capture_settings_revision
                    != expected_capture_settings_revision
                ):
                    return False, None
                if not self._capture_route_enabled(
                    self._capture_settings,
                    capture_route,
                ):
                    return False, None
                if not self._execution_allowed():
                    return False, None
                if additional_authorization_check is not None:
                    try:
                        additionally_authorized = (
                            additional_authorization_check() is True
                        )
                    except Exception:
                        additionally_authorized = False
                    if not additionally_authorized:
                        return False, None
                return True, callback()

    def _observe_tcp_counts(
        self,
        states: Iterable[_TcpState],
        _requested_at: float | None = None,
    ) -> tuple[dict[int, int] | None, int | None, float | None]:
        """Run one fresh provider query and publish only anonymous facts."""

        state_values = tuple(states)
        provider = self._tcp_counts
        if provider is None:
            self._tcp_observation = _TcpObservation(
                generation=self._tcp_gen,
                observed_at_monotonic=None,
                query_succeeded=False,
                observed_window_count=len(state_values),
            )
            return None, None, None
        self._tcp_gen += 1
        generation = self._tcp_gen
        process_ids = frozenset(
            state.instance.process_id for state in state_values
        )
        try:
            counts = provider(process_ids)
        except Exception:
            counts = None
        completed_at = self._monotonic_clock()
        valid = isinstance(counts, dict) and all(
            type(counts.get(process_id)) is int
            and counts[process_id] >= 0
            for process_id in process_ids
        )
        if not valid:
            self._tcp_observation = _TcpObservation(
                generation=generation,
                observed_at_monotonic=completed_at,
                query_succeeded=False,
                observed_window_count=len(state_values),
            )
            return None, generation, completed_at
        zero_count = sum(
            counts[state.instance.process_id] == 0
            for state in state_values
        )
        self._tcp_observation = _TcpObservation(
            generation=generation,
            observed_at_monotonic=completed_at,
            query_succeeded=True,
            observed_window_count=len(state_values),
            zero_window_count=zero_count,
        )
        return counts, generation, completed_at

    def _tcp_failures(
        self,
        windows: tuple[WindowInfo, ...],
        now: float,
    ) -> tuple[
        tuple[str, ...],
        tuple[tuple[str, _TcpState], ...],
        float | None,
    ]:
        """Track each complete entry/window token independently.

        A missing, malformed, negative, or failed query is UNKNOWN for the
        entire fresh observation round.  It breaks every zero sequence but can
        never promote a disconnect.  Simultaneous zeroes remain independent;
        ownership is selected later by the ordered launch plan.
        """

        self._retain_tcp_online_witnesses_for_contract(self._tcp_v)
        live: dict[tuple[str, WindowInstanceToken], _TcpState] = {}
        fingerprints: dict[tuple[str, WindowInstanceToken], str] = {}
        for window in windows:
            fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
            token = WindowInstanceToken.from_window(window)
            if fingerprint is None or token is None:
                continue
            entry_id = self._tcp_id(self._tcp_v, fingerprint, token)
            if (
                entry_id is None
                and self._group_launch_plan is not None
                and fingerprint not in self._detection_only_fingerprints
            ):
                continue
            # Read-only global monitoring keeps a token-local anonymous key;
            # it is never an entry_id and _ordered_tcp_owner therefore cannot
            # turn it into a restart target.
            key = (entry_id or f"observation:{fingerprint}", token)
            state = self._tcp_s.get(key)
            if state is None:
                state = _TcpState(token, entry_id)
                if entry_id is not None:
                    witness_key = (entry_id, fingerprint)
                    instance_identity = self._tcp_online_instance_identity(token)
                    witnessed_identity = self._tcp_online_witnesses.get(
                        witness_key
                    )
                    if witnessed_identity == instance_identity:
                        state.online = True
                    elif witnessed_identity is not None:
                        # A new HWND/process/thread/class/lifecycle is never the
                        # previously-online old instance, even if a fingerprint
                        # or entry is reused later.
                        self._tcp_online_witnesses.pop(witness_key, None)
            live[key] = state
            fingerprints[key] = fingerprint
        self._tcp_s = live
        counts, generation, observed_at = self._observe_tcp_counts(
            live.values(),
            now,
        )
        if counts is None or generation is None:
            for state in live.values():
                state.zero_since = None
                state.zero_count = 0
                state.gen = generation or self._tcp_gen
            return (
                ("tcp_observation_unavailable",)
                if self._tcp_counts is not None
                else (),
                (),
                observed_at,
            )

        confirmed: list[tuple[str, _TcpState]] = []
        suspected = False
        for key, state in live.items():
            fingerprint = fingerprints[key]
            count = counts[state.instance.process_id]
            if count > 0:
                state.online = True
                if state.entry_id is not None:
                    self._tcp_online_witnesses[
                        (state.entry_id, fingerprint)
                    ] = self._tcp_online_instance_identity(state.instance)
                state.zero_since = None
                state.zero_count = 0
                state.gen = generation
                self._tcp_timeout_isolated.discard(fingerprint)
                if (
                    self._battle_restart_attempts.get((fingerprint, True))
                    == _BattleRestartEvent.from_instance(state.instance)
                ):
                    self._battle_restart_attempts.pop((fingerprint, True), None)
                continue
            if not state.online:
                state.zero_since = None
                state.zero_count = 0
                state.gen = generation
                continue
            if state.zero_since is None or state.gen != generation - 1:
                state.zero_since = observed_at
                state.zero_count = 1
            else:
                state.zero_count += 1
            state.gen = generation
            suspected = True
            if (
                state.zero_count >= _TCP_N
                and state.zero_since is not None
                and observed_at is not None
                and observed_at - state.zero_since >= _TCP_T
            ):
                confirmed.append((fingerprint, state))
        self._tcp_observation = replace(
            self._tcp_observation,
            confirmed_window_count=len(confirmed),
        )
        if confirmed:
            return (
                ("tcp_disconnect_confirmed",),
                tuple(confirmed),
                observed_at,
            )
        return (
            (("tcp_disconnect_suspected",) if suspected else ()),
            (),
            observed_at,
        )

    @staticmethod
    def _tcp_online_instance_identity(
        instance: WindowInstanceToken,
    ) -> tuple[int, int, int, str, int]:
        """Return the immutable process/window identity used by TCP baseline."""

        return (
            instance.handle,
            instance.process_id,
            instance.thread_id,
            instance.window_class,
            instance.process_lifecycle_token,
        )

    def _retain_tcp_online_witnesses_for_contract(
        self,
        resolved: object,
    ) -> None:
        """Retain witnesses only for the same formal entry and core instance."""

        plan = self._group_launch_plan
        if (
            not isinstance(resolved, ResolvedTargetWindows)
            or plan is None
            or tuple(resolved.sync_scope_entry_ids)
            != tuple(target.entry_id for target in plan.targets)
            or len(resolved.windows) != len(resolved.sync_entry_ids)
        ):
            self._tcp_online_witnesses.clear()
            return
        current: dict[tuple[str, str], tuple[int, int, int, str, int]] = {}
        for entry_id, window in zip(
            resolved.sync_entry_ids,
            resolved.windows,
        ):
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            token = WindowInstanceToken.from_window(window)
            if not entry_id or fingerprint is None or token is None:
                self._tcp_online_witnesses.clear()
                return
            key = (entry_id, fingerprint)
            if key in current:
                self._tcp_online_witnesses.clear()
                return
            current[key] = self._tcp_online_instance_identity(token)
        for key, witnessed_identity in tuple(
            self._tcp_online_witnesses.items()
        ):
            if current.get(key) == witnessed_identity:
                continue
            matching_failures = tuple(
                evidence
                for evidence in resolved.target_failure_evidence
                if (
                    evidence.entry_id == key[0]
                    and evidence.fingerprint == key[1]
                    and evidence.failure_codes
                    == ("window_identity_duplicate",)
                )
            )
            if len(matching_failures) == 1:
                candidate_identities: list[
                    tuple[int, int, int, str, int]
                ] = []
                candidates_are_exact_fingerprint = True
                for candidate in matching_failures[0].candidate_windows:
                    candidate_token = WindowInstanceToken.from_window(candidate)
                    if (
                        normalize_launch_fingerprint(
                            candidate.launch_fingerprint
                        )
                        != key[1]
                        or candidate_token is None
                    ):
                        candidates_are_exact_fingerprint = False
                        break
                    candidate_identities.append(
                        self._tcp_online_instance_identity(candidate_token)
                    )
                if (
                    candidates_are_exact_fingerprint
                    and len(candidate_identities) >= 2
                    and candidate_identities.count(witnessed_identity) == 1
                ):
                    # The configured old instance is still present among an
                    # ambiguous duplicate set.  Keep only its prior online fact;
                    # the unsafe round still clears every zero/confirmation.
                    continue
            self._tcp_online_witnesses.pop(key, None)

    def _tcp_id(
        self,
        resolved: object,
        fingerprint: str,
        token: WindowInstanceToken,
    ) -> str | None:
        """Map one safe complete window to its single plan entry.

        Sync needs the controller entry and therefore uses the stricter sync
        collection. TCP recovery instead works from all individually safe
        reconnect targets, so a local sibling/controller failure cannot erase
        another entry's identity proof.
        """

        plan = self._group_launch_plan
        global_failures, target_failures = (
            self._contract_failure_evidence(resolved)
            if isinstance(resolved, ResolvedTargetWindows)
            else ((), {})
        )
        if (
            not isinstance(resolved, ResolvedTargetWindows)
            or plan is None
            or not plan.targets
            or global_failures
        ):
            return None
        entry_ids = tuple(target.entry_id for target in plan.targets)
        if (
            any(not entry_id for entry_id in entry_ids)
            or len(set(entry_ids)) != len(entry_ids)
            or tuple(resolved.sync_scope_entry_ids) != entry_ids
            or fingerprint in target_failures
        ):
            return None
        targets = tuple(
            target
            for target in plan.targets
            if target.fingerprint == fingerprint and target.entry_id
        )
        if len(targets) != 1:
            return None
        target = targets[0]
        matches = tuple(
            window
            for window in resolved.windows
            if (
                normalize_launch_fingerprint(window.launch_fingerprint)
                == fingerprint
                and WindowInstanceToken.from_window(window) == token
            )
        )
        return target.entry_id if len(matches) == 1 else None

    def _ordered_tcp_owner(
        self,
        confirmed: tuple[tuple[str, _TcpState], ...],
    ) -> tuple[str | None, _TcpState | None, str | None]:
        """Choose one formal recovery owner in plan order, never a batch."""

        plan = self._group_launch_plan
        if plan is None:
            return None, None, None
        current_authority = self._tcp_recovery_authority
        if current_authority is not None and not current_authority.terminal:
            current_state = next(
                (
                    state
                    for fingerprint, state in confirmed
                    if fingerprint == current_authority.fingerprint
                ),
                None,
            )
            # Once selected, one owner retains the queue head through close,
            # reopen, rebind and screen recovery. Confirmed peers stay queued
            # and can never cancel or overlap the active owner.
            return current_authority.fingerprint, current_state, None
        sessions = tuple(
            target
            for target in plan.targets
            if (
                target.role_id
                and target.fingerprint not in self._detection_only_fingerprints
                and (
                target.fingerprint in self._login_only_recovery_fingerprints
                and self._has_reconnect_session(target.fingerprint)
                )
            )
        )
        if len(sessions) > 1:
            return None, None, "reconnect_owner_ambiguous"
        if len(sessions) == 1:
            return sessions[0].fingerprint, None, None
        confirmed_by_fingerprint = {
            fingerprint: state for fingerprint, state in confirmed
        }
        for target in plan.targets:
            if (
                not target.role_id
                or target.fingerprint in self._detection_only_fingerprints
            ):
                continue
            state = confirmed_by_fingerprint.get(target.fingerprint)
            if state is None or target.fingerprint in self._tcp_timeout_isolated:
                continue
            event = _BattleRestartEvent.from_instance(state.instance)
            if self._battle_restart_attempts.get((target.fingerprint, True)) == event:
                # The old window still exists after this exact TCP event.  Its
                # retry is intentionally deduplicated, so let the next proven
                # owner proceed instead of stalling the queue.
                continue
            return target.fingerprint, state, None
        return None, None, None

    def _activation_direct_identity_collisions(
        self,
        windows: Iterable[WindowInfo],
    ) -> frozenset[str]:
        """Find new collisions against a snapshot's single direct identity."""

        snapshot = self._activation_snapshot_instances
        sources = self._activation_snapshot_source_fingerprints
        if snapshot is None or sources is None or set(sources) != set(snapshot):
            return frozenset()
        source_counts: Counter[str] = Counter()
        for window in windows:
            if not isinstance(window, WindowInfo):
                continue
            source_fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if (
                source_fingerprint is not None
                and WindowInstanceToken.from_window(window) is not None
            ):
                source_counts[source_fingerprint] += 1
        snapshot_source_counts = Counter(sources.values())
        return frozenset(
            source_fingerprint
            for source_fingerprint, count in source_counts.items()
            if (
                count > 1
                and source_fingerprint in snapshot
                and sources.get(source_fingerprint) == source_fingerprint
                and snapshot_source_counts[source_fingerprint] == 1
            )
        )

    def _bind_activation_snapshot_window_set(
        self,
        windows: Iterable[WindowInfo],
        locally_failed_fingerprints: frozenset[str],
    ) -> tuple[tuple[WindowInfo, ...], frozenset[str]]:
        snapshot = self._activation_snapshot_instances
        sources = self._activation_snapshot_source_fingerprints
        index = self._activation_snapshot_instance_index
        candidates = tuple(windows)
        if snapshot is None:
            self._activation_snapshot_direct_identity_collisions = frozenset()
            return candidates, locally_failed_fingerprints
        if (
            sources is None
            or index is None
            or set(sources) != set(snapshot)
            or len(index) != len(snapshot)
            or set(index.values()) != set(snapshot)
        ):
            self._activation_snapshot_direct_identity_collisions = frozenset(
                snapshot
            )
            return (), frozenset(snapshot)
        self._activation_snapshot_direct_identity_collisions = (
            self._activation_direct_identity_collisions(candidates)
        )

        bound: list[WindowInfo] = []
        used: set[str] = set()
        unmatched: list[tuple[WindowInfo, str, WindowInstanceToken]] = []
        candidate_handle_counts = Counter(
            window.handle
            for window in candidates
            if isinstance(window, WindowInfo)
            and isinstance(window.handle, int)
            and not isinstance(window.handle, bool)
            and window.handle > 0
        )
        parsed: list[
            tuple[
                WindowInfo,
                str,
                WindowInstanceToken,
                str | None,
            ]
        ] = []
        for window in candidates:
            if not isinstance(window, WindowInfo):
                continue
            source_fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            instance = WindowInstanceToken.from_window(window)
            if source_fingerprint is None or instance is None:
                continue
            instance_key = self._activation_instance_key(
                source_fingerprint,
                instance,
            )
            monitor_fingerprint = (
                index.get(instance_key)
                if instance_key is not None
                else None
            )
            parsed.append(
                (
                    window,
                    source_fingerprint,
                    instance,
                    monitor_fingerprint,
                )
            )
        monitor_counts = Counter(
            monitor_fingerprint
            for _window, _source, _instance, monitor_fingerprint in parsed
            if monitor_fingerprint is not None
        )
        for (
            window,
            candidate_source_fingerprint,
            instance,
            monitor_fingerprint,
        ) in parsed:
            if candidate_handle_counts[instance.handle] != 1:
                continue
            if (
                monitor_fingerprint is not None
                and monitor_counts[monitor_fingerprint] != 1
            ):
                continue
            if monitor_fingerprint is None:
                unmatched.append(
                    (window, candidate_source_fingerprint, instance)
                )
                continue
            expected = snapshot.get(monitor_fingerprint)
            if (
                expected is None
                or sources.get(monitor_fingerprint)
                != candidate_source_fingerprint
                or monitor_fingerprint in used
                or not self._same_live_instance_identity(expected, instance)
            ):
                continue
            used.add(monitor_fingerprint)
            bound.append(
                replace(
                    window,
                    launch_fingerprint=monitor_fingerprint,
                )
            )

        missing = set(snapshot) - used
        for source_fingerprint in set(sources.values()):
            replacement_candidates = tuple(
                item for item in unmatched if item[1] == source_fingerprint
            )
            replacement_targets = tuple(
                monitor_fingerprint
                for monitor_fingerprint in missing
                if sources.get(monitor_fingerprint) == source_fingerprint
                and self._has_reconnect_session(monitor_fingerprint)
            )
            if (
                len(replacement_candidates) != 1
                or len(replacement_targets) != 1
            ):
                continue
            window, _source_fingerprint, _instance = replacement_candidates[0]
            monitor_fingerprint = replacement_targets[0]
            bound.append(
                replace(
                    window,
                    launch_fingerprint=monitor_fingerprint,
                )
            )
            used.add(monitor_fingerprint)
            missing.discard(monitor_fingerprint)
        mapped_blocked = frozenset(
            {
                monitor_fingerprint
                for monitor_fingerprint, source_fingerprint in sources.items()
                if (
                    monitor_fingerprint in locally_failed_fingerprints
                    or source_fingerprint in locally_failed_fingerprints
                )
            }
            | {
                normalized
                for item in locally_failed_fingerprints
                if (
                    (normalized := normalize_launch_fingerprint(item))
                    is not None
                    and normalized not in sources.values()
                )
            }
        )
        return tuple(bound), mapped_blocked

    def _contract_failure_evidence(
        self,
        resolved: ResolvedTargetWindows,
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
        """Return only fully attributable target failures for local isolation.

        Runtime safety consumes only immutable target evidence and explicit
        global failure codes.  It never infers entry ownership from an
        aggregate report.
        """

        global_failures = list(resolved.global_failure_codes)
        local_failures: dict[str, tuple[str, ...]] = {}
        evidence_identities: dict[tuple[object, ...], str] = {}
        evidence_handles: dict[int, tuple[object, ...]] = {}
        evidence_processes: dict[int, tuple[object, ...]] = {}
        evidence_stable_tokens: dict[tuple[object, ...], tuple[object, ...]] = {}
        plan = self._group_launch_plan
        for evidence in resolved.target_failure_evidence:
            fingerprint = normalize_launch_fingerprint(evidence.fingerprint)
            entry_id = evidence.entry_id.strip()
            failure_codes = tuple(evidence.failure_codes)
            if (
                fingerprint is None
                or not entry_id
                or not failure_codes
                or any(
                    code not in _ISOLATABLE_TARGET_WINDOW_FAILURE_CODES
                    for code in failure_codes
                )
            ):
                global_failures.extend(failure_codes)
                continue
            malformed_candidates = False
            for candidate in evidence.candidate_windows:
                identity = complete_window_instance_identity(candidate)
                if identity is None or identity[0] != fingerprint:
                    malformed_candidates = True
                    break
                previous_entry = evidence_identities.setdefault(
                    identity,
                    entry_id,
                )
                previous_handle = evidence_handles.setdefault(
                    identity[1],
                    identity,
                )
                previous_process = evidence_processes.setdefault(
                    identity[2],
                    identity,
                )
                previous_stable = evidence_stable_tokens.setdefault(
                    identity[:6],
                    identity,
                )
                if (
                    previous_entry != entry_id
                    or previous_handle != identity
                    or previous_process != identity
                    or previous_stable != identity
                ):
                    malformed_candidates = True
                    break
            if malformed_candidates:
                global_failures.extend(failure_codes)
                global_failures.append("target_failure_unattributed")
                continue
            if plan is not None:
                targets = tuple(
                    target
                    for target in plan.targets
                    if (
                        target.entry_id == entry_id
                        and target.fingerprint == fingerprint
                    )
                )
                if len(targets) != 1:
                    global_failures.extend(failure_codes)
                    global_failures.append("target_failure_unattributed")
                    continue
            if fingerprint in local_failures:
                global_failures.extend(failure_codes)
                global_failures.append("target_failure_unattributed")
                continue
            local_failures[fingerprint] = failure_codes
        return tuple(dict.fromkeys(global_failures)), local_failures

    def _target_for_entry(self, entry_id: str):
        plan = self._group_launch_plan
        if plan is None:
            return None
        matches = tuple(
            target for target in plan.targets if target.entry_id == entry_id
        )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _complete_contract_identities(
        windows: Iterable[WindowInfo],
    ) -> tuple[tuple[object, ...], ...] | None:
        identities = tuple(
            complete_window_instance_identity(window) for window in windows
        )
        if any(identity is None for identity in identities):
            return None
        return tuple(identity for identity in identities if identity is not None)

    def _contract_entry_candidates(
        self,
        resolved: ResolvedTargetWindows,
    ) -> tuple[
        dict[str, tuple[WindowInfo, ...]],
        tuple[WindowInfo, ...],
    ] | None:
        """Return the contract's static candidates indexed by real entry id.

        This consumes the immutable per-target evidence directly.  The old
        aggregate fields deliberately cannot be used to guess which sibling
        owns a raw window.
        """

        plan = self._group_launch_plan
        if (
            plan is None
            or resolved.global_failure_codes
            or tuple(resolved.sync_scope_entry_ids)
            != tuple(target.entry_id for target in plan.targets)
            or len(resolved.windows) != len(resolved.sync_entry_ids)
        ):
            return None
        safe_by_entry: dict[str, tuple[WindowInfo, ...]] = {}
        for entry_id, window in zip(
            resolved.sync_entry_ids,
            resolved.windows,
        ):
            if entry_id in safe_by_entry:
                return None
            safe_by_entry[entry_id] = (window,)
        evidence_by_entry = {
            evidence.entry_id: evidence
            for evidence in resolved.target_failure_evidence
        }
        if len(evidence_by_entry) != len(resolved.target_failure_evidence):
            return None
        result: dict[str, tuple[WindowInfo, ...]] = {}
        all_candidates: list[WindowInfo] = []
        for target in plan.targets:
            evidence = evidence_by_entry.get(target.entry_id)
            safe = safe_by_entry.get(target.entry_id)
            if evidence is not None:
                if (
                    evidence.fingerprint != target.fingerprint
                    or safe is not None
                ):
                    return None
                candidates = evidence.candidate_windows
            elif safe is not None:
                if (
                    normalize_launch_fingerprint(safe[0].launch_fingerprint)
                    != target.fingerprint
                ):
                    return None
                candidates = safe
            else:
                return None
            identities = self._complete_contract_identities(candidates)
            if identities is None:
                return None
            result[target.entry_id] = candidates
            all_candidates.extend(candidates)
        detection_only = tuple(resolved.detection_only_windows)
        all_candidates.extend(detection_only)
        identities = self._complete_contract_identities(all_candidates)
        if identities is None:
            return None
        handles = tuple(identity[1] for identity in identities)
        process_ids = tuple(identity[2] for identity in identities)
        stable_tokens = tuple(identity[:6] for identity in identities)
        if (
            len(handles) != len(set(handles))
            or len(process_ids) != len(set(process_ids))
            or len(stable_tokens) != len(set(stable_tokens))
        ):
            return None
        return result, detection_only

    def _pre_close_backend_contract(
        self,
        resolved: ResolvedTargetWindows,
        entry_id: str,
        expected_instance: WindowInstanceToken,
    ) -> tuple[WindowInfo, tuple[WindowInfo, ...]] | None:
        contract = self._contract_entry_candidates(resolved)
        if contract is None:
            return None
        candidates_by_entry, detection_only = contract
        owner_candidates = candidates_by_entry.get(entry_id, ())
        owner_matches = tuple(
            window
            for window in owner_candidates
            if WindowInstanceToken.from_window(window) == expected_instance
        )
        if len(owner_matches) != 1 or len(owner_candidates) != 1:
            return None
        return owner_matches[0], tuple(
            window
            for candidates in candidates_by_entry.values()
            for window in candidates
        ) + detection_only

    def _post_close_backend_contract(
        self,
        before: ResolvedTargetWindows,
        after: ResolvedTargetWindows,
        entry_id: str,
        owner_window: WindowInfo,
    ) -> tuple[WindowInfo, ...] | None:
        before_contract = self._contract_entry_candidates(before)
        after_contract = self._contract_entry_candidates(after)
        if before_contract is None or after_contract is None:
            return None
        before_candidates, before_detection = before_contract
        after_candidates, after_detection = after_contract
        owner_identity = complete_window_instance_identity(owner_window)
        if owner_identity is None:
            return None
        old_owner = before_candidates.get(entry_id, ())
        new_owner = after_candidates.get(entry_id, ())
        if (
            len(old_owner) != 1
            or complete_window_instance_identity(old_owner[0]) != owner_identity
            or new_owner
            or self._complete_contract_identities(before_detection)
            != self._complete_contract_identities(after_detection)
        ):
            return None
        owner_evidence = tuple(
            evidence
            for evidence in after.target_failure_evidence
            if evidence.entry_id == entry_id
        )
        if (
            len(owner_evidence) != 1
            or owner_evidence[0].failure_codes != ("window_offline",)
            or owner_evidence[0].candidate_windows
        ):
            return None
        if set(before_candidates) != set(after_candidates):
            return None
        for candidate_entry, previous in before_candidates.items():
            if candidate_entry == entry_id:
                continue
            current = after_candidates[candidate_entry]
            if self._complete_contract_identities(previous) != (
                self._complete_contract_identities(current)
            ):
                return None
        return tuple(
            window
            for candidate_entry, candidates in after_candidates.items()
            if candidate_entry != entry_id
            for window in candidates
        ) + after_detection

    def _reopen_backend_contract(
        self,
        resolved: ResolvedTargetWindows,
        entry_id: str,
    ) -> tuple[WindowInfo, ...] | None:
        """Return only the exact post-close collection for one missing owner."""

        contract = self._contract_entry_candidates(resolved)
        if contract is None:
            return None
        candidates_by_entry, detection_only = contract
        if candidates_by_entry.get(entry_id) is None:
            return None
        owner_evidence = tuple(
            evidence
            for evidence in resolved.target_failure_evidence
            if evidence.entry_id == entry_id
        )
        if (
            len(owner_evidence) != 1
            or owner_evidence[0].failure_codes != ("window_offline",)
            or owner_evidence[0].candidate_windows
            or candidates_by_entry[entry_id]
        ):
            return None
        return tuple(
            window
            for candidate_entry, candidates in candidates_by_entry.items()
            if candidate_entry != entry_id
            for window in candidates
        ) + detection_only

    def _candidate_window_set(
        self,
    ) -> tuple[
        tuple[WindowInfo, ...],
        tuple[str, ...],
        dict[str, tuple[str, ...]],
    ]:
        self._tcp_v = None
        if self._target_windows_provider is not None:
            try:
                provided = self._target_windows_provider()
                if isinstance(provided, ResolvedTargetWindows):
                    self._tcp_v = provided
                    global_failures, target_failures = (
                        self._contract_failure_evidence(provided)
                    )
                    provided_windows = (
                        *provided.windows,
                        *provided.detection_only_windows,
                    )
                    provided_fingerprints = {
                        fingerprint
                        for window in provided_windows
                        if (
                            fingerprint := normalize_launch_fingerprint(
                                window.launch_fingerprint
                            )
                        )
                        is not None
                    }
                    target_failures.update(
                        {
                            fingerprint: ("recovery_identity_unavailable",)
                            for fingerprint in (
                                self._detection_only_fingerprints
                                - provided_fingerprints
                            )
                        }
                    )
                    windows, blocked = (
                        self._bind_activation_snapshot_window_set(
                            provided_windows,
                            frozenset(target_failures),
                        )
                    )
                    if blocked != frozenset(target_failures):
                        # The activation snapshot has remapped or rejected a
                        # contract-local identity.  It must not be guessed
                        # back into a different entry.
                        global_failures = tuple(
                            dict.fromkeys(
                                (*global_failures, "target_failure_unattributed")
                            )
                        )
                    resolved_fingerprints = {
                        fingerprint
                        for window in windows
                        if (
                            fingerprint := normalize_launch_fingerprint(
                                window.launch_fingerprint
                            )
                        )
                        is not None
                    }
                    expected_fingerprints = set(
                        self._allowed_fingerprints or ()
                    )
                    plan = self._group_launch_plan
                    if plan is not None:
                        expected_fingerprints.update(plan.fingerprints)
                    with self._screen_state_lock:
                        expected_fingerprints.update(
                            fingerprint
                            for fingerprint, state
                            in self._last_screen_states.items()
                            if state is not ReconnectScreenState.UNKNOWN
                        )
                        expected_fingerprints.update(
                            self._trusted_connected_evidence
                        )
                    if (
                        expected_fingerprints
                        and not expected_fingerprints.issubset(
                            resolved_fingerprints | set(target_failures)
                        )
                    ):
                        # A ResolvedTargetWindows subset with no immutable
                        # evidence for an omitted known entry is unattributed,
                        # therefore global rather than a guessed local revoke.
                        global_failures = tuple(
                            dict.fromkeys(
                                (*global_failures, "target_failure_unattributed")
                            )
                        )
                    return windows, global_failures, target_failures
                windows, blocked = self._bind_activation_snapshot_window_set(
                    tuple(provided),
                    frozenset(),
                )
                return (
                    windows,
                    (("window_identity_duplicate",) if blocked else ()),
                    {},
                )
            except Exception:
                return (
                    (),
                    ("target_window_provider_failed",),
                    {},
                )
        try:
            windows = tuple(
                window
                for window in self._window_backend.list_windows()
                if all(
                    keyword in window.title.casefold()
                    for keyword in self._keywords
                )
            )
            windows, blocked = self._bind_activation_snapshot_window_set(
                windows,
                frozenset(),
            )
            return (
                windows,
                (("window_identity_duplicate",) if blocked else ()),
                {},
            )
        except Exception:
            return (), ("window_enumeration_failed",), {}

    def _reconcile_activation_snapshot(
        self,
        candidate_windows: tuple[WindowInfo, ...],
        target_failures: dict[str, tuple[str, ...]],
    ) -> tuple[
        tuple[WindowInfo, ...],
        dict[str, tuple[str, ...]],
        tuple[str, ...],
    ]:
        """Ignore foreign windows and fail closed on snapshot identity drift."""
        snapshot = self._activation_snapshot_instances
        if snapshot is None:
            return candidate_windows, target_failures, ()
        sources = self._activation_snapshot_source_fingerprints
        index = self._activation_snapshot_instance_index
        if sources is None or index is None:
            return (), target_failures, ("snapshot_identity_collision",)
        allowed = frozenset(snapshot)
        scoped_target_failures = {
            fingerprint: codes
            for fingerprint, codes in target_failures.items()
            if fingerprint in allowed
        }
        scoped_candidates = tuple(
            window
            for window in candidate_windows
            if (
                (fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                ))
                in allowed
                and fingerprint not in scoped_target_failures
            )
        )
        complete_instances = self._unique_complete_candidate_instances(
            scoped_candidates
        )
        if complete_instances is None:
            self._initial_login_authorizations.clear()
            self._revoke_source_failure_evidence(
                allowed,
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            return (), scoped_target_failures, ("snapshot_identity_collision",)

        accepted: list[WindowInfo] = []
        changed_instances: dict[str, WindowInstanceToken] = {}
        failures: list[str] = []
        for fingerprint, (window, instance) in complete_instances.items():
            expected = snapshot[fingerprint]
            if instance == expected:
                accepted.append(window)
                continue
            if self._same_live_instance_identity(expected, instance):
                changed_instances[fingerprint] = instance
                accepted.append(window)
                continue
            authority = self._tcp_recovery_authority
            if (
                authority is not None
                and authority.fingerprint == fingerprint
                and authority.old_instance == expected
                and authority.shortcut_consumed
                and authority.stage
                in {
                    _TcpRecoveryStage.SHORTCUT_REQUESTED,
                    _TcpRecoveryStage.WAITING_NEW_INSTANCE,
                }
                and instance.handle != expected.handle
            ):
                # The state machine, not this generic reconciliation path,
                # owns the first binding of the requested new instance.
                accepted.append(window)
                continue
            if self._has_reconnect_session(fingerprint):
                changed_instances[fingerprint] = instance
                self._initial_login_authorizations.pop(
                    fingerprint,
                    None,
                )
                accepted.append(window)
                continue
            self._initial_login_authorizations.pop(fingerprint, None)
            self._revoke_source_failure_evidence(
                frozenset((fingerprint,)),
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            failures.append("snapshot_replacement_not_authorized")

        live_fingerprints = frozenset(complete_instances)
        for fingerprint in allowed - live_fingerprints:
            if not self._has_reconnect_session(fingerprint):
                self._initial_login_authorizations.pop(
                    fingerprint,
                    None,
                )

        if changed_instances:
            replacement_index = {
                key: value
                for key, value in index.items()
                if value not in changed_instances
            }
            replacement_index_valid = True
            for fingerprint, instance in changed_instances.items():
                source_fingerprint = sources.get(fingerprint)
                replacement_key = (
                    self._activation_instance_key(
                        source_fingerprint,
                        instance,
                    )
                    if source_fingerprint is not None
                    else None
                )
                if (
                    replacement_key is None
                    or replacement_key in replacement_index
                ):
                    replacement_index_valid = False
                    break
                replacement_index[replacement_key] = fingerprint
            if not replacement_index_valid:
                affected = frozenset(changed_instances)
                self._revoke_source_failure_evidence(
                    affected,
                    revoke_runtime_authority=True,
                    refresh_source_generation=True,
                )
                accepted = [
                    window
                    for window in accepted
                    if normalize_launch_fingerprint(
                        window.launch_fingerprint
                    )
                    not in affected
                ]
                failures.append("snapshot_replacement_identity_collision")
                return tuple(accepted), scoped_target_failures, tuple(failures)
            with self._source_authority_lock:
                # Adopting a newly proven token is always a distinct source
                # transition, even if an earlier denied replacement already
                # revoked the same fingerprint.
                self._source_revoked_fingerprints.difference_update(
                    changed_instances
                )
            self._revoke_source_failure_evidence(
                frozenset(changed_instances),
                revoke_runtime_authority=False,
                refresh_source_generation=True,
            )
            snapshot.update(changed_instances)
            index.clear()
            index.update(replacement_index)
            source_state_generation = (
                self._source_state_generation_snapshot()
            )
            for fingerprint, authorization in tuple(
                self._initial_login_authorizations.items()
            ):
                instance = snapshot.get(fingerprint)
                if instance is None:
                    self._initial_login_authorizations.pop(
                        fingerprint,
                        None,
                    )
                    continue
                self._initial_login_authorizations[fingerprint] = (
                    _InitialLoginAuthorization(
                        instance,
                        authorization.capture_settings_revision,
                        source_state_generation,
                        authorization.expires_at,
                    )
                )
        return tuple(accepted), scoped_target_failures, tuple(failures)

    def _target_for_fingerprint(self, fingerprint: str):
        plan = self._group_launch_plan
        return (
            plan.target_for_fingerprint(fingerprint)
            if plan is not None
            else None
        )

    def _has_reconnect_session(self, fingerprint: str) -> bool:
        if fingerprint in (
            self._pending_reconnect_fingerprints
            | self._pending_reopen_fingerprints
            | self._active_automation_fingerprints
        ):
            return True
        authority = self._tcp_recovery_authority
        return bool(
            authority is not None
            and authority.fingerprint == fingerprint
            and not authority.terminal
            and authority.stage
            in {
                _TcpRecoveryStage.SHORTCUT_REQUESTED,
                _TcpRecoveryStage.WAITING_NEW_INSTANCE,
                _TcpRecoveryStage.NEW_INSTANCE_BOUND,
                _TcpRecoveryStage.SCREEN_RECOVERY,
                _TcpRecoveryStage.LOGIN,
                _TcpRecoveryStage.LINE,
                _TcpRecoveryStage.ROLE,
                _TcpRecoveryStage.ENTER,
            }
        )

    @staticmethod
    def _action_signature(item: ScreenRecognition) -> tuple[object, ...]:
        return (
            item.state,
            item.click_point,
            item.reference_name,
            item.line_number,
            item.recent_line_present,
            item.recent_login_role,
            item.line_scroll_delta,
            item.character_level,
            item.character_importance,
            item.character_slot_index,
            item.character_slot_selected,
            item.character_target_key,
            tuple(
                (
                    candidate.level,
                    candidate.digit_count,
                    candidate.slot_index,
                    candidate.selected,
                    (
                        candidate.identity
                        if item.character_target_key is None
                        else None
                    ),
                )
                for candidate in item.character_candidates
            ),
            item.battle_context,
        )

    @staticmethod
    def _action_signature_is_complete(
        item: ScreenRecognition,
        *,
        require_point: bool = True,
    ) -> bool:
        point = item.click_point
        return bool(
            isinstance(item.reference_name, str)
            and item.reference_name.strip()
            and (
                not require_point
                or isinstance(point, tuple)
                and len(point) == 2
                and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and 0.0 <= value <= 1.0
                    for value in point
                )
            )
        )

    def _recognition_for_preferred_line(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> ScreenRecognition:
        """Authorize only the line shown by the current game frame."""
        if item.state is not ReconnectScreenState.LINE_SELECTION:
            return item
        if (
            item.line_scroll_delta in {-120, 120}
            and item.recent_line_present is True
            and item.line_number in LINE_ROUTE_CLICK_POINTS
            and item.click_point is None
        ):
            return replace(item, click_point=LINE_LIST_SCROLL_POINT)
        if (
            item.line_number not in LINE_ROUTE_CLICK_POINTS
            or item.click_point is None
        ):
            return replace(item, click_point=None, line_scroll_delta=0)
        return item

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
        self._character_selection_targets.pop(fingerprint, None)
        self._action_state_since.pop(fingerprint, None)
        self._flow_pause_until.pop(fingerprint, None)
        self._terminal_ready_after.pop(fingerprint, None)
        self._terminal_evidence.pop(fingerprint, None)
        self._recent_login_role_ids.pop(fingerprint, None)
        self._reconnect_entry_authorized.discard(fingerprint)
        self._login_only_recovery_fingerprints.discard(fingerprint)
        self._battle_restart_attempts.pop((fingerprint, False), None)
        self._battle_restart_attempts.pop((fingerprint, True), None)
        self._force_login_timeout_attempts.pop(fingerprint, None)
        self._clear_action_confirmation(fingerprint)

    def _action_is_confirmed(
        self,
        fingerprint: str,
        item: ScreenRecognition,
        *,
        instance: WindowInstanceToken | None,
        capture_route: str | None,
        capture_settings_revision: int,
        source_state_generation: int,
        confirmation_signature: tuple[object, ...] | None = None,
    ) -> bool:
        if instance is None or capture_route is None:
            self._clear_action_confirmation(fingerprint)
            return False
        if not self._source_authority_is_current(source_state_generation):
            self._clear_action_confirmation(fingerprint)
            return False
        if not self._record_evidence_decision(
            fingerprint=fingerprint,
            item=item,
            instance=instance,
            capture_route=capture_route,
            capture_settings_revision=capture_settings_revision,
            source_state_generation=source_state_generation,
        ):
            self._clear_action_confirmation(fingerprint)
            return False
        # A source revoke has to break both existing and concurrently captured
        # two-frame evidence. Keep this update inside the same source lock
        # used by the final backend boundary, so stale scans cannot recreate
        # a confirmation after the revoke has completed.
        with self._source_authority_lock:
            if (
                self._source_state_generation
                != source_state_generation
            ):
                with self._screen_state_lock:
                    self._clear_action_confirmation(fingerprint)
                return False
            signature = (
                confirmation_signature
                if confirmation_signature is not None
                else self._action_signature(item)
            )
            with self._screen_state_lock:
                previous = self._action_confirmations.get(fingerprint)
                count = (
                    previous.consecutive_frames + 1
                    if (
                        previous is not None
                        and previous.instance == instance
                        and previous.capture_route == capture_route
                        and previous.capture_settings_revision
                        == capture_settings_revision
                        and previous.source_state_generation
                        == source_state_generation
                        and previous.signature == signature
                    )
                    else 1
                )
                self._action_confirmations[fingerprint] = (
                    _ActionConfirmation(
                        instance=instance,
                        capture_route=capture_route,
                        capture_settings_revision=(
                            capture_settings_revision
                        ),
                        source_state_generation=(
                            source_state_generation
                        ),
                        signature=signature,
                        consecutive_frames=count,
                    )
                )
                return count >= ACTION_CONFIRMATION_FRAMES

    def _action_wait_seconds(
        self,
        fingerprint: str,
        state: ReconnectScreenState,
        now: float,
        instance: WindowInstanceToken | None = None,
    ) -> int:
        deadlines: list[float] = []
        first_seen = self._action_state_since.get(fingerprint)
        if (
            state is ReconnectScreenState.DISCONNECTED
            and first_seen is not None
            and (instance is None or first_seen[0] == instance)
        ):
            deadlines.append(
                first_seen[2]
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
        if target.registered_level is not None:
            return target.registered_level
        for label in (target.display_name, target.shortcut_path.stem):
            match = _ROLE_LEVEL_PREFIX.match(label)
            if match is not None:
                return int(match.group(1))
        return None

    @staticmethod
    def _identity_values(target) -> frozenset[str]:
        return frozenset(
            value.strip().casefold()
            for value in (
                target.role_id,
                target.display_name,
                target.shortcut_path.stem,
            )
            if isinstance(value, str) and value.strip()
        )

    @classmethod
    def _candidate_plan_target(
        cls,
        candidate: CharacterSelectionCandidate,
        plan: GroupLaunchPlan,
    ):
        identity = (
            candidate.identity.strip().casefold()
            if isinstance(candidate.identity, str)
            and candidate.identity.strip()
            else None
        )
        if identity is None:
            return None
        matches = tuple(
            target
            for target in plan.targets
            if identity in cls._identity_values(target)
        )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _candidate_result(
        item: ScreenRecognition,
        candidate: CharacterSelectionCandidate,
        importance: CharacterImportance | None,
        target_key: str | None = None,
    ) -> ScreenRecognition:
        if target_key is None:
            target_key = (
                f"slot:{candidate.slot_index}|"
                f"level:{candidate.level}|"
                f"digits:{candidate.digit_count}"
            )
        return replace(
            item,
            click_point=(
                CHARACTER_ENTER_CLICK_POINT
                if candidate.selected
                else candidate.click_point
            ),
            character_level=candidate.level,
            character_importance=importance,
            character_slot_index=candidate.slot_index,
            character_slot_selected=candidate.selected,
            character_identity=candidate.identity,
            character_target_key=target_key,
        )

    @staticmethod
    def _candidate_from_recognition(
        item: ScreenRecognition,
    ) -> CharacterSelectionCandidate | None:
        slot_index = item.character_slot_index
        if (
            not isinstance(slot_index, int)
            or isinstance(slot_index, bool)
            or slot_index < 0
            or slot_index >= 3
            or item.click_point is None
        ):
            return None
        return CharacterSelectionCandidate(
            level=item.character_level,
            importance=item.character_importance,
            slot_index=slot_index,
            selected=item.character_slot_selected is True,
            click_point=item.click_point,
            digit_count=(
                len(str(item.character_level))
                if item.character_level is not None
                else None
            ),
            identity=item.character_identity,
        )

    def _registered_role_for_candidate(
        self,
        candidate: CharacterSelectionCandidate,
    ) -> RegisteredReconnectRole | None:
        provider = self._registered_role_provider
        identity = candidate.identity
        if provider is None or not isinstance(identity, str):
            return None
        observed = identity.strip().casefold()
        abbreviated = observed.endswith(("…", "..."))
        observed = observed.rstrip(".…").strip()
        if len(observed) < 3:
            return None
        try:
            available = tuple(provider())
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        matches = tuple(
            role
            for role in available
            if isinstance(role, RegisteredReconnectRole)
            and (
                role.role_id.casefold() == observed
                if not abbreviated
                else role.role_id.casefold().startswith(observed)
            )
        )
        unique = {
            (role.role_id.casefold(), role.importance): role
            for role in matches
        }
        return next(iter(unique.values())) if len(unique) == 1 else None

    @staticmethod
    def _complete_role_identity(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        if (
            len(normalized) < 2
            or "..." in normalized
            or "…" in normalized
            or "/" in normalized
            or "\\" in normalized
            or normalized.endswith(".lnk")
        ):
            return None
        return normalized

    def _global_character_candidate(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> tuple[CharacterSelectionCandidate, CharacterImportance | None] | None:
        """Resolve exactly one saved PRIMARY; visible level never breaks ties."""
        expected_recent_role = self._recent_login_role_ids.get(fingerprint)
        primary: list[CharacterSelectionCandidate] = []
        matched_role_ids: list[str] = []
        for candidate in item.character_candidates:
            registered = self._registered_role_for_candidate(candidate)
            if (
                registered is None
                or registered.importance is not CharacterImportance.PRIMARY
            ):
                continue
            role_id = registered.role_id.casefold()
            if (
                expected_recent_role is not None
                and role_id != expected_recent_role
            ):
                continue
            primary.append(candidate)
            matched_role_ids.append(role_id)
        if len(primary) != 1 or len(set(matched_role_ids)) != 1:
            return None
        return primary[0], CharacterImportance.PRIMARY

    def _character_target_is_safe(
        self,
        fingerprint: str,
        item: ScreenRecognition,
        *,
        initial_login_authorized: bool = False,
    ) -> ScreenRecognition | None:
        """Choose only the uniquely proven registered primary role."""
        target = self._target_for_fingerprint(fingerprint)
        plan = self._group_launch_plan
        candidates = tuple(item.character_candidates)
        identity = (
            item.character_identity.strip().casefold()
            if isinstance(item.character_identity, str)
            and item.character_identity.strip()
            else None
        )
        pending_target = self._character_selection_targets.get(
            fingerprint
        )
        reconnect_session = self._has_reconnect_session(fingerprint)
        if (
            plan is not None
            and fingerprint in self._login_only_recovery_fingerprints
            and reconnect_session
        ):
            if target is None or not target.entry_id or not target.role_id:
                return None
            exact = tuple(
                candidate
                for candidate in candidates
                if isinstance(candidate.identity, str)
                and candidate.identity.strip().casefold()
                in self._identity_values(target)
            )
            if len(exact) != 1:
                if any(candidate.identity for candidate in candidates):
                    return None
                level = target.registered_level
                exact = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.level == level
                )
                if (
                    level is None
                    or len(exact) != 1
                    or any(candidate.level is None for candidate in candidates)
                ):
                    return None
            candidates = exact
            item = replace(item, character_candidates=candidates)
        if pending_target is not None:
            selected_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.selected
            )
            if len(selected_candidates) == 1:
                selected = selected_candidates[0]
            elif (
                not candidates
                and item.character_slot_selected is True
            ):
                selected = self._candidate_from_recognition(item)
                if selected is None:
                    return None
            else:
                return None
            if (
                selected.slot_index != pending_target.slot_index
                or (
                    pending_target.level is not None
                    and selected.level != pending_target.level
                )
                or (
                    pending_target.digit_count is not None
                    and selected.digit_count
                    != pending_target.digit_count
                )
            ):
                # A different selected card can never inherit authority from
                # the slot-selection click delivered in the previous phase.
                return None
            if plan is None:
                pending_role = self._registered_role_for_candidate(
                    pending_target
                )
                selected_role = self._registered_role_for_candidate(selected)
                expected_recent_role = self._recent_login_role_ids.get(
                    fingerprint
                )
                if (
                    pending_role is None
                    or selected_role is None
                    or pending_role.importance
                    is not CharacterImportance.PRIMARY
                    or selected_role.importance
                    is not CharacterImportance.PRIMARY
                    or pending_role.role_id.casefold()
                    != selected_role.role_id.casefold()
                    or (
                        expected_recent_role is not None
                        and selected_role.role_id.casefold()
                        != expected_recent_role
                    )
                ):
                    return None
                return self._candidate_result(
                    item,
                    selected,
                    CharacterImportance.PRIMARY,
                    selected_role.role_id.casefold(),
                )
            return self._candidate_result(
                item,
                selected,
                (
                    target.importance
                    if target is not None
                    else selected.importance
                ),
                (
                    target.role_id.casefold()
                    if target is not None
                    else None
                ),
            )
        if initial_login_authorized and not reconnect_session:
            if plan is None:
                global_selection = self._global_character_candidate(
                    fingerprint,
                    item,
                )
                if global_selection is None:
                    return None
                selected, importance = global_selection
                registered = self._registered_role_for_candidate(selected)
                if registered is None:
                    return None
                return self._candidate_result(
                    item,
                    selected,
                    importance,
                    registered.role_id.casefold(),
                )
            selected_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.selected
            )
            if len(selected_candidates) == 1:
                selected = selected_candidates[0]
            elif (
                not candidates
                and item.character_slot_selected is True
            ):
                selected = self._candidate_from_recognition(item)
                if selected is None:
                    return None
            else:
                # A grouped initial authorization may enter only the one role
                # already selected by the game.  The no-group path above uses
                # the uniquely registered PRIMARY identity; visible level,
                # ordering, and ties never select a slot.
                return None
            if plan is not None:
                if target is None:
                    return None
                expected_level = self._target_level(target)
                identity_matches = bool(
                    isinstance(selected.identity, str)
                    and selected.identity.strip().casefold()
                    in self._identity_values(target)
                )
                if not identity_matches and (
                    expected_level is None
                    or selected.level != expected_level
                ):
                    return None
            return self._candidate_result(
                item,
                selected,
                (
                    target.importance
                    if target is not None
                    else selected.importance
                ),
                (
                    target.role_id.casefold()
                    if target is not None
                    else None
                ),
            )
        if plan is None:
            if not reconnect_session:
                return None
            global_selection = self._global_character_candidate(
                fingerprint,
                item,
            )
            if global_selection is None:
                return None
            selected, importance = global_selection
            registered = self._registered_role_for_candidate(selected)
            if registered is None:
                return None
            return self._candidate_result(
                item,
                selected,
                importance,
                registered.role_id.casefold(),
            )
        if target is None:
            return None
        expected_level = self._target_level(target)
        # A selected-card summary is not sufficient when this fresh frame also
        # exposes the full candidate list: the saved role may be another,
        # uniquely identified card.  Let the exact candidate/slot proof below
        # decide that case; only a summary-only frame may use this shortcut.
        if identity is not None and not candidates:
            identity_matches = tuple(
                candidate
                for candidate in plan.targets
                if identity in self._identity_values(candidate)
            )
            if (
                len(identity_matches) != 1
                or identity_matches[0].fingerprint != fingerprint
                or (
                    expected_level is not None
                    and item.character_level is not None
                    and expected_level != item.character_level
                )
            ):
                return None
            return replace(
                item,
                character_target_key=target.role_id.casefold(),
            )

        if not candidates:
            return None

        exact_identity = tuple(
            candidate
            for candidate in candidates
            if (
                isinstance(candidate.identity, str)
                and candidate.identity.strip().casefold()
                in self._identity_values(target)
            )
        )
        if len(exact_identity) == 1:
            selected = exact_identity[0]
            if (
                expected_level is not None
                and selected.level is not None
                and selected.level != expected_level
            ):
                return None
            return self._candidate_result(
                item,
                selected,
                target.importance,
                target.role_id.casefold(),
            )

        selected_candidates = tuple(
            candidate for candidate in candidates if candidate.selected
        )
        if (
            len(selected_candidates) == 1
            and expected_level is not None
            and selected_candidates[0].level == expected_level
        ):
            return self._candidate_result(
                item,
                selected_candidates[0],
                target.importance,
                target.role_id.casefold(),
            )

        expected_level_candidates = tuple(
            candidate
            for candidate in candidates
            if (
                expected_level is not None
                and candidate.level == expected_level
            )
        )
        unknown_competitor = any(
            candidate.level is None
            and (
                candidate.digit_count is None
                or (
                    expected_level is not None
                    and candidate.digit_count
                    >= len(str(expected_level))
                )
            )
            for candidate in candidates
        )
        if len(expected_level_candidates) == 1 and not unknown_competitor:
            return self._candidate_result(
                item,
                expected_level_candidates[0],
                target.importance,
                target.role_id.casefold(),
            )
        if expected_level is not None:
            # A registered original-role level is authoritative. If the
            # current frame cannot prove that role, fail closed instead of
            # silently choosing a different, merely highest visible role.
            return None

        known_levels = tuple(
            candidate
            for candidate in candidates
            if candidate.level is not None
        )
        if not known_levels:
            return None
        highest_level = max(
            candidate.level
            for candidate in known_levels
            if candidate.level is not None
        )
        highest_digit_count = len(str(highest_level))
        if any(
            candidate.level is None
            and (
                candidate.digit_count is None
                or candidate.digit_count >= highest_digit_count
            )
            for candidate in candidates
        ):
            return None
        highest = tuple(
            candidate
            for candidate in known_levels
            if candidate.level == highest_level
        )
        if len(highest) == 1:
            selected = highest[0]
            resolved = self._candidate_plan_target(selected, plan)
            return self._candidate_result(
                item,
                selected,
                (
                    resolved.importance
                    if resolved is not None
                    else selected.importance
                ),
                (
                    resolved.role_id.casefold()
                    if resolved is not None
                    else target.role_id.casefold()
                ),
            )

        ranked: list[
            tuple[int, int, CharacterSelectionCandidate, object]
        ] = []
        for candidate in highest:
            resolved = self._candidate_plan_target(candidate, plan)
            if resolved is None or resolved.importance is None:
                return None
            ranked.append(
                (
                    character_importance_rank(resolved.importance),
                    resolved.order,
                    candidate,
                    resolved,
                )
            )
        ranked.sort(key=lambda value: (value[0], value[1]))
        if len(ranked) < 1 or (
            len(ranked) > 1
            and ranked[0][:2] == ranked[1][:2]
        ):
            return None
        _importance_rank, _order, selected, resolved = ranked[0]
        return self._candidate_result(
            item,
            selected,
            resolved.importance,
            resolved.role_id.casefold(),
        )

    def _recognition_for_session_action(
        self,
        fingerprint: str,
        recognition: ScreenRecognition,
        *,
        initial_login_authorized: bool = False,
    ) -> ScreenRecognition:
        if (
            recognition.state is ReconnectScreenState.LOGIN_START
            and (
                self._has_reconnect_session(fingerprint)
                or initial_login_authorized
            )
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
                initial_login_authorized=initial_login_authorized,
            )
            if role_target is None:
                return replace(recognition, click_point=None)
            return role_target
        return recognition

    def _selected_group_is_complete(
        self,
        windows: tuple[WindowInfo, ...],
        *,
        locally_isolated_fingerprints: frozenset[str] = frozenset(),
    ) -> bool:
        allowed = self._allowed_fingerprints
        if allowed is None:
            return True
        complete_instances = self._unique_complete_candidate_instances(windows)
        return (
            complete_instances is not None
            and set(complete_instances)
            == set(allowed) - set(locally_isolated_fingerprints)
        )

    def _current_action_window(
        self,
        expected: WindowInfo | WindowInstanceToken,
        fingerprint: str,
    ) -> WindowInfo | None:
        if fingerprint in self._detection_only_fingerprints:
            return None
        candidates, global_failures, target_failures = (
            self._candidate_window_set()
        )
        expected_instance = (
            expected
            if isinstance(expected, WindowInstanceToken)
            else WindowInstanceToken.from_window(expected)
        )
        allowed = self._allowed_fingerprints
        scoped_candidates = tuple(
            candidate
            for candidate in candidates
            if (
                allowed is None
                or normalize_launch_fingerprint(
                    candidate.launch_fingerprint
                )
                in allowed
            )
        )
        instances = self._unique_complete_candidate_instances(
            scoped_candidates
        )
        group_failures = tuple(
            self._group_failures(
                scoped_candidates,
                locally_isolated_fingerprints=frozenset(target_failures),
            )
        )
        if self._activation_snapshot_instances is not None:
            group_failures = tuple(
                code
                for code in group_failures
                if code != "group_identity_set_mismatch"
            )
        if (
            expected_instance is None
            or global_failures
            or fingerprint in target_failures
            or instances is None
            or group_failures
        ):
            # Broken source identity invalidates every prior action frame.
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        current = instances.get(fingerprint)
        if current is None:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        window, instance = current
        plan = self._group_launch_plan
        if isinstance(self._tcp_v, ResolvedTargetWindows) and plan is not None:
            target = plan.target_for_fingerprint(fingerprint)
            if (
                target is None
                or not target.entry_id
                or self._tcp_id(self._tcp_v, fingerprint, instance)
                != target.entry_id
            ):
                return None
        snapshot = self._activation_snapshot_instances
        if (
            snapshot is not None
            and snapshot.get(fingerprint) != instance
        ):
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        if instance != expected_instance:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        return window

    def _action_still_matches(
        self,
        expected_instance: WindowInstanceToken,
        fingerprint: str,
        expected: ScreenRecognition,
        expected_capture_settings_revision: int,
        expected_capture_route: str | None,
        expected_source_state_generation: int,
        *,
        require_recognition_match: bool = True,
    ) -> str | None:
        if not self._capture_authority_is_current(
            expected_capture_settings_revision,
            expected_capture_route,
        ):
            return None
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            return None
        window = self._current_action_window(expected_instance, fingerprint)
        if window is None:
            return None
        (
            _sample,
            recognition,
            fresh_capture,
            current_route,
        ) = self._capture_and_recognize(
            window,
            fingerprint,
            execute=True,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
        )
        if (
            not fresh_capture
            or current_route is None
            or not self._capture_authority_is_current(
                expected_capture_settings_revision,
                current_route,
            )
            or not self._source_authority_is_current(
                expected_source_state_generation,
            )
        ):
            return None
        initial_login_authorized = (
            self._initial_login_authorization_is_current(
                fingerprint,
                expected_instance,
                expected_capture_settings_revision,
                expected_source_state_generation,
            )
        )
        if (
            initial_login_authorized
            and recognition.state is ReconnectScreenState.LOGIN_START
        ):
            recognition = replace(
                recognition,
                state=ReconnectScreenState.FORCE_LOGIN_START,
                click_point=FORCE_LOGIN_CLICK_POINT,
            )
        current = self._recognition_for_session_action(
            fingerprint,
            recognition,
            initial_login_authorized=initial_login_authorized,
        )
        current = self._recognition_for_preferred_line(
            fingerprint,
            current,
        )
        if (
            require_recognition_match
            and self._action_signature(current)
            != self._action_signature(expected)
        ):
            return None
        if (
            self._current_action_window(expected_instance, fingerprint)
            is None
        ):
            return None
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            return None
        return current_route

    def _unknown_failure_key(self) -> str:
        group_name = (
            self._group_launch_plan.group_name
            if self._group_launch_plan is not None
            else "目前組別"
        )
        return f"group:{group_name}:unknown"

    def _report_reconnect_failure(
        self,
        fingerprint: str | None,
    ) -> None:
        service = self._failure_status_service
        target = (
            self._target_for_fingerprint(fingerprint)
            if fingerprint is not None
            else None
        )
        if target is None and service is None:
            return
        if target is not None:
            key = f"role:{target.fingerprint}"
            if service is not None:
                service.report(key, target.display_name)
            self._record_reconnect_failure(
                target.display_name,
                "重連失敗",
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

    def _clear_reconnect_failure(self, fingerprint: str) -> None:
        service = self._failure_status_service
        if service is None:
            return
        service.clear(f"role:{fingerprint}")

    def _clear_unknown_reconnect_failure(self) -> None:
        service = self._failure_status_service
        if service is not None:
            service.clear(self._unknown_failure_key())

    def _group_failures(
        self,
        windows: tuple[WindowInfo, ...],
        *,
        locally_isolated_fingerprints: frozenset[str] = frozenset(),
    ) -> list[str]:
        failures: list[str] = []
        # The default keeps the original fixed-count safety boundary.  The
        # player may explicitly enable global reconnect, which checks every
        # uniquely identified open game window without requiring a group.
        if (
            self._require_expected_window_count
            and
            self._allowed_fingerprints is None
            and not (
                self._pending_reconnect_fingerprints
                or self._pending_reopen_fingerprints
                or self._active_automation_fingerprints
            )
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
        if any(
            WindowInstanceToken.from_window(window) is None
            for window in windows
        ):
            failures.append("window_instance_incomplete")
        fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        if (
            any(fingerprint is None for fingerprint in fingerprints)
            or len(set(fingerprints)) != len(fingerprints)
        ):
            failures.append("fingerprint_missing_or_duplicate")
        observed_fingerprints = {
            fingerprint
            for fingerprint in fingerprints
            if fingerprint is not None
        }
        allowed_fingerprints = self._allowed_fingerprints
        local_isolation_is_exact = bool(
            allowed_fingerprints is not None
            and locally_isolated_fingerprints
            and locally_isolated_fingerprints <= allowed_fingerprints
            and observed_fingerprints
            == set(allowed_fingerprints) - set(locally_isolated_fingerprints)
        )
        if (
            allowed_fingerprints is not None
            and observed_fingerprints != set(allowed_fingerprints)
            and not local_isolation_is_exact
            and (
                self._action_confirmations
                or self._pending_reconnect_fingerprints
                or self._pending_reopen_fingerprints
                or self._active_automation_fingerprints
            )
        ):
            failures.append("group_identity_set_mismatch")
        return failures

    def _reopen_safety_failures(
        self,
        windows: tuple[WindowInfo, ...],
        global_failures: tuple[str, ...],
        target_failures: dict[str, tuple[str, ...]],
        owner: str | None,
    ) -> list[str]:
        """Fail closed before any restart or reopen backend call.

        A missing role is deliberately safe to reopen.  Every other live
        candidate must nevertheless form one complete, collision-free
        identity collection, independent of the caller that reached this
        boundary.
        """
        if global_failures:
            return list(global_failures)
        owner_codes = target_failures.get(owner or "", ())
        owner_offline = set(owner_codes) == {"window_offline"}
        failures = [] if owner_offline else (
            ["battle_reopen_identity_unsafe"] if owner_codes else []
        )
        instances = self._unique_complete_candidate_instances(windows)
        if instances is not None and owner_offline:
            plan = self._group_launch_plan
            expected_live = (
                plan.fingerprints - frozenset(target_failures)
                if plan is not None
                else frozenset()
            )
            if plan is not None and set(instances) == expected_live:
                return failures
        if instances is not None and not target_failures:
            return failures
        group_failures = self._group_failures(
            windows,
            locally_isolated_fingerprints=frozenset(target_failures),
        )
        identity_failures = [
            code
            for code in group_failures
            if code
            in {
                "window_handle_missing_or_duplicate",
                "process_identity_missing_or_duplicate",
                "window_instance_incomplete",
                "fingerprint_missing_or_duplicate",
                "group_identity_set_mismatch",
            }
        ]
        return failures + (identity_failures or ["window_identity_unsafe"])

    def _revoke_group_failure_evidence(
        self,
        windows: tuple[WindowInfo, ...],
        locally_isolated_fingerprints: frozenset[str],
    ) -> None:
        """Revoke only identities proven unsafe by a group failure.

        A temporarily absent planned role may still be retried safely.  It is
        not interchangeable with a live candidate that is incomplete or
        collides with another live instance, so do not erase the safe subset.
        """
        affected = set(locally_isolated_fingerprints)
        affected.update(self._last_screen_states)
        affected.update(self._trusted_connected_evidence)
        if self._allowed_fingerprints is not None:
            affected.update(self._allowed_fingerprints)
        normalized_windows = [
            (
                normalize_launch_fingerprint(window.launch_fingerprint),
                WindowInstanceToken.from_window(window),
            )
            for window in windows
        ]
        handle_counts = Counter(
            token.handle for _fingerprint, token in normalized_windows
            if token is not None
        )
        process_counts = Counter(
            token.process_id for _fingerprint, token in normalized_windows
            if token is not None
        )
        fingerprint_counts = Counter(
            fingerprint
            for fingerprint, _token in normalized_windows
            if fingerprint is not None
        )
        for fingerprint, token in normalized_windows:
            if fingerprint is None:
                continue
            if (
                token is None
                or handle_counts[token.handle] != 1
                or process_counts[token.process_id] != 1
                or fingerprint_counts[fingerprint] != 1
            ):
                affected.add(fingerprint)
        if not affected:
            return
        with self._screen_state_lock:
            self._mark_fingerprints_unknown_locked(
                affected,
                revoke_runtime_authority=False,
            )

    def _mark_fingerprints_unknown_locked(
        self,
        fingerprints: Iterable[str],
        *,
        revoke_runtime_authority: bool = False,
    ) -> None:
        for fingerprint in fingerprints:
            normalized = normalize_launch_fingerprint(fingerprint)
            if normalized is None:
                continue
            # No unknown source frame may bridge action confirmation or retain
            # a capture route. A full runtime revocation is reserved for a
            # passive source failure or an unsafe live instance; a planned
            # role that is simply absent remains eligible for its separately
            # guarded reopen retry.
            self._last_screen_states[normalized] = (
                ReconnectScreenState.UNKNOWN
            )
            self._trusted_connected_evidence.pop(
                normalized,
                None,
            )
            self._last_trusted_capture_routes.pop(normalized, None)
            self._action_retry_after.pop(normalized, None)
            self._action_state_since.pop(normalized, None)
            self._action_confirmations.pop(normalized, None)
            self._recent_login_role_ids.pop(normalized, None)
            self._auto_battle_evidence.pop(normalized, None)
            self._auto_battle_button_windows.pop(normalized, None)
            self._auto_battle_confirmed_instances.pop(normalized, None)
            self._auto_battle_attempted_actions = {
                item
                for item in self._auto_battle_attempted_actions
                if item[0] != normalized
            }
            self._primary_entry_authorized.discard(normalized)
            self._primary_connected_fingerprints.discard(normalized)
            self._reconnect_entry_authorized.discard(normalized)
            self._character_selection_pending.discard(normalized)
            self._character_selection_targets.pop(normalized, None)
            if revoke_runtime_authority:
                self._login_only_recovery_fingerprints.discard(normalized)
                self._pending_reconnect_fingerprints.discard(normalized)
                self._pending_reopen_fingerprints.discard(normalized)
                self._active_automation_fingerprints.discard(normalized)
                self._active_automation_until.pop(normalized, None)
                self._reopen_retry_after.pop(normalized, None)
                self._terminal_ready_after.pop(normalized, None)
                self._terminal_evidence.pop(normalized, None)
                self._flow_pause_until.pop(normalized, None)

    def _source_failure_affected_fingerprints(
        self,
        windows: tuple[WindowInfo, ...],
        global_failures: tuple[str, ...],
        target_failures: dict[str, tuple[str, ...]],
    ) -> frozenset[str]:
        if not global_failures:
            affected = set(target_failures)
            # Raw/non-contract monitoring has no entry evidence to attach to a
            # missing configured identity. Keep its historic conservative
            # local revoke rather than report a false all-connected result.
            if not affected and self._target_windows_provider is None:
                complete_instances = (
                    self._unique_complete_candidate_instances(windows)
                )
                if (
                    complete_instances is not None
                    and self._allowed_fingerprints is not None
                ):
                    affected.update(
                        self._allowed_fingerprints - set(complete_instances)
                    )
            return frozenset(affected)
        affected: set[str] = set(target_failures)
        allowed = self._allowed_fingerprints
        if allowed is not None:
            affected.update(allowed)
            return frozenset(affected)
        # An unattributed/global source failure revokes every previously
        # trusted identity because no target-local pairing is provable.
        with self._screen_state_lock:
            affected.update(self._last_screen_states)
            affected.update(self._trusted_connected_evidence)
        affected.update(
            fingerprint
            for window in windows
            if (
                fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
            )
            is not None
        )
        return frozenset(affected)

    def _clear_recovered_source_revocations(
        self,
        windows: tuple[WindowInfo, ...],
        global_failures: tuple[str, ...],
        target_failures: dict[str, tuple[str, ...]],
    ) -> None:
        """Recover each healthy peer even while a sibling remains isolated."""
        if global_failures:
            return
        complete_instances = self._unique_complete_candidate_instances(
            windows
        )
        if complete_instances is None:
            return
        with self._source_authority_lock:
            self._source_revoked_fingerprints.difference_update(
                fingerprint
                for fingerprint in complete_instances
                if fingerprint not in target_failures
            )

    def _revoke_source_failure_evidence(
        self,
        fingerprints: frozenset[str],
        *,
        revoke_runtime_authority: bool = False,
        refresh_source_generation: bool = True,
    ) -> None:
        if not fingerprints:
            return
        normalized_fingerprints = {
            fingerprint
            for fingerprint in (
                normalize_launch_fingerprint(item) for item in fingerprints
            )
            if fingerprint is not None
        }
        if not normalized_fingerprints:
            return
        # Revocation and final backend delivery share this source lock. Once
        # this method returns, an older scan cannot deliver any mutation that
        # was authorized by the previous source generation.
        with self._source_authority_lock:
            newly_revoked = (
                normalized_fingerprints
                - self._source_revoked_fingerprints
            )
            self._source_revoked_fingerprints.update(
                normalized_fingerprints
            )
            with self._screen_state_lock:
                if refresh_source_generation and newly_revoked:
                    self._source_state_generation += 1
                self._mark_fingerprints_unknown_locked(
                    normalized_fingerprints,
                    revoke_runtime_authority=revoke_runtime_authority,
                )
        self._publish_reconnecting_fingerprints()
        if revoke_runtime_authority:
            self._persist_runtime_state()

    def _runtime_state(self) -> ReconnectRuntimeState:
        with self._screen_state_lock:
            return ReconnectRuntimeState(
                set(self._pending_reconnect_fingerprints),
                set(self._active_automation_fingerprints),
                dict(self._active_automation_until),
                dict(self._action_retry_after),
                set(self._pending_reopen_fingerprints),
                dict(self._reopen_retry_after),
                dict(self._terminal_ready_after),
                dict(self._flow_pause_until),
                self._runtime_scope_token,
                dict(self._preferred_line_numbers),
                self._persisted_tcp_recovery_authority(),
            )

    @staticmethod
    def _instance_payload(
        instance: WindowInstanceToken | None,
    ) -> dict[str, object] | None:
        if instance is None:
            return None
        return {
            "handle": instance.handle,
            "process_id": instance.process_id,
            "thread_id": instance.thread_id,
            "window_class": instance.window_class,
            "rect": list(instance.rect),
            "minimized": instance.minimized,
            "process_lifecycle_token": instance.process_lifecycle_token,
        }

    @staticmethod
    def _instance_from_payload(
        payload: object,
    ) -> WindowInstanceToken | None:
        if not isinstance(payload, dict):
            return None
        try:
            rect = tuple(payload["rect"])
            instance = WindowInstanceToken(
                handle=payload["handle"],
                process_id=payload["process_id"],
                thread_id=payload["thread_id"],
                window_class=payload["window_class"],
                rect=rect,
                minimized=payload["minimized"],
                process_lifecycle_token=payload[
                    "process_lifecycle_token"
                ],
            )
        except (KeyError, TypeError, ValueError):
            return None
        return (
            instance
            if (
                type(instance.handle) is int
                and instance.handle > 0
                and type(instance.process_id) is int
                and instance.process_id > 0
                and type(instance.thread_id) is int
                and instance.thread_id > 0
                and isinstance(instance.window_class, str)
                and bool(instance.window_class.strip())
                and len(instance.rect) == 4
                and all(type(value) is int for value in instance.rect)
                and type(instance.minimized) is bool
                and type(instance.process_lifecycle_token) is int
                and instance.process_lifecycle_token > 0
            )
            else None
        )

    @staticmethod
    def _reopen_stage_evidence_from_payload(
        payload: object,
        *,
        fingerprint: str,
        entry_id: str,
        old_instance: WindowInstanceToken,
    ) -> tuple[BattleReopenStageEvidence, ...]:
        if not isinstance(payload, list):
            return ()
        expected_instance = (
            fingerprint,
            old_instance.handle,
            old_instance.process_id,
            old_instance.thread_id,
            old_instance.window_class,
            old_instance.process_lifecycle_token,
            old_instance.rect,
            old_instance.minimized,
        )
        allowed_stages = {stage.value for stage in BattleReopenStage}
        restored: list[BattleReopenStageEvidence] = []
        boundary_crossed = False
        for raw in payload:
            if not isinstance(raw, dict):
                return ()
            raw_instance = raw.get("original_instance")
            if (
                not isinstance(raw_instance, (list, tuple))
                or len(raw_instance) != 8
                or not isinstance(raw_instance[6], (list, tuple))
                or len(raw_instance[6]) != 4
            ):
                return ()
            original_instance = (
                normalize_launch_fingerprint(raw_instance[0]),
                raw_instance[1],
                raw_instance[2],
                raw_instance[3],
                raw_instance[4],
                raw_instance[5],
                tuple(raw_instance[6]),
                raw_instance[7],
            )
            owner = normalize_launch_fingerprint(raw.get("owner"))
            evidence_fingerprint = normalize_launch_fingerprint(
                raw.get("fingerprint")
            )
            raw_entry_id = raw.get("entry_id")
            shortcut = raw.get("original_shortcut")
            stage = raw.get("stage")
            started_at = raw.get("stage_started_at")
            ended_at = raw.get("stage_ended_at")
            delivery_boundary = raw.get("delivery_boundary_crossed")
            retry_allowed = raw.get("retry_allowed")
            wait_only = raw.get("wait_new_instance_only")
            failure_reason = raw.get("failure_reason")
            hard_timeout = raw.get("hard_timeout")
            if (
                owner != fingerprint
                or evidence_fingerprint != fingerprint
                or raw_entry_id != entry_id
                or original_instance != expected_instance
                or not isinstance(shortcut, str)
                or not shortcut.strip()
                or stage not in allowed_stages
                or not isinstance(started_at, (int, float))
                or isinstance(started_at, bool)
                or not math.isfinite(float(started_at))
                or (
                    ended_at is not None
                    and (
                        not isinstance(ended_at, (int, float))
                        or isinstance(ended_at, bool)
                        or not math.isfinite(float(ended_at))
                        or float(ended_at) < float(started_at)
                    )
                )
                or type(delivery_boundary) is not bool
                or type(retry_allowed) is not bool
                or type(wait_only) is not bool
                or type(hard_timeout) is not bool
                or (
                    failure_reason is not None
                    and (
                        not isinstance(failure_reason, str)
                        or not failure_reason.strip()
                    )
                )
                or (boundary_crossed and not delivery_boundary)
                or (retry_allowed and wait_only)
            ):
                return ()
            boundary_crossed = boundary_crossed or delivery_boundary
            restored.append(
                BattleReopenStageEvidence(
                    owner=owner,
                    entry_id=raw_entry_id,
                    fingerprint=evidence_fingerprint,
                    original_instance=expected_instance,
                    original_shortcut=shortcut,
                    stage=stage,
                    stage_started_at=float(started_at),
                    stage_ended_at=(
                        None if ended_at is None else float(ended_at)
                    ),
                    delivery_boundary_crossed=delivery_boundary,
                    retry_allowed=retry_allowed,
                    wait_new_instance_only=wait_only,
                    failure_reason=failure_reason,
                    hard_timeout=hard_timeout,
                )
            )
        return tuple(restored)

    def _persisted_tcp_recovery_authority(
        self,
    ) -> dict[str, object] | None:
        authority = self._tcp_recovery_authority
        if authority is None:
            return None
        immutable_authority = (
            authority.fingerprint,
            authority.entry_id,
            authority.old_instance,
            authority.activation_instance,
            authority.target_fingerprint,
            authority.shortcut_path,
            authority.plan_signature,
            authority.peer_signature,
            authority.source_state_generation,
            authority.scope_token,
        )
        return {
            "fingerprint": authority.fingerprint,
            "entry_id": authority.entry_id,
            "stage": authority.stage.value,
            "old_instance": self._instance_payload(authority.old_instance),
            "new_instance": self._instance_payload(authority.new_instance),
            "shortcut_consumed": authority.shortcut_consumed,
            "reopen_stage_evidence": [
                {
                    "owner": item.owner,
                    "entry_id": item.entry_id,
                    "fingerprint": item.fingerprint,
                    "original_instance": list(item.original_instance),
                    "original_shortcut": item.original_shortcut,
                    "stage": item.stage,
                    "stage_started_at": item.stage_started_at,
                    "stage_ended_at": item.stage_ended_at,
                    "delivery_boundary_crossed": (
                        item.delivery_boundary_crossed
                    ),
                    "retry_allowed": item.retry_allowed,
                    "wait_new_instance_only": item.wait_new_instance_only,
                    "failure_reason": item.failure_reason,
                    "hard_timeout": item.hard_timeout,
                }
                for item in authority.reopen_stage_evidence
            ],
            "authority_signature": hashlib.sha256(
                repr(immutable_authority).encode("utf-8")
            ).hexdigest(),
        }

    def _restore_tcp_recovery_tombstone(
        self,
        payload: dict[str, object] | None,
    ) -> _TcpRecoveryAuthority | None:
        if payload is None:
            return None
        fingerprint = normalize_launch_fingerprint(
            payload.get("fingerprint")
        )
        old_instance = self._instance_from_payload(
            payload.get("old_instance")
        )
        entry_id = payload.get("entry_id")
        if (
            fingerprint is None
            or old_instance is None
            or not isinstance(entry_id, str)
            or not entry_id.strip()
            or type(payload.get("shortcut_consumed")) is not bool
        ):
            return None
        reopen_stage_evidence = self._reopen_stage_evidence_from_payload(
            payload.get("reopen_stage_evidence", []),
            fingerprint=fingerprint,
            entry_id=entry_id.strip(),
            old_instance=old_instance,
        )
        # Monotonic deadlines and live source/plan objects cannot survive a
        # process boundary.  Keep only a non-actionable consumed-launch
        # tombstone: it blocks a duplicate shortcut request and is cleared
        # only by a fresh activation authority.
        return _TcpRecoveryAuthority(
            fingerprint=fingerprint,
            entry_id=entry_id.strip(),
            stage=_TcpRecoveryStage.CANCELLED,
            old_instance=old_instance,
            activation_instance=old_instance,
            target_fingerprint=fingerprint,
            shortcut_path="",
            plan_signature=(),
            peer_signature=(),
            source_state_generation=-1,
            scope_token=None,
            deadline=0.0,
            retry_at=0.0,
            shortcut_consumed=bool(payload.get("shortcut_consumed")),
            new_instance=self._instance_from_payload(
                payload.get("new_instance")
            ),
            restored_tombstone=True,
            reopen_stage_evidence=reopen_stage_evidence,
        )

    def _persist_runtime_state(self) -> bool:
        store = self._runtime_state_store
        if store is None:
            return True
        # Serialize snapshot creation and replacement. A caller that waited
        # behind a newer revocation snapshots the newest in-memory state rather
        # than writing an older authorization over it.
        with self._runtime_persist_lock:
            return store.save(self._runtime_state())

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
                tuple(sorted(self._terminal_ready_after.items())),
                tuple(sorted(self._flow_pause_until.items())),
                self._runtime_scope_token,
                tuple(sorted(self._preferred_line_numbers.items())),
                repr(self._persisted_tcp_recovery_authority()),
            )

    @staticmethod
    def _launch_plan_signature(
        plan: GroupLaunchPlan | None,
    ) -> tuple[tuple[object, ...], ...] | None:
        if plan is None or not plan.ready:
            return None
        signature = tuple(
            (
                target.order,
                target.entry_id,
                target.fingerprint,
                target.role_id,
                str(target.shortcut_path),
            )
            for target in plan.targets
        )
        if (
            not signature
            or any(not item[1] or not item[2] for item in signature)
            or len({item[1] for item in signature}) != len(signature)
        ):
            return None
        return signature

    def _tcp_peer_signature(
        self,
        resolved: ResolvedTargetWindows,
        owner_entry_id: str,
    ) -> tuple[tuple[object, ...], ...] | None:
        contract = self._contract_entry_candidates(resolved)
        plan = self._group_launch_plan
        if contract is None or plan is None:
            return None
        candidates_by_entry, detection_only = contract
        signature: list[tuple[object, ...]] = []
        for target in plan.targets:
            if target.entry_id == owner_entry_id:
                continue
            candidates = candidates_by_entry.get(target.entry_id, ())
            identities = self._complete_contract_identities(
                candidates
            )
            if identities is None:
                return None
            if len(identities) == 1:
                signature.append((target.entry_id, identities[0]))
                continue
            offline_evidence = tuple(
                evidence
                for evidence in resolved.target_failure_evidence
                if evidence.entry_id == target.entry_id
            )
            if (
                candidates
                or len(offline_evidence) != 1
                or offline_evidence[0].fingerprint != target.fingerprint
                or offline_evidence[0].failure_codes != ("window_offline",)
                or offline_evidence[0].candidate_windows
            ):
                return None
            signature.append(
                (target.entry_id, "window_offline", target.fingerprint)
            )
        detection_identities = self._complete_contract_identities(
            detection_only
        )
        if detection_identities is None:
            return None
        signature.extend(
            ("detection", identity) for identity in detection_identities
        )
        return tuple(signature)

    def _tcp_authority_signature(
        self,
        authority: _TcpRecoveryAuthority,
    ) -> str:
        return hashlib.sha256(
            repr(
                (
                    authority.fingerprint,
                    authority.entry_id,
                    authority.old_instance,
                    authority.activation_instance,
                    authority.target_fingerprint,
                    authority.shortcut_path,
                    authority.plan_signature,
                    authority.peer_signature,
                    authority.source_state_generation,
                    authority.scope_token,
                    authority.deadline,
                )
            ).encode("utf-8")
        ).hexdigest()

    def _begin_tcp_recovery_evidence(
        self,
        authority: _TcpRecoveryAuthority,
        action: str,
    ) -> tuple[int | None, str]:
        """Record owner recovery diagnostics without granting authority."""

        signature = self._tcp_authority_signature(authority)
        if (
            action == "reopen_window"
            and authority.reopen_intent_signature is not None
        ):
            return authority.reopen_intent_sequence, (
                authority.reopen_intent_signature
            )
        recorder = self._evidence_recorder
        if recorder is None or self._evidence_recording_failed:
            return None, signature
        try:
            sequence = recorder.record_action_intent(
                raw_window_key=authority.fingerprint,
                state=authority.stage.value,
                action=action,
                identity_verified=True,
                input_channel="window_control",
                authority_signature=signature,
            )
        except (OSError, TypeError, ValueError):
            self._mark_evidence_failure()
            return None, signature
        if action == "reopen_window":
            authority.reopen_intent_sequence = sequence
            authority.reopen_intent_signature = signature
        return sequence, signature

    def _finish_tcp_recovery_evidence(
        self,
        authority: _TcpRecoveryAuthority,
        action: str,
        sequence: int | None,
        signature: str,
        result: BattleRestartResult | None,
        *,
        allowed: bool,
    ) -> None:
        if action == "reopen_window":
            if authority.reopen_intent_finished:
                return
            if result is not None and (
                result.pending or result.retry_allowed
            ) and not result.delivery_boundary_crossed:
                return
        self._finish_evidence_action(
            fingerprint=authority.fingerprint,
            item=ScreenRecognition(
                ReconnectScreenState.RECONNECTING,
                None,
                None,
                authority.stage.value,
            ),
            action=action,
            intent_sequence=sequence,
            allowed=allowed,
            performed=bool(
                result is not None
                and (result.window_closed or result.shortcut_open_requested)
            ),
            clicked=False,
            identity_verified=True,
            restoration_verified=None,
            failure_reason=(
                result.failure_code
                if result is not None
                else ("authorization_revoked" if not allowed else None)
            ),
            authority_signature=signature,
            input_channel="window_control",
        )
        if action == "reopen_window":
            authority.reopen_intent_finished = True

    def _accept_tcp_reopen_stage_evidence(
        self,
        authority: _TcpRecoveryAuthority,
        result: BattleRestartResult | None,
    ) -> bool:
        if result is None or not result.stage_evidence:
            return True
        expected_instance = (
            authority.fingerprint,
            authority.old_instance.handle,
            authority.old_instance.process_id,
            authority.old_instance.thread_id,
            authority.old_instance.window_class,
            authority.old_instance.process_lifecycle_token,
            authority.old_instance.rect,
            authority.old_instance.minimized,
        )
        if any(
            not isinstance(item, BattleReopenStageEvidence)
            or item.owner != authority.fingerprint
            or item.entry_id != authority.entry_id
            or item.fingerprint != authority.target_fingerprint
            or item.original_instance != expected_instance
            or item.original_shortcut != authority.shortcut_path
            for item in result.stage_evidence
        ):
            return False
        if result.stage_evidence == authority.reopen_stage_evidence:
            return True
        authority.reopen_stage_evidence = result.stage_evidence
        self._persist_runtime_state()
        return True

    def _run_tcp_recovery_mutation(
        self,
        operation: str,
        authority: _TcpRecoveryAuthority,
        callback: Callable[[], object],
    ) -> tuple[bool, object | None]:
        """Mutate from owner authority without consulting screen capture."""

        execution_allowed = self._execution_enabled.is_set
        if not execution_allowed():
            return False, None
        gate = self._operation_gate
        lease = (
            gate.acquire(
                operation,
                execution_guard=execution_allowed,
                timeout_seconds=0,
            )
            if gate is not None
            else None
        )
        if gate is not None and lease is None:
            return False, None
        try:
            with self._source_authority_lock:
                if (
                    not execution_allowed()
                    or self._tcp_recovery_authority is not authority
                    or self._source_state_generation
                    != authority.source_state_generation
                ):
                    return False, None
                return True, callback()
        finally:
            if lease is not None:
                lease.release()

    def _cancel_tcp_recovery(
        self,
        authority: _TcpRecoveryAuthority,
        *,
        timed_out: bool = False,
    ) -> bool:
        if self._tcp_recovery_authority is not authority:
            return False
        cancel_reopen = getattr(
            self._battle_restarter,
            "cancel_bounded_reopen",
            None,
        )
        reopen_cancelled = True
        if callable(cancel_reopen):
            reopen_cancelled = bool(cancel_reopen(
                owner=authority.fingerprint,
                entry_id=authority.entry_id,
            ))
        if not reopen_cancelled:
            authority.stage = _TcpRecoveryStage.CANCELLED
            authority.shortcut_consumed = True
            authority.reopen_worker_unreaped = True
            self._persist_runtime_state()
            return False
        authority.stage = (
            _TcpRecoveryStage.TIMED_OUT
            if timed_out
            else _TcpRecoveryStage.CANCELLED
        )
        if not authority.shortcut_consumed:
            state = self._tcp_s.get(
                (authority.entry_id, authority.old_instance)
            )
            if state is not None:
                state.zero_since = None
                state.zero_count = 0
        self._persist_runtime_state()
        return True

    def _new_tcp_recovery_authority(
        self,
        fingerprint: str,
        state: _TcpState,
        now: float,
        source_state_generation: int,
    ) -> _TcpRecoveryAuthority | None:
        plan = self._group_launch_plan
        resolved = self._tcp_v
        target = self._target_for_entry(state.entry_id or "")
        snapshot = self._activation_snapshot_instances
        snapshot_sources = self._activation_snapshot_source_fingerprints
        plan_signature = self._launch_plan_signature(plan)
        deadline = self._reconnect_budget_deadline(fingerprint)
        if (
            state.entry_id is None
            or target is None
            or target.entry_id != state.entry_id
            or target.fingerprint != fingerprint
            or not isinstance(resolved, ResolvedTargetWindows)
            or plan_signature is None
            or deadline is None
            or now >= deadline
            or snapshot is None
            or snapshot.get(fingerprint) != state.instance
            or snapshot_sources is None
            or normalize_launch_fingerprint(
                snapshot_sources.get(fingerprint)
            )
            != target.fingerprint
            or self._tcp_id(resolved, fingerprint, state.instance)
            != state.entry_id
        ):
            return None
        peer_signature = self._tcp_peer_signature(
            resolved,
            state.entry_id,
        )
        if peer_signature is None:
            return None
        return _TcpRecoveryAuthority(
            fingerprint=fingerprint,
            entry_id=state.entry_id,
            stage=_TcpRecoveryStage.TCP_CONFIRMED_OWNER,
            old_instance=state.instance,
            activation_instance=state.instance,
            target_fingerprint=target.fingerprint,
            shortcut_path=str(target.shortcut_path),
            plan_signature=plan_signature,
            peer_signature=peer_signature,
            source_state_generation=source_state_generation,
            scope_token=self._runtime_scope_token,
            deadline=deadline,
            retry_at=now,
        )

    def _tcp_recovery_authority_is_current(
        self,
        authority: _TcpRecoveryAuthority,
        resolved: ResolvedTargetWindows,
    ) -> bool:
        target = self._target_for_entry(authority.entry_id)
        snapshot = self._activation_snapshot_instances
        snapshot_sources = self._activation_snapshot_source_fingerprints
        return bool(
            self._tcp_recovery_authority is authority
            and not authority.terminal
            and self._source_state_generation_snapshot()
            == authority.source_state_generation
            and self._runtime_scope_token == authority.scope_token
            and self._launch_plan_signature(self._group_launch_plan)
            == authority.plan_signature
            and target is not None
            and target.fingerprint == authority.target_fingerprint
            and str(target.shortcut_path) == authority.shortcut_path
            and snapshot is not None
            and snapshot.get(authority.fingerprint)
            == authority.activation_instance
            and snapshot_sources is not None
            and normalize_launch_fingerprint(
                snapshot_sources.get(authority.fingerprint)
            )
            == authority.target_fingerprint
            and self._tcp_peer_signature(resolved, authority.entry_id)
            == authority.peer_signature
        )

    def _bind_tcp_recovery_instance(
        self,
        authority: _TcpRecoveryAuthority,
        resolved: ResolvedTargetWindows,
    ) -> WindowInstanceToken | None:
        contract = self._contract_entry_candidates(resolved)
        if contract is None:
            return None
        candidates_by_entry, _detection = contract
        candidates = candidates_by_entry.get(authority.entry_id, ())
        if len(candidates) != 1:
            return None
        new_instance = WindowInstanceToken.from_window(candidates[0])
        if (
            new_instance is None
            or new_instance == authority.old_instance
            or new_instance.handle == authority.old_instance.handle
            or self._tcp_id(
                resolved,
                authority.fingerprint,
                new_instance,
            )
            != authority.entry_id
        ):
            return None
        return new_instance

    def _advance_tcp_recovery(
        self,
        *,
        owner: str | None,
        owner_state: _TcpState | None,
        execute: bool,
        now: float,
        source_state_generation: int,
    ) -> tuple[int, list[str], int | None]:
        """Advance exactly one owner through close/reopen without pixels."""

        authority = self._tcp_recovery_authority
        if authority is not None and not execute:
            return 0, [], None
        if authority is not None and authority.terminal:
            if authority.shortcut_consumed and (
                authority.restored_tombstone
                or authority.reopen_worker_unreaped
            ):
                return 0, [], None
            can_replace = bool(
                owner is not None
                and owner_state is not None
                and (
                    owner != authority.fingerprint
                    or not authority.shortcut_consumed
                    or (
                        authority.stage is _TcpRecoveryStage.CONNECTED
                        and owner_state.instance != authority.old_instance
                    )
                )
            )
            if not can_replace:
                return 0, [], None
            self._tcp_recovery_authority = None
            self._persist_runtime_state()
            authority = None
        if authority is None:
            if owner is None or owner_state is None or not execute:
                return 0, [], None
            authority = self._new_tcp_recovery_authority(
                owner,
                owner_state,
                now,
                source_state_generation,
            )
            if authority is None:
                return 0, ["tcp_recovery_authority_missing"], None
            self._tcp_recovery_authority = authority
            self._persist_runtime_state()
        elif owner not in {None, authority.fingerprint}:
            self._cancel_tcp_recovery(authority)
            return 0, ["tcp_recovery_owner_conflict"], None

        fresh_now = self._monotonic_clock()
        if fresh_now >= authority.deadline:
            poll_reopen = getattr(
                self._battle_restarter,
                "poll_bounded_reopen",
                None,
            )
            if callable(poll_reopen) and authority.stage in {
                _TcpRecoveryStage.REOPEN_PENDING,
                _TcpRecoveryStage.SHORTCUT_REQUESTED,
                _TcpRecoveryStage.WAITING_NEW_INSTANCE,
            }:
                _allowed, timeout_value = self._run_tcp_recovery_mutation(
                    "smart-reconnect-owner-reopen-timeout",
                    authority,
                    lambda: poll_reopen(
                        owner=authority.fingerprint,
                        entry_id=authority.entry_id,
                        deadline=authority.deadline,
                    ),
                )
                timeout_result = (
                    timeout_value
                    if isinstance(timeout_value, BattleRestartResult)
                    else None
                )
                timeout_evidence_valid = self._accept_tcp_reopen_stage_evidence(
                    authority,
                    timeout_result,
                )
                if not timeout_evidence_valid:
                    timeout_result = BattleRestartResult(
                        False,
                        "battle_reopen_stage_evidence_invalid",
                        wait_new_instance_only=True,
                    )
                self._finish_tcp_recovery_evidence(
                    authority,
                    "reopen_window",
                    authority.reopen_intent_sequence,
                    authority.reopen_intent_signature
                    or self._tcp_authority_signature(authority),
                    timeout_result,
                    allowed=_allowed,
                )
            self._cancel_tcp_recovery(authority, timed_out=True)
            self._tcp_timeout_isolated.add(authority.fingerprint)
            return 0, ["tcp_reconnect_timeout"], None
        resolved = self._tcp_v
        if (
            not isinstance(resolved, ResolvedTargetWindows)
            or not self._tcp_recovery_authority_is_current(
                authority,
                resolved,
            )
        ):
            self._cancel_tcp_recovery(authority)
            return 0, ["battle_contract_changed"], None

        if authority.stage is _TcpRecoveryStage.TCP_CONFIRMED_OWNER:
            states = tuple(self._tcp_s.values())
            counts, generation, observed_at = self._observe_tcp_counts(
                states,
                fresh_now,
            )
            target_state = self._tcp_s.get(
                (authority.entry_id, authority.old_instance)
            )
            if (
                counts is None
                or generation is None
                or observed_at is None
                or observed_at >= authority.deadline
                or target_state is None
                or counts.get(authority.old_instance.process_id) != 0
            ):
                self._cancel_tcp_recovery(
                    authority,
                    timed_out=bool(
                        observed_at is not None
                        and observed_at >= authority.deadline
                    ),
                )
                return 0, [
                    "tcp_reconnect_timeout"
                    if observed_at is not None
                    and observed_at >= authority.deadline
                    else "tcp_final_query_changed"
                ], None
            for state in states:
                if counts.get(state.instance.process_id, 0) > 0:
                    state.online = True
                    state.zero_since = None
                    state.zero_count = 0
                state.gen = generation
            self._candidate_window_set()
            resolved = self._tcp_v
            staged = (
                self._pre_close_backend_contract(
                    resolved,
                    authority.entry_id,
                    authority.old_instance,
                )
                if isinstance(resolved, ResolvedTargetWindows)
                and self._tcp_recovery_authority_is_current(
                    authority,
                    resolved,
                )
                else None
            )
            if staged is None or self._battle_restarter is None:
                self._cancel_tcp_recovery(authority)
                return 0, ["battle_contract_changed"], None
            owner_window, pre_candidates = staged
            sequence, signature = (
                self._begin_tcp_recovery_evidence(
                    authority,
                    "close_window",
                )
            )
            allowed, value = self._run_tcp_recovery_mutation(
                "smart-reconnect-owner-close",
                authority,
                lambda: self._battle_restarter.close_verified(
                    owner_window,
                    pre_candidates,
                    deadline=authority.deadline,
                ),
            )
            result = value if isinstance(value, BattleRestartResult) else None
            self._finish_tcp_recovery_evidence(
                authority,
                "close_window",
                sequence,
                signature,
                result,
                allowed=allowed,
            )
            if (
                not allowed
                or result is None
                or not result.success
                or not result.window_closed
            ):
                self._cancel_tcp_recovery(authority)
                return 0, [
                    result.failure_code
                    if result is not None and result.failure_code
                    else "battle_window_close_failed"
                ], None
            self._candidate_window_set()
            after = self._tcp_v
            if (
                not isinstance(after, ResolvedTargetWindows)
                or self._post_close_backend_contract(
                    resolved,
                    after,
                    authority.entry_id,
                    owner_window,
                )
                is None
                or not self._tcp_recovery_authority_is_current(
                    authority,
                    after,
                )
            ):
                self._cancel_tcp_recovery(authority)
                return 0, ["battle_contract_changed"], None
            authority.stage = _TcpRecoveryStage.CLOSE_VERIFIED
            authority.stage = _TcpRecoveryStage.REOPEN_PENDING
            authority.retry_at = self._monotonic_clock()
            self._persist_runtime_state()

        if authority.stage is _TcpRecoveryStage.REOPEN_PENDING:
            fresh_now = self._monotonic_clock()
            if fresh_now < authority.retry_at:
                return 0, [], max(1, math.ceil(authority.retry_at - fresh_now))
            self._candidate_window_set()
            resolved = self._tcp_v
            target = self._target_for_entry(authority.entry_id)
            candidates = (
                self._reopen_backend_contract(
                    resolved,
                    authority.entry_id,
                )
                if isinstance(resolved, ResolvedTargetWindows)
                and self._tcp_recovery_authority_is_current(
                    authority,
                    resolved,
                )
                else None
            )
            if (
                candidates is None
                or target is None
                or self._battle_restarter is None
            ):
                self._cancel_tcp_recovery(authority)
                return 0, ["battle_reopen_identity_unsafe"], None
            sequence, signature = (
                self._begin_tcp_recovery_evidence(
                    authority,
                    "reopen_window",
                )
            )
            allowed, value = self._run_tcp_recovery_mutation(
                "smart-reconnect-owner-reopen",
                authority,
                lambda: self._battle_restarter.begin_bounded_reopen(
                    owner=authority.fingerprint,
                    entry_id=authority.entry_id,
                    original_instance=(
                        authority.fingerprint,
                        authority.old_instance.handle,
                        authority.old_instance.process_id,
                        authority.old_instance.thread_id,
                        authority.old_instance.window_class,
                        authority.old_instance.process_lifecycle_token,
                        authority.old_instance.rect,
                        authority.old_instance.minimized,
                    ),
                    target=target,
                    candidate_windows=candidates,
                    deadline=authority.deadline,
                ),
            )
            result = value if isinstance(value, BattleRestartResult) else None
            evidence_valid = self._accept_tcp_reopen_stage_evidence(
                authority,
                result,
            )
            if not evidence_valid:
                invalid_result = BattleRestartResult(
                    False,
                    "battle_reopen_stage_evidence_invalid",
                    wait_new_instance_only=True,
                )
                self._finish_tcp_recovery_evidence(
                    authority,
                    "reopen_window",
                    sequence,
                    signature,
                    invalid_result,
                    allowed=allowed,
                )
                self._cancel_tcp_recovery(authority)
                return 0, [invalid_result.failure_code], None
            self._finish_tcp_recovery_evidence(
                authority,
                "reopen_window",
                sequence,
                signature,
                result,
                allowed=allowed,
            )
            if not allowed:
                self._cancel_tcp_recovery(authority)
                return 0, ["authorization_revoked"], None
            if result is None:
                self._cancel_tcp_recovery(authority)
                return 0, ["battle_reopen_result_invalid"], None
            if (
                result.pending
                and result.stage
                == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
            ):
                # The worker is stopped at a private ACK gate.  Persist the
                # consumed edge first; only a durable success may release it
                # into the Windows shortcut-launch boundary.
                authority.stage = _TcpRecoveryStage.SHORTCUT_REQUESTED
                authority.shortcut_consumed = True
                if not self._persist_runtime_state():
                    self._battle_restarter.cancel_bounded_reopen(
                        owner=authority.fingerprint,
                        entry_id=authority.entry_id,
                    )
                    self._cancel_tcp_recovery(authority)
                    return 0, ["reconnect_state_persistence_failed"], None
                authorized, authorization_value = (
                    self._run_tcp_recovery_mutation(
                        "smart-reconnect-owner-reopen-authorize",
                        authority,
                        lambda: (
                            self._battle_restarter.authorize_bounded_reopen(
                                owner=authority.fingerprint,
                                entry_id=authority.entry_id,
                            )
                        ),
                    )
                )
                authorization_result = (
                    authorization_value
                    if isinstance(
                        authorization_value,
                        BattleRestartResult,
                    )
                    else None
                )
                authorization_evidence_valid = (
                    self._accept_tcp_reopen_stage_evidence(
                        authority,
                        authorization_result,
                    )
                )
                if not authorization_evidence_valid:
                    authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                    authority.shortcut_consumed = True
                    self._persist_runtime_state()
                    return (
                        0,
                        ["battle_reopen_stage_evidence_invalid"],
                        self._policy.progress_interval_seconds,
                    )
                if not authorized or authorization_result is None:
                    authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                    self._persist_runtime_state()
                    return 0, [], self._policy.progress_interval_seconds
                if authorization_result.failure_code is not None:
                    authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                    self._persist_runtime_state()
                    return 0, [], self._policy.progress_interval_seconds
                authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                self._persist_runtime_state()
                return 1, [], self._policy.progress_interval_seconds
            if result.delivery_boundary_crossed or result.success:
                authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                self._persist_runtime_state()
                return 1, [], self._policy.progress_interval_seconds
            if result.pending:
                return 0, [], self._policy.progress_interval_seconds
            if result.wait_new_instance_only:
                authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                authority.shortcut_consumed = True
                self._persist_runtime_state()
                return 0, [], self._policy.progress_interval_seconds
            authority.stage = _TcpRecoveryStage.REOPEN_PENDING
            authority.shortcut_consumed = not result.retry_allowed
            authority.retry_at = (
                self._monotonic_clock()
                + self._policy.progress_interval_seconds
            )
            self._persist_runtime_state()
            return 0, [
                result.failure_code
                if result is not None and result.failure_code
                else "battle_shortcut_open_failed"
            ], self._policy.progress_interval_seconds

        if authority.stage in {
            _TcpRecoveryStage.SHORTCUT_REQUESTED,
            _TcpRecoveryStage.WAITING_NEW_INSTANCE,
        }:
            if self._battle_restarter is not None:
                poll_reopen = getattr(
                    self._battle_restarter,
                    "poll_bounded_reopen",
                    None,
                )
                if callable(poll_reopen):
                    allowed, poll_value = self._run_tcp_recovery_mutation(
                        "smart-reconnect-owner-reopen-poll",
                        authority,
                        lambda: poll_reopen(
                            owner=authority.fingerprint,
                            entry_id=authority.entry_id,
                            deadline=authority.deadline,
                        ),
                    )
                    poll_result = (
                        poll_value
                        if isinstance(poll_value, BattleRestartResult)
                        else None
                    )
                    poll_evidence_valid = (
                        self._accept_tcp_reopen_stage_evidence(
                            authority,
                            poll_result,
                        )
                    )
                    if not poll_evidence_valid:
                        authority.stage = (
                            _TcpRecoveryStage.WAITING_NEW_INSTANCE
                        )
                        authority.shortcut_consumed = True
                        self._persist_runtime_state()
                        return (
                            0,
                            ["battle_reopen_stage_evidence_invalid"],
                            self._policy.progress_interval_seconds,
                        )
                    if (
                        poll_result is not None
                        and (
                            poll_result.delivery_boundary_crossed
                            or poll_result.success
                            or poll_result.failure_code is not None
                        )
                    ):
                        self._finish_tcp_recovery_evidence(
                            authority,
                            "reopen_window",
                            authority.reopen_intent_sequence,
                            authority.reopen_intent_signature
                            or self._tcp_authority_signature(authority),
                            poll_result,
                            allowed=allowed,
                        )
                    if not allowed or poll_result is None:
                        authority.stage = (
                            _TcpRecoveryStage.WAITING_NEW_INSTANCE
                        )
                        self._persist_runtime_state()
                    elif (
                        poll_result.delivery_boundary_crossed
                        or poll_result.success
                        or poll_result.wait_new_instance_only
                    ):
                        authority.stage = (
                            _TcpRecoveryStage.WAITING_NEW_INSTANCE
                        )
                        self._persist_runtime_state()
            self._candidate_window_set()
            resolved = self._tcp_v
            if (
                not isinstance(resolved, ResolvedTargetWindows)
                or not self._tcp_recovery_authority_is_current(
                    authority,
                    resolved,
                )
            ):
                self._cancel_tcp_recovery(authority)
                return 0, ["battle_contract_changed"], None
            new_instance = self._bind_tcp_recovery_instance(
                authority,
                resolved,
            )
            if new_instance is None:
                if self._monotonic_clock() >= authority.deadline:
                    self._cancel_tcp_recovery(authority, timed_out=True)
                    return 0, ["tcp_reconnect_timeout"], None
                return 0, [], self._policy.progress_interval_seconds
            complete_reopen = getattr(
                self._battle_restarter,
                "complete_bounded_reopen",
                None,
            )
            completed_result = None
            if callable(complete_reopen):
                completed_reopen = complete_reopen(
                    owner=authority.fingerprint,
                    entry_id=authority.entry_id,
                )
                completed_result = (
                    completed_reopen
                    if isinstance(
                        completed_reopen,
                        BattleRestartResult,
                    )
                    else None
                )
                complete_evidence_valid = (
                    self._accept_tcp_reopen_stage_evidence(
                        authority,
                        completed_result,
                    )
                )
                if not complete_evidence_valid:
                    authority.stage = (
                        _TcpRecoveryStage.WAITING_NEW_INSTANCE
                    )
                    authority.shortcut_consumed = True
                    self._persist_runtime_state()
                    return (
                        0,
                        ["battle_reopen_stage_evidence_invalid"],
                        self._policy.progress_interval_seconds,
                    )
            if completed_result is None or not completed_result.success:
                authority.stage = _TcpRecoveryStage.WAITING_NEW_INSTANCE
                authority.shortcut_consumed = True
                self._persist_runtime_state()
                return (
                    0,
                    [
                        completed_result.failure_code
                        if (
                            completed_result is not None
                            and completed_result.failure_code
                        )
                        else "battle_reopen_completion_failed"
                    ],
                    self._policy.progress_interval_seconds,
                )
            self._finish_tcp_recovery_evidence(
                authority,
                "reopen_window",
                authority.reopen_intent_sequence,
                authority.reopen_intent_signature
                or self._tcp_authority_signature(authority),
                completed_result,
                allowed=True,
            )
            authority.new_instance = new_instance
            authority.stage = _TcpRecoveryStage.NEW_INSTANCE_BOUND
            snapshot = self._activation_snapshot_instances
            sources = self._activation_snapshot_source_fingerprints
            index = self._activation_snapshot_instance_index
            source_fingerprint = (
                sources.get(authority.fingerprint)
                if sources is not None
                else None
            )
            new_key = self._activation_instance_key(
                source_fingerprint,
                new_instance,
            )
            old_key = self._activation_instance_key(
                source_fingerprint,
                authority.activation_instance,
            )
            if (
                snapshot is None
                or index is None
                or new_key is None
                or old_key is None
                or (
                    new_key in index
                    and index[new_key] != authority.fingerprint
                )
            ):
                self._cancel_tcp_recovery(authority)
                return 0, ["snapshot_replacement_identity_collision"], None
            snapshot[authority.fingerprint] = new_instance
            index.pop(old_key, None)
            index[new_key] = authority.fingerprint
            authority.activation_instance = new_instance
            with self._source_authority_lock:
                self._source_state_generation += 1
                authority.source_state_generation = (
                    self._source_state_generation
                )
            authority.stage = _TcpRecoveryStage.SCREEN_RECOVERY
            self._login_only_recovery_fingerprints.add(
                authority.fingerprint
            )
            self._pending_reconnect_fingerprints.add(
                authority.fingerprint
            )
            self._active_automation_fingerprints.add(
                authority.fingerprint
            )
            self._active_automation_until[authority.fingerprint] = (
                self._monotonic_clock()
                + POST_LOGIN_AUTOMATION_GRACE_SECONDS
            )
            self._persist_runtime_state()
        return 0, [], self._policy.progress_interval_seconds

    def _advance_tcp_screen_stage(
        self,
        fingerprint: str,
        recognition: ScreenRecognition,
    ) -> None:
        authority = self._tcp_recovery_authority
        if (
            authority is None
            or authority.fingerprint != fingerprint
            or self._source_state_generation_snapshot()
            != authority.source_state_generation
            or authority.stage.value
            not in {
                _TcpRecoveryStage.SCREEN_RECOVERY.value,
                _TcpRecoveryStage.LOGIN.value,
                _TcpRecoveryStage.LINE.value,
                _TcpRecoveryStage.ROLE.value,
                _TcpRecoveryStage.ENTER.value,
            }
        ):
            return
        state = recognition.state
        next_stage = {
            ReconnectScreenState.LOGIN_START: _TcpRecoveryStage.LOGIN,
            ReconnectScreenState.FORCE_LOGIN_START: _TcpRecoveryStage.LOGIN,
            ReconnectScreenState.FORCE_LOGIN_TIMEOUT: _TcpRecoveryStage.LOGIN,
            ReconnectScreenState.LINE_SELECTION: _TcpRecoveryStage.LINE,
            ReconnectScreenState.CONNECTED: _TcpRecoveryStage.CONNECTED,
        }.get(state)
        if state is ReconnectScreenState.CHARACTER_SELECTION:
            next_stage = (
                _TcpRecoveryStage.ENTER
                if recognition.character_slot_selected is True
                else _TcpRecoveryStage.ROLE
            )
        if next_stage is not None and authority.stage is not next_stage:
            authority.stage = next_stage
            self._persist_runtime_state()

    def _retry_pending_reopens(
        self,
        *,
        candidate_windows: tuple[WindowInfo, ...],
        global_failures: tuple[str, ...],
        target_failures: dict[str, tuple[str, ...]],
        execute: bool,
        now: float,
        expected_capture_settings_revision: int,
        expected_source_state_generation: int,
        safety_failures: tuple[str, ...] = (),
        owner: str | None = None,
    ) -> tuple[int, list[str], int | None]:
        live_counts: Counter[str] = Counter()
        unsafe_live_fingerprints: set[str] = set()
        for window in candidate_windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is None:
                continue
            if (
                fingerprint in target_failures
                or WindowInstanceToken.from_window(window) is None
            ):
                unsafe_live_fingerprints.add(fingerprint)
            else:
                live_counts[fingerprint] += 1
        duplicate_live_fingerprints = {
            fingerprint
            for fingerprint, count in live_counts.items()
            if count > 1
        }
        clearable_live_fingerprints = {
            fingerprint
            for fingerprint, count in live_counts.items()
            if (
                count == 1
                and fingerprint not in unsafe_live_fingerprints
            )
        }
        appeared = (
            self._pending_reopen_fingerprints & clearable_live_fingerprints
        )
        self._pending_reopen_fingerprints.difference_update(appeared)
        for fingerprint in appeared:
            self._reopen_retry_after.pop(fingerprint, None)

        missing = tuple(sorted(self._pending_reopen_fingerprints))
        if not missing:
            return 0, [], None
        if owner is None or missing != (owner,):
            return 0, [], self._policy.progress_interval_seconds
        if safety_failures:
            failures = list(safety_failures)
            if set(missing) & set(target_failures):
                failures.append("battle_reopen_identity_unsafe")
            return (
                0,
                failures,
                self._policy.retry_interval_seconds,
            )
        if "target_window_provider_failed" in global_failures:
            return (
                0,
                [],
                self._policy.retry_interval_seconds,
            )

        failures: list[str] = []
        reopened = 0
        next_delays: list[int] = []
        for fingerprint in missing:
            with self._screen_state_lock:
                capture_route = self._last_trusted_capture_routes.get(
                    fingerprint
                )
            if not self._capture_authority_is_current(
                expected_capture_settings_revision,
                capture_route,
            ):
                continue
            if not self._source_authority_is_current(
                expected_source_state_generation,
            ):
                continue
            retry_at = self._reopen_retry_after.get(fingerprint, now)
            owner_offline = set(target_failures.get(fingerprint, ())) == {
                "window_offline"
            }
            if fingerprint in target_failures and not owner_offline:
                failures.append("battle_reopen_identity_unsafe")
                next_delays.append(self._policy.retry_interval_seconds)
                continue
            if fingerprint in unsafe_live_fingerprints:
                failures.append("window_instance_incomplete")
                next_delays.append(self._policy.retry_interval_seconds)
                continue
            if fingerprint in duplicate_live_fingerprints:
                failures.append("fingerprint_missing_or_duplicate")
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
            if target is None or not callable(retry_open):
                self._clear_action_confirmation(fingerprint)
                failures.append("battle_restart_identity_unresolved")
                self._report_reconnect_failure(None)
                continue

            # A pending reopen starts after the owner has already transitioned
            # to its explicit local ``window_offline`` evidence.  Re-read that
            # post-close contract outside the final authorization locks and
            # pass only its static complete candidates to the native backend.
            self._candidate_window_set()
            reopen_contract = self._tcp_v
            backend_candidates = (
                self._reopen_backend_contract(
                    reopen_contract,
                    target.entry_id,
                )
                if isinstance(reopen_contract, ResolvedTargetWindows)
                else None
            )
            if (
                backend_candidates is None
                or not self._reconnect_budget_current(
                    fingerprint,
                    self._monotonic_clock(),
                )
            ):
                failures.append(
                    "tcp_reconnect_timeout"
                    if fingerprint in self._tcp_timeout_isolated
                    else "battle_reopen_identity_unsafe"
                )
                next_delays.append(self._policy.retry_interval_seconds)
                continue
            owner_deadline = self._reconnect_budget_deadline(fingerprint)

            evidence_item = ScreenRecognition(
                ReconnectScreenState.RECONNECTING,
                None,
                None,
                "pending_reopen",
            )
            evidence_intent: int | None = None
            evidence_authority: str | None = None
            evidence_candidates = candidate_windows
            if self._evidence_recorder is not None or self._evidence_required:
                snapshot = self._activation_snapshot_instances
                evidence_instance = (
                    snapshot.get(fingerprint)
                    if snapshot is not None
                    else None
                )
                if evidence_instance is None or capture_route is None:
                    failures.append("reopen_evidence_identity_missing")
                    continue
                (
                    absence_ready,
                    fresh_absence_candidates,
                    evidence_authority,
                ) = self._record_reopen_absence_evidence(
                    fingerprint=fingerprint,
                    instance=evidence_instance,
                    target=target,
                    capture_route=capture_route,
                    capture_settings_revision=(
                        expected_capture_settings_revision
                    ),
                    source_state_generation=(
                        expected_source_state_generation
                    ),
                )
                if not absence_ready:
                    failures.append("reopen_absence_evidence_missing")
                    continue
                evidence_candidates = fresh_absence_candidates
                (
                    evidence_ready,
                    evidence_intent,
                    intent_authority,
                ) = self._begin_evidence_action(
                    fingerprint=fingerprint,
                    item=evidence_item,
                    action="reopen_window",
                    instance=evidence_instance,
                    capture_route=capture_route,
                    capture_settings_revision=(
                        expected_capture_settings_revision
                    ),
                    source_state_generation=(
                        expected_source_state_generation
                    ),
                    input_channel="window_control",
                    identity_verified_override=True,
                )
                if not evidence_ready or intent_authority != evidence_authority:
                    failures.append("evidence_recording_unavailable")
                    continue

            permitted, mutation_result = self._run_game_mutation(
                "smart-reconnect-reopen",
                lambda: self._run_authorized_backend_call(
                    lambda: retry_open(
                        target,
                        backend_candidates,
                        deadline=owner_deadline,
                    ),
                    expected_capture_settings_revision=(
                        expected_capture_settings_revision
                    ),
                    capture_route=capture_route,
                    expected_source_state_generation=(
                        expected_source_state_generation
                    ),
                    additional_authorization_check=(
                        lambda: self._reconnect_budget_current(
                            fingerprint,
                            self._monotonic_clock(),
                        )
                    ),
                )[1],
                expected_capture_settings_revision=(
                    expected_capture_settings_revision
                ),
                capture_route=capture_route,
                expected_source_state_generation=(
                    expected_source_state_generation
                ),
            )
            evidence_reopen_result = (
                mutation_result
                if isinstance(mutation_result, BattleRestartResult)
                else None
            )
            self._finish_evidence_action(
                fingerprint=fingerprint,
                item=evidence_item,
                action="reopen_window",
                intent_sequence=evidence_intent,
                allowed=permitted,
                performed=bool(
                    evidence_reopen_result is not None
                    and evidence_reopen_result.shortcut_open_requested
                ),
                clicked=False,
                identity_verified=True,
                restoration_verified=None,
                failure_reason=(
                    evidence_reopen_result.failure_code
                    if evidence_reopen_result is not None
                    and evidence_reopen_result.failure_code is not None
                    else (
                        "authorization_revoked"
                        if not permitted
                        else (
                            "reopen_result_invalid"
                            if evidence_reopen_result is None
                            else None
                        )
                    )
                ),
                authority_signature=evidence_authority,
                input_channel="window_control",
            )
            if not permitted or not isinstance(
                mutation_result,
                BattleRestartResult,
            ):
                self._clear_action_confirmation(fingerprint)
                if self._capture_authority_is_current(
                    expected_capture_settings_revision,
                    capture_route,
                ):
                    next_delays.append(1)
                continue
            result = mutation_result
            if not self._capture_authority_is_current(
                expected_capture_settings_revision,
                capture_route,
            ):
                self._clear_action_confirmation(fingerprint)
                continue
            if not self._source_authority_is_current(
                expected_source_state_generation,
            ):
                self._clear_action_confirmation(fingerprint)
                continue
            mutation_completed_at = self._monotonic_clock()
            self._reopen_retry_after[fingerprint] = (
                mutation_completed_at
                + self._policy.progress_interval_seconds
            )
            next_delays.append(self._policy.progress_interval_seconds)
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
        # Group identity may be rebound from the UI thread while recognition is
        # running. Keep reconnect state internally consistent without holding
        # the shared game-operation gate during this read-only work.
        with self._scan_lock:
            return self._scan_locked(execute=execute)

    def _scan_locked(self, *, execute: bool) -> ReconnectBatchResult:
        with self._capture_settings_lock:
            capture_settings_revision = self._capture_settings_revision
        (
            candidate_windows,
            global_failures,
            target_failures,
        ) = self._candidate_window_set()
        direct_identity_collisions = (
            self._activation_snapshot_direct_identity_collisions
        )
        (
            candidate_windows,
            target_failures,
            snapshot_failures,
        ) = self._reconcile_activation_snapshot(
            candidate_windows,
            target_failures,
        )
        windows = tuple(
            window
            for window in candidate_windows
            if (
                (
                    fingerprint := normalize_launch_fingerprint(
                        window.launch_fingerprint
                    )
                )
                not in target_failures
                and (
                    self._allowed_fingerprints is None
                    or fingerprint in self._allowed_fingerprints
                )
            )
        )
        self._begin_capture_diagnostics(windows)
        source_failure_affected_fingerprints = (
            self._source_failure_affected_fingerprints(
                windows,
                global_failures,
                target_failures,
            )
        )
        self._clear_recovered_source_revocations(
            candidate_windows,
            global_failures,
            target_failures,
        )
        runtime_authority_revoked = False
        tcp_recovery_authority = self._tcp_recovery_authority
        machine_pending_reopen = frozenset(
            (tcp_recovery_authority.fingerprint,)
            if (
                tcp_recovery_authority is not None
                and not tcp_recovery_authority.terminal
                and tcp_recovery_authority.stage
                in {
                    _TcpRecoveryStage.CLOSE_VERIFIED,
                    _TcpRecoveryStage.REOPEN_PENDING,
                    _TcpRecoveryStage.SHORTCUT_REQUESTED,
                    _TcpRecoveryStage.WAITING_NEW_INSTANCE,
                }
            )
            else ()
        )
        pending_reopen = frozenset(
            self._pending_reopen_fingerprints
        ) | machine_pending_reopen
        formal_reopen = (
            pending_reopen & self._login_only_recovery_fingerprints
        ) | machine_pending_reopen
        plan = self._group_launch_plan
        resolved_contract = self._tcp_v
        contract_plan_unsafe = bool(
            formal_reopen
            and (
                len(formal_reopen) != 1
                or plan is None
                or not isinstance(resolved_contract, ResolvedTargetWindows)
                or tuple(target.entry_id for target in plan.targets)
                != resolved_contract.sync_scope_entry_ids
                or not plan.targets
                or any(
                    not target.entry_id or not target.role_id
                    for target in plan.targets
                    if target.fingerprint in formal_reopen
                )
            )
        )
        expected_reopen_absence = bool(
            len(pending_reopen) == 1
            and next(iter(pending_reopen)) in target_failures
            and not snapshot_failures
            and not global_failures
            and not contract_plan_unsafe
            and set(
                target_failures.get(next(iter(pending_reopen)), ())
            ) == {"window_offline"}
            and self._unique_complete_candidate_instances(candidate_windows)
            is not None
        )
        if source_failure_affected_fingerprints:
            pending_owner = (
                next(iter(pending_reopen))
                if len(pending_reopen) == 1
                else None
            )
            for fingerprint in source_failure_affected_fingerprints:
                if fingerprint == pending_owner and expected_reopen_absence:
                    # Preserve only this exact owner long enough to collect
                    # two fresh absence captures.  A mapped sibling failure
                    # cannot change this owner's evidence.
                    with self._screen_state_lock:
                        self._last_screen_states[fingerprint] = (
                            ReconnectScreenState.UNKNOWN
                        )
                        self._action_confirmations.pop(fingerprint, None)
                    continue
                self._revoke_source_failure_evidence(
                    frozenset((fingerprint,)),
                    # Any unsafe local role loses its own runtime authority
                    # immediately.  Healthy peers rebuild next scan and can
                    # become the single TCP owner without a global 60-second
                    # freeze.
                    revoke_runtime_authority=True,
                    refresh_source_generation=True,
                )
        if contract_plan_unsafe and not runtime_authority_revoked:
            self.set_execution_enabled(False)
            runtime_authority_revoked = True
        scan_source_state_generation = (
            self._source_state_generation_snapshot()
        )
        state_before = self._runtime_state_signature()
        now = self._monotonic_clock()
        group_failures = self._group_failures(
            windows,
            locally_isolated_fingerprints=frozenset(target_failures),
        )
        source_identity_unsafe = bool(
            global_failures
            or snapshot_failures
            or contract_plan_unsafe
            or direct_identity_collisions
            or self._unique_complete_candidate_instances(
                candidate_windows
            )
            is None
        )
        blocking_group_failures = tuple(
            code for code in group_failures
            if code != "group_identity_set_mismatch"
        )
        if source_identity_unsafe or blocking_group_failures:
            self._retain_tcp_online_witnesses_for_contract(
                resolved_contract
            )
            self._tcp_s.clear()
            self._tcp_observation = _TcpObservation(
                generation=self._tcp_gen,
                observed_at_monotonic=None,
                query_succeeded=False,
                observed_window_count=0,
            )
            tcp_failures: tuple[str, ...] = ()
            tcp_evidence: tuple[tuple[str, _TcpState], ...] = ()
            tcp_observed_at: float | None = None
        else:
            (
                tcp_failures,
                tcp_evidence,
                tcp_observed_at,
            ) = self._tcp_failures(windows, now)
        if execute and tcp_observed_at is not None:
            # Query completion, not the stale cycle-start timestamp, is the
            # first point at which an existing owner can be known overdue.
            self._check_reconnect_timing_deadlines(tcp_observed_at)
        tcp_owner, tcp_owner_state, tcp_owner_failure = self._ordered_tcp_owner(
            tcp_evidence
        )
        if (
            execute
            and tcp_owner is not None
            and tcp_owner_state is not None
        ):
            # The TCP 60-second contract starts when one confirmed owner gains
            # the only action authorization, before captures, two-frame
            # confirmation, final TCP recheck, or any slow backend call.
            self._start_reconnect_timing(
                tcp_owner,
                "tcp_disconnect_to_connected",
                "tcp_owner_confirmed",
                tcp_observed_at if tcp_observed_at is not None else now,
            )
        mutation_veto = bool(
            runtime_authority_revoked
            or tcp_owner_failure
        )
        mutation_execute = execute and not mutation_veto
        if mutation_veto:
            self._clear_action_confirmation()
        if source_identity_unsafe:
            # A source-reported failure or unsafe live instance interrupts
            # every pending action frame.  No safe-looking peer may rebuild
            # confirmation until the source itself is complete again.
            self._clear_action_confirmation()
        selected_group_complete = self._selected_group_is_complete(
            windows,
            locally_isolated_fingerprints=frozenset(target_failures),
        )
        reopen_safety_failures = self._reopen_safety_failures(
            candidate_windows,
            global_failures,
            target_failures,
            (
                tcp_recovery_authority.fingerprint
                if (
                    tcp_recovery_authority is not None
                    and not tcp_recovery_authority.terminal
                )
                else tcp_owner
            ),
        )
        if (
            self._tcp_recovery_authority is None
            and tcp_owner is not None
            and tcp_owner_state is None
            and tcp_owner in self._pending_reopen_fingerprints
        ):
            # Preserve the pre-existing non-TCP visual-battle retry path.
            # Formal TCP owners always have a private authority record and
            # therefore continue exclusively through the state machine.
            retried_reopens, retry_failures, pending_reopen_delay = (
                self._retry_pending_reopens(
                    candidate_windows=candidate_windows,
                    global_failures=global_failures,
                    target_failures=target_failures,
                    execute=mutation_execute,
                    now=now,
                    expected_capture_settings_revision=(
                        capture_settings_revision
                    ),
                    expected_source_state_generation=(
                        scan_source_state_generation
                    ),
                    safety_failures=tuple(reopen_safety_failures),
                    owner=tcp_owner,
                )
            )
        else:
            retried_reopens, retry_failures, pending_reopen_delay = (
                self._advance_tcp_recovery(
                    owner=tcp_owner,
                    owner_state=tcp_owner_state,
                    execute=mutation_execute,
                    now=now,
                    source_state_generation=scan_source_state_generation,
                )
            )
        failures = [
            *group_failures,
            *global_failures,
            *(
                code
                for codes in target_failures.values()
                for code in codes
            ),
            *snapshot_failures,
            *retry_failures,
            *tcp_failures,
        ]
        if any(
            fingerprint in self._detection_only_fingerprints
            for fingerprint, state in tcp_evidence
        ):
            failures.append("recovery_identity_unavailable")
        if tcp_owner_failure:
            failures.append(tcp_owner_failure)
        if blocking_group_failures:
            if (
                self._action_confirmations
                and self._allowed_fingerprints is not None
                and not selected_group_complete
            ):
                failures.append("input_target_changed_before_delivery")
            scan_complete_group_fingerprints = (
                frozenset(
                    fingerprint
                    for fingerprint in (
                        normalize_launch_fingerprint(window.launch_fingerprint)
                        for window in windows
                    )
                    if fingerprint is not None
                )
                if (self._allowed_fingerprints is not None and not selected_group_complete)
                else frozenset()
            )
            # A partially validated group must never carry a first-frame
            # confirmation or old CONNECTED evidence into a later, different
            # group or identity set.
            self._clear_action_confirmation()
            self._revoke_group_failure_evidence(
                windows,
                frozenset(target_failures),
            )
            if (
                self._runtime_state_store is not None
                and state_before != self._runtime_state_signature()
                and not self._persist_runtime_state()
            ):
                failures.append("reconnect_state_persistence_failed")
            if not self._evidence_available():
                failures.append("evidence_recording_unavailable")
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
                source_missing_windows=len(
                    source_failure_affected_fingerprints
                ),
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
                capture_diagnostics=self.anonymous_capture_diagnostics(),
                timing_diagnostics=(
                    self.anonymous_reconnect_timing_diagnostics()
                ),
                tcp_observation=self._tcp_observation.items(),
            )
            self._last_result = result
            self._publish_reconnecting_fingerprints(
                now,
                observed_fingerprints=scan_complete_group_fingerprints,
            )
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
        session_fingerprints = (
            self._pending_reconnect_fingerprints
            | self._pending_reopen_fingerprints
            | self._active_automation_fingerprints
        )
        missing_pending_reopen_targets = (
            self._pending_reopen_fingerprints - live_fingerprints
        )
        if missing_pending_reopen_targets:
            # While an original role is still being reopened, no other role
            # may receive a reconnect click from this scan.
            self._clear_action_confirmation()
        retained_deadline_fingerprints = (
            live_fingerprints | session_fingerprints
        )
        self._action_confirmations = {
            fingerprint: confirmation
            for fingerprint, confirmation in self._action_confirmations.items()
            if fingerprint in live_fingerprints
        }
        self._action_retry_after = {
            fingerprint: retry
            for fingerprint, retry in self._action_retry_after.items()
            if fingerprint in retained_deadline_fingerprints
        }
        self._action_state_since = {
            fingerprint: state_and_time
            for fingerprint, state_and_time in self._action_state_since.items()
            if fingerprint in live_fingerprints
        }
        self._flow_pause_until = {
            fingerprint: deadline
            for fingerprint, deadline in self._flow_pause_until.items()
            if fingerprint in retained_deadline_fingerprints
        }
        recognized: list[tuple[WindowInfo, str, ScreenRecognition]] = []
        capture_routes: dict[str, str | None] = {}
        fresh_capture_instances: dict[
            str,
            tuple[WindowInstanceToken, str],
        ] = {}
        auto_battle_capture_samples: dict[str, object | None] = {}
        confirmed_action_instances: dict[str, WindowInstanceToken] = {}
        tcp_action_entries: dict[
            str, tuple[str, WindowInstanceToken]
        ] = {}
        terminal_completed_fingerprints: set[str] = set()
        initial_authorized_action_fingerprints: set[str] = set()
        current_initial_login_authorizations: set[str] = set()
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
            (
                sample,
                recognition,
                fresh_capture,
                capture_route,
            ) = self._capture_and_recognize(
                window,
                fingerprint,
                execute=mutation_execute,
                expected_source_state_generation=(
                    scan_source_state_generation
                ),
                diagnostic_stage="scan",
            )
            capture_routes[fingerprint] = capture_route
            _, current_capture_settings_revision = (
                self._capture_settings_snapshot()
            )
            if (
                current_capture_settings_revision
                != capture_settings_revision
            ):
                recognition = ScreenRecognition(
                    state=ReconnectScreenState.UNKNOWN,
                    score=None,
                    click_point=None,
                    reference_name=None,
                )
                fresh_capture = False
            instance = WindowInstanceToken.from_window(window)
            initial_login_authorized = (
                self._initial_login_authorization_is_current(
                    fingerprint,
                    instance,
                    capture_settings_revision,
                    scan_source_state_generation,
                    now,
                )
            )
            if initial_login_authorized:
                current_initial_login_authorizations.add(fingerprint)
            if (
                fresh_capture
                and instance is not None
                and capture_route is not None
            ):
                fresh_capture_instances[fingerprint] = (
                    instance,
                    capture_route,
                )
                auto_battle_capture_samples[fingerprint] = sample
                timeout_event = _BattleRestartEvent.from_instance(instance)
                previous_timeout_event = (
                    self._force_login_timeout_attempts.get(fingerprint)
                )
                if previous_timeout_event is not None:
                    if previous_timeout_event != timeout_event:
                        # A complete window-session replacement is the only
                        # identity change that can start a separate timeout
                        # event without first proving the old dialog gone.
                        self._force_login_timeout_attempts.pop(
                            fingerprint,
                            None,
                        )
                        retry = self._action_retry_after.get(fingerprint)
                        if (
                            retry is not None
                            and retry[0]
                            is ReconnectScreenState.FORCE_LOGIN_TIMEOUT
                        ):
                            self._action_retry_after.pop(fingerprint, None)
                    elif recognition.state not in {
                        ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
                        ReconnectScreenState.UNKNOWN,
                        ReconnectScreenState.CHECK_DISABLED,
                    }:
                        # A fresh recognized non-timeout frame proves this
                        # dialog event ended.  A later timeout may then form
                        # new two-frame evidence.
                        self._force_login_timeout_attempts.pop(
                            fingerprint,
                            None,
                        )
                if recognition.state is ReconnectScreenState.LINE_SELECTION:
                    recent_role = self._complete_role_identity(
                        recognition.recent_login_role
                    )
                    if (
                        recognition.recent_line_present is True
                        and recent_role is not None
                    ):
                        self._recent_login_role_ids[fingerprint] = recent_role
                    elif recognition.recent_line_present is False:
                        self._recent_login_role_ids.pop(fingerprint, None)
                if (
                    recognition.state
                    in {
                        ReconnectScreenState.LOGIN_START,
                        ReconnectScreenState.FORCE_LOGIN_START,
                    }
                    and (
                        self._has_reconnect_session(fingerprint)
                        or initial_login_authorized
                    )
                    and not (
                        fingerprint
                        in self._login_only_recovery_fingerprints
                        and (
                            (target := self._target_for_fingerprint(
                                fingerprint
                            )) is None
                            or target.importance
                            is not CharacterImportance.PRIMARY
                        )
                    )
                ):
                    self._start_reconnect_timing(
                        fingerprint,
                        "start_game_to_primary_connected",
                        recognition.state.value,
                        now,
                    )
                self._advance_reconnect_timing(
                    fingerprint,
                    recognition.state.value,
                    now,
                )
            if sample is not None and sample.api_succeeded:
                captured_windows += 1
            else:
                self._clear_action_confirmation(fingerprint)
            if recognition.state is ReconnectScreenState.CONNECTED:
                self._initial_login_authorizations.pop(
                    fingerprint,
                    None,
                )
                initial_login_authorized = False
                if self._has_reconnect_session(fingerprint):
                    if (
                        fingerprint not in self._terminal_ready_after
                        and fresh_capture
                    ):
                        # Preserve the existing fresh terminal evidence gate.
                        self._arm_terminal_completion(
                            fingerprint,
                            now,
                        )
                    primary_entry_authorized = (
                        fingerprint in self._primary_entry_authorized
                    )
                    reconnect_entry_authorized = (
                        fingerprint in self._reconnect_entry_authorized
                    )
                    terminal_now = now
                    terminal_budget_current = False
                    terminal_confirmed = (
                        fingerprint in self._terminal_ready_after
                        and self._terminal_completion_confirmed(
                            fingerprint,
                            window,
                            sample,
                            now,
                        )
                        and (
                            primary_entry_authorized
                            or reconnect_entry_authorized
                        )
                    )
                    if terminal_confirmed:
                        # The probe/capture above may itself have crossed the
                        # owner deadline.  Never pop a flow or publish a
                        # completed reconnect using the old cycle timestamp.
                        terminal_now = self._monotonic_clock()
                        terminal_budget_current = (
                            self._reconnect_budget_current(
                                fingerprint,
                                terminal_now,
                            )
                        )
                    if (
                        terminal_confirmed
                        and terminal_budget_current
                    ):
                        self._advance_tcp_screen_stage(
                            fingerprint,
                            recognition,
                        )
                        self._complete_reconnect_timing(
                            fingerprint,
                            "tcp_disconnect_to_connected",
                            terminal_now,
                        )
                        if primary_entry_authorized:
                            self._complete_reconnect_timing(
                                fingerprint,
                                "start_game_to_primary_connected",
                                terminal_now,
                            )
                            self._primary_connected_fingerprints.add(
                                fingerprint
                            )
                            if not self._auto_battle_enabled:
                                self._complete_reconnect_timing(
                                    fingerprint,
                                    "disconnect_to_primary_auto",
                                    terminal_now,
                                )
                        self._clear_reconnect_session(fingerprint)
                        # The terminal scan is still part of the reconnect
                        # transaction.  Do not let a later auto-battle pass in
                        # this same scan operate on the just-restored window.
                        terminal_completed_fingerprints.add(fingerprint)
                        self._clear_reconnect_failure(fingerprint)
                        if primary_entry_authorized and (
                            not self._auto_battle_enabled
                            or (
                                fingerprint,
                                "disconnect_to_primary_auto",
                            )
                            not in self._reconnect_timing_flows
                        ):
                            self._primary_entry_authorized.discard(
                                fingerprint
                            )
                            self._primary_connected_fingerprints.discard(
                                fingerprint
                            )
                    else:
                        # A changing, responsive gameplay frame is required
                        # after the final delivered login action. Until then,
                        # retain reconnect authority but never click gameplay.
                        recognition = replace(
                            recognition,
                            state=ReconnectScreenState.RECONNECTING,
                            click_point=None,
                        )
                else:
                    self._terminal_evidence.pop(fingerprint, None)
                    self._clear_reconnect_failure(fingerprint)
            if (
                initial_login_authorized
                and recognition.state is ReconnectScreenState.LOGIN_START
            ):
                recognition = replace(
                    recognition,
                    state=ReconnectScreenState.FORCE_LOGIN_START,
                    click_point=FORCE_LOGIN_CLICK_POINT,
                )
            recognition = self._recognition_for_session_action(
                fingerprint,
                recognition,
                initial_login_authorized=initial_login_authorized,
            )
            if recognition.state is not ReconnectScreenState.CONNECTED:
                self._advance_tcp_screen_stage(
                    fingerprint,
                    recognition,
                )
            if (
                fingerprint in self._pending_reopen_fingerprints
                and fingerprint in self._login_only_recovery_fingerprints
                and recognition.state in _POST
            ):
                recognition = replace(recognition, click_point=None)
            if (
                recognition.state is ReconnectScreenState.DISCONNECTED
                and fingerprint not in self._pending_reopen_fingerprints
            ):
                # A later ordinary disconnect is a new non-restart flow and
                # may use the existing post-login/auto-battle policy again.
                self._login_only_recovery_fingerprints.discard(fingerprint)
            recognition = self._recognition_for_preferred_line(
                fingerprint,
                recognition,
            )
            if (
                recognition.state is ReconnectScreenState.FORCE_LOGIN_TIMEOUT
                and instance is not None
                and self._force_login_timeout_attempts.get(fingerprint)
                == _BattleRestartEvent.from_instance(instance)
            ):
                # The same dialog is still present.  Its first confirmed
                # delivery was final even if capture authority changed later.
                self._clear_action_confirmation(fingerprint)
                recognition = replace(recognition, click_point=None)
            if (
                recognition.state is ReconnectScreenState.CHARACTER_SELECTION
                and recognition.click_point is None
            ):
                self._clear_action_confirmation(fingerprint)
            if recognition.state not in {
                ReconnectScreenState.CONNECTED,
                ReconnectScreenState.RECONNECTING,
            }:
                self._terminal_evidence.pop(fingerprint, None)
            previous_state = self._action_state_since.get(fingerprint)
            if (
                instance is None
                or previous_state is None
                or previous_state[0] != instance
                or previous_state[1] is not recognition.state
            ):
                if (
                    previous_state is not None
                    and instance is not None
                    and previous_state[0] == instance
                    and previous_state[1] is not recognition.state
                ):
                    preserve_login_cooldown = bool(
                        previous_state[1]
                        in {
                            ReconnectScreenState.DISCONNECTED,
                            ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
                        }
                        and recognition.state
                        is ReconnectScreenState.FORCE_LOGIN_START
                    )
                    if not preserve_login_cooldown:
                        # Other proven state transitions are readiness
                        # signals and can continue immediately.
                        self._flow_pause_until.pop(fingerprint, None)
                if instance is None:
                    self._action_state_since.pop(fingerprint, None)
                else:
                    self._action_state_since[fingerprint] = (
                        instance,
                        recognition.state,
                        now,
                    )
            action = self._policy.decide(recognition.state).action
            # Formal TCP recovery is advanced once, before capture, by the
            # owner-local state machine.  It never enters the legacy visual
            # battle restart path below.
            tcp_action = False
            is_action_candidate = (
                tcp_action
                or (
                    action in ACTIONABLE_RECONNECT_ACTIONS
                    and recognition.click_point is not None
                    and (
                        recognition.state is ReconnectScreenState.DISCONNECTED
                        or (
                            recognition.state in _SESSION_ONLY_STATES
                            and (
                                self._has_reconnect_session(fingerprint)
                                or initial_login_authorized
                            )
                        )
                    )
                )
            )
            action_evidence_complete = (
                not mutation_veto
                and fresh_capture
                and instance is not None
                and capture_route is not None
                and (
                    tcp_action
                    or self._action_signature_is_complete(recognition)
                )
                and (
                    tcp_action
                    or recognition.state
                    not in {
                        ReconnectScreenState.UNKNOWN,
                        ReconnectScreenState.CHECK_DISABLED,
                    }
                )
                and is_action_candidate
            )
            if not action_evidence_complete:
                # A frame with no actionable point, an unknown/disabled
                # result, stale capture, route change, or instance change is
                # an interruption.  It must never bridge two action frames.
                self._clear_action_confirmation(fingerprint)
                if (
                    recognition.state is ReconnectScreenState.DISCONNECTED
                    and instance is not None
                ):
                    # A disconnect with an incomplete action signature cannot
                    # lend its earlier start time to a later safe click.
                    self._action_state_since[fingerprint] = (
                        instance,
                        recognition.state,
                        now,
                    )
            elif (
                not source_identity_unsafe
                and self._action_is_confirmed(
                    fingerprint,
                    recognition,
                    instance=instance,
                    capture_route=capture_route,
                    capture_settings_revision=(
                        capture_settings_revision
                    ),
                    source_state_generation=(
                        scan_source_state_generation
                    ),
                    confirmation_signature=(
                        (
                            "tcp_disconnect_confirmed",
                            tcp_owner_state.entry_id,
                            tcp_owner_state.instance,
                        )
                        if tcp_action and tcp_owner_state is not None
                        else None
                    ),
                )
            ):
                confirmed_action_instances[fingerprint] = instance
                if tcp_action and tcp_owner_state is not None:
                    tcp_action_entries[fingerprint] = (
                        tcp_owner_state.entry_id,
                        tcp_owner_state.instance,
                    )
                if (
                    mutation_execute
                    and recognition.state
                    is ReconnectScreenState.DISCONNECTED
                ):
                    self._primary_entry_authorized.discard(fingerprint)
                    self._primary_connected_fingerprints.discard(
                        fingerprint
                    )
                    self._reconnect_entry_authorized.discard(fingerprint)
                    self._start_reconnect_timing(
                        fingerprint,
                        "disconnect_to_primary_auto",
                        "disconnect_confirmed",
                        now,
                    )
                if (
                    initial_login_authorized
                    and recognition.state in _SESSION_ONLY_STATES
                    and not self._has_reconnect_session(fingerprint)
                ):
                    initial_authorized_action_fingerprints.add(
                        fingerprint
                    )
            elif source_identity_unsafe:
                self._clear_action_confirmation(fingerprint)
            if (
                is_action_candidate
                and fingerprint not in confirmed_action_instances
            ):
                pending_confirmation_delays.append(
                    self._policy.progress_interval_seconds
                )
            retry = self._action_retry_after.get(fingerprint)
            if retry is not None and retry[0] is not recognition.state:
                self._action_retry_after.pop(fingerprint, None)
            if recognition.state not in {
                ReconnectScreenState.DISCONNECTED,
                ReconnectScreenState.UNKNOWN,
            } and not tcp_action:
                # Only a newly proven non-disconnect state ends the current
                # disconnect event.  UNKNOWN never re-arms a restart.
                self._battle_restart_attempts.pop((fingerprint, False), None)
            if recognition.state is not ReconnectScreenState.CHARACTER_SELECTION:
                self._character_selection_pending.discard(fingerprint)
                self._character_selection_targets.pop(fingerprint, None)
            elif (
                recognition.character_slot_selected is True
                and (
                    fingerprint in self._character_selection_pending
                    or fingerprint not in self._terminal_ready_after
                )
            ):
                # Selecting the preferred character does not leave this
                # screen. Permit the next distinct step ("進入遊戲") without
                # waiting for the one-minute retry window. The terminal-ready
                # marker persists across a process restart, so this cannot
                # repeatedly re-arm after an already delivered enter click.
                self._action_retry_after.pop(fingerprint, None)
                self._flow_pause_until.pop(fingerprint, None)
            recognized.append((window, fingerprint, recognition))

        current_capture_settings, current_capture_settings_revision = (
            self._capture_settings_snapshot()
        )
        settings_changed_during_scan = (
            capture_settings_revision
            != current_capture_settings_revision
        )
        source_changed_during_scan = (
            not self._source_authority_is_current(
                scan_source_state_generation
            )
        )
        if settings_changed_during_scan or source_changed_during_scan:
            confirmed_action_instances.clear()
            tcp_action_entries.clear()
            current_initial_login_authorizations.clear()
            recognized = [
                (
                    window,
                    fingerprint,
                    replace(
                        item,
                        state=(
                            ReconnectScreenState.UNKNOWN
                            if source_changed_during_scan
                            else self._revoked_screen_state(
                                current_capture_settings,
                                capture_routes.get(fingerprint),
                            )
                        ),
                        click_point=None,
                    ),
                )
                for window, fingerprint, item in recognized
            ]

        if (
            mutation_execute
            and not settings_changed_during_scan
            and not source_changed_during_scan
        ):
            (
                latest_capture_settings,
                latest_capture_settings_revision,
            ) = self._capture_settings_snapshot()
            with self._source_authority_lock:
                settings_changed_during_scan = (
                    capture_settings_revision
                    != latest_capture_settings_revision
                )
                source_changed_during_scan = (
                    self._source_state_generation
                    != scan_source_state_generation
                )
                if (
                    not settings_changed_during_scan
                    and not source_changed_during_scan
                ):
                    with self._screen_state_lock:
                        self._pending_reconnect_fingerprints.update(
                            fingerprint
                            for _window, fingerprint, item in recognized
                            if (
                                item.state
                                is ReconnectScreenState.DISCONNECTED
                                and fingerprint
                                in confirmed_action_instances
                            )
                        )
                        self._publish_reconnecting_fingerprints(now)
            if settings_changed_during_scan or source_changed_during_scan:
                confirmed_action_instances.clear()
                tcp_action_entries.clear()
                current_initial_login_authorizations.clear()
                recognized = [
                    (
                        window,
                        fingerprint,
                        replace(
                            item,
                            state=(
                                ReconnectScreenState.UNKNOWN
                                if source_changed_during_scan
                                else self._revoked_screen_state(
                                    latest_capture_settings,
                                    capture_routes.get(fingerprint),
                                )
                            ),
                            click_point=None,
                        ),
                    )
                    for window, fingerprint, item in recognized
                ]

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
        unrecognized_windows = state_counts.get(
            ReconnectScreenState.UNKNOWN.value,
            0,
        )
        disabled_windows = state_counts.get(
            ReconnectScreenState.CHECK_DISABLED.value,
            0,
        )
        unknown_windows = unrecognized_windows + disabled_windows
        if captured_windows + disabled_windows != len(windows):
            failures.append("capture_failed")
        if unrecognized_windows:
            failures.append("screen_unknown")
        if disabled_windows:
            failures.append("capture_mode_disabled")
        # TCP can authorize a restart only for its owner.  Without that owner,
        # a normal launch may continue only through its separate, current
        # initial-login grant; visual DISCONNECTED never becomes a fallback.
        tcp_managed_scope = bool(
            self._tcp_counts is not None
            and self._group_launch_plan is not None
            and isinstance(self._tcp_v, ResolvedTargetWindows)
        )
        # General auto-battle remains independently available to healthy
        # windows.  It is narrowed only while one concrete recovery owner is
        # active; zero evidence without an owner must not freeze the group.
        recovery_authority = self._tcp_recovery_authority
        active_recovery_owner = (
            recovery_authority.fingerprint
            if (
                recovery_authority is not None
                and not recovery_authority.terminal
            )
            else tcp_owner
        )
        operation_scope = (
            frozenset((active_recovery_owner,))
            if active_recovery_owner is not None
            else (
                frozenset()
                if tcp_owner_failure
                else None
            )
        )
        if operation_scope is not None:
            for other in tuple(self._action_confirmations):
                if other not in operation_scope:
                    self._clear_action_confirmation(other)
        elif tcp_managed_scope:
            initial_login_scope = {
                fingerprint
                for _window, fingerprint, item in recognized
                if (
                    fingerprint in current_initial_login_authorizations
                    and item.state is not ReconnectScreenState.DISCONNECTED
                )
            }
            for other in tuple(self._action_confirmations):
                if other not in initial_login_scope:
                    self._clear_action_confirmation(other)
        # Identity and group completeness were already validated before any
        # capture.  A capture or recognition failure is local to that exact
        # window: it must never receive input, but it must not prevent another
        # uniquely identified window with two matching disconnected frames
        # from being recovered.
        actionable_candidates = [
            (
                window,
                fingerprint,
                item,
                confirmed_action_instances[fingerprint],
            )
            for window, fingerprint, item in recognized
            if self._policy.decide(item.state).action
            in ACTIONABLE_RECONNECT_ACTIONS
            and item.click_point is not None
            and fingerprint in confirmed_action_instances
            and fingerprint not in self._tcp_timeout_isolated
            and (operation_scope is None or fingerprint in operation_scope)
            and (
                not tcp_managed_scope
                or fingerprint == active_recovery_owner
                or (
                    fingerprint in initial_authorized_action_fingerprints
                    and item.state is not ReconnectScreenState.DISCONNECTED
                )
            )
            and (
                item.state is ReconnectScreenState.DISCONNECTED
                or self._has_reconnect_session(fingerprint)
                or fingerprint
                in initial_authorized_action_fingerprints
            )
            and not (
                item.state is ReconnectScreenState.DISCONNECTED
                and item.battle_context
            )
            and (
                item.state not in _POST
                or fingerprint in self._active_automation_fingerprints
                or fingerprint
                in initial_authorized_action_fingerprints
            )
        ]
        actionable: list[
            tuple[WindowInfo, str, ScreenRecognition, WindowInstanceToken]
        ] = []
        for window, fingerprint, item, instance in actionable_candidates:
            wait_seconds = self._action_wait_seconds(
                fingerprint,
                item.state,
                now,
                instance,
            )
            if wait_seconds:
                pending_action_wait_delays.append(wait_seconds)
            else:
                actionable.append((window, fingerprint, item, instance))
        battle_actionable = [
            (
                window,
                fingerprint,
                item,
                confirmed_action_instances[fingerprint],
            )
            for window, fingerprint, item in recognized
            if (
                item.state is ReconnectScreenState.DISCONNECTED
                and item.battle_context
            )
            and fingerprint in confirmed_action_instances
            and fingerprint not in self._tcp_timeout_isolated
            and not (
                recovery_authority is not None
                and not recovery_authority.terminal
                and recovery_authority.fingerprint == fingerprint
            )
            and (operation_scope is None or fingerprint in operation_scope)
            and (
                not tcp_managed_scope
                or fingerprint == active_recovery_owner
            )
        ]
        clicked_windows = 0
        restarted_windows = retried_reopens
        battle_restart_attempted = False
        tcp_restart_progressed = retried_reopens > 0
        invalid_targets = 0
        unresponsive_targets = 0
        delivery_failures = 0
        if mutation_execute:
            for window, fingerprint, item, instance in battle_actionable:
                if not self._execution_allowed():
                    break
                confirmation = self._action_confirmations.get(fingerprint)
                if (
                    confirmation is None
                    or confirmation.instance != instance
                    or confirmation.capture_settings_revision
                    != capture_settings_revision
                    or confirmation.source_state_generation
                    != scan_source_state_generation
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                capture_route = confirmation.capture_route
                if not self._capture_authority_is_current(
                    capture_settings_revision,
                    capture_route,
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                if not self._source_authority_is_current(
                    confirmation.source_state_generation,
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                wait_seconds = self._action_wait_seconds(
                    fingerprint,
                    item.state,
                    now,
                    instance,
                )
                if wait_seconds:
                    pending_action_wait_delays.append(wait_seconds)
                    continue
                current_battle_window = self._current_action_window(
                    instance,
                    fingerprint,
                )
                if current_battle_window is None:
                    self._clear_action_confirmation(fingerprint)
                    failures.append("input_target_changed_before_delivery")
                    continue
                retry = self._action_retry_after.get(fingerprint)
                if (
                    retry is not None
                    and retry[0] is item.state
                    and now < retry[1]
                ):
                    continue
                tcp_entry = tcp_action_entries.get(fingerprint)
                restart_event = _BattleRestartEvent.from_instance(instance)
                restart_event_key = (fingerprint, tcp_entry is not None)
                if (
                    self._battle_restart_attempts.get(restart_event_key)
                    == restart_event
                ):
                    continue
                plan = self._group_launch_plan
                target = (
                    self._target_for_entry(tcp_entry[0])
                    if tcp_entry is not None
                    else self._target_for_fingerprint(fingerprint)
                )
                if (
                    self._battle_restarter is None
                    or plan is None
                    or target is None
                    or (
                        tcp_entry is not None
                        and target.entry_id != tcp_entry[0]
                    )
                ):
                    self._clear_action_confirmation(fingerprint)
                    failures.append("battle_restart_identity_unresolved")
                    self._report_reconnect_failure(None)
                    continue
                (
                    evidence_ready,
                    evidence_intent,
                    evidence_authority,
                ) = self._begin_evidence_action(
                    fingerprint=fingerprint,
                    item=item,
                    action="restart_window",
                    instance=instance,
                    capture_route=capture_route,
                    capture_settings_revision=capture_settings_revision,
                    source_state_generation=(
                        confirmation.source_state_generation
                    ),
                    input_channel="window_control",
                    allow_unknown=tcp_entry is not None,
                )
                if not evidence_ready:
                    failures.append("evidence_recording_unavailable")
                    self._clear_action_confirmation(fingerprint)
                    continue
                battle_restart_attempted = True

                def restart_confirmed_battle_window():
                    current_capture_route = self._action_still_matches(
                        instance,
                        fingerprint,
                        item,
                        capture_settings_revision,
                        capture_route,
                        confirmation.source_state_generation,
                        require_recognition_match=tcp_entry is None,
                    )
                    if current_capture_route != capture_route:
                        return None
                    refreshed_window = self._current_action_window(
                        instance,
                        fingerprint,
                    )
                    if refreshed_window is None:
                        return None
                    owner_window = refreshed_window
                    pre_candidates: tuple[WindowInfo, ...] = tuple(
                        candidate_windows
                    )
                    owner_deadline: float | None = None
                    if tcp_entry is not None:
                        resolved = self._tcp_v
                        if (
                            tcp_entry[1] != instance
                            or self._tcp_id(
                                resolved,
                                fingerprint,
                                instance,
                            ) != tcp_entry[0]
                            or not isinstance(resolved, ResolvedTargetWindows)
                        ):
                            return None
                        counts, generation, final_observed_at = (
                            self._observe_tcp_counts(
                            self._tcp_s.values(),
                            self._monotonic_clock(),
                            )
                        )
                        if (
                            counts is None
                            or generation is None
                            or final_observed_at is None
                        ):
                            # An unavailable final observation invalidates the
                            # entire zero sequence.  It never consumes an
                            # untouched restart event.
                            for state in self._tcp_s.values():
                                state.zero_since = None
                                state.zero_count = 0
                                state.gen = generation or self._tcp_gen
                            return None
                        target_state = self._tcp_s.get(
                            (tcp_entry[0], instance)
                        )
                        if (
                            target_state is None
                            or counts.get(instance.process_id) != 0
                        ):
                            # Peers may safely remain at zero; only a recovered
                            # selected owner loses this action authorization.
                            if target_state is not None:
                                target_state.online = True
                                target_state.zero_since = None
                                target_state.zero_count = 0
                                target_state.gen = generation
                            return None
                        # This final fresh query belongs to the same TCP
                        # observation chain.  Retain a still-zero peer's
                        # independent proof by advancing only its generation;
                        # a peer that recovered is reset locally.  Otherwise
                        # the mandatory final owner query would create an
                        # artificial generation gap and make the next queued
                        # owner re-prove the same disconnect from scratch.
                        for _entry_id, state in self._tcp_s.items():
                            if counts.get(state.instance.process_id, 0) > 0:
                                state.online = True
                                state.zero_since = None
                                state.zero_count = 0
                            state.gen = generation
                        self._tcp_observation = replace(
                            self._tcp_observation,
                            confirmed_window_count=sum(
                                state.zero_count >= _TCP_N
                                and state.zero_since is not None
                                and final_observed_at - state.zero_since
                                >= _TCP_T
                                for state in self._tcp_s.values()
                            ),
                        )
                        refreshed_window = self._current_action_window(
                            instance,
                            fingerprint,
                        )
                        if (
                            refreshed_window is None
                            or self._tcp_v != resolved
                            or self._tcp_id(
                                self._tcp_v,
                                fingerprint,
                                instance,
                            ) != tcp_entry[0]
                        ):
                            return None
                        staged_contract = self._pre_close_backend_contract(
                            resolved,
                            tcp_entry[0],
                            instance,
                        )
                        if staged_contract is None:
                            return BattleRestartResult(
                                False,
                                "battle_contract_changed",
                            )
                        owner_window, pre_candidates = staged_contract
                        owner_deadline = self._reconnect_budget_deadline(
                            fingerprint
                        )
                    if not self._reconnect_budget_current(
                        fingerprint,
                        self._monotonic_clock(),
                    ):
                        return BattleRestartResult(
                            False,
                            "tcp_reconnect_timeout",
                        )
                    authorized, close_result = self._run_authorized_backend_call(
                        lambda: (
                            self._battle_restart_attempts.__setitem__(
                                restart_event_key,
                                restart_event,
                            )
                            or self._battle_restarter.close_verified(
                                owner_window,
                                pre_candidates,
                                deadline=owner_deadline,
                            )
                        ),
                        expected_capture_settings_revision=(
                            capture_settings_revision
                        ),
                        capture_route=capture_route,
                        expected_source_state_generation=(
                            confirmation.source_state_generation
                        ),
                        additional_authorization_check=(
                            lambda: (
                                self._action_confirmations.get(fingerprint)
                                == confirmation
                                and self._group_launch_plan is plan
                                and self._reconnect_budget_current(
                                    fingerprint,
                                    self._monotonic_clock(),
                                )
                            )
                        ),
                    )
                    if not authorized or not isinstance(
                        close_result,
                        BattleRestartResult,
                    ):
                        return None
                    if not close_result.success or not close_result.window_closed:
                        return close_result
                    if tcp_entry is None:
                        # Legacy visual recovery keeps the same static native
                        # contract but has no TCP owner transition to prove.
                        post_candidates = tuple(
                            candidate
                            for candidate in pre_candidates
                            if complete_window_instance_identity(candidate)
                            != complete_window_instance_identity(owner_window)
                        )
                    else:
                        self._candidate_window_set()
                        post_contract = self._tcp_v
                        post_candidates = (
                            self._post_close_backend_contract(
                                resolved,
                                post_contract,
                                tcp_entry[0],
                                owner_window,
                            )
                            if isinstance(
                                post_contract,
                                ResolvedTargetWindows,
                            )
                            else None
                        )
                    if post_candidates is None:
                        return BattleRestartResult(
                            False,
                            "battle_contract_changed",
                            window_closed=True,
                        )
                    authorized, reopen_result = (
                        self._run_authorized_backend_call(
                            lambda: self._battle_restarter.reopen_missing(
                                target,
                                post_candidates,
                                deadline=owner_deadline,
                            ),
                            expected_capture_settings_revision=(
                                capture_settings_revision
                            ),
                            capture_route=capture_route,
                            expected_source_state_generation=(
                                confirmation.source_state_generation
                            ),
                            additional_authorization_check=(
                                lambda: (
                                    self._action_confirmations.get(fingerprint)
                                    == confirmation
                                    and self._group_launch_plan is plan
                                    and self._reconnect_budget_current(
                                        fingerprint,
                                        self._monotonic_clock(),
                                    )
                                )
                            ),
                        )
                    )
                    if not authorized or not isinstance(
                        reopen_result,
                        BattleRestartResult,
                    ):
                        return BattleRestartResult(
                            False,
                            "authorization_revoked",
                            window_closed=True,
                        )
                    return BattleRestartResult(
                        reopen_result.success,
                        reopen_result.failure_code,
                        window_closed=True,
                        shortcut_open_requested=(
                            reopen_result.shortcut_open_requested
                        ),
                    )

                permitted, mutation_result = self._run_game_mutation(
                    "smart-reconnect-battle-restart",
                    restart_confirmed_battle_window,
                    expected_capture_settings_revision=(
                        capture_settings_revision
                    ),
                    capture_route=capture_route,
                    expected_source_state_generation=(
                        confirmation.source_state_generation
                    ),
                )
                evidence_restart_result = (
                    mutation_result
                    if isinstance(mutation_result, BattleRestartResult)
                    else None
                )
                self._finish_evidence_action(
                    fingerprint=fingerprint,
                    item=item,
                    action="restart_window",
                    intent_sequence=evidence_intent,
                    allowed=permitted,
                    performed=bool(
                        evidence_restart_result is not None
                        and (
                            evidence_restart_result.window_closed
                            or evidence_restart_result.shortcut_open_requested
                        )
                    ),
                    clicked=False,
                    identity_verified=True,
                    restoration_verified=None,
                    failure_reason=(
                        evidence_restart_result.failure_code
                        if evidence_restart_result is not None
                        and evidence_restart_result.failure_code is not None
                        else (
                            "authorization_revoked"
                            if not permitted
                            else (
                                "restart_result_invalid"
                                if evidence_restart_result is None
                                else None
                            )
                        )
                    ),
                    authority_signature=evidence_authority,
                    input_channel="window_control",
                )
                if not permitted or not isinstance(
                    mutation_result,
                    BattleRestartResult,
                ):
                    self._clear_action_confirmation(fingerprint)
                    if self._capture_authority_is_current(
                        capture_settings_revision,
                        capture_route,
                    ) and self._source_authority_is_current(
                        confirmation.source_state_generation,
                    ):
                        pending_action_wait_delays.append(1)
                    continue
                restart_result = mutation_result
                if not self._capture_authority_is_current(
                    capture_settings_revision,
                    capture_route,
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                if not self._source_authority_is_current(
                    confirmation.source_state_generation,
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                mutation_completed_at = self._monotonic_clock()
                if tcp_entry is not None:
                    # A slow close/reopen backend consumes the same owner
                    # budget that began at TCP confirmation.  Do not allow a
                    # post-backend click or session write after sixty seconds.
                    self._check_reconnect_timing_deadlines(
                        mutation_completed_at
                    )
                    if fingerprint in self._tcp_timeout_isolated:
                        failures.append("tcp_reconnect_timeout")
                        self._clear_action_confirmation(fingerprint)
                        continue
                self._action_retry_after[fingerprint] = (
                    item.state,
                    mutation_completed_at
                    + self._policy.progress_interval_seconds,
                )
                if not restart_result.success:
                    failures.append(
                        restart_result.failure_code
                        or "battle_restart_failed"
                    )
                    self._report_reconnect_failure(fingerprint)
                    if restart_result.window_closed:
                        self._login_only_recovery_fingerprints.add(
                            fingerprint
                        )
                        self._pending_reconnect_fingerprints.add(
                            fingerprint
                        )
                        self._pending_reopen_fingerprints.add(fingerprint)
                        self._reopen_retry_after[fingerprint] = (
                            mutation_completed_at
                            + self._policy.progress_interval_seconds
                        )
                    continue
                restarted_windows += 1
                self._login_only_recovery_fingerprints.add(fingerprint)
                self._pending_reconnect_fingerprints.add(fingerprint)
                if tcp_entry is not None:
                    tcp_restart_progressed = True
                self._pending_reopen_fingerprints.add(fingerprint)
                self._reopen_retry_after[fingerprint] = (
                    mutation_completed_at
                    + self._policy.progress_interval_seconds
                )
                self._active_automation_fingerprints.add(fingerprint)
                self._active_automation_until[fingerprint] = (
                    mutation_completed_at
                    + POST_LOGIN_AUTOMATION_GRACE_SECONDS
                )
            if tcp_restart_progressed:
                failures = [
                    code
                    for code in failures
                    if code != "tcp_disconnect_confirmed"
                ]
            for window, fingerprint, item, confirmed_instance in actionable:
                if not self._execution_allowed():
                    break
                if (
                    fingerprint
                    in initial_authorized_action_fingerprints
                    and not self._initial_login_authorization_is_current(
                        fingerprint,
                        confirmed_instance,
                        capture_settings_revision,
                        scan_source_state_generation,
                    )
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                confirmation = self._action_confirmations.get(fingerprint)
                if (
                    confirmation is None
                    or confirmation.instance != confirmed_instance
                    or confirmation.capture_settings_revision
                    != capture_settings_revision
                    or confirmation.source_state_generation
                    != scan_source_state_generation
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                initial_capture_route = confirmation.capture_route
                if not self._capture_authority_is_current(
                    capture_settings_revision,
                    initial_capture_route,
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                if not self._source_authority_is_current(
                    confirmation.source_state_generation,
                ):
                    self._clear_action_confirmation(fingerprint)
                    continue
                retry = self._action_retry_after.get(fingerprint)
                if (
                    retry is not None
                    and retry[0] is item.state
                    and now < retry[1]
                ):
                    continue
                uses_initial_login_authorization = (
                    fingerprint
                    in initial_authorized_action_fingerprints
                )
                is_line_scroll = bool(
                    item.state is ReconnectScreenState.LINE_SELECTION
                    and item.line_scroll_delta in {-120, 120}
                )

                def deliver_click():
                    current_window = self._current_action_window(
                        confirmed_instance,
                        fingerprint,
                    )
                    if current_window is None:
                        return "changed", False
                    current_capture_route = self._action_still_matches(
                        confirmed_instance,
                        fingerprint,
                        item,
                        capture_settings_revision,
                        initial_capture_route,
                        confirmation.source_state_generation,
                    )
                    if current_capture_route != initial_capture_route:
                        return "changed", False
                    current_window = self._current_action_window(
                        confirmed_instance,
                        fingerprint,
                    )
                    if current_window is None:
                        return "changed", False
                    if not self._mouse_backend.is_window(
                        current_window.handle
                    ):
                        return "invalid", False
                    current_window = self._current_action_window(
                        confirmed_instance,
                        fingerprint,
                    )
                    if current_window is None:
                        return "changed", False
                    if not self._mouse_backend.probe_responsive(
                        current_window.handle,
                        self._preflight_timeout_ms,
                    ):
                        return "unresponsive", False
                    current_window = self._current_action_window(
                        confirmed_instance,
                        fingerprint,
                    )
                    if (
                        current_window is None
                        or current_window.minimized
                        or not self._capture_authority_is_current(
                            capture_settings_revision,
                            current_capture_route,
                        )
                        or not self._source_authority_is_current(
                            confirmation.source_state_generation,
                        )
                    ):
                        return "changed", False
                    if (
                        uses_initial_login_authorization
                        and not self._initial_login_authorization_is_current(
                            fingerprint,
                            confirmed_instance,
                            capture_settings_revision,
                            confirmation.source_state_generation,
                        )
                    ):
                        return "changed", False
                    try:
                        permitted, click_result = (
                            self._run_authorized_backend_call(
                                lambda: (
                                    self._mouse_backend.scroll_relative(
                                        confirmed_instance.handle,
                                        item.click_point,
                                        item.line_scroll_delta,
                                        confirmed_instance.process_id,
                                        confirmed_instance,
                                    )
                                    if is_line_scroll
                                    and callable(
                                        getattr(
                                            self._mouse_backend,
                                            "scroll_relative",
                                            None,
                                        )
                                    )
                                    else (
                                        MouseClickResult(
                                            False,
                                            True,
                                            False,
                                            "input_wheel_backend_unavailable",
                                        )
                                        if is_line_scroll
                                        else self._mouse_backend.click_relative(
                                            confirmed_instance.handle,
                                            item.click_point,
                                            confirmed_instance.process_id,
                                            confirmed_instance,
                                        )
                                    )
                                ),
                                expected_capture_settings_revision=(
                                    capture_settings_revision
                                ),
                                capture_route=current_capture_route,
                                expected_source_state_generation=(
                                    confirmation.source_state_generation
                                ),
                                additional_authorization_check=(
                                    lambda: self._reconnect_budget_current(
                                        fingerprint,
                                        self._monotonic_clock(),
                                    )
                                ),
                            )
                        )
                        if not permitted:
                            return "changed", False
                        return (
                            "delivered",
                            click_result,
                        )
                    except OSError:
                        return (
                            "delivery_failed",
                            MouseClickResult(
                                False,
                                False,
                                True,
                                "input_delivery_os_error",
                            ),
                        )

                evidence_action = self._evidence_action_name(
                    item,
                    line_scroll=is_line_scroll,
                )
                (
                    evidence_ready,
                    evidence_intent,
                    evidence_authority,
                ) = (
                    self._begin_evidence_action(
                        fingerprint=fingerprint,
                        item=item,
                        action=evidence_action,
                        instance=confirmed_instance,
                        capture_route=initial_capture_route,
                        capture_settings_revision=(
                            capture_settings_revision
                        ),
                        source_state_generation=(
                            confirmation.source_state_generation
                        ),
                    )
                )
                if not evidence_ready:
                    failures.append("evidence_recording_unavailable")
                    self._clear_action_confirmation(fingerprint)
                    continue
                permitted, mutation_result = self._run_game_mutation(
                    "smart-reconnect-click",
                    deliver_click,
                    expected_capture_settings_revision=(
                        capture_settings_revision
                    ),
                    capture_route=initial_capture_route,
                    expected_source_state_generation=(
                        confirmation.source_state_generation
                    ),
                )
                evidence_delivery_state = (
                    mutation_result[0]
                    if isinstance(mutation_result, tuple)
                    and len(mutation_result) == 2
                    else None
                )
                evidence_click_result = (
                    mutation_result[1]
                    if isinstance(mutation_result, tuple)
                    and len(mutation_result) == 2
                    and isinstance(mutation_result[1], MouseClickResult)
                    else None
                )
                self._finish_evidence_action(
                    fingerprint=fingerprint,
                    item=item,
                    action=evidence_action,
                    intent_sequence=evidence_intent,
                    allowed=permitted,
                    performed=(
                        permitted
                        and evidence_click_result is not None
                        and (
                            evidence_click_result.delivered
                            or evidence_click_result.delivery_uncertain
                        )
                    ),
                    clicked=bool(
                        evidence_click_result is not None
                        and evidence_click_result.delivered
                    ),
                    identity_verified=True,
                    restoration_verified=(
                        evidence_click_result.restored
                        if evidence_click_result is not None
                        else None
                    ),
                    failure_reason=(
                        evidence_click_result.failure_code
                        if evidence_click_result is not None
                        and evidence_click_result.failure_code is not None
                        else (
                            str(evidence_delivery_state)
                            if evidence_delivery_state is not None
                            and evidence_delivery_state != "delivered"
                            else (
                                "authorization_revoked"
                                if not permitted
                                else None
                            )
                        )
                    ),
                    authority_signature=evidence_authority,
                )
                if not permitted or not isinstance(mutation_result, tuple):
                    self._clear_action_confirmation(fingerprint)
                    if self._capture_authority_is_current(
                        capture_settings_revision,
                        initial_capture_route,
                    ) and self._source_authority_is_current(
                        confirmation.source_state_generation,
                    ):
                        pending_action_wait_delays.append(1)
                    continue
                mutation_completed_at = self._monotonic_clock()
                delivery_state, click_result = mutation_result
                if delivery_state == "changed":
                    self._clear_action_confirmation(fingerprint)
                    failures.append("input_target_changed_before_delivery")
                    continue
                if delivery_state == "invalid":
                    self._clear_action_confirmation(fingerprint)
                    invalid_targets += 1
                    continue
                if delivery_state == "unresponsive":
                    self._clear_action_confirmation(fingerprint)
                    unresponsive_targets += 1
                    continue
                if not isinstance(click_result, MouseClickResult):
                    self._clear_action_confirmation(fingerprint)
                    delivery_failures += 1
                    failures.append("click_result_invalid")
                    continue
                if click_result.failure_code is not None:
                    failures.append(click_result.failure_code)
                if not click_result.restored:
                    failures.append("input_window_restore_failed")
                if click_result.delivery_uncertain:
                    self._clear_action_confirmation(fingerprint)
                    failures.append("click_delivery_uncertain")
                authority_is_current = self._capture_authority_is_current(
                    capture_settings_revision,
                    initial_capture_route,
                ) and self._source_authority_is_current(
                    confirmation.source_state_generation,
                )
                if (
                    authority_is_current
                    or click_result.delivered
                    or click_result.delivery_uncertain
                ):
                    self._action_retry_after[fingerprint] = (
                        item.state,
                        mutation_completed_at
                        + (
                            self._policy.progress_interval_seconds
                            if is_line_scroll
                            else self._policy.retry_interval_seconds
                        ),
                    )
                if click_result.delivered:
                    clicked_windows += 1
                    self._action_confirmations.pop(fingerprint, None)
                    self._initial_login_authorizations.pop(
                        fingerprint,
                        None,
                    )
                    self._pending_reconnect_fingerprints.add(fingerprint)
                    if item.state is ReconnectScreenState.DISCONNECTED:
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.force_login_wait_seconds
                        )
                    elif item.state is ReconnectScreenState.FORCE_LOGIN_START:
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.entry_transition_wait_seconds
                        )
                    elif (
                        item.state
                        is ReconnectScreenState.FORCE_LOGIN_TIMEOUT
                    ):
                        self._force_login_timeout_attempts[fingerprint] = (
                            _BattleRestartEvent.from_instance(
                                confirmed_instance
                            )
                        )
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.force_login_wait_seconds
                        )
                    elif (
                        item.state is ReconnectScreenState.LINE_SELECTION
                        and not is_line_scroll
                    ):
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.line_transition_wait_seconds
                        )
                    elif item.state is ReconnectScreenState.CHARACTER_SELECTION:
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.entry_transition_wait_seconds
                        )
                    elif item.state in _POST:
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.announcement_transition_wait_seconds
                        )
                if not authority_is_current:
                    self._clear_action_confirmation(fingerprint)
                    continue
                if click_result.delivered:
                    if (
                        item.state is ReconnectScreenState.LINE_SELECTION
                        and not is_line_scroll
                        and item.line_number in LINE_ROUTE_CLICK_POINTS
                    ):
                        self._preferred_line_numbers[fingerprint] = (
                            item.line_number
                        )
                    if item.state is ReconnectScreenState.CHARACTER_SELECTION:
                        if item.character_slot_selected is True:
                            reconnect_target = self._target_for_fingerprint(
                                fingerprint
                            )
                            if (
                                fingerprint
                                in self._login_only_recovery_fingerprints
                                and reconnect_target is not None
                                and isinstance(
                                    item.character_target_key,
                                    str,
                                )
                                and item.character_target_key.strip().casefold()
                                == reconnect_target.role_id.strip().casefold()
                            ):
                                self._reconnect_entry_authorized.add(
                                    fingerprint
                                )
                            else:
                                self._reconnect_entry_authorized.discard(
                                    fingerprint
                                )
                            if (
                                item.character_importance
                                is CharacterImportance.PRIMARY
                                and isinstance(
                                    item.character_target_key,
                                    str,
                                )
                                and item.character_target_key.strip()
                            ):
                                self._primary_entry_authorized.add(
                                    fingerprint
                                )
                            else:
                                self._primary_entry_authorized.discard(
                                    fingerprint
                                )
                            self._character_selection_pending.discard(
                                fingerprint
                            )
                            self._character_selection_targets.pop(
                                fingerprint,
                                None,
                            )
                            self._arm_terminal_completion(
                                fingerprint,
                                mutation_completed_at,
                            )
                        elif item.character_slot_selected is False:
                            pending_target = next(
                                (
                                    candidate
                                    for candidate in item.character_candidates
                                    if candidate.slot_index
                                    == item.character_slot_index
                                    and not candidate.selected
                                ),
                                None,
                            )
                            if pending_target is None:
                                pending_target = (
                                    self._candidate_from_recognition(item)
                                )
                            if pending_target is not None:
                                self._character_selection_pending.add(
                                    fingerprint
                                )
                                self._character_selection_targets[
                                    fingerprint
                                ] = pending_target
                    elif item.state in _POST:
                        self._arm_terminal_completion(
                            fingerprint,
                            mutation_completed_at,
                        )
                    self._active_automation_fingerprints.add(fingerprint)
                    self._active_automation_until[fingerprint] = (
                        mutation_completed_at
                        + POST_LOGIN_AUTOMATION_GRACE_SECONDS
                    )
                else:
                    if not click_result.delivery_uncertain:
                        delivery_failures += 1
        if (
            self._runtime_state_store is not None
            and state_before != self._runtime_state_signature()
            and not self._persist_runtime_state()
        ):
            failures.append("reconnect_state_persistence_failed")
        self._publish_reconnecting_fingerprints(
            now,
            observed_fingerprints=(operation_scope or frozenset()),
        )
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
        if battle_actionable and restarted_windows == 0 and mutation_execute:
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
                for _window, _fingerprint, item, _instance in (
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
        elif (
            failures
            and decisions
            and not unknown_windows
            and all(code == "window_offline" for code in failures)
        ):
            # A role the player intentionally left closed remains recorded and
            # is never opened here, but it must not slow monitoring of the
            # uniquely matched roles that are still running.
            next_check_seconds = min(
                decision.delay_seconds for decision in decisions
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

        # Auto-battle evidence remains isolated per role.
        if mutation_execute and self.auto_battle_execution_allowed():
            tcp_non_operable_instances = frozenset(
                state.instance
                for state in self._tcp_s.values()
                if state.zero_count > 0
            )
            for window, fingerprint, item in recognized:
                if (
                    fingerprint in self._detection_only_fingerprints
                    or fingerprint in terminal_completed_fingerprints
                    or fingerprint in self._login_only_recovery_fingerprints
                    or fingerprint in self._tcp_timeout_isolated
                    or WindowInstanceToken.from_window(window)
                    in tcp_non_operable_instances
                    or (
                        active_recovery_owner is not None
                        and fingerprint != active_recovery_owner
                    )
                ):
                    continue
                if fingerprint in direct_identity_collisions:
                    # The activation snapshot still identifies the original
                    # window, but a new second live instance claiming its
                    # direct source identity makes an automatic click unsafe.
                    self._auto_battle_evidence.pop(fingerprint, None)
                    self._auto_battle_button_windows.pop(fingerprint, None)
                    self._auto_battle_confirmed_instances.pop(
                        fingerprint,
                        None,
                    )
                    continue
                if item.state not in _AUTO_BATTLE_GENERAL_STATES:
                    continue
                instance_and_route = fresh_capture_instances.get(fingerprint)
                if instance_and_route is None:
                    continue
                instance, route = instance_and_route
                auto_battle_confirmed = self._run_auto_battle_for_connected(
                    window,
                    fingerprint,
                    instance,
                    auto_battle_capture_samples.get(fingerprint),
                    route,
                    capture_settings_revision,
                    scan_source_state_generation,
                    first_screen_state=item.state,
                    first_battle_context=item.battle_context,
                )
                if (
                    auto_battle_confirmed
                    and fingerprint
                    in self._primary_entry_authorized
                ):
                    self._complete_reconnect_timing(
                        fingerprint,
                        "disconnect_to_primary_auto",
                        self._monotonic_clock(),
                    )
                    self._primary_entry_authorized.discard(fingerprint)
                    self._primary_connected_fingerprints.discard(
                        fingerprint
                    )
            if self._auto_battle_button_windows:
                next_check_seconds = min(
                    next_check_seconds,
                    AUTO_BATTLE_RECHECK_SECONDS,
                )

        if execute:
            self._check_reconnect_timing_deadlines(
                self._monotonic_clock()
            )
        if self._tcp_timeout_isolated:
            failures.append("tcp_reconnect_timeout")
        if self._reconnect_timing_flows:
            next_check_seconds = min(
                next_check_seconds,
                self._policy.progress_interval_seconds,
            )

        # A re-entrant backend or policy callback can change settings after the
        # earlier action checks. Revoke the returned batch itself, not only the
        # separately published role-state cache, so callers can never receive
        # all_connected=True beside CHECK_DISABLED/UNKNOWN role states.
        final_capture_settings, final_capture_settings_revision = (
            self._capture_settings_snapshot()
        )
        source_changed_during_scan = (
            not self._source_authority_is_current(
                scan_source_state_generation
            )
        )
        if (
            final_capture_settings_revision != capture_settings_revision
            or source_changed_during_scan
        ):
            settings_changed_during_scan = True
            confirmed_action_instances.clear()
            recognized = [
                (
                    window,
                    fingerprint,
                    replace(
                        item,
                        # A changed capture configuration or revoked source
                        # cannot retroactively validate this old observation.
                        state=ReconnectScreenState.UNKNOWN,
                        click_point=None,
                    ),
                )
                for window, fingerprint, item in recognized
            ]
            state_counts = Counter(
                item.state.value
                for _window, _fingerprint, item in recognized
            )
            unrecognized_windows = state_counts.get(
                ReconnectScreenState.UNKNOWN.value,
                0,
            )
            disabled_windows = state_counts.get(
                ReconnectScreenState.CHECK_DISABLED.value,
                0,
            )
            unknown_windows = unrecognized_windows + disabled_windows
            captured_windows = 0
            actionable.clear()
            battle_actionable.clear()
            failures = [
                code
                for code in failures
                if code
                not in {
                    "capture_failed",
                    "screen_unknown",
                    "capture_mode_disabled",
                }
            ]
            if captured_windows + disabled_windows != len(windows):
                failures.append("capture_failed")
            if unrecognized_windows:
                failures.append("screen_unknown")
            if disabled_windows:
                failures.append("capture_mode_disabled")
            next_check_seconds = self._policy.retry_interval_seconds

        if not self._evidence_available():
            failures.append("evidence_recording_unavailable")
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
            source_missing_windows=len(source_failure_affected_fingerprints),
            execution_requested=execute,
            next_check_seconds=next_check_seconds,
            state_counts=tuple(sorted(state_counts.items())),
            failure_codes=tuple(dict.fromkeys(failures)),
            capture_diagnostics=self.anonymous_capture_diagnostics(),
            timing_diagnostics=(
                self.anonymous_reconnect_timing_diagnostics()
            ),
            tcp_observation=self._tcp_observation.items(),
        )
        (
            latest_capture_settings,
            latest_capture_settings_revision,
        ) = self._capture_settings_snapshot()
        # Do not allow the last publication of an old scan to revive its
        # source evidence. Source revocation already advanced the generation;
        # this block only emits UNKNOWN and removes trust for stale work.
        with self._source_authority_lock:
            settings_changed_during_scan = (
                capture_settings_revision
                != latest_capture_settings_revision
            )
            source_changed_during_scan = (
                self._source_state_generation
                != scan_source_state_generation
            )
            with self._screen_state_lock:
                published_states = (
                    {
                        fingerprint: ReconnectScreenState.UNKNOWN
                        for _window, fingerprint, _item in recognized
                    }
                    if (
                        settings_changed_during_scan
                        or source_changed_during_scan
                    )
                    else {
                        fingerprint: item.state
                        for _window, fingerprint, item in recognized
                    }
                )
                published_states.update(
                    {
                        fingerprint: ReconnectScreenState.UNKNOWN
                        for fingerprint in (
                            source_failure_affected_fingerprints
                        )
                    }
                )
                self._last_screen_states = published_states
                evidence_observed_at = time.monotonic()
                for _window, fingerprint, item in recognized:
                    fresh_instance = fresh_capture_instances.get(fingerprint)
                    if (
                        not settings_changed_during_scan
                        and not source_changed_during_scan
                        and item.state is ReconnectScreenState.CONNECTED
                        and fresh_instance is not None
                    ):
                        instance, route = fresh_instance
                        self._trusted_connected_evidence[fingerprint] = (
                            _TrustedConnectedEvidence(
                                instance,
                                route,
                                capture_settings_revision,
                                evidence_observed_at,
                            )
                        )
                    elif item.state is not ReconnectScreenState.CONNECTED:
                        self._trusted_connected_evidence.pop(
                            fingerprint,
                            None,
                        )
                for fingerprint in source_failure_affected_fingerprints:
                    self._trusted_connected_evidence.pop(fingerprint, None)
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
        if (
            self._operation_gate is not None
            and not self._operation_gate.snapshot().open
        ):
            return OperationResult(
                False,
                "reconnect.operation_paused",
                "Reconnect execution is paused while targets are rebinding.",
                {
                    "next_check_seconds": 1,
                    "failure_codes": ["operation_gate_closed"],
                },
            )
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
