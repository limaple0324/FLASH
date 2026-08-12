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
from core.target_window_contract import (
    TargetWindowPhase,
    TargetWindowSnapshot,
)
from services.group_launch_service import (
    GroupLaunchService,
    GroupLaunchTarget,
)
from services.game_operation_gate import GameOperationGate
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.event_bus import EventBus


ROLE_STATUS_OPEN = "已開啟"
ROLE_STATUS_CLOSED = "未開啟"
ROLE_STATUS_DISCONNECTED = "斷線"
ROLE_STATUS_RECONNECTING = "重連中"
ROLE_STATUS_FAILED = "重連失敗"
ROLE_STATUS_CHECK_DISABLED = "檢查已關閉"
GROUP_ROLE_STATUS_CHANGED_EVENT = "group_role_status_changed"


@dataclass(frozen=True, slots=True)
class GroupRoleStatus:
    """Player-facing state with an opaque action identity."""

    action_id: str
    display_name: str
    status: str
    order: int


@dataclass(frozen=True, slots=True)
class GroupRoleStatusChange:
    """A real observed role-state transition for downstream reminders."""

    group_name: str
    previous_status: str | None
    current: GroupRoleStatus


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

    _DISCONNECTED_STATES = frozenset(
        {
            ReconnectScreenState.LOGIN_START,
            ReconnectScreenState.FORCE_LOGIN_START,
            ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
            ReconnectScreenState.LINE_SELECTION,
            ReconnectScreenState.CHARACTER_SELECTION,
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
        target_snapshot_provider: (
            Callable[[str], TargetWindowSnapshot] | None
        ) = None,
        operation_gate: GameOperationGate | None = None,
        event_bus: EventBus | None = None,
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
        self._target_snapshot_provider = target_snapshot_provider
        self._operation_gate = operation_gate
        if event_bus is not None and not isinstance(event_bus, EventBus):
            raise TypeError("event_bus must be EventBus.")
        self._event_bus = event_bus
        self._lock = threading.RLock()
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
            self._rows = ()

    def _candidate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if self._keywords
            and all(keyword in window.title.casefold() for keyword in self._keywords)
        )

    def _target_snapshot(self, group_name: str) -> TargetWindowSnapshot | None:
        if self._target_snapshot_provider is None:
            return None
        try:
            snapshot = self._target_snapshot_provider(group_name)
        except Exception:
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                group_name,
                failure_codes=("target_snapshot_failed",),
            )
        if (
            not isinstance(snapshot, TargetWindowSnapshot)
            or snapshot.group_name != group_name
        ):
            return TargetWindowSnapshot(
                TargetWindowSnapshot.SCHEMA_VERSION,
                group_name,
                failure_codes=("target_snapshot_invalid",),
            )
        return snapshot

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
                self._rows = ()
            return ()
        plan = self._launch_service.plan(group_name.strip())
        if not plan.ready:
            with self._lock:
                self._rows = ()
            return ()

        central_snapshot = self._target_snapshot(plan.group_name)
        if central_snapshot is None:
            windows = self._candidate_windows()
            by_fingerprint = self._windows_by_fingerprint(windows)
            contract_by_fingerprint = {}
        else:
            by_fingerprint = {}
            contract_by_fingerprint = {
                target.fingerprint: target
                for target in central_snapshot.targets
                if target.fingerprint is not None
            }
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
            contract = contract_by_fingerprint.get(fingerprint)
            matches = by_fingerprint.get(fingerprint, ())
            failure_key = f"role:{fingerprint}"
            state = states.get(fingerprint)
            central_failed = (
                contract is None
                or any(
                    code != "unidentified_candidate_window"
                    for code in central_snapshot.failure_codes
                )
                or (
                    not contract.safe
                    and contract.phase is not TargetWindowPhase.OFFLINE
                )
            ) if central_snapshot is not None else False
            if state is ReconnectScreenState.CHECK_DISABLED:
                status = ROLE_STATUS_CHECK_DISABLED
            elif (
                self._failure_service.has(failure_key)
                or len(matches) > 1
                or central_failed
            ):
                status = ROLE_STATUS_FAILED
            elif fingerprint in reconnecting or fingerprint in launching:
                status = ROLE_STATUS_RECONNECTING
            elif state is ReconnectScreenState.DISCONNECTED:
                status = ROLE_STATUS_DISCONNECTED
            elif state in self._DISCONNECTED_STATES:
                # Login/line/character screens are not proof that this
                # process started a reconnect session. Only the controller's
                # explicit reconnect-session set may show "重連中".
                status = ROLE_STATUS_DISCONNECTED
            elif (
                (contract is not None and contract.safe)
                if central_snapshot is not None
                else len(matches) == 1
            ):
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
            self._rows = result
        for item in result:
            previous_status = previous.get(item.action_id)
            if previous_status != item.status:
                self._record("狀態偵測", item.display_name, item.status)
                if self._event_bus is not None:
                    self._event_bus.publish(
                        GROUP_ROLE_STATUS_CHANGED_EVENT,
                        GroupRoleStatusChange(
                            group_name=plan.group_name,
                            previous_status=previous_status,
                            current=item,
                        ),
                    )
        return result

    def activate_or_launch(
        self,
        group_name: object,
        action_id: object,
    ) -> GroupRoleActionResult:
        lease = (
            self._operation_gate.acquire(
                "role-activate-or-launch",
                timeout_seconds=0,
            )
            if self._operation_gate is not None
            else None
        )
        if self._operation_gate is not None and lease is None:
            return GroupRoleActionResult(
                False,
                failure_code="operation_gate_closed",
            )
        try:
            return self._activate_or_launch_without_gate(
                group_name,
                action_id,
            )
        finally:
            if lease is not None:
                lease.release()

    def _activate_or_launch_without_gate(
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

        central_snapshot = self._target_snapshot(plan.group_name)
        if central_snapshot is None:
            windows = self._candidate_windows()
            by_fingerprint = self._windows_by_fingerprint(windows)
            matches = by_fingerprint.get(target.fingerprint, ())
            contract = None
            central_failures: tuple[str, ...] = ()
        else:
            windows = ()
            matches = ()
            contract = next(
                (
                    item
                    for item in central_snapshot.targets
                    if item.fingerprint == target.fingerprint
                ),
                None,
            )
            central_failures = central_snapshot.failure_codes

        active_handle = (
            contract.handle
            if contract is not None and contract.safe
            else (matches[0].handle if len(matches) == 1 else None)
        )
        if active_handle is not None:
            if self._activation_backend.activate(active_handle):
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
        if (
            len(matches) > 1
            or (
                contract is not None
                and "window_identity_duplicate" in contract.failure_codes
            )
        ):
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
        if (
            "unidentified_candidate_window" in central_failures
            or any(
                normalize_launch_fingerprint(window.launch_fingerprint) is None
                for window in windows
            )
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
        if (
            central_snapshot is not None
            and any(
                code != "unidentified_candidate_window"
                for code in central_failures
            )
        ):
            self._failure_service.report(
                f"role:{target.fingerprint}",
                target.display_name,
            )
            self._record("角色操作", target.display_name, "中央目標資料不完整")
            return GroupRoleActionResult(
                False,
                failure_code="role_identity_unresolved",
            )
        if (
            central_snapshot is not None
            and (
                contract is None
                or contract.phase is not TargetWindowPhase.OFFLINE
            )
        ):
            self._failure_service.report(
                f"role:{target.fingerprint}",
                target.display_name,
            )
            self._record("角色操作", target.display_name, "中央身分無法確認")
            return GroupRoleActionResult(
                False,
                failure_code="role_identity_unresolved",
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
