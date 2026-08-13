"""Safely restore the legacy Flash client-size controls."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from adapters.windows_client_size import WindowClientSizeBackend
from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_window import WindowBackend, WindowInfo
from services.group_launch_service import (
    GroupLaunchPlan,
    GroupLaunchService,
    GroupLaunchTarget,
)


DEFAULT_FLASH_CLIENT_WIDTH = 1000
DEFAULT_FLASH_CLIENT_HEIGHT = 700
MIN_FLASH_CLIENT_SIZE = 200
MAX_FLASH_CLIENT_SIZE = 5000


@dataclass(frozen=True, slots=True)
class WindowSizeAdjustmentResult:
    success: bool
    action: str
    width: int = DEFAULT_FLASH_CLIENT_WIDTH
    height: int = DEFAULT_FLASH_CLIENT_HEIGHT
    changed_count: int = 0
    failure_code: str | None = None

    @property
    def player_message(self) -> str:
        if self.success and self.action == "read_main":
            return f"已讀取主窗口尺寸：{self.width}×{self.height}。"
        if self.success:
            label = (
                "目前組別"
                if self.action == "current_group"
                else "全部遊戲視窗"
            )
            if self.changed_count:
                return (
                    f"{label}：已調整 {self.changed_count} 個視窗為 "
                    f"{self.width}×{self.height}。"
                )
            return f"{label}：所有已開啟視窗均已是指定尺寸。"
        messages = {
            "window_size_invalid": "尺寸必須是 200～5000 的整數。",
            "group_plan_unavailable": "目前組別設定不完整，沒有調整任何視窗。",
            "main_window_unavailable": "目前組別的主窗口尚未開啟。",
            "window_identity_unknown": "遊戲視窗身分無法唯一確認，沒有調整任何視窗。",
            "window_identity_duplicate": "偵測到重複角色視窗，沒有調整任何視窗。",
            "window_size_read_failed": "無法讀取遊戲視窗尺寸。",
            "group_window_unavailable": "目前組別沒有已開啟且可唯一確認的視窗。",
            "flash_window_unavailable": "目前找不到可唯一確認的遊戲視窗。",
            "window_resize_failed": "部分遊戲視窗未能調整，請查看紀錄。",
        }
        return messages.get(
            self.failure_code,
            "視窗尺寸操作未完成，原有視窗保持不變。",
        )


class WindowSizeAdjustmentService:
    """Read or resize exact game windows without activating them."""

    def __init__(
        self,
        launch_service: GroupLaunchService,
        window_backend: WindowBackend,
        size_backend: WindowClientSizeBackend,
        *,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
    ) -> None:
        self._launch_service = launch_service
        self._window_backend = window_backend
        self._size_backend = size_backend
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )

    @staticmethod
    def valid_size(width: object, height: object) -> bool:
        return (
            isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
            and MIN_FLASH_CLIENT_SIZE <= width <= MAX_FLASH_CLIENT_SIZE
            and MIN_FLASH_CLIENT_SIZE <= height <= MAX_FLASH_CLIENT_SIZE
        )

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

    def _safe_windows(
        self,
    ) -> tuple[dict[str, WindowInfo] | None, str | None]:
        windows = self._candidate_windows()
        if any(
            normalize_launch_fingerprint(window.launch_fingerprint) is None
            for window in windows
        ):
            return None, "window_identity_unknown"
        grouped: dict[str, list[WindowInfo]] = {}
        for window in windows:
            fingerprint = normalize_launch_fingerprint(
                window.launch_fingerprint
            )
            if fingerprint is not None:
                grouped.setdefault(fingerprint, []).append(window)
        if any(len(matches) > 1 for matches in grouped.values()):
            return None, "window_identity_duplicate"
        return {
            fingerprint: matches[0]
            for fingerprint, matches in grouped.items()
        }, None

    @staticmethod
    def _target_for_shortcut(
        plan: GroupLaunchPlan,
        shortcut_path: Path,
    ) -> GroupLaunchTarget | None:
        expected = str(
            Path(shortcut_path).resolve(strict=False)
        ).casefold()
        matches = tuple(
            target
            for target in plan.targets
            if str(
                target.shortcut_path.resolve(strict=False)
            ).casefold()
            == expected
        )
        return matches[0] if len(matches) == 1 else None

    def read_main(
        self,
        group_name: str,
        main_shortcut_path: Path | None,
    ) -> WindowSizeAdjustmentResult:
        plan = self._launch_service.plan(group_name)
        if not plan.ready or main_shortcut_path is None:
            return WindowSizeAdjustmentResult(
                False,
                "read_main",
                failure_code="group_plan_unavailable",
            )
        target = self._target_for_shortcut(plan, main_shortcut_path)
        if target is None:
            return WindowSizeAdjustmentResult(
                False,
                "read_main",
                failure_code="group_plan_unavailable",
            )
        windows, failure_code = self._safe_windows()
        if windows is None:
            return WindowSizeAdjustmentResult(
                False,
                "read_main",
                failure_code=failure_code,
            )
        window = windows.get(target.fingerprint)
        if window is None:
            return WindowSizeAdjustmentResult(
                False,
                "read_main",
                failure_code="main_window_unavailable",
            )
        size = self._size_backend.read(window.handle)
        if size is None or not self.valid_size(*size):
            return WindowSizeAdjustmentResult(
                False,
                "read_main",
                failure_code="window_size_read_failed",
            )
        return WindowSizeAdjustmentResult(
            True,
            "read_main",
            width=size[0],
            height=size[1],
        )

    def _apply(
        self,
        action: str,
        windows: tuple[WindowInfo, ...],
        width: int,
        height: int,
        *,
        empty_failure: str,
    ) -> WindowSizeAdjustmentResult:
        if not self.valid_size(width, height):
            return WindowSizeAdjustmentResult(
                False,
                action,
                failure_code="window_size_invalid",
            )
        if not windows:
            return WindowSizeAdjustmentResult(
                False,
                action,
                width=width,
                height=height,
                failure_code=empty_failure,
            )
        sizes: dict[int, tuple[int, int]] = {}
        for window in windows:
            size = self._size_backend.read(window.handle)
            if size is None:
                return WindowSizeAdjustmentResult(
                    False,
                    action,
                    width=width,
                    height=height,
                    failure_code="window_size_read_failed",
                )
            sizes[window.handle] = size
        changed = 0
        for window in windows:
            if sizes[window.handle] == (width, height):
                continue
            if not self._size_backend.resize(
                window.handle,
                width,
                height,
            ):
                return WindowSizeAdjustmentResult(
                    False,
                    action,
                    width=width,
                    height=height,
                    changed_count=changed,
                    failure_code="window_resize_failed",
                )
            changed += 1
        return WindowSizeAdjustmentResult(
            True,
            action,
            width=width,
            height=height,
            changed_count=changed,
        )

    def apply_current_group(
        self,
        group_name: str,
        width: int,
        height: int,
    ) -> WindowSizeAdjustmentResult:
        plan = self._launch_service.plan(group_name)
        if not plan.ready:
            return WindowSizeAdjustmentResult(
                False,
                "current_group",
                width=width,
                height=height,
                failure_code="group_plan_unavailable",
            )
        windows, failure_code = self._safe_windows()
        if windows is None:
            return WindowSizeAdjustmentResult(
                False,
                "current_group",
                width=width,
                height=height,
                failure_code=failure_code,
            )
        selected = tuple(
            windows[target.fingerprint]
            for target in plan.targets
            if target.fingerprint in windows
        )
        return self._apply(
            "current_group",
            selected,
            width,
            height,
            empty_failure="group_window_unavailable",
        )

    def apply_all(
        self,
        width: int,
        height: int,
    ) -> WindowSizeAdjustmentResult:
        windows, failure_code = self._safe_windows()
        if windows is None:
            return WindowSizeAdjustmentResult(
                False,
                "all",
                width=width,
                height=height,
                failure_code=failure_code,
            )
        return self._apply(
            "all",
            tuple(windows.values()),
            width,
            height,
            empty_failure="flash_window_unavailable",
        )
