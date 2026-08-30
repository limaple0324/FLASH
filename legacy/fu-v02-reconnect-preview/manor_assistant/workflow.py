from __future__ import annotations

from datetime import datetime, timedelta
import threading
import time
from typing import Callable

import numpy as np

from .models import Profile, RunResult
from .vision import Match, USER_PLOT_ORDER, VisionEngine
from .win32_api import (
    BackgroundWindowSession,
    WindowInfo,
    enumerate_windows,
    get_window_pid,
    get_window_title,
    is_game_window,
    is_window,
    launch_shortcut,
)


LogCallback = Callable[[str, str], None]
BindCallback = Callable[[Profile], None]
MANOR_INDIVIDUAL_HARVEST = True


class WindowResolver:
    def __init__(self, bind_callback: BindCallback, log: LogCallback) -> None:
        self.bind_callback = bind_callback
        self.log = log
        self._last_launch: dict[str, datetime] = {}

    @staticmethod
    def _game_candidates(previous_title: str, assigned: set[int]) -> list[WindowInfo]:
        return [
            info
            for info in enumerate_windows()
            if info.hwnd not in assigned and is_game_window(info, previous_title)
        ]

    def _bind(self, profile: Profile, info: WindowInfo) -> int:
        profile.window_hwnd = info.hwnd
        profile.window_pid = info.pid
        profile.window_title = info.title
        self.bind_callback(profile)
        self.log(profile.id, f"已綁定遊戲視窗：{info.title} (PID {info.pid})")
        return info.hwnd

    def ensure_window(
        self,
        profile: Profile,
        assigned: set[int],
        cancelled: threading.Event,
    ) -> tuple[int | None, RunResult | None]:
        if is_window(profile.window_hwnd):
            return profile.window_hwnd, None

        candidates = self._game_candidates(profile.window_title, assigned)
        if len(candidates) == 1:
            return self._bind(profile, candidates[0]), None
        if len(candidates) > 1:
            return None, RunResult.pause("發現多個未綁定的 Flash 視窗，請手動綁定此角色")

        if not profile.shortcut_path:
            return None, RunResult.pause("尚未設定遊戲捷徑或 EXE")

        last_launch = self._last_launch.get(profile.id)
        if last_launch and datetime.now() - last_launch < timedelta(minutes=2):
            return None, RunResult.retry("已啟動捷徑，仍在等待遊戲視窗")

        before = {info.hwnd for info in enumerate_windows()}
        try:
            launch_shortcut(profile.shortcut_path)
        except (OSError, FileNotFoundError) as exc:
            return None, RunResult.pause(f"無法啟動捷徑：{exc}")
        self._last_launch[profile.id] = datetime.now()
        self.log(profile.id, f"已啟動捷徑：{profile.shortcut_name}")

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if cancelled.wait(0.5):
                return None, RunResult.stopped()
            new_candidates = [
                info
                for info in self._game_candidates(profile.window_title, assigned)
                if info.hwnd not in before
            ]
            if new_candidates:
                # Launches are serialized; the newest handle is the least
                # ambiguous association when a launcher creates helper windows.
                chosen = sorted(new_candidates, key=lambda item: item.hwnd)[-1]
                return self._bind(profile, chosen), None
        return None, RunResult.retry("捷徑已啟動，但 45 秒內找不到新的遊戲視窗")


