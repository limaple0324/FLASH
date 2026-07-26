"""Launch a configured group and restore every exact saved window position."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from adapters.windows_battle_restart import (
    ShortcutOpenBackend,
    WindowsShortcutOpenBackend,
)
from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import WindowBackend, WindowInfo
from adapters.windows_window_placement import (
    WindowPlacementBackend,
    Win32WindowPlacementBackend,
)
from services.group_launch_service import (
    GroupLaunchService,
    GroupLaunchTarget,
    SavedWindowPlacement,
)


@dataclass(frozen=True, slots=True)
class GroupWindowLaunchResult:
    success: bool
    group_name: str
    total_count: int = 0
    launched_count: int = 0
    restored_count: int = 0
    failure_code: str | None = None
    action: str = "launch"

    @property
    def player_message(self) -> str:
        if self.success:
            if self.action == "record":
                return (
                    f"已記錄：{self.group_name} 共 {self.total_count} 個"
                    "視窗的位置與大小。"
                )
            if self.action == "restore":
                return (
                    f"已還原：{self.group_name} 共 {self.restored_count} 個"
                    "視窗的位置與大小。"
                )
            return (
                f"已完成：{self.group_name} 共 {self.total_count} 個視窗，"
                f"新啟動 {self.launched_count} 個，位置已還原。"
            )
        messages = {
            "group_launch_already_running": "整組啟動正在進行中。",
            "group_launch_plan_unavailable": "目前組別設定不完整，沒有啟動任何視窗。",
            "group_existing_window_unknown": "現有遊戲視窗無法唯一確認，已停止整組啟動。",
            "group_existing_window_duplicate": "偵測到重複角色視窗，已停止整組啟動。",
            "group_shortcut_open_failed": "部分角色捷徑未能啟動，請查看紀錄。",
            "group_window_wait_timeout": "部分角色啟動逾時，請查看紀錄。",
            "group_layout_unavailable": "部分角色沒有已保存的位置，請重新記錄位置。",
            "group_window_place_failed": "部分遊戲視窗未能還原位置，請查看紀錄。",
            "group_launch_cancelled": "整組啟動已停止，尚未處理的視窗保持不變。",
            "group_window_missing": "目前組別尚有角色視窗未開啟，沒有變更任何位置。",
            "group_layout_save_failed": "目前位置未能完整保存，原設定保持不變。",
        }
        return messages.get(
            self.failure_code,
            "整組啟動未完成，原有遊戲視窗保持不變。",
        )


class GroupWindowLaunchService:
    """Run a non-blocking exact group launch with legacy-compatible layout."""

    def __init__(
        self,
        launch_service: GroupLaunchService,
        window_backend: WindowBackend,
        *,
        shortcut_open_backend: ShortcutOpenBackend | None = None,
        placement_backend: WindowPlacementBackend | None = None,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        launch_timeout_seconds: float = 20.0,
        poll_seconds: float = 0.2,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        completion_dispatch: Callable[[Callable[[], None]], object] | None = None,
        record_callback: Callable[[str, str, str], object] | None = None,
        placement_update_callback: (
            Callable[
                [str, dict[Path, SavedWindowPlacement]],
                bool,
            ]
            | None
        ) = None,
    ) -> None:
        if launch_timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("launch timing values must be positive.")
        self._launch_service = launch_service
        self._window_backend = window_backend
        self._shortcut_open_backend = (
            shortcut_open_backend or WindowsShortcutOpenBackend()
        )
        self._placement_backend = (
            placement_backend or Win32WindowPlacementBackend()
        )
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        self._launch_timeout_seconds = float(launch_timeout_seconds)
        self._poll_seconds = float(poll_seconds)
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._completion_dispatch = completion_dispatch or (lambda callback: callback())
        self._record_callback = record_callback
        self._placement_update_callback = placement_update_callback
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _record(self, role_name: str, detail: str) -> None:
        if self._record_callback is None:
            return
        try:
            self._record_callback("整組啟動", role_name, detail)
        except Exception:
            pass

    def _candidate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if self._keywords
            and all(
                keyword in window.title.casefold()
                for keyword in self._keywords
            )
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

    def _safe_windows(
        self,
    ) -> tuple[dict[str, list[WindowInfo]] | None, str | None]:
        windows = self._candidate_windows()
        if any(
            normalize_launch_fingerprint(window.launch_fingerprint) is None
            for window in windows
        ):
            return None, "group_existing_window_unknown"
        by_fingerprint = self._windows_by_fingerprint(windows)
        if any(len(matches) > 1 for matches in by_fingerprint.values()):
            return None, "group_existing_window_duplicate"
        return by_fingerprint, None

    def _wait_for_window(
        self,
        target: GroupLaunchTarget,
    ) -> tuple[WindowInfo | None, str | None]:
        deadline = self._monotonic_clock() + self._launch_timeout_seconds
        while self._monotonic_clock() < deadline:
            if self._stop_event.is_set():
                return None, "group_launch_cancelled"
            by_fingerprint, failure_code = self._safe_windows()
            if failure_code is not None or by_fingerprint is None:
                return None, failure_code
            matches = by_fingerprint.get(target.fingerprint, ())
            if len(matches) == 1:
                return matches[0], None
            self._sleeper(self._poll_seconds)
        return None, "group_window_wait_timeout"

    def _place_target(
        self,
        target: GroupLaunchTarget,
        window: WindowInfo,
    ) -> str | None:
        if target.placement is None:
            self._record(target.display_name, "沒有已保存位置")
            return "group_layout_unavailable"
        if not self._placement_backend.place(
            window.handle,
            target.placement,
        ):
            self._record(target.display_name, "位置還原失敗")
            return "group_window_place_failed"
        self._record(target.display_name, "位置已還原")
        return None

    def _run(self, group_name: str) -> GroupWindowLaunchResult:
        plan = self._launch_service.plan(group_name)
        if not plan.ready:
            return GroupWindowLaunchResult(
                False,
                group_name,
                failure_code="group_launch_plan_unavailable",
            )
        by_fingerprint, failure_code = self._safe_windows()
        if failure_code is not None or by_fingerprint is None:
            return GroupWindowLaunchResult(
                False,
                plan.group_name,
                total_count=len(plan.targets),
                failure_code=failure_code,
            )

        launched_count = 0
        restored_count = 0
        first_failure: str | None = None
        for target in plan.targets:
            if self._stop_event.is_set():
                first_failure = first_failure or "group_launch_cancelled"
                break
            matches = by_fingerprint.get(target.fingerprint, ())
            window = matches[0] if len(matches) == 1 else None
            if window is None:
                if not self._shortcut_open_backend.open_shortcut(target):
                    self._record(target.display_name, "捷徑啟動失敗")
                    first_failure = (
                        first_failure or "group_shortcut_open_failed"
                    )
                    continue
                launched_count += 1
                self._record(target.display_name, "已送出捷徑啟動")
                window, wait_failure = self._wait_for_window(target)
                if wait_failure is not None or window is None:
                    self._record(target.display_name, "等待視窗失敗")
                    first_failure = first_failure or wait_failure
                    if wait_failure in {
                        "group_existing_window_unknown",
                        "group_existing_window_duplicate",
                    }:
                        break
                    continue
            place_failure = self._place_target(target, window)
            if place_failure is None:
                restored_count += 1
            else:
                first_failure = first_failure or place_failure
            if target.placement is not None and target.placement.delay_ms:
                self._sleeper(target.placement.delay_ms / 1000.0)
            by_fingerprint, refresh_failure = self._safe_windows()
            if refresh_failure is not None or by_fingerprint is None:
                first_failure = first_failure or refresh_failure
                break

        success = first_failure is None and restored_count == len(plan.targets)
        return GroupWindowLaunchResult(
            success,
            plan.group_name,
            total_count=len(plan.targets),
            launched_count=launched_count,
            restored_count=restored_count,
            failure_code=first_failure,
        )

    def _complete_existing_windows(
        self,
        group_name: str,
    ) -> tuple[
        tuple[GroupLaunchTarget, ...],
        dict[str, WindowInfo],
        str | None,
    ]:
        plan = self._launch_service.plan(group_name)
        if not plan.ready:
            return (), {}, "group_launch_plan_unavailable"
        by_fingerprint, failure_code = self._safe_windows()
        if failure_code is not None or by_fingerprint is None:
            return plan.targets, {}, failure_code
        matched: dict[str, WindowInfo] = {}
        for target in plan.targets:
            matches = by_fingerprint.get(target.fingerprint, ())
            if len(matches) != 1:
                return plan.targets, {}, "group_window_missing"
            matched[target.fingerprint] = matches[0]
        return plan.targets, matched, None

    def _run_restore(
        self,
        group_name: str,
    ) -> GroupWindowLaunchResult:
        targets, matched, failure_code = self._complete_existing_windows(
            group_name
        )
        if failure_code is not None:
            return GroupWindowLaunchResult(
                False,
                group_name,
                total_count=len(targets),
                failure_code=failure_code,
                action="restore",
            )
        restored_count = 0
        first_failure: str | None = None
        for target in targets:
            if self._stop_event.is_set():
                first_failure = "group_launch_cancelled"
                break
            place_failure = self._place_target(
                target,
                matched[target.fingerprint],
            )
            if place_failure is None:
                restored_count += 1
            else:
                first_failure = first_failure or place_failure
        return GroupWindowLaunchResult(
            first_failure is None and restored_count == len(targets),
            group_name,
            total_count=len(targets),
            restored_count=restored_count,
            failure_code=first_failure,
            action="restore",
        )

    def _run_record(
        self,
        group_name: str,
    ) -> GroupWindowLaunchResult:
        targets, matched, failure_code = self._complete_existing_windows(
            group_name
        )
        if failure_code is not None:
            return GroupWindowLaunchResult(
                False,
                group_name,
                total_count=len(targets),
                failure_code=failure_code,
                action="record",
            )
        placements: dict[Path, SavedWindowPlacement] = {}
        for target in targets:
            window = matched[target.fingerprint]
            left, top, right, bottom = window.rect
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 0:
                return GroupWindowLaunchResult(
                    False,
                    group_name,
                    total_count=len(targets),
                    failure_code="group_layout_save_failed",
                    action="record",
                )
            placements[target.shortcut_path] = SavedWindowPlacement(
                left,
                top,
                width,
                height,
                (
                    target.placement.delay_ms
                    if target.placement is not None
                    else 0
                ),
            )
        saved = (
            self._placement_update_callback(group_name, placements)
            if self._placement_update_callback is not None
            else False
        )
        if not saved:
            return GroupWindowLaunchResult(
                False,
                group_name,
                total_count=len(targets),
                failure_code="group_layout_save_failed",
                action="record",
            )
        for target in targets:
            self._record(target.display_name, "目前位置已記錄")
        return GroupWindowLaunchResult(
            True,
            group_name,
            total_count=len(targets),
            restored_count=len(targets),
            action="record",
        )

    def _start_operation(
        self,
        group_name: object,
        runner: Callable[[str], GroupWindowLaunchResult],
        on_complete: Callable[[GroupWindowLaunchResult], object] | None,
    ) -> bool:
        if not isinstance(group_name, str) or not group_name.strip():
            return False
        cleaned_group = group_name.strip()
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()

        def worker() -> None:
            try:
                result = runner(cleaned_group)
            except Exception:
                result = GroupWindowLaunchResult(
                    False,
                    cleaned_group,
                    failure_code="group_launch_failed",
                )
            finally:
                with self._lock:
                    self._running = False

            if on_complete is not None:
                self._completion_dispatch(lambda: on_complete(result))

        threading.Thread(
            target=worker,
            name="GroupWindowLaunchService",
            daemon=True,
        ).start()
        return True

    def start(
        self,
        group_name: object,
        on_complete: Callable[[GroupWindowLaunchResult], object] | None = None,
    ) -> bool:
        return self._start_operation(
            group_name,
            self._run,
            on_complete,
        )

    def start_restore(
        self,
        group_name: object,
        on_complete: Callable[[GroupWindowLaunchResult], object] | None = None,
    ) -> bool:
        return self._start_operation(
            group_name,
            self._run_restore,
            on_complete,
        )

    def start_record(
        self,
        group_name: object,
        on_complete: Callable[[GroupWindowLaunchResult], object] | None = None,
    ) -> bool:
        return self._start_operation(
            group_name,
            self._run_record,
            on_complete,
        )

    def stop(self) -> None:
        self._stop_event.set()
