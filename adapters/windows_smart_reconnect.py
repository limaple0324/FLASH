"""Safe multi-window smart reconnect for the confirmed Flash login flow."""

from __future__ import annotations

import ctypes
import hashlib
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
    DEFAULT_LINE_NUMBER,
    FORCE_LOGIN_CLICK_POINT,
    CHARACTER_ENTER_CLICK_POINT,
    LINE_ROUTE_CLICK_POINTS,
    NormalizedPoint,
    ReferenceScreenRecognizer,
    ScreenRecognition,
)
from adapters.windows_background_capture import (
    _WINDOWPLACEMENT,
    Win32PrintWindowProvider,
    Win32RecoveringPrintWindowProvider,
    Win32TemporarilyRevealedCaptureProvider,
    Win32VisibleRegionCaptureProvider,
    WindowCaptureProvider,
)
from adapters.windows_battle_restart import (
    BattleRestartResult,
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
from domain.character import CharacterImportance, character_importance_rank
from services.group_launch_service import GroupLaunchPlan
from services.game_operation_gate import GameOperationGate
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.smart_reconnect_capture_settings_service import (
    SmartReconnectCaptureSettings,
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


class Win32MouseMessageBackend:
    """Pure-ctypes mouse-message delivery that does not move the real cursor."""

    WM_NULL = 0x0000
    WM_MOUSEMOVE = 0x0200
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
        previous_handle: int,
        next_handle: int,
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
                and cls._handle_value(user32.GetForegroundWindow())
                == expected_foreground
                and recovery._both_neighbor_relations_are_restored(
                    user32,
                    hwnd,
                    previous_handle=previous_handle,
                    next_handle=next_handle,
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
        original_previous_handle: int,
        original_next_handle: int,
        transient_rect: tuple[int, int, int, int],
        transient_previous_handle: int,
        transient_next_handle: int,
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
            previous_handle=transient_previous_handle,
            next_handle=transient_next_handle,
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
        ):
            return False
        if not recovery._both_neighbor_relations_are_restored(
            user32,
            hwnd,
            previous_handle=original_previous_handle,
            next_handle=original_next_handle,
        ):
            try:
                insert_after, _verify_relation = (
                    state._restore_insert_after(
                        user32,
                        hwnd,
                        previous_handle=original_previous_handle,
                        next_handle=original_next_handle,
                        was_topmost=was_topmost,
                    )
                )
                if not cls._static_instance_is_current(
                    user32,
                    kernel32,
                    hwnd,
                    token=token,
                    process_handle=process_handle,
                ):
                    return False
                if not user32.SetWindowPos(
                    hwnd,
                    wintypes.HWND(insert_after),
                    0,
                    0,
                    0,
                    0,
                    state._Z_ORDER_FLAGS,
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
                )
                and recovery._both_neighbor_relations_are_restored(
                    user32,
                    hwnd,
                    previous_handle=original_previous_handle,
                    next_handle=original_next_handle,
                )
                and cls._handle_value(user32.GetForegroundWindow())
                == original_foreground
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
        original_previous_handle = 0
        original_next_handle = 0
        transient_rect = original_target_rect
        transient_previous_handle = 0
        transient_next_handle = 0
        temporarily_restored = False
        restoration_succeeded = True

        def perform_delivery() -> MouseClickResult:
            nonlocal original_placement_signature
            nonlocal was_topmost
            nonlocal original_foreground
            nonlocal original_previous_handle
            nonlocal original_next_handle
            nonlocal transient_rect
            nonlocal transient_previous_handle
            nonlocal transient_next_handle
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
                        previous_handle=transient_previous_handle,
                        next_handle=transient_next_handle,
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
                            previous_handle=transient_previous_handle,
                            next_handle=transient_next_handle,
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
                        original_previous_handle=original_previous_handle,
                        original_next_handle=original_next_handle,
                        transient_rect=transient_rect,
                        transient_previous_handle=(
                            transient_previous_handle
                        ),
                        transient_next_handle=transient_next_handle,
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
    handle: int
    width: int
    height: int
    first_seen: float
    last_digest: bytes
    changing_frames: int


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
        self._action_state_since: dict[
            str,
            tuple[ReconnectScreenState, float],
        ] = {}
        self._flow_pause_until = runtime_state.flow_pause_until
        self._preferred_line_numbers = runtime_state.preferred_line_numbers
        self._allowed_fingerprints: frozenset[str] | None = None
        self.set_allowed_fingerprints(allowed_fingerprints)
        self._battle_restarter = battle_restarter
        self._group_launch_plan: GroupLaunchPlan | None = None
        self._failure_status_service = failure_status_service
        self._failure_record_callback = failure_record_callback
        self._target_windows_provider = target_windows_provider
        self._operation_gate = operation_gate
        self._last_screen_states: dict[str, ReconnectScreenState] = {}
        self._last_trusted_capture_routes: dict[str, str] = {}
        self._trusted_connected_evidence: dict[
            str,
            _TrustedConnectedEvidence,
        ] = {}
        self._source_state_generation = 0
        self._action_confirmations: dict[
            str,
            tuple[tuple[object, ...], int],
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
        operation_gate: GameOperationGate | None = None,
        capture_settings: SmartReconnectCaptureSettings | None = None,
    ) -> "WindowsSmartReconnectController":
        window_backend = (
            window_backend
            or Win32WindowBackend(PowerShellLaunchFingerprintResolver())
        )
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=window_backend,
            # All reading remains in the background. Reconnect checks must not
            # reveal, restore, reorder, or activate a player's game window.
            capture_provider=Win32PrintWindowProvider(),
            visible_capture_provider=Win32VisibleRegionCaptureProvider(),
            obscured_capture_provider=None,
            active_refresh_capture_provider=None,
            primary_capture_is_trusted=True,
            primary_capture_is_fresh_without_visibility=True,
            recognizer=ReferenceScreenRecognizer(reference_dir),
            mouse_backend=Win32MouseMessageBackend(),
            capture_settings=capture_settings,
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
            self._action_state_since.clear()
            self._flow_pause_until.clear()
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
            allowed = self._allowed_fingerprints
            with self._screen_state_lock:
                tracked = set(self._last_screen_states)
                tracked.update(self._last_trusted_capture_routes)
                if allowed is not None:
                    tracked.update(allowed)
                self._last_screen_states = {
                    fingerprint: self._revoked_screen_state(
                        settings,
                        self._last_trusted_capture_routes.get(fingerprint),
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

    def _capture_and_recognize(
        self,
        window: WindowInfo,
        fingerprint: str,
        *,
        execute: bool = False,
    ) -> tuple[
        object | None,
        ScreenRecognition,
        bool,
        str | None,
    ]:
        settings = self.capture_settings
        if window.minimized:
            route = CAPTURE_ROUTE_MINIMIZED
            self._remember_capture_route(fingerprint, route)
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
                self._remember_capture_route(fingerprint, route)
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
                self._remember_capture_route(
                    fingerprint,
                    CAPTURE_ROUTE_VISIBLE,
                )
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
            self._remember_capture_route(fingerprint, route)
            return self._unknown_capture_result(primary_sample, route)

        visibly_unobscured = self._window_is_fully_visible_without_capture(
            window
        )
        if visibly_unobscured is True:
            route = CAPTURE_ROUTE_VISIBLE
            self._remember_capture_route(fingerprint, route)
            if not settings.visible:
                return self._disabled_capture_result(route)
            return self._unknown_capture_result(visible_sample, route)
        if visibly_unobscured is None:
            # Classification itself was not trustworthy. It cannot be
            # assigned to either the enabled or disabled route, and an old
            # online/disconnected state must not survive this observation.
            return self._unknown_capture_result(visible_sample)

        route = CAPTURE_ROUTE_OBSCURED
        self._remember_capture_route(fingerprint, route)
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

        # Keep the old background provider only as non-authoritative evidence.
        # It may help diagnostics, but can never report online/disconnected or
        # authorize a click for an obscured production window.
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
        ready_at = self._terminal_ready_after.get(fingerprint)
        if (
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
        same_window = (
            previous is not None
            and previous.handle == window.handle
            and previous.width == sample.width
            and previous.height == sample.height
        )
        if (
            same_window
            and previous is not None
            and previous.last_digest != digest
        ):
            evidence = _TerminalEvidence(
                handle=window.handle,
                width=sample.width,
                height=sample.height,
                first_seen=previous.first_seen,
                last_digest=digest,
                changing_frames=previous.changing_frames + 1,
            )
        else:
            evidence = _TerminalEvidence(
                handle=window.handle,
                width=sample.width,
                height=sample.height,
                first_seen=now,
                last_digest=digest,
                changing_frames=1,
            )
        self._terminal_evidence[fingerprint] = evidence
        return (
            evidence.changing_frames >= TERMINAL_CONFIRMATION_FRAMES
            and now - evidence.first_seen >= TERMINAL_CONFIRMATION_SECONDS
            and now - ready_at >= TERMINAL_CONFIRMATION_SECONDS
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
        with self._screen_state_lock:
            observation_source_generation = self._source_state_generation
        _settings, observation_revision = self._capture_settings_snapshot()
        candidates = (
            tuple(candidate_windows)
            if candidate_windows is not None
            else self._candidate_windows()
        )
        by_fingerprint: dict[str, list[WindowInfo]] = {}
        for window in candidates:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint in requested:
                by_fingerprint.setdefault(fingerprint, []).append(window)
        observed: dict[str, ReconnectScreenState] = {}
        observed_routes: dict[str, str | None] = {}
        fresh_instances: dict[
            str,
            tuple[WindowInstanceToken, str],
        ] = {}
        for fingerprint in requested:
            matches = by_fingerprint.get(fingerprint, ())
            if len(matches) != 1:
                # A missing or ambiguous live instance invalidates both the
                # passive status cache and any short-lived CONNECTED evidence.
                observed[fingerprint] = ReconnectScreenState.UNKNOWN
                continue
            (
                sample,
                recognition,
                fresh_capture,
                route,
            ) = self._capture_and_recognize(
                matches[0],
                fingerprint,
            )
            observed_routes[fingerprint] = route
            instance = WindowInstanceToken.from_window(matches[0])
            if (
                fresh_capture
                and instance is not None
                and route is not None
            ):
                fresh_instances[fingerprint] = (instance, route)
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
                matches[0],
                fingerprint,
                route,
                observation_revision,
            ):
                observed[fingerprint] = ReconnectScreenState.CONNECTED
            else:
                observed[fingerprint] = ReconnectScreenState.UNKNOWN
        with self._screen_state_lock:
            source_generation_changed = (
                self._source_state_generation
                != observation_source_generation
            )
            with self._capture_settings_lock:
                if self._capture_settings_revision != observation_revision:
                    observed = {
                        fingerprint: self._revoked_screen_state(
                            self._capture_settings,
                            observed_routes.get(fingerprint),
                        )
                        for fingerprint in observed
                    }
                    for fingerprint in fresh_instances:
                        self._trusted_connected_evidence.pop(
                            fingerprint,
                            None,
                        )
                elif source_generation_changed:
                    for fingerprint, state in tuple(observed.items()):
                        if state is ReconnectScreenState.CONNECTED:
                            observed[fingerprint] = (
                                ReconnectScreenState.UNKNOWN
                            )
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

    def set_group_launch_plan(self, plan: GroupLaunchPlan | None) -> None:
        with self._scan_lock:
            previous_plan = self._group_launch_plan
            previous_scope = self._allowed_fingerprints
            previous_token = self._runtime_scope_token
            if plan is None:
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
            self.set_allowed_fingerprints(plan.fingerprints)
            self._runtime_scope_token = scope_token
            if previous_token != scope_token:
                # A group switch is a new reconnect context even when the two
                # groups share one role or the entire fingerprint set.
                self._retain_runtime_scope(frozenset())
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
            )
            removed = tracked - fingerprints
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
                self._terminal_ready_after,
                self._terminal_evidence,
                self._last_screen_states,
                self._last_trusted_capture_routes,
                self._trusted_connected_evidence,
            ):
                for fingerprint in removed:
                    mapping.pop(fingerprint, None)
        for fingerprint in removed:
            self._clear_reconnect_failure(fingerprint)
        self._publish_reconnecting_fingerprints()
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

    def _run_game_mutation(
        self,
        operation: str,
        callback: Callable[[], object],
        *,
        expected_capture_settings_revision: int | None = None,
        capture_route: str | None = None,
    ) -> tuple[bool, object | None]:
        """Run only the actual game change under the shared exclusive gate."""
        if not self._execution_allowed():
            return False, None
        if not self._capture_authority_is_current(
            expected_capture_settings_revision,
            capture_route,
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
            # Keep this lock through the backend call. A settings update then
            # linearizes either before this final authorization check or after
            # the mutation has completed, never between the two.
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
                return True, callback()
        finally:
            if lease is not None:
                lease.release()

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

    def _recognition_for_preferred_line(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> ScreenRecognition:
        """Use the last confirmed route instead of an ambiguous dialog hint."""
        if (
            item.state is not ReconnectScreenState.LINE_SELECTION
            or item.click_point is None
        ):
            return item
        line_number = self._preferred_line_numbers.get(
            fingerprint,
            DEFAULT_LINE_NUMBER,
        )
        click_point = LINE_ROUTE_CLICK_POINTS.get(line_number)
        if click_point is None:
            return item
        return replace(
            item,
            line_number=line_number,
            click_point=click_point,
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
        self._terminal_ready_after.pop(fingerprint, None)
        self._terminal_evidence.pop(fingerprint, None)
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
    ) -> ScreenRecognition:
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
        )

    def _character_target_is_safe(
        self,
        fingerprint: str,
        item: ScreenRecognition,
    ) -> ScreenRecognition | None:
        """Apply the confirmed original-role, highest-level, then role order."""
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
            return item

        candidates: tuple[CharacterSelectionCandidate, ...] = (
            item.character_candidates
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
            )

        expected_level_candidates = tuple(
            candidate
            for candidate in candidates
            if (
                expected_level is not None
                and candidate.level == expected_level
            )
        )
        if len(expected_level_candidates) == 1:
            return self._candidate_result(
                item,
                expected_level_candidates[0],
                target.importance,
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
        )

    def _recognition_for_session_action(
        self,
        fingerprint: str,
        recognition: ScreenRecognition,
    ) -> ScreenRecognition:
        if (
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
                return replace(recognition, click_point=None)
            return role_target
        return recognition

    def _current_action_window(
        self,
        expected: WindowInfo,
        fingerprint: str,
    ) -> WindowInfo | None:
        candidates, failures, blocked_fingerprints = (
            self._candidate_window_set()
        )
        if (
            fingerprint in blocked_fingerprints
            or "target_window_provider_failed" in failures
        ):
            return None
        matches = tuple(
            candidate
            for candidate in candidates
            if normalize_launch_fingerprint(candidate.launch_fingerprint)
            == fingerprint
        )
        if len(matches) != 1:
            return None
        current = matches[0]
        if (
            current.handle != expected.handle
            or current.process_id is None
            or expected.process_id is None
            or current.process_id != expected.process_id
        ):
            return None
        return current

    def _action_still_matches(
        self,
        window: WindowInfo,
        fingerprint: str,
        expected: ScreenRecognition,
        expected_capture_settings_revision: int,
        expected_capture_route: str | None,
    ) -> str | None:
        if not self._capture_authority_is_current(
            expected_capture_settings_revision,
            expected_capture_route,
        ):
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
        )
        if (
            not fresh_capture
            or current_route is None
            or not self._capture_authority_is_current(
                expected_capture_settings_revision,
                current_route,
            )
        ):
            return None
        current = self._recognition_for_session_action(
            fingerprint,
            recognition,
        )
        current = self._recognition_for_preferred_line(
            fingerprint,
            current,
        )
        if self._action_signature(current) != self._action_signature(expected):
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
    ) -> BattleRestartResult:
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
        if "target_window_provider_failed" in source_failures:
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(
                False,
                "target_window_provider_failed",
            )
        if fingerprint in blocked_fingerprints:
            self._clear_action_confirmation(fingerprint)
            return BattleRestartResult(
                False,
                "reconnect_restart_identity_unsafe",
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
            mutation = lambda: restarter.restart(matches[0], target)
        else:
            reopen_missing = getattr(restarter, "reopen_missing", None)
            if not callable(reopen_missing):
                self._clear_action_confirmation(fingerprint)
                return BattleRestartResult(
                    False,
                    "reconnect_restart_unavailable",
                )
            mutation = lambda: reopen_missing(target, candidates)
        permitted, mutation_result = self._run_game_mutation(
            "smart-reconnect-restart",
            mutation,
            expected_capture_settings_revision=(
                expected_capture_settings_revision
            ),
            capture_route=capture_route,
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
        if self._allowed_fingerprints is not None and not set(
            fingerprints
        ).issubset(self._allowed_fingerprints):
            failures.append("group_identity_set_mismatch")
        return failures

    def _revoke_group_failure_evidence(
        self,
        windows: tuple[WindowInfo, ...],
        blocked_fingerprints: frozenset[str],
    ) -> None:
        """Replace every identity affected by an unsafe group with UNKNOWN."""
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
        with self._screen_state_lock:
            affected = (
                set(self._last_screen_states)
                | set(self._trusted_connected_evidence)
                | set(blocked_fingerprints)
                | live_fingerprints
            )
            if self._allowed_fingerprints is not None:
                affected.update(self._allowed_fingerprints)
            self._mark_fingerprints_unknown_locked(affected)

    def _mark_fingerprints_unknown_locked(
        self,
        fingerprints: Iterable[str],
    ) -> None:
        for fingerprint in fingerprints:
            if normalize_launch_fingerprint(fingerprint) is None:
                continue
            self._last_screen_states[fingerprint] = (
                ReconnectScreenState.UNKNOWN
            )
            self._trusted_connected_evidence.pop(
                fingerprint,
                None,
            )

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
        allowed = self._allowed_fingerprints
        if allowed is None:
            return frozenset(affected)
        live_counts = Counter(
            fingerprint
            for window in windows
            if (
                fingerprint := normalize_launch_fingerprint(
                    window.launch_fingerprint
                )
            )
            is not None
            and fingerprint in allowed
            and fingerprint not in affected
        )
        uniquely_valid = {
            fingerprint
            for fingerprint, count in live_counts.items()
            if count == 1
        }
        affected.update(allowed - uniquely_valid)
        return frozenset(affected)

    def _revoke_source_failure_evidence(
        self,
        fingerprints: frozenset[str],
    ) -> None:
        if not fingerprints:
            return
        with self._screen_state_lock:
            self._source_state_generation += 1
            self._mark_fingerprints_unknown_locked(fingerprints)

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

        missing = tuple(sorted(self._pending_reopen_fingerprints))
        if not missing:
            return 0, [], None
        if "target_window_provider_failed" in source_failures:
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

            permitted, mutation_result = self._run_game_mutation(
                "smart-reconnect-reopen",
                lambda: retry_open(target, candidate_windows),
                expected_capture_settings_revision=(
                    expected_capture_settings_revision
                ),
                capture_route=capture_route,
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
                self._report_reconnect_failure(
                    fingerprint,
                    expected_capture_settings_revision=(
                        expected_capture_settings_revision
                    ),
                    capture_route=capture_route,
                )

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
            source_failures,
            blocked_fingerprints,
        ) = self._candidate_window_set()
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
        source_failure_affected_fingerprints = (
            self._source_failure_affected_fingerprints(
                windows,
                source_failures,
                blocked_fingerprints,
            )
        )
        self._revoke_source_failure_evidence(
            source_failure_affected_fingerprints
        )
        state_before = self._runtime_state_signature()
        now = self._monotonic_clock()
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
            )
        )
        group_failures = self._group_failures(windows)
        failures = [*group_failures, *source_failures, *retry_failures]
        if blocked_fingerprints and not source_failures:
            failures.append("window_identity_blocked")
        if group_failures:
            # A partially validated group must never carry a first-frame
            # confirmation or old CONNECTED evidence into a later, different
            # group or identity set.
            self._clear_action_confirmation()
            self._revoke_group_failure_evidence(
                windows,
                blocked_fingerprints,
            )
            if (
                self._runtime_state_store is not None
                and state_before != self._runtime_state_signature()
                and not self._persist_runtime_state()
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
            )
            self._last_result = result
            self._publish_reconnecting_fingerprints(now)
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
            (
                sample,
                recognition,
                fresh_capture,
                capture_route,
            ) = self._capture_and_recognize(
                window,
                fingerprint,
                execute=execute,
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
            if (
                fresh_capture
                and instance is not None
                and capture_route is not None
            ):
                fresh_capture_instances[fingerprint] = (
                    instance,
                    capture_route,
                )
            if sample is not None and sample.api_succeeded:
                captured_windows += 1
            else:
                self._clear_action_confirmation(fingerprint)
            if recognition.state is ReconnectScreenState.DISCONNECTED:
                if (
                    fresh_capture
                    and recognition.click_point is not None
                    and self._action_is_confirmed(fingerprint, recognition)
                ):
                    confirmed_action_fingerprints.add(fingerprint)
                elif not fresh_capture:
                    self._clear_action_confirmation(fingerprint)
            elif recognition.state is ReconnectScreenState.CONNECTED:
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
                    ):
                        self._clear_reconnect_session(fingerprint)
                        self._clear_reconnect_failure(fingerprint)
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
            recognition = self._recognition_for_session_action(
                fingerprint,
                recognition,
            )
            recognition = self._recognition_for_preferred_line(
                fingerprint,
                recognition,
            )
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
            recognized.append((window, fingerprint, recognition))

        current_capture_settings, current_capture_settings_revision = (
            self._capture_settings_snapshot()
        )
        settings_changed_during_scan = (
            capture_settings_revision
            != current_capture_settings_revision
        )
        if settings_changed_during_scan:
            confirmed_action_fingerprints.clear()
            recognized = [
                (
                    window,
                    fingerprint,
                    replace(
                        item,
                        state=self._revoked_screen_state(
                            current_capture_settings,
                            capture_routes.get(fingerprint),
                        ),
                        click_point=None,
                    ),
                )
                for window, fingerprint, item in recognized
            ]

        if execute and not settings_changed_during_scan:
            latest_capture_settings = current_capture_settings
            with self._screen_state_lock:
                with self._capture_settings_lock:
                    latest_capture_settings = self._capture_settings
                    settings_changed_during_scan = (
                        capture_settings_revision
                        != self._capture_settings_revision
                    )
                    if not settings_changed_during_scan:
                        self._pending_reconnect_fingerprints.update(
                            fingerprint
                            for _window, fingerprint, item in recognized
                            if (
                                item.state
                                is ReconnectScreenState.DISCONNECTED
                                and fingerprint
                                in confirmed_action_fingerprints
                            )
                        )
                        self._publish_reconnecting_fingerprints(now)
            if settings_changed_during_scan:
                confirmed_action_fingerprints.clear()
                recognized = [
                    (
                        window,
                        fingerprint,
                        replace(
                            item,
                            state=self._revoked_screen_state(
                                latest_capture_settings,
                                capture_routes.get(fingerprint),
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
                capture_route = capture_routes.get(fingerprint)
                if not self._capture_authority_is_current(
                    capture_settings_revision,
                    capture_route,
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
                permitted, mutation_result = self._run_game_mutation(
                    "smart-reconnect-battle-restart",
                    lambda: self._battle_restarter.restart(
                        window,
                        target,
                    ),
                    expected_capture_settings_revision=(
                        capture_settings_revision
                    ),
                    capture_route=capture_route,
                )
                if not permitted or not isinstance(
                    mutation_result,
                    BattleRestartResult,
                ):
                    self._clear_action_confirmation(fingerprint)
                    if self._capture_authority_is_current(
                        capture_settings_revision,
                        capture_route,
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
            for window, fingerprint, item in actionable:
                if not self._execution_allowed():
                    break
                initial_capture_route = capture_routes.get(fingerprint)
                if not self._capture_authority_is_current(
                    capture_settings_revision,
                    initial_capture_route,
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
                def deliver_click():
                    current_window = self._current_action_window(
                        window,
                        fingerprint,
                    )
                    if current_window is None:
                        return "changed", False
                    capture_instance_token = (
                        WindowInstanceToken.from_window(current_window)
                    )
                    if capture_instance_token is None:
                        return "changed", False
                    current_capture_route = self._action_still_matches(
                        current_window,
                        fingerprint,
                        item,
                        capture_settings_revision,
                        initial_capture_route,
                    )
                    if current_capture_route is None:
                        return "changed", False
                    current_window = self._current_action_window(
                        current_window,
                        fingerprint,
                    )
                    if (
                        current_window is None
                        or WindowInstanceToken.from_window(current_window)
                        != capture_instance_token
                    ):
                        return "changed", False
                    if not self._mouse_backend.is_window(
                        current_window.handle
                    ):
                        return "invalid", False
                    if not self._mouse_backend.probe_responsive(
                        current_window.handle,
                        self._preflight_timeout_ms,
                    ):
                        return "unresponsive", False
                    current_window = self._current_action_window(
                        current_window,
                        fingerprint,
                    )
                    if (
                        current_window is None
                        or not self._capture_authority_is_current(
                            capture_settings_revision,
                            current_capture_route,
                        )
                    ):
                        return "changed", False
                    instance_token = WindowInstanceToken.from_window(
                        current_window
                    )
                    if instance_token != capture_instance_token:
                        return "changed", False
                    try:
                        return (
                            "delivered",
                            self._mouse_backend.click_relative(
                                current_window.handle,
                                item.click_point,
                                current_window.process_id,
                                instance_token,
                            ),
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

                permitted, mutation_result = self._run_game_mutation(
                    "smart-reconnect-click",
                    deliver_click,
                    expected_capture_settings_revision=(
                        capture_settings_revision
                    ),
                    capture_route=initial_capture_route,
                )
                if not permitted or not isinstance(mutation_result, tuple):
                    self._clear_action_confirmation(fingerprint)
                    if self._capture_authority_is_current(
                        capture_settings_revision,
                        initial_capture_route,
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
                )
                if (
                    authority_is_current
                    or click_result.delivered
                    or click_result.delivery_uncertain
                ):
                    self._action_retry_after[fingerprint] = (
                        item.state,
                        mutation_completed_at
                        + self._policy.retry_interval_seconds,
                    )
                if click_result.delivered:
                    clicked_windows += 1
                    self._action_confirmations.pop(fingerprint, None)
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
                        self._flow_pause_until[fingerprint] = (
                            mutation_completed_at
                            + self._policy.retry_interval_seconds
                        )
                if not authority_is_current:
                    self._clear_action_confirmation(fingerprint)
                    continue
                if click_result.delivered:
                    if (
                        item.state is ReconnectScreenState.LINE_SELECTION
                        and item.line_number in LINE_ROUTE_CLICK_POINTS
                    ):
                        self._preferred_line_numbers[fingerprint] = (
                            item.line_number
                        )
                    if item.state is ReconnectScreenState.CHARACTER_SELECTION:
                        if item.character_slot_selected is True:
                            self._character_selection_pending.discard(
                                fingerprint
                            )
                            self._arm_terminal_completion(
                                fingerprint,
                                mutation_completed_at,
                            )
                        elif item.character_slot_selected is False:
                            self._character_selection_pending.add(fingerprint)
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

        # A re-entrant backend or policy callback can change settings after the
        # earlier action checks. Revoke the returned batch itself, not only the
        # separately published role-state cache, so callers can never receive
        # all_connected=True beside CHECK_DISABLED/UNKNOWN role states.
        final_capture_settings, final_capture_settings_revision = (
            self._capture_settings_snapshot()
        )
        if final_capture_settings_revision != capture_settings_revision:
            settings_changed_during_scan = True
            confirmed_action_fingerprints.clear()
            recognized = [
                (
                    window,
                    fingerprint,
                    replace(
                        item,
                        state=self._revoked_screen_state(
                            final_capture_settings,
                            capture_routes.get(fingerprint),
                        ),
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
        )
        with self._screen_state_lock:
            with self._capture_settings_lock:
                latest_capture_settings = self._capture_settings
                settings_changed_during_scan = (
                    capture_settings_revision
                    != self._capture_settings_revision
                )
            published_states = (
                {
                    fingerprint: self._revoked_screen_state(
                        latest_capture_settings,
                        capture_routes.get(fingerprint),
                    )
                    for _window, fingerprint, _item in recognized
                }
                if settings_changed_during_scan
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
            if source_failure_affected_fingerprints:
                self._source_state_generation += 1
            evidence_observed_at = time.monotonic()
            for _window, fingerprint, item in recognized:
                fresh_instance = fresh_capture_instances.get(fingerprint)
                if (
                    not settings_changed_during_scan
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