class ManorWorkflow:
    def __init__(self, vision: VisionEngine, log: LogCallback) -> None:
        self.vision = vision
        self.log = log

    @staticmethod
    def _wait(cancelled: threading.Event, seconds: float) -> bool:
        return not cancelled.wait(seconds)

    def _capture(self, session: BackgroundWindowSession) -> np.ndarray | None:
        # The integrated assistant must never change a user's visible/minimized
        # window state merely to obtain a different capture path.
        return session.capture()

    def _wait_for(
        self,
        session: BackgroundWindowSession,
        finder: Callable[[np.ndarray], Match | None],
        cancelled: threading.Event,
        timeout: float = 6.0,
    ) -> tuple[np.ndarray | None, Match | None]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled.is_set():
                return None, None
            frame = session.capture()
            if frame is not None:
                match = finder(frame)
                if match:
                    return frame, match
            if not self._wait(cancelled, 0.35):
                return None, None
        return None, None

    def _click_and_wait(
        self,
        session: BackgroundWindowSession,
        point: tuple[int, int],
        finder: Callable[[np.ndarray], Match | None],
        cancelled: threading.Event,
        timeout: float = 6.0,
    ) -> tuple[np.ndarray | None, Match | None]:
        session.click(*point)
        frame, match = self._wait_for(session, finder, cancelled, timeout)
        return frame, match

    def _close_shop_if_open(
        self,
        session: BackgroundWindowSession,
        frame: np.ndarray,
        cancelled: threading.Event,
    ) -> tuple[np.ndarray | None, Match | None]:
        shop = self.vision.locate_shop(frame)
        if not shop:
            return frame, self.vision.locate_action_bar(frame)
        close_point = self.vision.action_point(shop, 397, 16)
        self.log("system", "偵測到莊園商店已開啟，先關閉商店以檢查格子")
        return self._click_and_wait(
            session,
            close_point,
            self.vision.locate_action_bar,
            cancelled,
        )

    def _enter_manor(
        self,
        profile: Profile,
        session: BackgroundWindowSession,
        cancelled: threading.Event,
    ) -> tuple[np.ndarray | None, Match | None, RunResult | None]:
        frame = self._capture(session)
        if frame is None:
            return None, None, RunResult.retry("無法取得遊戲背景畫面，3 分鐘後重試")

        action = self.vision.locate_action_bar(frame)
        if action:
            return frame, action, None

        frame, action = self._close_shop_if_open(session, frame, cancelled)
        if action:
            return frame, action, None
        if frame is None:
            return None, None, RunResult.retry("無法確認目前遊戲介面，3 分鐘後重試")

        nonbattle, evidence = self.vision.nonbattle_evidence(frame)
        manor_button = self.vision.locate_manor_button(frame)
        if not nonbattle or not manor_button:
            self.log(
                profile.id,
                f"非戰鬥證據不足（莊園 {evidence['manor']:.2f}／線路 {evidence['line']:.2f}）",
            )
            return None, None, RunResult.retry(
                "目前不是可操作畫面，可能正在戰鬥；3 分鐘後重試"
            )

        self.log(profile.id, "非戰鬥畫面確認，正在開啟魔力莊園")
        frame, action = self._click_and_wait(
            session,
            manor_button.center,
            self.vision.locate_action_bar,
            cancelled,
            timeout=8,
        )
        if cancelled.is_set():
            return None, None, RunResult.stopped()
        if not action:
            return None, None, RunResult.retry("點擊莊園後沒有出現魔力莊園，3 分鐘後重試")
        return frame, action, None

    def _select_crop(
        self,
        profile: Profile,
        session: BackgroundWindowSession,
        action: Match,
        cancelled: threading.Event,
    ) -> tuple[Match | None, RunResult | None]:
        breed_point = self.vision.action_point(action, 30, 33)
        self.log(profile.id, f"開啟養殖商店，選擇 {profile.crop.label}")
        _, shop = self._click_and_wait(
            session,
            breed_point,
            self.vision.locate_shop,
            cancelled,
            timeout=7,
        )
        if cancelled.is_set():
            return None, RunResult.stopped()
        if not shop:
            return None, RunResult.retry("養殖商店沒有出現，3 分鐘後重試")

        crop_point = self.vision.action_point(shop, profile.crop.shop_x, profile.crop.shop_y)
        _, action_after = self._click_and_wait(
            session,
            crop_point,
            self.vision.locate_action_bar,
            cancelled,
            timeout=7,
        )
        if cancelled.is_set():
            return None, RunResult.stopped()
        if not action_after:
            return None, RunResult.pause("選擇作物後商店沒有關閉，請檢查遊戲畫面")
        return action_after, None

    def _wait_plot_state(
        self,
        session: BackgroundWindowSession,
        index: int,
        expected_empty: bool,
        cancelled: threading.Event,
        timeout: float = 2.5,
    ) -> tuple[bool, Match | None]:
        deadline = time.monotonic() + timeout
        last_action: Match | None = None
        while time.monotonic() < deadline:
            if cancelled.is_set():
                return False, last_action
            frame = session.capture()
            if frame is not None:
                last_action = self.vision.locate_action_bar(frame)
                if last_action:
                    states = self.vision.classify_plots(frame, last_action)
                    if states[index].empty == expected_empty:
                        return True, last_action
            if not self._wait(cancelled, 0.3):
                break
        return False, last_action

    def _harvest_pass(
        self,
        session: BackgroundWindowSession,
        action: Match,
        cancelled: threading.Event,
        profile_id: str = "收菜",
    ) -> bool:
        # The original action bar is ordered 養殖、收穫、全部、孵器、事件.
        # Harvest must use 收穫 (x=90), never 全部 (x=150), then visit the
        # user's numbered plots in the fixed 1 -> 16 order.
        harvest_point = self.vision.action_point(action, 90, 33)
        if not session.click(*harvest_point):
            return False
        if not self._wait(cancelled, 0.45):
            return False
        # Freeze one anchor and one target list for the entire pass. Re-detecting
        # and swapping target arrays between plots made the visible path jump.
        points = self.vision.harvest_points(action)
        for user_number, point in enumerate(points, start=1):
            if cancelled.is_set():
                return False
            if not session.click(*point):
                self.log(profile_id, f"第 {user_number} 格背景點擊未送達；停止本輪，禁止跳到下一格")
                return False
            self.log(profile_id, f"已依序點擊第 {user_number} 格（{user_number}/16）")
            if not self._wait(cancelled, 0.28):
                return False
        return self._wait(cancelled, 0.35)

    def _wait_harvest_result(
        self,
        session: BackgroundWindowSession,
        cancelled: threading.Event,
        timeout: float = 3.0,
    ) -> tuple[Match | None, list[int]]:
        deadline = time.monotonic() + timeout
        last_action: Match | None = None
        remaining = list(USER_PLOT_ORDER)
        while time.monotonic() < deadline:
            if cancelled.is_set():
                break
            frame = session.capture()
            action = self.vision.locate_action_bar(frame) if frame is not None else None
            if action:
                last_action = action
                remaining = [
                    index
                    for index in USER_PLOT_ORDER
                    if self.vision.harvest_plot_evidence(frame, action, index)
                    not in ("empty", "cooldown")
                ]
                if not remaining:
                    return last_action, remaining
            if not self._wait(cancelled, 0.3):
                break
        return last_action, remaining

    def _close_manor(
        self,
        session: BackgroundWindowSession,
        action: Match | None,
        cancelled: threading.Event,
    ) -> None:
        if cancelled.is_set():
            return
        frame = session.capture()
        close = self.vision.locate_close(frame) if frame is not None else None
        if close:
            session.click(*close.center)
        elif action:
            # Close center relative to the supplied action bar reference.
            session.click(*self.vision.action_point(action, 429, -411))
        self._wait(cancelled, 0.35)

    def run(
        self,
        profile: Profile,
        hwnd: int,
        cancelled: threading.Event,
    ) -> RunResult:
        try:
            with BackgroundWindowSession(hwnd, cancelled) as session:
                frame, action, failure = self._enter_manor(profile, session, cancelled)
                if failure:
                    return failure
                assert frame is not None and action is not None

                states = self.vision.classify_plots(frame, action)
                # classify_plots is defined by the user's fixed 1..16 map.  Keep
                # that numeric order explicitly; do not reorder by screen x/y.
                empty_set = {state.index - 1 for state in states if state.empty}
                empty_indices = [index for index in USER_PLOT_ORDER if index in empty_set]
                occupied_count = 16 - len(empty_indices)
                targets = empty_indices[: profile.quantity]
                self.log(
                    profile.id,
                    f"格子狀態：已有作物 {occupied_count} 格、空格 {len(empty_indices)} 格；本輪準備新種 {len(targets)} 格",
                )

                planting_failure = ""
                if targets:
                    action, failure = self._select_crop(profile, session, action, cancelled)
                    if failure:
                        self._close_manor(session, action, cancelled)
                        return failure
                    assert action is not None
                    points = self.vision.plot_points(action)
                    planted = 0
                    user_number_by_index = {index: number for number, index in enumerate(USER_PLOT_ORDER, start=1)}
                    for index in targets:
                        if cancelled.is_set():
                            self._close_manor(session, action, cancelled)
                            return RunResult.stopped()
                        session.click(*points[index])
                        # The selected creature follows Flash's internal mouse
                        # cursor. Move that preview away before checking whether
                        # the target plot actually changed.
                        session.move(*self.vision.action_point(action, -250, -380))
                        if not self._wait(cancelled, 0.15):
                            return RunResult.stopped()
                        changed, current_action = self._wait_plot_state(
                            session, index, False, cancelled
                        )
                        if current_action:
                            action = current_action
                            points = self.vision.plot_points(action)
                        if not changed:
                            planting_failure = (
                                f"第 {user_number_by_index[index]} 格種植未成功，可能金錢或活力不足"
                            )
                            self.log(profile.id, planting_failure)
                            break
                        planted += 1
                        self.log(profile.id, f"已種植第 {user_number_by_index[index]} 格（{planted}/{len(targets)}）")
                else:
                    self.log(profile.id, "沒有可種植的空格，直接進入逐格收菜")

                if cancelled.is_set():
                    self._close_manor(session, action, cancelled)
                    return RunResult.stopped()

                self.log(profile.id, "點擊「收穫」並依第 1→16 格逐格收取")
                if not self._harvest_pass(session, action, cancelled, profile_id=profile.id):
                    self._close_manor(session, action, cancelled)
                    return RunResult.stopped()

                verify_action, remaining = self._wait_harvest_result(
                    session, cancelled, timeout=3.0
                )
                if verify_action:
                    action = verify_action

                if remaining:
                    self.log(profile.id, "仍有格子缺少收成證據；保持原視窗狀態重試")
                    refreshed = session.capture()
                    refreshed_action = (
                        self.vision.locate_action_bar(refreshed) if refreshed is not None else None
                    )
                    if refreshed_action:
                        action = refreshed_action
                    self.log(profile.id, "仍有格子缺少收成證據；再次完整依第 1→16 格重收，不跳號")
                    self._harvest_pass(session, action, cancelled, profile.id)
                    verify_action, remaining = self._wait_harvest_result(
                        session, cancelled, timeout=3.0
                    )
                    if verify_action:
                        action = verify_action

                self._close_manor(session, action, cancelled)

                if planting_failure:
                    return RunResult.pause(
                        planting_failure + "；已收取現有及本輪成功種下的作物，角色已暫停"
                    )
                if remaining:
                    user_number_by_index = {index: number for number, index in enumerate(USER_PLOT_ORDER, start=1)}
                    numbers = "、".join(str(user_number_by_index[index]) for index in USER_PLOT_ORDER if index in set(remaining))
                    return RunResult.pause(f"第 {numbers} 格收菜後仍有作物，角色已暫停")
                method = session.capture_method or "背景訊息"
                return RunResult.success(f"種植與收菜完成（{method}）")
        except OSError as exc:
            return RunResult.retry(f"背景視窗操作失敗：{exc}；3 分鐘後重試")
        except Exception as exc:  # keep the scheduler alive and expose the exact error in logs
            self.log(profile.id, f"未預期錯誤：{type(exc).__name__}: {exc}")
            return RunResult.retry("自動化發生未預期錯誤，3 分鐘後重試")
