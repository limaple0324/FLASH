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
    WindowCaptureProvider,
)
from adapters.windows_auto_battle import AutoBattleEvidence, AutoBattleRecognizer
from adapters.windows_battle_restart import (
    BattleRestartResult,
    Win32WindowCloseBackend,
    WindowsBattleWindowRestarter,
    WindowsShortcutOpenBackend,
)
from adapters.windows_shortcut_seal import ShortcutSealResolver
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
from core.smart_reconnect_authorization import (
    ReconnectActionContext,
    ReconnectAuthorizationBatch,
    ReconnectAuthorizationTarget,
    ReconnectEvidencePhase,
    ReconnectFrameWitness,
    ReconnectLaunchMode,
    ReconnectRevocationReason,
)
from core.window_instance import WindowInstanceToken
from domain.character import CharacterImportance, character_importance_rank
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.game_operation_gate import GameOperationGate
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.smart_reconnect_capture_settings_service import (
    SmartReconnectCaptureSettings,
)
from services.smart_reconnect_authorization_coordinator import (
    ReconnectAuthorizationError,
    ReconnectAuthorizationState,
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_evidence_store import (
    SmartReconnectEvidenceRecorder,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentity,
    normalize_reconnect_role_alias,
)
from services.smart_reconnect_preparation_service import (
    SmartReconnectPreparationError,
    SmartReconnectPreparationService,
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
_SESSION_ONLY_STATES = frozenset(
    {
        ReconnectScreenState.LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
        ReconnectScreenState.LINE_SELECTION,
        ReconnectScreenState.CHARACTER_SELECTION,
        ReconnectScreenState.POST_LOGIN_ACTIVITY,
        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
    }
)
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
_ISOLATABLE_TARGET_WINDOW_FAILURE_CODES = frozenset(
    {
        "shortcut_identity_unresolved",
        "window_identity_duplicate",
        "window_instance_incomplete",
    }
)


class ScreenRecognizer(Protocol):
    def recognize_capture(self, sample) -> ScreenRecognition:
        """Recognize a capture without changing or persisting it."""


@dataclass(frozen=True, slots=True)
class _TrustedConnectedEvidence:
    instance: WindowInstanceToken
    capture_route: str
    capture_settings_revision: int
    observed_at: float


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

    @property
    def all_connected(self) -> bool:
        return (
            self.discovered_windows > 0
            and self.validated_windows == self.discovered_windows
            and self.connected_windows == self.discovered_windows
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
    action_context: ReconnectActionContext
    witnesses: tuple[ReconnectFrameWitness, ...]
    consecutive_frames: int


@dataclass(frozen=True, slots=True)
class _BoundPendingReopen:
    batch: ReconnectAuthorizationBatch
    target: ReconnectAuthorizationTarget
    action_context: ReconnectActionContext


@dataclass(frozen=True, slots=True)
class _PendingTargetCharacterSelection:
    candidate: CharacterSelectionCandidate
    target: SmartReconnectTargetIdentity
    instance: WindowInstanceToken
    capture_route: str
    capture_settings_revision: int
    source_state_generation: int


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
        target_identity_provider: (
            Callable[[str], SmartReconnectTargetIdentity | None] | None
        ) = None,
        verified_slot_recorder: (
            Callable[[str, str, int], object] | None
        ) = None,
        verified_line_recorder: (
            Callable[[str, str, int], object] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
        authorization_coordinator: (
            SmartReconnectAuthorizationCoordinator | None
        ) = None,
        preparation_service: SmartReconnectPreparationService | None = None,
        shortcut_seal_resolver: ShortcutSealResolver | None = None,
        auto_battle_enabled: bool = True,
        auto_battle_recognizer: AutoBattleRecognizer | None = None,
        evidence_recorder: SmartReconnectEvidenceRecorder | None = None,
        evidence_required: bool = False,
        evidence_initialization_failed: bool = False,
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
        self._execution_scan_active = threading.Event()
        self._execution_scan_thread_id: int | None = None
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
            CharacterSelectionCandidate | _PendingTargetCharacterSelection,
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
        self._group_launch_plan_explicit = False
        self._explicit_plan_reprepare_pending = False
        self._battle_restart_group_batch: (
            ReconnectAuthorizationBatch | None
        ) = None
        self._activation_snapshot_instances: (
            dict[str, WindowInstanceToken] | None
        ) = None
        self._activation_snapshot_source_fingerprints: (
            dict[str, str] | None
        ) = None
        self._activation_snapshot_direct_identity_collisions: (
            frozenset[str]
        ) = frozenset()
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
        for callback, name in (
            (target_identity_provider, "target_identity_provider"),
            (verified_slot_recorder, "verified_slot_recorder"),
            (verified_line_recorder, "verified_line_recorder"),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or None")
        self._target_identity_provider = target_identity_provider
        self._verified_slot_recorder = verified_slot_recorder
        self._verified_line_recorder = verified_line_recorder
        self._operation_gate = operation_gate
        if authorization_coordinator is not None and not isinstance(
            authorization_coordinator,
            SmartReconnectAuthorizationCoordinator,
        ):
            raise TypeError(
                "authorization_coordinator must be "
                "SmartReconnectAuthorizationCoordinator or None"
            )
        if preparation_service is not None:
            if not callable(getattr(preparation_service, "prepare", None)):
                raise TypeError("preparation_service must provide prepare")
            if (
                authorization_coordinator is None
                or getattr(
                    preparation_service,
                    "authorization_coordinator",
                    authorization_coordinator,
                )
                is not authorization_coordinator
            ):
                raise ValueError(
                    "preparation service must share the authorization coordinator"
                )
        if shortcut_seal_resolver is not None and not callable(
            getattr(shortcut_seal_resolver, "revalidate", None)
        ):
            raise TypeError("shortcut_seal_resolver must provide revalidate")
        self._authorization = authorization_coordinator
        self._preparation = preparation_service
        self._shortcut_seals = shortcut_seal_resolver
        self._authorization_batch: ReconnectAuthorizationBatch | None = None
        self._authorization_contexts: dict[str, ReconnectActionContext] = {}
        self._pending_reopen_authorizations: dict[
            str,
            _BoundPendingReopen,
        ] = {}
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
            str,
            _BattleRestartEvent,
        ] = {}
        # One delivered timeout confirmation may not be repeated merely
        # because capture routing, settings, or source generation changes.
        # The event is intentionally bound only to immutable window-session
        # facts; geometry and capture details remain final-delivery gates.
        self._force_login_timeout_attempts: dict[
            str,
            _BattleRestartEvent,
        ] = {}
        self._ungrouped_shortcut_provider: (
            Callable[[str], Path | None] | None
        ) = None
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
        target_identity_provider: (
            Callable[[str], SmartReconnectTargetIdentity | None] | None
        ) = None,
        verified_slot_recorder: (
            Callable[[str, str, int], object] | None
        ) = None,
        verified_line_recorder: (
            Callable[[str, str, int], object] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
        authorization_coordinator: (
            SmartReconnectAuthorizationCoordinator | None
        ) = None,
        preparation_service: SmartReconnectPreparationService | None = None,
        shortcut_seal_resolver: ShortcutSealResolver | None = None,
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
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=window_backend,
            # Passive observation never changes window state. Active reconnect
            # scans use guarded reversible providers for fresh desktop pixels.
            capture_provider=Win32PrintWindowProvider(),
            visible_capture_provider=Win32VisibleRegionCaptureProvider(),
            obscured_capture_provider=(
                Win32TemporarilyRevealedCaptureProvider()
            ),
            active_refresh_capture_provider=(
                Win32RecoveringPrintWindowProvider()
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
                WindowsShortcutOpenBackend(
                    shortcut_seal_resolver=shortcut_seal_resolver,
                ),
            ),
            failure_status_service=failure_status_service,
            failure_record_callback=failure_record_callback,
            target_windows_provider=target_windows_provider,
            registered_role_provider=registered_role_provider,
            target_identity_provider=target_identity_provider,
            verified_slot_recorder=verified_slot_recorder,
            verified_line_recorder=verified_line_recorder,
            operation_gate=operation_gate,
            authorization_coordinator=authorization_coordinator,
            preparation_service=preparation_service,
            shortcut_seal_resolver=shortcut_seal_resolver,
            auto_battle_enabled=auto_battle_enabled,
            auto_battle_recognizer=AutoBattleRecognizer(
                reference_dir / "auto_battle"
            ),
            evidence_recorder=evidence_recorder,
            evidence_required=True,
            evidence_initialization_failed=evidence_initialization_failed,
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
        self._pending_reopen_authorizations.clear()
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
    ) -> None:
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
        self._auto_battle_attempted_actions = {
            item
            for item in self._auto_battle_attempted_actions
            if item[0] != fingerprint
        }
        self._recent_login_role_ids.pop(fingerprint, None)
        self._primary_entry_authorized.discard(fingerprint)
        self._primary_connected_fingerprints.discard(fingerprint)

    def _check_reconnect_timing_deadlines(self, now: float) -> None:
        for (fingerprint, lifecycle), flow in tuple(
            self._reconnect_timing_flows.items()
        ):
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
        scan_duration_ms: int | None,
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
                scan_duration_ms=scan_duration_ms,
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
                source_failures,
                blocked_fingerprints,
            ) = self._candidate_window_set()
            matching = tuple(
                window
                for window in latest_candidates
                if normalize_launch_fingerprint(window.launch_fingerprint)
                == fingerprint
            )
            identity_verified = bool(
                self._source_authority_is_current(source_state_generation)
                and fingerprint not in blocked_fingerprints
                and not source_failures
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
    ) -> tuple[bool, int | None, str | None]:
        if item.state is ReconnectScreenState.CHECK_DISABLED:
            return False, None, None
        if item.state is ReconnectScreenState.UNKNOWN:
            specialized_evidence = self._auto_battle_evidence.get(
                fingerprint
            )
            confirmation = self._action_confirmations.get(fingerprint)
            if (
                not isinstance(specialized_evidence, tuple)
                or len(specialized_evidence) != 5
                or specialized_evidence[:4]
                != (
                    instance,
                    capture_route,
                    capture_settings_revision,
                    source_state_generation,
                )
                or confirmation is None
                or confirmation.instance != instance
                or confirmation.capture_route != capture_route
                or confirmation.capture_settings_revision
                != capture_settings_revision
                or confirmation.source_state_generation
                != source_state_generation
                or confirmation.signature != self._action_signature(item)
                or confirmation.consecutive_frames
                < ACTION_CONFIRMATION_FRAMES
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
        target = self._target_identity_for_fingerprint(fingerprint)
        preferred = (
            target.original_line_number
            if target is not None
            else self._preferred_line_numbers.get(fingerprint)
        )
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
        target_identity = self._target_identity_for_fingerprint(fingerprint)
        if target_identity is not None:
            return bool(
                isinstance(item.character_target_key, str)
                and item.character_target_key
                == self._target_identity_action_key(target_identity)
            )
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
        execution_requested: bool,
        expected_source_state_generation: int | None,
        scan_duration_ms: int | None,
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
            if (
                not execution_requested
                and capture_route
                in {CAPTURE_ROUTE_OBSCURED, CAPTURE_ROUTE_MINIMIZED}
            ):
                rejection_gate = "passive_capture_withheld"
            else:
                diagnostic_provider = (
                    self._active_refresh_capture_provider
                    if capture_route == CAPTURE_ROUTE_MINIMIZED
                    else (
                        self._obscured_capture_provider
                        if capture_route == CAPTURE_ROUTE_OBSCURED
                        else None
                    )
                )
                provider_stage = (
                    getattr(
                        diagnostic_provider,
                        "last_failure_stage",
                        None,
                    )
                    if diagnostic_provider is not None
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
            scan_duration_ms=scan_duration_ms,
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
        scan_started = time.perf_counter()
        result = self._capture_and_recognize_unobserved(
            window,
            fingerprint,
            execute=execute,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
        )
        scan_duration_ms = max(
            0,
            round((time.perf_counter() - scan_started) * 1000),
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
            execution_requested=execute,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
            scan_duration_ms=scan_duration_ms,
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
        instance = WindowInstanceToken.from_window(window)
        if instance is None:
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

        def mutation_stage_is_authorized() -> bool:
            context = self._action_context_for(fingerprint, instance)
            coordinator = self._authorization
            if (
                not execute
                or not self._execution_allowed()
                or context is None
                or coordinator is None
            ):
                return False
            try:
                return coordinator.validate(
                    epoch=context.authorization_epoch,
                    batch_id=context.batch_id,
                    source_generation=context.source_generation,
                    fingerprint=context.fingerprint,
                    character_id=context.character_id,
                    instance=context.instance,
                    callback=lambda target: (
                        self._authorization_batch is not None
                        and self._authorization_batch.target_for(
                            fingerprint
                        )
                        == target
                    ),
                ) is True
            except ReconnectAuthorizationError:
                return False

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
                and mutation_stage_is_authorized()
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
                    and mutation_stage_is_authorized()
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
            if not mutation_stage_is_authorized():
                return self._unknown_capture_result(route=route)
            try:
                obscured_sample = self._obscured_capture_provider.capture(
                    window.handle
                )
            except OSError:
                obscured_sample = None
            if (
                obscured_sample is not None
                and obscured_sample.api_succeeded
                and mutation_stage_is_authorized()
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
    ) -> dict[str, tuple[WindowInfo, WindowInstanceToken]] | None:
        """Return a fail-closed, one-role-per-live-instance collection."""
        resolved: dict[str, tuple[WindowInfo, WindowInstanceToken]] = {}
        handles: set[int] = set()
        process_ids: set[int] = set()
        for window in windows:
            if not isinstance(window, WindowInfo):
                return None
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            instance = WindowInstanceToken.from_window(window)
            if fingerprint is None or instance is None:
                return None
            if (
                fingerprint in resolved
                or instance.handle in handles
                or instance.process_id in process_ids
            ):
                return None
            resolved[fingerprint] = (window, instance)
            handles.add(instance.handle)
            process_ids.add(instance.process_id)
        return resolved

    @staticmethod
    def _activation_monitor_fingerprint(
        source_fingerprint: str,
        instance: WindowInstanceToken,
    ) -> str:
        """Derive one activation-local identity from immutable window facts."""

        encoded = json.dumps(
            (
                "smart-reconnect-activation-v1",
                source_fingerprint,
                instance.handle,
                instance.process_id,
                instance.thread_id,
                instance.window_class,
                instance.process_lifecycle_token,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _activation_snapshot_candidate_instances(
        cls,
        windows: Iterable[WindowInfo],
    ) -> tuple[
        dict[str, tuple[WindowInfo, WindowInstanceToken]],
        dict[str, str],
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
        process_counts = Counter(item[2].process_id for item in parsed)
        safe = tuple(
            item
            for item in parsed
            if handle_counts[item[2].handle] == 1
            and process_counts[item[2].process_id] == 1
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

    @staticmethod
    def _source_failure_isolated_to_blocked_windows(
        failure_codes: tuple[str, ...],
        blocked_fingerprints: frozenset[str],
    ) -> bool:
        """Allow only explicitly identified target-local failures to isolate."""

        normalized = tuple(
            normalize_launch_fingerprint(fingerprint)
            for fingerprint in blocked_fingerprints
        )
        return bool(normalized) and all(
            fingerprint is not None for fingerprint in normalized
        ) and all(
            isinstance(code, str)
            and code in _ISOLATABLE_TARGET_WINDOW_FAILURE_CODES
            for code in failure_codes
        )

    def observe_screen_states(
        self,
        fingerprints: Iterable[str],
        *,
        candidate_windows: Iterable[WindowInfo] | None = None,
    ) -> dict[str, ReconnectScreenState]:
        requested = {
            fingerprint
            for item in fingerprints
            if (fingerprint := normalize_launch_fingerprint(item)) is not None
        }
        if not requested:
            return {}
        # Passive UI/synchronization readers are not an execution authority.
        # While a reconnect transaction is active they must not capture,
        # wait, or advance the source generation used by its final input gate.
        with self._source_authority_lock:
            foreign_execution_scan_active = bool(
                self._execution_scan_active.is_set()
                and self._execution_scan_thread_id
                != threading.get_ident()
            )
        if foreign_execution_scan_active:
            return {
                fingerprint: ReconnectScreenState.UNKNOWN
                for fingerprint in requested
            }
        _settings, observation_revision = self._capture_settings_snapshot()
        supplied = (
            tuple(candidate_windows)
            if candidate_windows is not None
            else None
        )
        source_windows, source_failures, blocked_fingerprints = (
            self._candidate_window_set()
        )
        source_instances = self._unique_complete_candidate_instances(
            source_windows
        )
        if supplied is None:
            candidates = source_windows
            by_fingerprint = source_instances
            recheck_live_authority = True
            unsafe_candidate_set = bool(
                source_failures
                or blocked_fingerprints
                or by_fingerprint is None
            )
        else:
            candidates = supplied
            by_fingerprint = self._unique_complete_candidate_instances(
                candidates
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
                source_failures
                or blocked_fingerprints
                or source_instances is None
                or any(
                    source_instances.get(fingerprint, (None, None))[1]
                    != instance
                    for fingerprint, (_window, instance)
                    in (by_fingerprint or {}).items()
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
            self._revoke_passive_observation_failure(
                frozenset(requested),
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
            final_windows, final_failures, final_blocked = (
                self._candidate_window_set()
            )
            final_instances = self._unique_complete_candidate_instances(
                final_windows
            )
            if (
                final_failures
                or final_blocked
                or final_instances is None
                or any(
                    final_instances.get(fingerprint, (None, None))[1]
                    != instance
                    for fingerprint, (_window, instance)
                    in by_fingerprint.items()
                )
            ):
                self._revoke_passive_observation_failure(
                    frozenset(requested),
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

    def _revoke_passive_observation_failure(
        self,
        fingerprints: frozenset[str],
    ) -> bool:
        """Revoke only when no execution scan owns the source generation."""

        with self._source_authority_lock:
            if (
                self._execution_scan_active.is_set()
                and self._execution_scan_thread_id
                != threading.get_ident()
            ):
                return False
            self._revoke_source_failure_evidence(
                fingerprints,
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            return True

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

    @staticmethod
    def _authorization_plan(
        batch: ReconnectAuthorizationBatch,
    ) -> GroupLaunchPlan:
        targets = tuple(
            GroupLaunchTarget(
                order=index,
                display_name=(target.role_aliases[0] or target.character_id or "role"),
                shortcut_path=Path(target.shortcut_seal.file_identity.normalized_path),
                fingerprint=target.fingerprint,
                entry_id=target.character_id or "",
                role_id=target.role_aliases[0] if target.role_aliases else "",
                importance=target.importance,
            )
            for index, target in enumerate(batch.targets, start=1)
            if target.shortcut_seal is not None
        )
        plan = GroupLaunchPlan(batch.source.group_name, targets)
        if len(targets) != len(batch.targets) or not plan.ready:
            raise ValueError("authorization batch cannot build a complete launch plan")
        return plan

    def _clear_execution_authority_locked(self) -> None:
        self._revoke_capture_authority()
        self._authorization_batch = None
        self._authorization_contexts.clear()
        self._pending_reopen_authorizations.clear()
        self._auto_battle_evidence.clear()
        self._auto_battle_button_windows.clear()
        self._auto_battle_attempted_actions.clear()
        self._auto_battle_confirmed_instances.clear()
        self._recent_login_role_ids.clear()
        self._primary_entry_authorized.clear()
        self._primary_connected_fingerprints.clear()
        self._initial_login_authorizations.clear()
        self._force_login_timeout_attempts.clear()
        self._activation_snapshot_instances = None
        self._activation_snapshot_source_fingerprints = None
        self._activation_snapshot_direct_identity_collisions = frozenset()
        self._allowed_fingerprints = None
        self._group_launch_plan = None
        self._explicit_plan_reprepare_pending = bool(
            self._explicit_plan_reprepare_pending
            or self._group_launch_plan_explicit
        )
        self._group_launch_plan_explicit = False
        self._battle_restart_group_batch = None
        self._runtime_scope_token = None
        with self._source_authority_lock:
            self._source_state_generation += 1
            self._source_revoked_fingerprints.clear()

    def _bind_authorization_batch_locked(
        self,
        batch: ReconnectAuthorizationBatch,
    ) -> None:
        coordinator = self._authorization
        if (
            coordinator is None
            or not isinstance(batch, ReconnectAuthorizationBatch)
            or batch.launch_mode is not ReconnectLaunchMode.IDENTITY_BOUND
            or coordinator.current_authorization() != batch
        ):
            raise ValueError("authorization batch is not the current product batch")
        contexts = {
            target.fingerprint: ReconnectActionContext.from_batch_target(
                batch,
                target,
            )
            for target in batch.targets
        }
        if len(contexts) != len(batch.targets):
            raise ValueError("authorization contexts are incomplete")
        plan = self._authorization_plan(batch)
        # The legacy snapshot transition deliberately discarded an explicitly
        # supplied group plan.  Preserve that observable boundary: the new
        # batch may monitor the same identities, but a later battle restart
        # still needs the unique ungrouped shortcut proof.  A batch prepared
        # directly from the product group source keeps its sealed group mode.
        replaced_explicit_plan = bool(
            self._group_launch_plan_explicit
            or self._explicit_plan_reprepare_pending
        )
        self._clear_execution_authority_locked()
        self._authorization_batch = batch
        self._authorization_contexts = contexts
        self._group_launch_plan = plan
        self._group_launch_plan_explicit = False
        self._explicit_plan_reprepare_pending = False
        self._battle_restart_group_batch = (
            None if replaced_explicit_plan else batch
        )
        self._allowed_fingerprints = plan.fingerprints
        self._runtime_scope_token = self._group_scope_token(plan)
        self._activation_snapshot_instances = {
            target.fingerprint: target.instance for target in batch.targets
        }
        self._activation_snapshot_source_fingerprints = {
            target.fingerprint: target.fingerprint for target in batch.targets
        }
        self._activation_snapshot_direct_identity_collisions = frozenset()
        with self._source_authority_lock:
            source_state_generation = self._source_state_generation
        _settings, capture_settings_revision = self._capture_settings_snapshot()
        expires_at = self._monotonic_clock() + INITIAL_LOGIN_AUTHORIZATION_SECONDS
        self._initial_login_authorizations = {
            target.fingerprint: _InitialLoginAuthorization(
                target.instance,
                capture_settings_revision,
                source_state_generation,
                expires_at,
            )
            for target in batch.targets
        }
        self._persist_runtime_state()

    def _prepare_product_authorization_locked(
        self,
    ) -> ReconnectAuthorizationBatch | None:
        preparation = self._preparation
        coordinator = self._authorization
        if preparation is None or coordinator is None:
            return None
        try:
            batch = preparation.prepare(
                launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
            )
            self._bind_authorization_batch_locked(batch)
            return batch
        except (
            OSError,
            ReconnectAuthorizationError,
            SmartReconnectPreparationError,
            TypeError,
            ValueError,
        ):
            coordinator.fail_preparation()
            self._clear_execution_authority_locked()
            return None

    def _action_context_for(
        self,
        fingerprint: str,
        instance: WindowInstanceToken | None = None,
    ) -> ReconnectActionContext | None:
        context = self._authorization_contexts.get(fingerprint)
        batch = self._authorization_batch
        if (
            context is None
            or batch is None
            or context.batch_id != batch.batch_id
            or context.authorization_epoch != batch.epoch
            or context.source_generation != batch.source.source_generation
            or context.fingerprint != fingerprint
            or (instance is not None and context.instance != instance)
        ):
            return None
        return context

    def _revoke_product_authorization(
        self,
        reason: ReconnectRevocationReason,
        *,
        preserve_pending_diagnostics: bool = False,
    ) -> None:
        if self._authorization is not None:
            self._authorization.revoke(reason)
        self._authorization_batch = None
        self._authorization_contexts.clear()
        self._pending_reopen_authorizations.clear()
        self._initial_login_authorizations.clear()
        self._clear_action_confirmation()
        if not preserve_pending_diagnostics:
            self._pending_reopen_fingerprints.clear()
            self._reopen_retry_after.clear()

    @staticmethod
    def _frame_witness(
        context: ReconnectActionContext,
        sample: CaptureSample,
        phase: ReconnectEvidencePhase,
    ) -> ReconnectFrameWitness:
        return ReconnectFrameWitness(
            authorization_epoch=context.authorization_epoch,
            batch_id=context.batch_id,
            source_generation=context.source_generation,
            fingerprint=context.fingerprint,
            character_id=context.character_id,
            instance=context.instance,
            launch_mode=context.launch_mode,
            phase=phase,
            frame_sha256=hashlib.sha256(sample.pixels).hexdigest(),
            observed_at_ns=time.time_ns(),
        )

    def prepare_execution_snapshot(self) -> OperationResult:
        """Atomically prepare the complete identity-bound authorization batch."""
        with self._scan_lock:
            if not self._evidence_available():
                if self._authorization is not None:
                    self._authorization.revoke(
                        ReconnectRevocationReason.PREPARATION_FAILED
                    )
                self._clear_execution_authority_locked()
                return self._snapshot_failure(
                    "reconnect.snapshot_evidence_unavailable",
                    "Reconnect evidence recording is unavailable.",
                    "evidence_recording_unavailable",
                )
            if self._execution_allowed():
                return self._snapshot_failure(
                    "reconnect.snapshot_execution_open",
                    "Reconnect execution must be stopped before preparation.",
                    "execution_gate_open",
                )
            batch = self._prepare_product_authorization_locked()
            if batch is None:
                return self._snapshot_failure(
                    "reconnect.snapshot_identity_unsafe",
                    "The complete reconnect authorization batch is unavailable.",
                    "authorization_batch_unavailable",
                )
            return OperationResult(
                True,
                "reconnect.snapshot_ready",
                "The complete reconnect authorization batch is ready.",
                {
                    "failure_codes": [],
                    "window_count": len(batch.targets),
                    "isolated_window_count": 0,
                    "authorization_epoch": batch.epoch,
                    "authorization_batch_id": batch.batch_id,
                },
            )
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
                self._group_launch_plan = None
                self._group_launch_plan_explicit = False
                self._explicit_plan_reprepare_pending = False
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
            self._group_launch_plan_explicit = True
            self._explicit_plan_reprepare_pending = False
            self.set_allowed_fingerprints(plan.fingerprints)
            self._runtime_scope_token = scope_token
            if previous_token != scope_token:
                # A group switch is a new reconnect context even when the two
                # groups share one role or the entire fingerprint set.
                self._retain_runtime_scope(frozenset())
            elif previous_scope != self._allowed_fingerprints:
                self._retain_runtime_scope(self._allowed_fingerprints)

    def set_ungrouped_shortcut_provider(
        self,
        provider: Callable[[str], Path | None] | None,
    ) -> None:
        """Inject the existing unique ungrouped shortcut lookup."""

        if provider is not None and not callable(provider):
            raise TypeError("provider must be callable or None")
        with self._scan_lock:
            if provider is self._ungrouped_shortcut_provider:
                return
            self._ungrouped_shortcut_provider = provider
            self._clear_action_confirmation()

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

    def set_execution_enabled(self, enabled: bool) -> bool:
        """Allow an active scan to stop before its next game-changing click."""
        if enabled:
            batch = self._authorization_batch
            coordinator = self._authorization
            if (
                batch is None
                or coordinator is None
                or coordinator.current_authorization() != batch
                or len(self._authorization_contexts) != len(batch.targets)
            ):
                self._execution_enabled.clear()
                return False
            if not self._record_evidence_monitoring_state(True):
                self._execution_enabled.clear()
                return False
            self._execution_enabled.set()
            return True
        # Close the gate before waiting for an active scan.  Its next final
        # authorization check must fail even while the remaining revocation is
        # waiting for the scan's read-only work to finish.
        self._execution_enabled.clear()
        self._record_evidence_monitoring_state(False)
        if self._authorization is not None:
            self._authorization.revoke(ReconnectRevocationReason.EXPLICIT)
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
            self._initial_login_authorizations.clear()
            self._force_login_timeout_attempts.clear()
            self._activation_snapshot_instances = None
            self._activation_snapshot_source_fingerprints = None
            self._activation_snapshot_direct_identity_collisions = frozenset()
            self._allowed_fingerprints = None
            self._explicit_plan_reprepare_pending = bool(
                self._explicit_plan_reprepare_pending
                or self._group_launch_plan_explicit
            )
            self._group_launch_plan = None
            self._group_launch_plan_explicit = False
            self._battle_restart_group_batch = None
            self._runtime_scope_token = None
            with self._source_authority_lock:
                self._source_state_generation += 1
                self._source_revoked_fingerprints.clear()
            self._authorization_batch = None
            self._authorization_contexts.clear()
            self._pending_reopen_authorizations.clear()
        return True

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
            sample=(
                first_sample
                if isinstance(first_sample, CaptureSample)
                else None
            ),
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
            sample=(
                second_sample
                if isinstance(second_sample, CaptureSample)
                else None
            ),
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
        confirmation = self._action_confirmations.get(fingerprint)
        if (
            confirmation is None
            or confirmation.action_context
            != self._action_context_for(fingerprint, instance)
        ):
            return False, False

        def deliver() -> MouseClickResult | None:
            if (
                not self.auto_battle_execution_allowed()
                or attempt_key in self._auto_battle_attempted_actions
                or self._auto_battle_evidence.get(fingerprint) != evidence
                or not self._auto_battle_snapshot_is_current(
                    fingerprint,
                    instance,
                )
                or self._current_action_window(instance, fingerprint) is None
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
                action_context=confirmation.action_context,
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
        action_context: ReconnectActionContext | None,
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
        coordinator = self._authorization
        if coordinator is None or action_context is None:
            return False, None

        # Hold the source authority generation across the backend call.  A
        # completed source revocation therefore linearizes before an older
        # scan can reach this final mutation boundary.  The only nested lock
        # is source-authority -> capture-settings; no path takes the reverse
        # order or involves the screen-state lock here.
        def authorized_backend_call(
            target: ReconnectAuthorizationTarget,
        ) -> object:
            if (
                target.fingerprint != action_context.fingerprint
                or target.character_id != action_context.character_id
                or target.instance != action_context.instance
            ):
                raise ReconnectAuthorizationError(
                    "authorization target changed before delivery"
                )
            with self._source_authority_lock:
                if (
                    expected_source_state_generation is not None
                    and self._source_state_generation
                    != expected_source_state_generation
                ):
                    raise ReconnectAuthorizationError(
                        "source generation changed before delivery"
                    )
                with self._capture_settings_lock:
                    if (
                        expected_capture_settings_revision is not None
                        and self._capture_settings_revision
                        != expected_capture_settings_revision
                    ):
                        raise ReconnectAuthorizationError(
                            "capture settings changed before delivery"
                        )
                    if not self._capture_route_enabled(
                        self._capture_settings,
                        capture_route,
                    ):
                        raise ReconnectAuthorizationError(
                            "capture route changed before delivery"
                        )
                    if not self._execution_allowed():
                        raise ReconnectAuthorizationError(
                            "execution stopped before delivery"
                        )
                    if additional_authorization_check is not None:
                        try:
                            additionally_authorized = (
                                additional_authorization_check() is True
                            )
                        except Exception:
                            additionally_authorized = False
                        if not additionally_authorized:
                            raise ReconnectAuthorizationError(
                                "action evidence changed before delivery"
                            )
                    return callback()

        try:
            result = coordinator.run_authorized(
                epoch=action_context.authorization_epoch,
                batch_id=action_context.batch_id,
                source_generation=action_context.source_generation,
                fingerprint=action_context.fingerprint,
                character_id=action_context.character_id,
                instance=action_context.instance,
                callback=authorized_backend_call,
            )
        except ReconnectAuthorizationError:
            return False, None
        return True, result

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
        blocked_fingerprints: frozenset[str],
    ) -> tuple[tuple[WindowInfo, ...], frozenset[str]]:
        snapshot = self._activation_snapshot_instances
        sources = self._activation_snapshot_source_fingerprints
        candidates = tuple(windows)
        if snapshot is None:
            self._activation_snapshot_direct_identity_collisions = frozenset()
            return candidates, blocked_fingerprints
        if sources is None or set(sources) != set(snapshot):
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
        for window in candidates:
            if not isinstance(window, WindowInfo):
                continue
            source_fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            instance = WindowInstanceToken.from_window(window)
            if source_fingerprint is None or instance is None:
                continue
            if source_fingerprint in snapshot:
                monitor_fingerprint = source_fingerprint
                candidate_source_fingerprint = sources.get(
                    monitor_fingerprint
                )
            else:
                monitor_fingerprint = self._activation_monitor_fingerprint(
                    source_fingerprint,
                    instance,
                )
                candidate_source_fingerprint = source_fingerprint
            expected = snapshot.get(monitor_fingerprint)
            if (
                expected is None
                or sources.get(monitor_fingerprint)
                != candidate_source_fingerprint
                or monitor_fingerprint in used
                or not self._same_live_instance_identity(expected, instance)
            ):
                unmatched.append(
                    (window, candidate_source_fingerprint, instance)
                )
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
                    monitor_fingerprint in blocked_fingerprints
                    or source_fingerprint in blocked_fingerprints
                )
            }
            | {
                normalized
                for item in blocked_fingerprints
                if (
                    (normalized := normalize_launch_fingerprint(item))
                    is not None
                    and normalized not in sources.values()
                )
            }
        )
        return tuple(bound), mapped_blocked

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
                    windows, blocked = (
                        self._bind_activation_snapshot_window_set(
                            provided.windows,
                            provided.blocked_fingerprints,
                        )
                    )
                    return windows, provided.failure_codes, blocked
                windows, blocked = self._bind_activation_snapshot_window_set(
                    tuple(provided),
                    frozenset(),
                )
                return windows, (), blocked
            except Exception:
                return (
                    (),
                    ("target_window_provider_failed",),
                    frozenset(),
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
            return windows, (), blocked
        except Exception:
            return (), ("window_enumeration_failed",), frozenset()

    def _reconcile_activation_snapshot(
        self,
        candidate_windows: tuple[WindowInfo, ...],
        blocked_fingerprints: frozenset[str],
    ) -> tuple[
        tuple[WindowInfo, ...],
        frozenset[str],
        tuple[str, ...],
    ]:
        """Reject every full-instance change until a new complete batch is published."""
        snapshot = self._activation_snapshot_instances
        if snapshot is None:
            return candidate_windows, blocked_fingerprints, ()
        allowed = frozenset(snapshot)
        scoped_blocked = frozenset(
            fingerprint
            for fingerprint in blocked_fingerprints
            if fingerprint in allowed
        )
        direct_collisions = frozenset(
            self._activation_snapshot_direct_identity_collisions & allowed
        )
        if direct_collisions:
            self._revoke_source_failure_evidence(
                allowed,
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED
            )
            return (
                (),
                frozenset(scoped_blocked | direct_collisions),
                ("snapshot_identity_collision",),
            )
        scoped_candidates = tuple(
            window
            for window in candidate_windows
            if (
                (fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                ))
                in allowed
                and fingerprint not in scoped_blocked
            )
        )
        complete_instances = self._unique_complete_candidate_instances(
            scoped_candidates
        )
        if complete_instances is None:
            self._revoke_source_failure_evidence(
                allowed,
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED
            )
            return (), scoped_blocked, ("snapshot_identity_collision",)

        accepted: list[WindowInfo] = []
        changed: set[str] = set()
        for fingerprint, (window, instance) in complete_instances.items():
            if instance == snapshot[fingerprint]:
                accepted.append(window)
            else:
                changed.add(fingerprint)

        missing = allowed - frozenset(complete_instances)
        expected_missing = frozenset(
            fingerprint
            for fingerprint in missing
            if (
                fingerprint in self._pending_reopen_fingerprints
                and fingerprint in self._pending_reopen_authorizations
            )
        )
        unsafe_missing = missing - expected_missing
        if changed:
            if self._authorization is not None:
                self._authorization.begin_reprepare()
            self._authorization_batch = None
            self._authorization_contexts.clear()
            self._pending_reopen_authorizations.clear()
            self._clear_action_confirmation()
            return (), scoped_blocked, ("snapshot_rebind_required",)
        if unsafe_missing or scoped_blocked:
            affected = frozenset(unsafe_missing | set(scoped_blocked))
            self._revoke_source_failure_evidence(
                affected,
                revoke_runtime_authority=True,
                refresh_source_generation=True,
            )
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED
            )
            return (), scoped_blocked, ("snapshot_source_changed",)
        return tuple(accepted), scoped_blocked, ()
    def _target_identity_for_fingerprint(
        self,
        fingerprint: str,
    ) -> SmartReconnectTargetIdentity | None:
        authorization_target = (
            self._authorization_batch.target_for(fingerprint)
            if self._authorization_batch is not None
            else None
        )
        if authorization_target is not None:
            try:
                return SmartReconnectTargetIdentity(
                    fingerprint=authorization_target.fingerprint,
                    character_id=authorization_target.character_id or "",
                    role_aliases=authorization_target.role_aliases,
                    importance=authorization_target.importance,
                    original_slot_index=authorization_target.original_slot_index,
                    original_line_number=authorization_target.original_line_number,
                )
            except (TypeError, ValueError):
                return None
        return self._provided_target_identity_for_fingerprint(fingerprint)

    def _provided_target_identity_for_fingerprint(
        self,
        fingerprint: str,
    ) -> SmartReconnectTargetIdentity | None:
        provider = self._target_identity_provider
        if provider is None:
            return None
        source_fingerprint = (
            self._activation_snapshot_source_fingerprints or {}
        ).get(fingerprint, fingerprint)
        try:
            target = provider(source_fingerprint)
        except Exception:
            return None
        if (
            not isinstance(target, SmartReconnectTargetIdentity)
            or target.fingerprint != source_fingerprint
        ):
            return None
        return target

    @staticmethod
    def _target_identity_action_key(
        target: SmartReconnectTargetIdentity,
    ) -> str:
        aliases_digest = hashlib.sha256(
            "\0".join(target.role_aliases).encode("utf-8")
        ).hexdigest()
        payload = (
            target.fingerprint,
            target.character_id,
            aliases_digest,
            target.importance.value,
            target.original_slot_index,
            target.original_line_number,
        )
        digest = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
        return f"target:{digest}"

    def _target_identity_action_is_current(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> bool:
        if self._authorization_batch is not None:
            target = self._target_identity_for_fingerprint(fingerprint)
            if target is None:
                return False
            if (
                item.character_target_key is not None
                and item.character_target_key
                != self._target_identity_action_key(target)
            ):
                return False
            if self._target_identity_provider is None:
                return True
            current = self._provided_target_identity_for_fingerprint(
                fingerprint
            )
            return bool(
                current is not None
                and self._target_identity_action_key(current)
                == self._target_identity_action_key(target)
            )
        if self._group_launch_plan is not None:
            return True
        if self._target_identity_provider is None:
            return True
        return self._current_target_identity_for_action(
            fingerprint,
            item,
        ) is not None

    def _current_target_identity_for_action(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> SmartReconnectTargetIdentity | None:
        target = self._target_identity_for_fingerprint(fingerprint)
        if (
            target is None
            or item.character_target_key
            != self._target_identity_action_key(target)
        ):
            return None
        return target

    @staticmethod
    def _record_verified_target_value(
        recorder: Callable[[str, str, int], object] | None,
        target: SmartReconnectTargetIdentity,
        value: int,
    ) -> bool:
        if recorder is None:
            return True
        try:
            return recorder(
                target.fingerprint,
                target.character_id,
                value,
            ) is True
        except Exception:
            return False

    def _recognition_for_target_identity_scope(
        self,
        item: ScreenRecognition,
        target: SmartReconnectTargetIdentity | None,
    ) -> ScreenRecognition:
        if target is not None:
            return replace(
                item,
                character_target_key=self._target_identity_action_key(target),
            )
        if self._authorization_batch is not None:
            return replace(
                item,
                click_point=None,
                character_target_key=None,
                line_scroll_delta=0,
            )
        if self._group_launch_plan is not None:
            return item
        if self._target_identity_provider is None:
            return item
        if target is None:
            return replace(
                item,
                click_point=None,
                character_target_key=None,
                line_scroll_delta=0,
            )
        return replace(
            item,
            character_target_key=self._target_identity_action_key(target),
        )

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
    def _action_signature_is_complete(item: ScreenRecognition) -> bool:
        point = item.click_point
        return bool(
            isinstance(item.reference_name, str)
            and item.reference_name.strip()
            and isinstance(point, tuple)
            and len(point) == 2
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0.0 <= value <= 1.0
                for value in point
            )
        )

    def _recognition_for_preferred_line(
        self,
        fingerprint: str,
        item: ScreenRecognition,
        *,
        target_identity: SmartReconnectTargetIdentity | None = None,
    ) -> ScreenRecognition:
        """Authorize only the line shown by the current game frame."""
        if item.state is not ReconnectScreenState.LINE_SELECTION:
            return item
        if (
            target_identity is not None
            and (
                self._authorization_batch is not None
                or (
                    self._group_launch_plan is None
                    and self._target_identity_provider is not None
                )
            )
            and target_identity.original_line_number
            in LINE_ROUTE_CLICK_POINTS
        ):
            if (
                item.line_number
                != target_identity.original_line_number
                or item.recent_line_present is not True
                or item.click_point is None
                or item.line_scroll_delta
            ):
                return replace(
                    item,
                    click_point=None,
                    line_scroll_delta=0,
                )
            return item
        if (
            item.recent_line_present is not True
        ):
            return replace(item, click_point=None, line_scroll_delta=0)
        if (
            item.line_scroll_delta in {-120, 120}
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

    def _ungrouped_restart_target(
        self,
        fingerprint: str,
    ) -> GroupLaunchTarget | None:
        provider = self._ungrouped_shortcut_provider
        if provider is None:
            return None
        source_fingerprint = (
            self._activation_snapshot_source_fingerprints or {}
        ).get(fingerprint, fingerprint)
        try:
            candidate = provider(source_fingerprint)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if not isinstance(candidate, Path):
            return None
        shortcut = Path(candidate)
        try:
            available = shortcut.is_file()
        except OSError:
            available = False
        if shortcut.suffix.casefold() != ".lnk" or not available:
            return None
        try:
            return GroupLaunchTarget(
                order=1,
                display_name="未分組角色",
                shortcut_path=shortcut,
                fingerprint=fingerprint,
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _restart_target_matches_authorization(
        target: GroupLaunchTarget | None,
        authorization_target: ReconnectAuthorizationTarget | None,
    ) -> bool:
        if (
            target is None
            or authorization_target is None
            or authorization_target.shortcut_seal is None
            or target.fingerprint != authorization_target.fingerprint
        ):
            return False
        target_path = os.path.normcase(
            os.path.abspath(os.fspath(target.shortcut_path))
        )
        sealed_identity = authorization_target.shortcut_seal.file_identity
        sealed_path = os.path.normcase(
            os.path.abspath(os.fspath(sealed_identity.normalized_path))
        )
        return target_path == sealed_path

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
        self._battle_restart_attempts.pop(fingerprint, None)
        self._force_login_timeout_attempts.pop(fingerprint, None)
        self._clear_action_confirmation(fingerprint)

    def _clear_reconnect_authority(
        self,
        windows: tuple[WindowInfo, ...],
    ) -> None:
        for window in windows:
            fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
            if fingerprint is None:
                continue
            self._clear_reconnect_session(fingerprint)
            self._clear_reconnect_failure(fingerprint)

    def _action_is_confirmed(
        self,
        fingerprint: str,
        item: ScreenRecognition,
        *,
        sample: CaptureSample | None,
        instance: WindowInstanceToken | None,
        capture_route: str | None,
        capture_settings_revision: int,
        source_state_generation: int,
    ) -> bool:
        context = self._action_context_for(fingerprint, instance)
        if (
            instance is None
            or capture_route is None
            or not isinstance(sample, CaptureSample)
            or not sample.api_succeeded
            or context is None
        ):
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
            signature = self._action_signature(item)
            witness = self._frame_witness(
                context,
                sample,
                ReconnectEvidencePhase.IDENTITY_CONFIRMED,
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
                        and previous.action_context == context
                    )
                    else 1
                )
                witnesses = (
                    (*previous.witnesses, witness)[-ACTION_CONFIRMATION_FRAMES:]
                    if count > 1 and previous is not None
                    else (witness,)
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
                        action_context=context,
                        witnesses=witnesses,
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

    @staticmethod
    def _complete_target_candidate_identity(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        if "..." in value or "…" in value:
            return None
        normalized = normalize_reconnect_role_alias(value)
        if normalized is None or len(normalized) < 3:
            return None
        return normalized

    @staticmethod
    def _short_complete_target_candidate_identity(value: object) -> bool:
        if not isinstance(value, str):
            return False
        if "..." in value or "…" in value:
            return False
        normalized = normalize_reconnect_role_alias(value)
        return normalized is not None and len(normalized) < 3

    def _stable_target_character_is_safe(
        self,
        fingerprint: str,
        item: ScreenRecognition,
        target: SmartReconnectTargetIdentity | None,
        *,
        instance: WindowInstanceToken | None,
        capture_route: str | None,
        capture_settings_revision: int | None,
        source_state_generation: int | None,
    ) -> ScreenRecognition | None:
        if target is None:
            return None
        expected_recent_role = self._recent_login_role_ids.get(fingerprint)
        if (
            expected_recent_role is not None
            and not target.matches_observed_identity(expected_recent_role)
        ):
            return None
        candidates = tuple(item.character_candidates)
        pending = self._character_selection_targets.get(fingerprint)
        action_key = self._target_identity_action_key(target)
        if pending is not None:
            if (
                not isinstance(pending, _PendingTargetCharacterSelection)
                or pending.target != target
                or instance != pending.instance
                or capture_route != pending.capture_route
                or capture_settings_revision
                != pending.capture_settings_revision
                or source_state_generation
                != pending.source_state_generation
            ):
                return None
            selected = tuple(candidate for candidate in candidates if candidate.selected)
            if (
                len(selected) != 1
                or selected[0].slot_index != pending.candidate.slot_index
            ):
                return None
            candidate = selected[0]
            if self._short_complete_target_candidate_identity(
                candidate.identity
            ):
                return None
            complete_identity = self._complete_target_candidate_identity(
                candidate.identity
            )
            if (
                complete_identity is not None
                and complete_identity not in target.role_aliases
            ):
                return None
            if any(
                other.slot_index != candidate.slot_index
                and self._complete_target_candidate_identity(
                    other.identity
                ) in target.role_aliases
                for other in candidates
            ):
                return None
            return self._candidate_result(
                item,
                candidate,
                target.importance,
                action_key,
            )

        if not candidates:
            return None
        exact_alias_matches = tuple(
            candidate
            for candidate in candidates
            if self._complete_target_candidate_identity(candidate.identity)
            in target.role_aliases
        )
        saved_slot = target.original_slot_index
        if saved_slot is not None:
            slot_matches = tuple(
                candidate
                for candidate in candidates
                if candidate.slot_index == saved_slot
            )
            if len(slot_matches) != 1:
                return None
            candidate = slot_matches[0]
            if self._short_complete_target_candidate_identity(
                candidate.identity
            ):
                return None
            if any(
                match.slot_index != saved_slot for match in exact_alias_matches
            ):
                return None
            complete_identity = self._complete_target_candidate_identity(
                candidate.identity
            )
            if (
                complete_identity is not None
                and complete_identity not in target.role_aliases
            ):
                return None
            if complete_identity is None and any(
                other.slot_index != saved_slot
                and other.level is None
                and self._complete_target_candidate_identity(
                    other.identity
                )
                is None
                for other in candidates
            ):
                return None
        elif len(exact_alias_matches) == 1:
            candidate = exact_alias_matches[0]
        elif (
            not exact_alias_matches
            and len(candidates) == 1
            and candidates[0].selected
        ):
            candidate = candidates[0]
            if self._short_complete_target_candidate_identity(
                candidate.identity
            ):
                return None
            complete_identity = self._complete_target_candidate_identity(
                candidate.identity
            )
            if (
                complete_identity is not None
                and complete_identity not in target.role_aliases
            ):
                return None
        else:
            return None
        if candidate.selected:
            selected = tuple(item for item in candidates if item.selected)
            if len(selected) != 1 or selected[0].slot_index != candidate.slot_index:
                return None
        return self._candidate_result(
            item,
            candidate,
            target.importance,
            action_key,
        )

    def _character_target_is_safe(
        self,
        fingerprint: str,
        item: ScreenRecognition,
        *,
        initial_login_authorized: bool = False,
        target_identity: SmartReconnectTargetIdentity | None = None,
        instance: WindowInstanceToken | None = None,
        capture_route: str | None = None,
        capture_settings_revision: int | None = None,
        source_state_generation: int | None = None,
    ) -> ScreenRecognition | None:
        """Choose only the uniquely proven registered primary role."""
        target = self._target_for_fingerprint(fingerprint)
        plan = self._group_launch_plan
        if target_identity is not None and (
            self._authorization_batch is not None
            or (plan is None and self._target_identity_provider is not None)
        ):
            return self._stable_target_character_is_safe(
                fingerprint,
                item,
                target_identity,
                instance=instance,
                capture_route=capture_route,
                capture_settings_revision=capture_settings_revision,
                source_state_generation=source_state_generation,
            )
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
        if pending_target is not None:
            if isinstance(pending_target, _PendingTargetCharacterSelection):
                return None
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
        if identity is not None:
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
        target_identity: SmartReconnectTargetIdentity | None = None,
        instance: WindowInstanceToken | None = None,
        capture_route: str | None = None,
        capture_settings_revision: int | None = None,
        source_state_generation: int | None = None,
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
                target_identity=target_identity,
                instance=instance,
                capture_route=capture_route,
                capture_settings_revision=capture_settings_revision,
                source_state_generation=source_state_generation,
            )
            if role_target is None:
                return replace(recognition, click_point=None)
            return role_target
        return recognition

    def _selected_group_is_complete(
        self,
        windows: tuple[WindowInfo, ...],
    ) -> bool:
        allowed = self._allowed_fingerprints
        if allowed is None:
            return True
        complete_instances = self._unique_complete_candidate_instances(windows)
        return (
            complete_instances is not None
            and set(complete_instances) == allowed
        )

    def _current_action_window(
        self,
        expected: WindowInfo | WindowInstanceToken,
        fingerprint: str,
    ) -> WindowInfo | None:
        candidates, failures, blocked_fingerprints = (
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
        scoped_blocked_fingerprints = frozenset(
            item
            for item in blocked_fingerprints
            if allowed is None or item in allowed
        )
        isolated_source_block = (
            self._source_failure_isolated_to_blocked_windows(
                failures,
                blocked_fingerprints,
            )
        )
        instances = self._unique_complete_candidate_instances(
            scoped_candidates
        )
        group_failures = tuple(self._group_failures(scoped_candidates))
        if self._activation_snapshot_instances is not None:
            group_failures = tuple(
                code
                for code in group_failures
                if code != "group_identity_set_mismatch"
            )
        selected_group_complete = self._selected_group_is_complete(
            scoped_candidates
        )
        snapshot = self._activation_snapshot_instances
        snapshot_matches_live_group = bool(
            instances is not None
            and (
                snapshot is None
                or (
                    set(instances) == set(snapshot)
                    and all(
                        instances[item][1] == expected_instance_item
                        for item, expected_instance_item in snapshot.items()
                    )
                )
            )
        )
        batch = self._authorization_batch
        batch_matches_live_group = bool(
            instances is not None
            and batch is not None
            and {target.fingerprint for target in batch.targets}
            == set(instances)
            and all(
                instances[target.fingerprint][1] == target.instance
                for target in batch.targets
            )
        )
        if (
            expected_instance is None
            or (failures and not isolated_source_block)
            or fingerprint in scoped_blocked_fingerprints
            or instances is None
            or group_failures
            or not selected_group_complete
            or not snapshot_matches_live_group
            or not batch_matches_live_group
        ):
            # A final delivery must not reuse a two-frame confirmation when
            # its original group identity premise has disappeared.  Clear the
            # target's captured route and confirmation together, so the next
            # scan starts from fresh, consecutive evidence.
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked(
                    tuple(allowed) if allowed is not None else (fingerprint,)
                )
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED
            )
            return None
        current = instances.get(fingerprint)
        if current is None:
            with self._screen_state_lock:
                self._mark_fingerprints_unknown_locked((fingerprint,))
            return None
        window, instance = current
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
        target_identity = self._target_identity_for_fingerprint(fingerprint)
        current = self._recognition_for_session_action(
            fingerprint,
            recognition,
            initial_login_authorized=initial_login_authorized,
            target_identity=target_identity,
            instance=expected_instance,
            capture_route=current_route,
            capture_settings_revision=expected_capture_settings_revision,
            source_state_generation=expected_source_state_generation,
        )
        current = self._recognition_for_preferred_line(
            fingerprint,
            current,
            target_identity=target_identity,
        )
        current = self._recognition_for_target_identity_scope(
            current,
            target_identity,
        )
        if self._action_signature(current) != self._action_signature(expected):
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
        *,
        expected_capture_settings_revision: int | None = None,
        capture_route: str | None = None,
        expected_source_state_generation: int | None = None,
        restart_role: bool = True,
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
            if not restart_role:
                return
            if capture_route is None:
                with self._screen_state_lock:
                    capture_route = self._last_trusted_capture_routes.get(
                        target.fingerprint
                    )
            restart = self._restart_failed_role(
                target.fingerprint,
                expected_capture_settings_revision=(
                    expected_capture_settings_revision
                ),
                capture_route=capture_route,
                expected_source_state_generation=(
                    expected_source_state_generation
                ),
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
        *,
        expected_capture_settings_revision: int | None = None,
        capture_route: str | None = None,
        expected_source_state_generation: int | None = None,
    ) -> BattleRestartResult:
        if self._evidence_required:
            # This legacy helper has no current screen, no two-frame decision
            # evidence and no pre-action window proof.  Keep monitoring and
            # retrying fresh capture, but never mutate from this path.
            return BattleRestartResult(
                False,
                "restart_evidence_missing",
            )
        if expected_source_state_generation is None:
            expected_source_state_generation = (
                self._source_state_generation_snapshot()
            )
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(False, "reconnect_stopped")
        target = self._target_for_fingerprint(fingerprint)
        restarter = self._battle_restarter
        if target is None or restarter is None:
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(
                False,
                "reconnect_restart_identity_unresolved",
            )
        candidates, source_failures, blocked_fingerprints = (
            self._candidate_window_set()
        )
        safety_failures = self._reopen_safety_failures(
            candidates,
            source_failures,
            blocked_fingerprints,
        )
        if safety_failures:
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(
                False,
                (
                    "target_window_provider_failed"
                    if "target_window_provider_failed" in safety_failures
                    else "reconnect_restart_identity_unsafe"
                ),
            )
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
        if len(matches) == 1:
            action_context = self._action_context_for(
                fingerprint,
                WindowInstanceToken.from_window(matches[0]),
            )
            mutation = lambda: self._run_authorized_backend_call(
                lambda: restarter.restart(matches[0], target),
                action_context=action_context,
                expected_capture_settings_revision=(
                    expected_capture_settings_revision
                ),
                capture_route=capture_route,
                expected_source_state_generation=(
                    expected_source_state_generation
                ),
            )[1]
        else:
            bound_pending = self._pending_reopen_authorizations.get(fingerprint)
            action_context = (
                bound_pending.action_context
                if bound_pending is not None
                else None
            )
            reopen_missing = getattr(restarter, "reopen_missing", None)
            if not callable(reopen_missing):
                self._clear_action_confirmation(fingerprint)
                return BattleRestartResult(
                    False,
                    "reconnect_restart_unavailable",
                )
            mutation = lambda: self._run_authorized_backend_call(
                lambda: reopen_missing(target, candidates),
                action_context=action_context,
                expected_capture_settings_revision=(
                    expected_capture_settings_revision
                ),
                capture_route=capture_route,
                expected_source_state_generation=(
                    expected_source_state_generation
                ),
            )[1]
        permitted, mutation_result = self._run_game_mutation(
            "smart-reconnect-restart",
            mutation,
            expected_capture_settings_revision=(
                expected_capture_settings_revision
            ),
            capture_route=capture_route,
            expected_source_state_generation=(
                expected_source_state_generation
            ),
        )
        if not permitted or not isinstance(
            mutation_result,
            BattleRestartResult,
        ):
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(False, "reconnect_stopped")
        result = mutation_result
        if not self._capture_authority_is_current(
            expected_capture_settings_revision,
            capture_route,
        ):
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(False, "reconnect_stopped")
        if not self._source_authority_is_current(
            expected_source_state_generation,
        ):
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(False, "reconnect_stopped")
        if result.success:
            now = self._monotonic_clock()
            self._pending_reconnect_fingerprints.add(fingerprint)
            self._pending_reopen_fingerprints.add(fingerprint)
            self._reopen_retry_after[fingerprint] = (
                now + self._policy.progress_interval_seconds
            )
            self._publish_reconnecting_fingerprints(now)
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
        if (
            self._allowed_fingerprints is not None
            and set(
                fingerprint
                for fingerprint in fingerprints
                if fingerprint is not None
            ) != set(self._allowed_fingerprints)
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
        source_failures: tuple[str, ...],
        blocked_fingerprints: frozenset[str],
    ) -> list[str]:
        """Fail closed before any restart or reopen backend call.

        A missing role is deliberately safe to reopen.  Every other live
        candidate must nevertheless form one complete, collision-free
        identity collection, independent of the caller that reached this
        boundary.
        """
        failures = list(source_failures)
        if blocked_fingerprints:
            failures.append("window_identity_blocked")
        if self._unique_complete_candidate_instances(windows) is not None:
            return failures
        group_failures = self._group_failures(windows)
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
        blocked_fingerprints: frozenset[str],
    ) -> None:
        """Revoke only identities proven unsafe by a group failure.

        A temporarily absent planned role may still be retried safely.  It is
        not interchangeable with a live candidate that is incomplete or
        collides with another live instance, so do not erase the safe subset.
        """
        affected = set(blocked_fingerprints)
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
            self._character_selection_pending.discard(normalized)
            self._character_selection_targets.pop(normalized, None)
            if revoke_runtime_authority:
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
        source_failures: tuple[str, ...],
        blocked_fingerprints: frozenset[str],
    ) -> frozenset[str]:
        affected = {
            fingerprint
            for item in blocked_fingerprints
            if (
                fingerprint := normalize_launch_fingerprint(item)
            )
            is not None
        }
        complete_counts: Counter[str] = Counter()
        unsafe_live_fingerprints: set[str] = set()
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is None:
                continue
            if (
                fingerprint in affected
                or WindowInstanceToken.from_window(window) is None
            ):
                unsafe_live_fingerprints.add(fingerprint)
                continue
            complete_counts[fingerprint] += 1
        uniquely_valid = {
            fingerprint
            for fingerprint, count in complete_counts.items()
            if count == 1 and fingerprint not in unsafe_live_fingerprints
        }
        allowed = self._allowed_fingerprints
        if allowed is not None:
            affected.update(allowed - uniquely_valid)
            return frozenset(affected)
        # In global mode there is no planned identity set to compare against.
        # Previously published states and trusted CONNECTED evidence are the
        # only known identities. A source empty-set or subset must revoke any
        # one no longer represented by exactly one complete instance.
        with self._screen_state_lock:
            # An already UNKNOWN identity was revoked by an earlier source
            # transition.  It is not fresh evidence that should revoke the
            # same source generation again on every subsequent scan.
            previously_published = {
                fingerprint
                for fingerprint, state in self._last_screen_states.items()
                if state is not ReconnectScreenState.UNKNOWN
            } | set(self._trusted_connected_evidence)
        affected.update(previously_published - uniquely_valid)
        return frozenset(affected)

    def _clear_recovered_source_revocations(
        self,
        windows: tuple[WindowInfo, ...],
        source_failures: tuple[str, ...],
        blocked_fingerprints: frozenset[str],
    ) -> None:
        """Allow a later, new failure only after full authority recovery."""
        if source_failures or blocked_fingerprints:
            return
        complete_instances = self._unique_complete_candidate_instances(
            windows
        )
        if complete_instances is None:
            return
        with self._source_authority_lock:
            self._source_revoked_fingerprints.difference_update(
                complete_instances
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
            )

    def _retry_pending_reopens(
        self,
        *,
        candidate_windows: tuple[WindowInfo, ...],
        source_failures: tuple[str, ...],
        blocked_fingerprints: frozenset[str],
        execute: bool,
        now: float,
        expected_capture_settings_revision: int,
        expected_source_state_generation: int,
        safety_failures: tuple[str, ...] = (),
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
                fingerprint in blocked_fingerprints
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
            self._pending_reopen_authorizations.pop(fingerprint, None)
        if appeared:
            if self._authorization is not None:
                self._authorization.begin_reprepare()
            self._authorization_batch = None
            self._authorization_contexts.clear()
            self._clear_action_confirmation()

        missing = tuple(sorted(self._pending_reopen_fingerprints))
        if not missing:
            return 0, [], None
        if safety_failures:
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED,
                preserve_pending_diagnostics=True,
            )
            failures = list(safety_failures)
            if set(missing) & set(blocked_fingerprints):
                failures.append("battle_reopen_identity_unsafe")
            return (
                0,
                failures,
                self._policy.retry_interval_seconds,
            )
        if "target_window_provider_failed" in source_failures:
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED,
                preserve_pending_diagnostics=True,
            )
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
            if fingerprint in blocked_fingerprints:
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

            bound_pending = self._pending_reopen_authorizations.get(
                fingerprint
            )
            target = self._target_for_fingerprint(fingerprint)
            retry_open = getattr(
                self._battle_restarter,
                "reopen_missing",
                None,
            )
            if (
                bound_pending is None
                or self._authorization_batch != bound_pending.batch
                or bound_pending.target.fingerprint != fingerprint
                or bound_pending.target.shortcut_seal is None
                or target is None
                or target.fingerprint != bound_pending.target.fingerprint
                or not callable(retry_open)
            ):
                self._clear_action_confirmation(fingerprint)
                failures.append("battle_restart_identity_unresolved")
                self._report_reconnect_failure(None)
                continue

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

            def retry_bound_open():
                def authorize_open_leaf(callback):
                    return self._run_authorized_backend_call(
                        callback,
                        action_context=bound_pending.action_context,
                        expected_capture_settings_revision=(
                            expected_capture_settings_revision
                        ),
                        capture_route=capture_route,
                        expected_source_state_generation=(
                            expected_source_state_generation
                        ),
                        additional_authorization_check=(
                            lambda: (
                                self._authorization_batch
                                == bound_pending.batch
                                and self._pending_reopen_authorizations.get(
                                    fingerprint
                                )
                                == bound_pending
                            )
                        ),
                    )

                if isinstance(
                    self._battle_restarter,
                    WindowsBattleWindowRestarter,
                ):
                    return retry_open(
                        target,
                        evidence_candidates,
                        open_authorizer=authorize_open_leaf,
                        expected_shortcut_seal=(
                            bound_pending.target.shortcut_seal
                        ),
                    )
                authorized, raw_result = authorize_open_leaf(
                    lambda: retry_open(target, evidence_candidates)
                )
                return raw_result if authorized else None

            permitted, mutation_result = self._run_game_mutation(
                "smart-reconnect-reopen",
                retry_bound_open,
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
                if result.shortcut_open_requested:
                    if self._authorization is not None:
                        self._authorization.begin_reprepare()
                    self._authorization_batch = None
                    self._authorization_contexts.clear()
                    self._pending_reopen_authorizations.clear()
                    self._clear_action_confirmation()
                    break
            else:
                failures.append(
                    result.failure_code or "battle_shortcut_open_failed"
                )
                # The role still failed. Record it and immediately retry only
                # this exact role instead of waiting for the old 60-second
                # interval.
                self._report_reconnect_failure(
                    fingerprint,
                    expected_capture_settings_revision=(
                        expected_capture_settings_revision
                    ),
                    capture_route=capture_route,
                    expected_source_state_generation=(
                        expected_source_state_generation
                    ),
                )

        next_delay = min(next_delays) if next_delays else None
        return reopened, failures, next_delay

    def _scan(self, *, execute: bool) -> ReconnectBatchResult:
        # Group identity may be rebound from the UI thread while recognition is
        # running. Keep reconnect state internally consistent without holding
        # the shared game-operation gate during this read-only work.
        with self._scan_lock:
            if execute:
                with self._source_authority_lock:
                    self._execution_scan_active.set()
                    self._execution_scan_thread_id = threading.get_ident()
            try:
                return self._scan_locked(execute=execute)
            finally:
                if execute:
                    with self._source_authority_lock:
                        self._execution_scan_thread_id = None
                        self._execution_scan_active.clear()

    def _scan_locked(self, *, execute: bool) -> ReconnectBatchResult:
        if execute:
            coordinator = self._authorization
            batch = self._authorization_batch
            if (
                coordinator is None
                or batch is None
                or coordinator.current_authorization() != batch
            ):
                batch = self._prepare_product_authorization_locked()
            if batch is None:
                result = ReconnectBatchResult(
                    expected_windows=self._expected_windows,
                    discovered_windows=0,
                    validated_windows=0,
                    captured_windows=0,
                    recognized_windows=0,
                    connected_windows=0,
                    actionable_windows=0,
                    clicked_windows=0,
                    restarted_windows=0,
                    unknown_windows=0,
                    source_missing_windows=0,
                    execution_requested=True,
                    next_check_seconds=self._policy.retry_interval_seconds,
                    state_counts=(),
                    failure_codes=("authorization_batch_unavailable",),
                    capture_diagnostics=self.anonymous_capture_diagnostics(),
                    timing_diagnostics=(
                        self.anonymous_reconnect_timing_diagnostics()
                    ),
                )
                self._last_result = result
                return result
        with self._capture_settings_lock:
            capture_settings_revision = self._capture_settings_revision
        (
            candidate_windows,
            source_failures,
            blocked_fingerprints,
        ) = self._candidate_window_set()
        direct_identity_collisions = (
            self._activation_snapshot_direct_identity_collisions
        )
        isolated_source_block = (
            self._source_failure_isolated_to_blocked_windows(
                source_failures,
                blocked_fingerprints,
            )
        )
        (
            candidate_windows,
            blocked_fingerprints,
            snapshot_failures,
        ) = self._reconcile_activation_snapshot(
            candidate_windows,
            blocked_fingerprints,
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
                not in blocked_fingerprints
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
                source_failures,
                blocked_fingerprints,
            )
        )
        self._clear_recovered_source_revocations(
            candidate_windows,
            source_failures,
            blocked_fingerprints,
        )
        if source_failure_affected_fingerprints:
            expected_pending_absence = bool(
                source_failure_affected_fingerprints
                and source_failure_affected_fingerprints.issubset(
                    self._pending_reopen_authorizations
                )
                and not (
                    set(source_failure_affected_fingerprints)
                    & set(blocked_fingerprints)
                )
                and "target_window_provider_failed" not in source_failures
                and self._authorization_batch is not None
                and self._authorization is not None
                and self._authorization.current_authorization()
                == self._authorization_batch
            )
            if expected_pending_absence:
                self._revoke_source_failure_evidence(
                    source_failure_affected_fingerprints,
                    refresh_source_generation=False,
                )
            else:
                self._revoke_source_failure_evidence(
                    source_failure_affected_fingerprints,
                    revoke_runtime_authority=True,
                    refresh_source_generation=True,
                )
                self._revoke_product_authorization(
                    ReconnectRevocationReason.SOURCE_CHANGED
                )
        scan_source_state_generation = (
            self._source_state_generation_snapshot()
        )
        state_before = self._runtime_state_signature()
        now = self._monotonic_clock()
        group_failures = self._group_failures(windows)
        source_identity_unsafe = bool(
            (source_failures and not isolated_source_block)
            or (blocked_fingerprints and not isolated_source_block)
            or snapshot_failures
            or self._unique_complete_candidate_instances(
                candidate_windows
            )
            is None
        )
        if source_identity_unsafe:
            # A source-reported failure or unsafe live instance interrupts
            # every pending action frame.  No safe-looking peer may rebuild
            # confirmation until the source itself is complete again.
            self._clear_action_confirmation()
        selected_group_complete = self._selected_group_is_complete(windows)
        if not self._pending_reopen_fingerprints:
            retried_reopens = 0
            retry_failures: list[str] = []
            pending_reopen_delay = None
        else:
            reopen_safety_failures = self._reopen_safety_failures(
                candidate_windows,
                source_failures,
                blocked_fingerprints,
            )
            retried_reopens, retry_failures, pending_reopen_delay = (
                self._retry_pending_reopens(
                    candidate_windows=candidate_windows,
                    source_failures=source_failures,
                    blocked_fingerprints=blocked_fingerprints,
                    execute=execute,
                    now=now,
                    expected_capture_settings_revision=(
                        capture_settings_revision
                    ),
                    expected_source_state_generation=(
                        scan_source_state_generation
                    ),
                    safety_failures=tuple(reopen_safety_failures),
                )
            )
        failures = [
            *group_failures,
            *source_failures,
            *snapshot_failures,
            *retry_failures,
        ]
        if blocked_fingerprints and not source_failures:
            failures.append("window_identity_blocked")
        blocking_group_failures = [
            code
            for code in group_failures
            if code != "group_identity_set_mismatch"
        ]
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
                blocked_fingerprints,
            )
            self._revoke_product_authorization(
                ReconnectRevocationReason.SOURCE_CHANGED
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
        initial_authorized_action_fingerprints: set[str] = set()
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
                execute=execute,
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
                        # The server or player may safely finish a reconnect
                        # without the controller delivering the final click.
                        # Fresh changing gameplay frames still have to pass
                        # the same terminal evidence gate before authority is
                        # cleared.
                        self._arm_terminal_completion(
                            fingerprint,
                            now,
                        )
                    if (
                        fingerprint in self._terminal_ready_after
                        and self._terminal_completion_confirmed(
                        fingerprint,
                        window,
                        sample,
                        now,
                        )
                        and fingerprint
                        in self._primary_entry_authorized
                    ):
                        self._complete_reconnect_timing(
                            fingerprint,
                            "start_game_to_primary_connected",
                            now,
                        )
                        self._primary_connected_fingerprints.add(
                            fingerprint
                        )
                        if not self._auto_battle_enabled:
                            self._complete_reconnect_timing(
                                fingerprint,
                                "disconnect_to_primary_auto",
                                now,
                            )
                        self._clear_reconnect_session(fingerprint)
                        self._clear_reconnect_failure(fingerprint)
                        if (
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
            target_identity = self._target_identity_for_fingerprint(
                fingerprint
            )
            recognition = self._recognition_for_session_action(
                fingerprint,
                recognition,
                initial_login_authorized=initial_login_authorized,
                target_identity=target_identity,
                instance=instance,
                capture_route=capture_route,
                capture_settings_revision=capture_settings_revision,
                source_state_generation=scan_source_state_generation,
            )
            recognition = self._recognition_for_preferred_line(
                fingerprint,
                recognition,
                target_identity=target_identity,
            )
            recognition = self._recognition_for_target_identity_scope(
                recognition,
                target_identity,
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
            is_action_candidate = (
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
            action_evidence_complete = (
                fresh_capture
                and instance is not None
                and capture_route is not None
                and self._action_signature_is_complete(recognition)
                and recognition.state
                not in {
                    ReconnectScreenState.UNKNOWN,
                    ReconnectScreenState.CHECK_DISABLED,
                }
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
                    sample=(
                        sample if isinstance(sample, CaptureSample) else None
                    ),
                    instance=instance,
                    capture_route=capture_route,
                    capture_settings_revision=(
                        capture_settings_revision
                    ),
                    source_state_generation=(
                        scan_source_state_generation
                    ),
                )
            ):
                confirmed_action_instances[fingerprint] = instance
                if (
                    execute
                    and recognition.state
                    is ReconnectScreenState.DISCONNECTED
                ):
                    self._primary_entry_authorized.discard(fingerprint)
                    self._primary_connected_fingerprints.discard(
                        fingerprint
                    )
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
            }:
                # Only a newly proven non-disconnect state ends the current
                # disconnect event.  UNKNOWN never re-arms a restart.
                self._battle_restart_attempts.pop(fingerprint, None)
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
            execute
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
                item.state
                not in {
                    ReconnectScreenState.POST_LOGIN_ACTIVITY,
                    ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
                    ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
                }
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
            if item.state is ReconnectScreenState.DISCONNECTED
            and item.battle_context
            and fingerprint in confirmed_action_instances
        ]
        clicked_windows = 0
        restarted_windows = retried_reopens
        battle_restart_attempted = False
        invalid_targets = 0
        unresponsive_targets = 0
        delivery_failures = 0
        if execute:
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
                restart_event = _BattleRestartEvent.from_instance(instance)
                if (
                    self._battle_restart_attempts.get(fingerprint)
                    == restart_event
                ):
                    continue
                batch = self._authorization_batch
                uses_group_authorization = bool(
                    batch is not None
                    and self._battle_restart_group_batch == batch
                )
                plan = (
                    self._group_launch_plan
                    if uses_group_authorization
                    else None
                )
                if uses_group_authorization:
                    target = (
                        plan.target_for_fingerprint(fingerprint)
                        if plan is not None
                        else None
                    )
                else:
                    target = self._ungrouped_restart_target(fingerprint)
                authorization_target = (
                    batch.target_for(fingerprint)
                    if batch is not None
                    else None
                )
                snapshot = self._activation_snapshot_instances
                if (
                    self._battle_restarter is None
                    or not self._restart_target_matches_authorization(
                        target,
                        authorization_target,
                    )
                    or (
                        plan is None
                        and (
                            snapshot is None
                            or snapshot.get(fingerprint) != instance
                        )
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
                    )
                    if current_capture_route != capture_route:
                        return None
                    refreshed_window = self._current_action_window(
                        instance,
                        fingerprint,
                    )
                    if refreshed_window is None:
                        return None
                    batch = self._authorization_batch
                    authorization_target = (
                        batch.target_for(fingerprint)
                        if batch is not None
                        else None
                    )
                    if (
                        batch is None
                        or authorization_target is None
                        or authorization_target.instance != instance
                        or authorization_target.shortcut_seal is None
                        or not self._restart_target_matches_authorization(
                            target,
                            authorization_target,
                        )
                        or (
                            uses_group_authorization
                            and (
                                self._battle_restart_group_batch != batch
                                or self._group_launch_plan is not plan
                                or plan is None
                                or plan.target_for_fingerprint(fingerprint)
                                != target
                            )
                        )
                        or (
                            not uses_group_authorization
                            and (
                                self._battle_restart_group_batch == batch
                                or self._ungrouped_restart_target(fingerprint)
                                != target
                            )
                        )
                    ):
                        return None
                    bound_pending = _BoundPendingReopen(
                        batch=batch,
                        target=authorization_target,
                        action_context=confirmation.action_context,
                    )
                    self._pending_reopen_authorizations[fingerprint] = (
                        bound_pending
                    )

                    def authorize_restart_leaf(callback):
                        return self._run_authorized_backend_call(
                            callback,
                            action_context=bound_pending.action_context,
                            expected_capture_settings_revision=(
                                capture_settings_revision
                            ),
                            capture_route=capture_route,
                            expected_source_state_generation=(
                                confirmation.source_state_generation
                            ),
                            additional_authorization_check=(
                                lambda: (
                                    self._authorization_batch
                                    == bound_pending.batch
                                    and self._action_confirmations.get(
                                        fingerprint
                                    )
                                    == confirmation
                                )
                            ),
                        )

                    if isinstance(
                        self._battle_restarter,
                        WindowsBattleWindowRestarter,
                    ):
                        restart = self._battle_restarter.restart(
                            refreshed_window,
                            target,
                            close_authorizer=authorize_restart_leaf,
                            open_authorizer=authorize_restart_leaf,
                            expected_shortcut_seal=(
                                authorization_target.shortcut_seal
                            ),
                        )
                    else:
                        authorized, raw_restart = authorize_restart_leaf(
                            lambda: self._battle_restarter.restart(
                                refreshed_window,
                                target,
                            )
                        )
                        restart = (
                            raw_restart
                            if authorized
                            and isinstance(raw_restart, BattleRestartResult)
                            else BattleRestartResult(
                                False,
                                "battle_window_authorization_changed",
                            )
                        )
                    if not restart.window_closed:
                        self._pending_reopen_authorizations.pop(
                            fingerprint,
                            None,
                        )
                    return restart

                # Mark before entering the mutation boundary.  One confirmed
                # disconnect event can authorize at most one restart attempt,
                # even when that attempt reports failure.
                self._battle_restart_attempts[fingerprint] = restart_event
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
                    self._report_reconnect_failure(
                        fingerprint,
                        expected_capture_settings_revision=(
                            capture_settings_revision
                        ),
                        capture_route=capture_route,
                        expected_source_state_generation=(
                            confirmation.source_state_generation
                        ),
                        restart_role=False,
                    )
                    if restart_result.window_closed:
                        self._pending_reopen_fingerprints.add(fingerprint)
                        self._reopen_retry_after[fingerprint] = (
                            mutation_completed_at
                            + self._policy.progress_interval_seconds
                        )
                    continue
                restarted_windows += 1
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
                if restart_result.shortcut_open_requested:
                    if self._authorization is not None:
                        self._authorization.begin_reprepare()
                    self._authorization_batch = None
                    self._authorization_contexts.clear()
                    self._pending_reopen_authorizations.clear()
                    self._clear_action_confirmation()
                    break
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
                        or not self._capture_authority_is_current(
                            capture_settings_revision,
                            current_capture_route,
                        )
                        or not self._source_authority_is_current(
                            confirmation.source_state_generation,
                        )
                    ):
                        return "changed", False
                    if not self._target_identity_action_is_current(
                        fingerprint,
                        item,
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
                                action_context=confirmation.action_context,
                                expected_capture_settings_revision=(
                                    capture_settings_revision
                                ),
                                capture_route=current_capture_route,
                                expected_source_state_generation=(
                                    confirmation.source_state_generation
                                ),
                                additional_authorization_check=(
                                    lambda: (
                                        self._action_confirmations.get(
                                            fingerprint
                                        )
                                        == confirmation
                                        and self._action_context_for(
                                            fingerprint,
                                            confirmed_instance,
                                        )
                                        == confirmation.action_context
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
                    elif item.state in {
                        ReconnectScreenState.POST_LOGIN_ACTIVITY,
                        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
                        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
                    }:
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
                        if (
                            self._group_launch_plan is None
                            and self._target_identity_provider is not None
                        ):
                            target_after_delivery = (
                                self._current_target_identity_for_action(
                                    fingerprint,
                                    item,
                                )
                            )
                            if (
                                target_after_delivery is None
                                or not self._record_verified_target_value(
                                    self._verified_line_recorder,
                                    target_after_delivery,
                                    item.line_number,
                                )
                            ):
                                failures.append(
                                    "target_identity_state_persistence_failed"
                                )
                    if item.state is ReconnectScreenState.CHARACTER_SELECTION:
                        if item.character_slot_selected is True:
                            target_after_delivery = (
                                self._current_target_identity_for_action(
                                    fingerprint,
                                    item,
                                )
                                if (
                                    self._group_launch_plan is None
                                    and self._target_identity_provider
                                    is not None
                                )
                                else None
                            )
                            stable_target_verified = bool(
                                target_after_delivery is not None
                                and isinstance(
                                    item.character_slot_index,
                                    int,
                                )
                                and not isinstance(
                                    item.character_slot_index,
                                    bool,
                                )
                                and self._record_verified_target_value(
                                    self._verified_slot_recorder,
                                    target_after_delivery,
                                    item.character_slot_index,
                                )
                            )
                            if stable_target_verified or (
                                (
                                    self._group_launch_plan is not None
                                    or self._target_identity_provider is None
                                )
                                and item.character_importance
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
                                if (
                                    self._authorization_batch is not None
                                    or (
                                        self._group_launch_plan is None
                                        and self._target_identity_provider
                                        is not None
                                    )
                                ):
                                    failures.append(
                                        "target_identity_state_persistence_failed"
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
                                pending_value: (
                                    CharacterSelectionCandidate
                                    | _PendingTargetCharacterSelection
                                ) = pending_target
                                if (
                                    self._authorization_batch is not None
                                    or (
                                        self._group_launch_plan is None
                                        and self._target_identity_provider
                                        is not None
                                    )
                                ):
                                    target_after_delivery = (
                                        self._current_target_identity_for_action(
                                            fingerprint,
                                            item,
                                        )
                                    )
                                    if (
                                        target_after_delivery is None
                                        or initial_capture_route is None
                                    ):
                                        pending_target = None
                                    else:
                                        pending_value = (
                                            _PendingTargetCharacterSelection(
                                                pending_target,
                                                target_after_delivery,
                                                confirmed_instance,
                                                initial_capture_route,
                                                capture_settings_revision,
                                                confirmation.source_state_generation,
                                            )
                                        )
                                if pending_target is not None:
                                    self._character_selection_pending.add(
                                        fingerprint
                                    )
                                    self._character_selection_targets[
                                        fingerprint
                                    ] = pending_value
                    elif item.state in {
                        ReconnectScreenState.POST_LOGIN_ACTIVITY,
                        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
                        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
                    }:
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
        self._publish_reconnecting_fingerprints(now)
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

        # This exact-image path never changes general reconnect state and
        # never shares a role's two-frame evidence with any other role.
        if execute and self.auto_battle_execution_allowed():
            for window, fingerprint, item in recognized:
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

        self._check_reconnect_timing_deadlines(
            self._monotonic_clock()
        )
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
