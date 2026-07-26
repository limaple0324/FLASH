"""Homepage role status and exact single-role actions."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Iterable, Protocol

from adapters.windows_battle_restart import (
    ShortcutOpenBackend,
    WindowsShortcutOpenBackend,
)
from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import WindowBackend, WindowInfo
from core.reconnect_policy import ReconnectScreenState
from services.group_launch_service import (
    GroupLaunchService,
    GroupLaunchTarget,
)
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)


ROLE_STATUS_OPEN = "已開啟"
ROLE_STATUS_CLOSED = "未開啟"
ROLE_STATUS_DISCONNECTED = "斷線"
ROLE_STATUS_RECONNECTING = "重連中"
ROLE_STATUS_FAILED = "重連失敗"


@dataclass(frozen=True, slots=True)
class GroupRoleStatus:
    """Player-facing state with an opaque action identity."""

    action_id: str
    display_name: str
    status: str
    order: int


@dataclass(frozen=True, slots=True)
class GroupRoleActionResult:
    success: bool
    action: str | None = None
    failure_code: str | None = None


class WindowActivationBackend(Protocol):
    def activate(self, handle: int) -> bool:
        """Bring exactly one existing game window to the foreground."""


class Win32WindowActivationBackend:
    SW_RESTORE = 9

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
        user32.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL

    def activate(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindowAsync(hwnd, self.SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))


class GroupRoleStatusService:
    """Cache ordered role rows and fail closed on ambiguous identities."""

    _RECONNECTING_STATES = frozenset(
        {
            ReconnectScreenState.LOGIN_START,
            ReconnectScreenState.FORCE_LOGIN_START,
            ReconnectScreenState.LINE_SELECTION,
            ReconnectScreenState.CHARACTER_SELECTION,
            ReconnectScreenState.POST_LOGIN_ACTIVITY,
            ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
            ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
            ReconnectScreenState.RECONNECTING,
        }
    )

    def __init__(
        self,
        launch_service: GroupLaunchService,
        window_backend: WindowBackend,
        reconnect_failure_service: ReconnectFailureStatusService,
        *,
        screen_states_provider: (
            Callable[[], dict[str, ReconnectScreenState]] | None
        ) = None,
        reconnecting_provider: Callable[[], Iterable[str]] | None = None,
        activation_backend: WindowActivationBackend | None = None,
        shortcut_open_backend: ShortcutOpenBackend | None = None,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        monotonic_clock: Callable[[], float] = time.monotonic,
        record_callback: (
            Callable[[str, str, str], object] | None
        ) = None,
    ) -> None:
        self._launch_service = launch_service
        self._window_backend = window_backend
        self._failure_service = reconnect_failure_service
        self._screen_states_provider = screen_states_provider or (lambda: {})
        self._reconnecting_provider = reconnecting_provider or (lambda: ())
        self._activation_backend = (
            activation_backend or Win32WindowActivationBackend()
        )
        self._shortcut_open_backend = (
            shortcut_open_backend or WindowsShortcutOpenBackend()
        )
        self._keywords = tuple(
            value.strip().casefold()
            for value in title_keywords
            if isinstance(value, str) and value.strip()
        )
        self._monotonic_clock = monotonic_clock
        self._record_callback = record_callback
        self._lock = threading.RLock()
        self._group_name: str | None = None
        self._rows: tuple[GroupRoleStatus, ...] = ()
        self._launching_until: dict[str, float] = {}

    def _record(self, category: str, role_name: str, detail: str) -> None:
        if self._record_callback is None:
            return
        try:
            self._record_callback(category, role_name, detail)
        except Exception:
            pass

    def snapshot(self) -> tuple[GroupRoleStatus, ...]:
        with self._lock:
            return self._rows

    def clear_cache(self) -> None:
        with self._lock:
            self._group_name = None
            self._rows = ()

    def _candidate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if self._keywords
            and all(keyword in window.title.casefold() for keyword in self._keywords)
        )

    @staticmethod
    def _windows_by_fingerprint(
        windows: Iterable[WindowInfo],
    ) -> dict[str, list[WindowInfo]]:
        result: dict[str, list[WindowInfo]] = {}
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is not None:
                result.setdefault(fingerprint, []).append(window)
        return result

    def refresh(self, group_name: object) -> tuple[GroupRoleStatus, ...]:
        if not isinstance(group_name, str) or not group_name.strip():
            with self._lock:
                self._group_name = None
                self._rows = ()
            return ()
        plan = self._launch_service.plan(group_name.strip())
        if not plan.ready:
            with self._lock:
                self._group_name = group_name.strip()
                self._rows = ()
            return ()

        windows = self._candidate_windows()
        by_fingerprint = self._windows_by_fingerprint(windows)
        states = self._screen_states_provider()
        reconnecting = {
            fingerprint
            for item in self._reconnecting_provider()
            if (fingerprint := normalize_launch_fingerprint(item)) is not None
        }
        now = self._monotonic_clock()
        with self._lock:
            self._launching_until = {
                fingerprint: deadline
                for fingerprint, deadline in self._launching_until.items()
                if deadline > now
            }
            launching = set(self._launching_until)

        rows: list[GroupRoleStatus] = []
        for target in plan.targets:
            fingerprint = target.fingerprint
            matches = by_fingerprint.get(fingerprint, ())
            failure_key = f"role:{fingerprint}"
            state = states.get(fingerprint)
            if self._failure_service.has(failure_key) or len(matches) > 1:
                status = ROLE_STATUS_FAILED
            elif fingerprint in reconnecting or fingerprint in launching:
                status = ROLE_STATUS_RECONNECTING
            elif state is ReconnectScreenState.DISCONNECTED:
                status = ROLE_STATUS_DISCONNECTED
            elif state in self._RECONNECTING_STATES:
                status = ROLE_STATUS_RECONNECTING
            elif len(matches) == 1:
                status = ROLE_STATUS_OPEN
            else:
                status = ROLE_STATUS_CLOSED
            rows.append(
                GroupRoleStatus(
                    action_id=fingerprint,
                    display_name=target.display_name,
                    status=status,
                    order=target.order,
                )
            )
        result = tuple(rows)
        with self._lock:
            previous = {
                item.action_id: item.status for item in self._rows
            }
            self._group_name = plan.group_name
            self._rows = result
        for item in result:
            if previous.get(item.action_id) != item.status:
                self._record("狀態偵測", item.display_name, item.status)
        return result

    def activate_or_launch(
        self,
        group_name: object,
        action_id: object,
    ) -> GroupRoleActionResult:
        if (
            not isinstance(group_name, str)
            or not group_name.strip()
            or not isinstance(action_id, str)
        ):
            return GroupRoleActionResult(False, failure_code="role_invalid")
        fingerprint = normalize_launch_fingerprint(action_id)
        plan = self._launch_service.plan(group_name.strip())
        target = (
            plan.target_for_fingerprint(fingerprint)
            if plan.ready and fingerprint is not None
            else None
        )
        if target is None:
            return GroupRoleActionResult(
                False,
                failure_code="role_identity_unresolved",
            )

        windows = self._candidate_windows()
        by_fingerprint = self._windows_by_fingerprint(windows)
        matches = by_fingerprint.get(target.fingerprint, ())
        if len(matches) == 1:
            if self._activation_backend.activate(matches[0].handle):
                self._record("角色操作", target.display_name, "切換至前景成功")
                return GroupRoleActionResult(True, action="activated")
            self._failure_service.report(
                f"role:{target.fingerprint}",
                target.display_name,
            )
            self._record("角色操作", target.display_name, "切換至前景失敗")
            return GroupRoleActionResult(
                False,
                failure_code="role_activation_failed",
            )
        if len(matches) > 1:
            self._failure_service.report(
                f"role:{target.fingerprint}",
                target.display_name,
            )
            self._record("角色操作", target.display_name, "視窗身分重複")
            return GroupRoleActionResult(
                False,
                failure_code="role_window_ambiguous",
            )

        # An unidentified Flash window could already be this role. Never open
        # another copy until every visible candidate has a unique identity.
        if any(
            normalize_launch_fingerprint(window.launch_fingerprint) is None
            for window in windows
        ):
            self._failure_service.report(
                f"role:{target.fingerprint}",
                target.display_name,
            )
            self._record("角色操作", target.display_name, "視窗無法唯一確認")
            return GroupRoleActionResult(
                False,
                failure_code="role_existing_window_unknown",
            )
        if not self._shortcut_open_backend.open_shortcut(target):
            self._failure_service.report(
                f"role:{target.fingerprint}",
                target.display_name,
            )
            self._record("角色操作", target.display_name, "啟動失敗")
            return GroupRoleActionResult(
                False,
                failure_code="role_shortcut_open_failed",
            )
        self._failure_service.clear(f"role:{target.fingerprint}")
        with self._lock:
            self._launching_until[target.fingerprint] = (
                self._monotonic_clock() + 60.0
            )
        self._record("角色操作", target.display_name, "啟動成功")
        return GroupRoleActionResult(True, action="launched")
