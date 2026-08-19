"""Legacy-compatible game clock and identity-safe timed click coordination."""

from __future__ import annotations

import re
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from services.game_operation_gate import (
    GameOperationGate,
    GameOperationLease,
)
from services.server_clock import ServerClock


DAY_MS = 86_400_000
TAIPEI_UTC_OFFSET_MS = 8 * 60 * 60 * 1_000
MIN_TIME_OFFSET_MS = -60_000
MAX_TIME_OFFSET_MS = 60_000
MIN_TIMED_CLICK_LEAD_MS = -5_000
MAX_TIMED_CLICK_LEAD_MS = 5_000
MIN_TIMED_CLICK_REPEAT_COUNT = 1
MAX_TIMED_CLICK_REPEAT_COUNT = 10
MIN_TIMED_CLICK_INTERVAL_MS = 0
MAX_TIMED_CLICK_INTERVAL_MS = 3_000
DEFAULT_TIMED_CLICK_TARGET_TIME = "08:00:00.000"
DEFAULT_TIMED_CLICK_LEAD_MS = 0
DEFAULT_TIMED_CLICK_REPEAT_COUNT = 3
DEFAULT_TIMED_CLICK_INTERVAL_MS = 0
TIMED_CLICK_POLL_MS = 5
TIMED_CLICK_TRIGGER_WINDOW_MS = 8
TIMED_CLICK_LATE_WINDOW_MS = 1_000
TIMED_CLICK_PRESS_MS = 35


@dataclass(frozen=True, slots=True)
class TimedClickTarget:
    fingerprint: str
    x_ratio: float
    y_ratio: float
    display_name: str = ""

    def __post_init__(self) -> None:
        normalized = normalize_launch_fingerprint(self.fingerprint)
        if normalized is None:
            raise ValueError("target fingerprint must be a complete SHA-256 value")
        if not 0.0 <= self.x_ratio <= 1.0:
            raise ValueError("target x ratio must be between zero and one")
        if not 0.0 <= self.y_ratio <= 1.0:
            raise ValueError("target y ratio must be between zero and one")
        object.__setattr__(self, "fingerprint", normalized)
        object.__setattr__(self, "display_name", self.display_name.strip())


@dataclass(frozen=True, slots=True)
class TimedClickPressReceipt:
    handle: int
    x_ratio: float
    y_ratio: float
    synchronized_handles: tuple[int, ...] = ()
    instance_identities: tuple[
        tuple[str, int, int, int, str, int], ...
    ] = ()

    @property
    def handles(self) -> tuple[int, ...]:
        if self.instance_identities:
            return tuple(identity[1] for identity in self.instance_identities)
        return self.synchronized_handles or (self.handle,)


class TimedClickBackend(Protocol):
    def capture_target(
        self,
        allowed_fingerprints: Iterable[str],
    ) -> TimedClickTarget | None: ...

    def press(
        self,
        target: TimedClickTarget,
        allowed_fingerprints: Iterable[str],
    ) -> TimedClickPressReceipt | None: ...

    def release(self, receipt: TimedClickPressReceipt) -> bool: ...


@dataclass(frozen=True, slots=True)
class GameTimeTimedClickSnapshot:
    offset_ms: int
    auto_update: bool
    current_time_ms: int | None
    current_time_text: str
    target: TimedClickTarget | None
    enabled: bool
    target_time_ms: int | None
    lead_ms: int
    repeat_count: int
    repeat_interval_ms: int
    sent_count: int
    status: str
    source: str = "系統時間"


@dataclass(frozen=True, slots=True)
class GameTimeTimedClickResult:
    success: bool
    action: str
    message: str
    failure_code: str | None = None
    snapshot: GameTimeTimedClickSnapshot | None = None


def clamp_time_offset_ms(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(MIN_TIME_OFFSET_MS, min(MAX_TIME_OFFSET_MS, parsed))


def parse_target_time_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    compact = re.fullmatch(r"\s*(\d{3,6}|\d{8,9})\s*", value)
    if compact:
        digits = compact.group(1)
        milli = 0
        if len(digits) <= 4:
            hour = int(digits[:-2])
            minute = int(digits[-2:])
            second = 0
        elif len(digits) <= 6:
            hour = int(digits[:-4])
            minute = int(digits[-4:-2])
            second = int(digits[-2:])
        else:
            main = digits[:-3]
            milli = int(digits[-3:])
            hour = int(main[:-4])
            minute = int(main[-4:-2])
            second = int(main[-2:])
        if hour > 23 or minute > 59 or second > 59:
            return None
        return ((hour * 60 + minute) * 60 + second) * 1000 + milli

    matched = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?\s*",
        value,
    )
    if matched is None:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2))
    second = int(matched.group(3) or 0)
    milli = int((matched.group(4) or "0").ljust(3, "0")[:3])
    if hour > 23 or minute > 59 or second > 59:
        return None
    return ((hour * 60 + minute) * 60 + second) * 1000 + milli


def game_time_ms_to_text(value: int) -> str:
    normalized = int(value) % DAY_MS
    hour, remaining = divmod(normalized, 3_600_000)
    minute, remaining = divmod(remaining, 60_000)
    second, milli = divmod(remaining, 1_000)
    return f"{hour:02d}:{minute:02d}:{second:02d}.{milli:03d}"


class GameTimeTimedClickService:
    """Keep the old system-clock behavior while failing closed on window identity."""

    def __init__(
        self,
        backend: TimedClickBackend,
        *,
        schedule: Callable[[int, Callable[[], object]], object],
        cancel: Callable[[object], object],
        allowed_fingerprints_provider: Callable[[], Iterable[str]],
        result_callback: Callable[[GameTimeTimedClickResult], object] | None = None,
        wall_clock_ns: Callable[[], int] = time.time_ns,
        localtime: Callable[[float | None], time.struct_time] = time.localtime,
        operation_gate: GameOperationGate | None = None,
        server_clock: ServerClock | None = None,
    ) -> None:
        self._backend = backend
        self._schedule = schedule
        self._cancel = cancel
        self._allowed_fingerprints_provider = allowed_fingerprints_provider
        self._result_callback = result_callback
        self._wall_clock_ns = wall_clock_ns
        self._localtime = localtime
        self._operation_gate = operation_gate
        self._server_clock = server_clock
        self._offset_ms = 0
        self._auto_update = True
        self._target: TimedClickTarget | None = None
        self._enabled = False
        self._target_time_ms: int | None = None
        self._lead_ms = DEFAULT_TIMED_CLICK_LEAD_MS
        self._repeat_count = DEFAULT_TIMED_CLICK_REPEAT_COUNT
        self._repeat_interval_ms = DEFAULT_TIMED_CLICK_INTERVAL_MS
        self._sent_count = 0
        self._status = "定時按下：未啟用"
        self._poll_handle: object | None = None
        self._next_trigger_ms: int | None = None
        self._firing = False
        self._scheduled_handles: list[object] = []
        self._active_receipts: list[TimedClickPressReceipt] = []
        self._active_operation_leases: dict[int, GameOperationLease] = {}
        self._work_queue: queue.Queue[tuple[object, ...]] = queue.Queue()
        self._work_lock = threading.RLock()
        self._work_generation = 0
        self._work_worker: threading.Thread | None = None
        self._work_inflight = 0
        self._work_pending = 0
        self._result_queue: queue.Queue[Callable[[], object]] = queue.Queue()
        self._result_pump_handle: object | None = None
        self._pending_release_ids: set[int] = set()

    def configure_game_time(
        self,
        *,
        offset_ms: object,
        auto_update: object,
    ) -> GameTimeTimedClickSnapshot:
        self._offset_ms = (
            0 if self._server_clock is not None else clamp_time_offset_ms(offset_ms)
        )
        self._auto_update = bool(auto_update)
        return self.snapshot()

    def _absolute_time_ms(self) -> int | None:
        if self._server_clock is not None:
            server_now_ms = self._server_clock.now_ms()
            return (
                None
                if server_now_ms is None
                else server_now_ms + TAIPEI_UTC_OFFSET_MS
            )
        now_ns = int(self._wall_clock_ns())
        now_seconds = now_ns // 1_000_000_000
        local = self._localtime(float(now_seconds))
        total_ms = (
            ((local.tm_hour * 60 + local.tm_min) * 60 + local.tm_sec) * 1000
            + (now_ns // 1_000_000) % 1000
        )
        day_number = date(local.tm_year, local.tm_mon, local.tm_mday).toordinal()
        return day_number * DAY_MS + total_ms + self._offset_ms

    def current_time_ms(self) -> int | None:
        absolute = self._absolute_time_ms()
        return None if absolute is None else absolute % DAY_MS

    def snapshot(self) -> GameTimeTimedClickSnapshot:
        current = self.current_time_ms()
        return GameTimeTimedClickSnapshot(
            offset_ms=self._offset_ms,
            auto_update=self._auto_update,
            current_time_ms=current,
            current_time_text=(
                game_time_ms_to_text(current)
                if current is not None
                else "尚未校正"
            ),
            target=self._target,
            enabled=self._enabled,
            target_time_ms=self._target_time_ms,
            lead_ms=self._lead_ms,
            repeat_count=self._repeat_count,
            repeat_interval_ms=self._repeat_interval_ms,
            sent_count=self._sent_count,
            status=self._status,
            source=(
                "遊戲伺服器時間"
                if self._server_clock is not None
                else "系統時間"
            ),
        )

    def _result(
        self,
        success: bool,
        action: str,
        message: str,
        failure_code: str | None = None,
        *,
        notify: bool = True,
    ) -> GameTimeTimedClickResult:
        result = GameTimeTimedClickResult(
            success,
            action,
            message,
            failure_code,
            self.snapshot(),
        )
        if notify and self._result_callback is not None:
            self._result_callback(result)
        return result

    def _allowed_fingerprints(self) -> tuple[str, ...]:
        normalized = tuple(
            fingerprint
            for value in self._allowed_fingerprints_provider()
            if (fingerprint := normalize_launch_fingerprint(value)) is not None
        )
        if not normalized or len(normalized) != len(set(normalized)):
            return ()
        return normalized

    def capture_target(self) -> GameTimeTimedClickResult:
        allowed = self._allowed_fingerprints()
        if not allowed:
            return self._result(
                False,
                "capture",
                "目前組別無法唯一確認，按鈕位置保持不變。",
                "group_identity_unavailable",
            )
        try:
            target = self._backend.capture_target(allowed)
        except OSError:
            target = None
        if target is None:
            return self._result(
                False,
                "capture",
                "沒有抓到目前組別內的唯一遊戲視窗，請再試一次。",
                "target_capture_failed",
            )
        self.cancel(notify=False)
        self._target = target
        name = target.display_name or "遊戲視窗"
        return self._result(
            True,
            "capture",
            f"按鈕位置已設定：{name}。",
        )

    def clear_target(self, *, notify: bool = True) -> GameTimeTimedClickResult:
        self.cancel(notify=False)
        self._target = None
        return self._result(
            True,
            "clear_target",
            "按鈕位置已清除。",
            notify=notify,
        )

    @staticmethod
    def _bounded_integer(
        value: object,
        *,
        minimum: int,
        maximum: int,
    ) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if minimum <= parsed <= maximum else None

    def arm(
        self,
        target_time: object,
        *,
        lead_ms: object = DEFAULT_TIMED_CLICK_LEAD_MS,
        repeat_count: object = DEFAULT_TIMED_CLICK_REPEAT_COUNT,
        repeat_interval_ms: object = DEFAULT_TIMED_CLICK_INTERVAL_MS,
    ) -> GameTimeTimedClickResult:
        target_ms = parse_target_time_ms(target_time)
        lead = self._bounded_integer(
            lead_ms,
            minimum=MIN_TIMED_CLICK_LEAD_MS,
            maximum=MAX_TIMED_CLICK_LEAD_MS,
        )
        repeats = self._bounded_integer(
            repeat_count,
            minimum=MIN_TIMED_CLICK_REPEAT_COUNT,
            maximum=MAX_TIMED_CLICK_REPEAT_COUNT,
        )
        interval = self._bounded_integer(
            repeat_interval_ms,
            minimum=MIN_TIMED_CLICK_INTERVAL_MS,
            maximum=MAX_TIMED_CLICK_INTERVAL_MS,
        )
        if target_ms is None:
            return self._result(
                False,
                "arm",
                "目標時間格式錯誤；可輸入 21:37、21:37:00.120 或 2137。",
                "target_time_invalid",
            )
        if lead is None:
            return self._result(
                False,
                "arm",
                "時間校正必須介於 -5000 到 5000 毫秒。",
                "lead_ms_invalid",
            )
        if repeats is None:
            return self._result(
                False,
                "arm",
                "連點次數必須介於 1 到 10。",
                "repeat_count_invalid",
            )
        if interval is None:
            return self._result(
                False,
                "arm",
                "連點間隔必須介於 0 到 3000 毫秒。",
                "repeat_interval_invalid",
            )
        if self._target is None:
            return self._result(
                False,
                "arm",
                "請先設定要按下的按鈕位置。",
                "target_unavailable",
            )
        now_ms = self._absolute_time_ms()
        if now_ms is None:
            return self._result(
                False,
                "arm",
                "尚未取得遊戲伺服器時間，沒有啟用定時按下。",
                "server_time_uncalibrated",
            )
        allowed = self._allowed_fingerprints()
        if self._target.fingerprint not in allowed:
            return self._result(
                False,
                "arm",
                "按鈕位置不屬於目前組別，沒有啟用定時按下。",
                "target_not_in_current_group",
            )
        self.cancel(notify=False)
        self._target_time_ms = target_ms
        self._lead_ms = lead
        self._repeat_count = repeats
        self._repeat_interval_ms = interval
        self._sent_count = 0
        trigger_time_ms = target_ms - lead
        day_start_ms = (now_ms // DAY_MS) * DAY_MS
        next_trigger_ms = day_start_ms + trigger_time_ms
        if next_trigger_ms < now_ms - TIMED_CLICK_TRIGGER_WINDOW_MS:
            next_trigger_ms += DAY_MS
        self._next_trigger_ms = next_trigger_ms
        self._firing = False
        self._enabled = True
        self._auto_update = True
        self._status = "每日定時按下已啟用。"
        result = self._result(True, "arm", self._status)
        self._schedule_poll()
        return result

    def _ensure_work_worker(self) -> bool:
        with self._work_lock:
            worker = self._work_worker
            if worker is not None and worker.is_alive():
                return True
            worker = threading.Thread(
                target=self._work_worker_loop,
                name="FLASH-TimedClickIO",
                daemon=True,
            )
            self._work_worker = worker
            worker.start()
            return True

    def _ensure_result_pump(self) -> None:
        if self._result_pump_handle is not None:
            return
        try:
            self._result_pump_handle = self._schedule(
                0,
                self._drain_worker_results,
            )
        except Exception:
            self._result_pump_handle = None

    def _drain_worker_results(self) -> None:
        self._result_pump_handle = None
        for _ in range(64):
            try:
                callback = self._result_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            finally:
                with self._work_lock:
                    self._work_pending = max(0, self._work_pending - 1)
        with self._work_lock:
            worker_active = self._work_inflight > 0
            work_pending = self._work_pending
        if worker_active or work_pending or not self._result_queue.empty():
            try:
                self._result_pump_handle = self._schedule(
                    5,
                    self._drain_worker_results,
                )
            except Exception:
                self._result_pump_handle = None

    def _work_worker_loop(self) -> None:
        while True:
            item = self._work_queue.get()
            with self._work_lock:
                self._work_inflight += 1
                current_generation = self._work_generation
            try:
                kind, generation, payload, lease = item
                if kind == "press":
                    if generation != current_generation:
                        self._result_queue.put(
                            lambda generation=generation, payload=payload, lease=lease: self._complete_press(
                                generation,
                                payload,
                                None,
                                lease,
                            )
                        )
                        continue
                    try:
                        receipt = self._backend.press(
                            payload,
                            self._allowed_fingerprints(),
                        )
                    except Exception:
                        receipt = None
                    self._result_queue.put(
                        lambda generation=generation, payload=payload, receipt=receipt, lease=lease: self._complete_press(
                            generation,
                            payload,
                            receipt,
                            lease,
                        )
                    )
                elif kind == "release":
                    try:
                        released = bool(self._backend.release(payload))
                    except Exception:
                        released = False
                    self._result_queue.put(
                        lambda generation=generation, payload=payload, released=released, lease=lease: self._complete_release(
                            generation,
                            payload,
                            released,
                            lease,
                        )
                    )
            finally:
                with self._work_lock:
                    self._work_inflight -= 1
                self._work_queue.task_done()

    def _queue_press(
        self,
        generation: int,
        target: TimedClickTarget,
        lease: GameOperationLease | None,
    ) -> bool:
        if not self._ensure_work_worker():
            if lease is not None:
                lease.release()
            return False
        with self._work_lock:
            self._work_pending += 1
            self._work_queue.put(("press", generation, target, lease))
        self._ensure_result_pump()
        return True

    def _queue_release(
        self,
        generation: int,
        receipt: TimedClickPressReceipt,
        lease: GameOperationLease | None = None,
    ) -> bool:
        receipt_id = id(receipt)
        if receipt_id in self._pending_release_ids:
            return True
        self._pending_release_ids.add(receipt_id)
        if not self._ensure_work_worker():
            self._pending_release_ids.discard(receipt_id)
            if lease is not None:
                lease.release()
            return False
        with self._work_lock:
            self._work_pending += 1
            self._work_queue.put(("release", generation, receipt, lease))
        self._ensure_result_pump()
        return True

    def _complete_press(
        self,
        generation: int,
        target: TimedClickTarget,
        receipt: TimedClickPressReceipt | None,
        lease: GameOperationLease | None,
    ) -> None:
        with self._work_lock:
            current_generation = self._work_generation
        if (
            receipt is not None
            and generation == current_generation
            and self._enabled
        ):
            self._active_receipts.append(receipt)
            if lease is not None:
                self._active_operation_leases[id(receipt)] = lease
            self._sent_count += 1
            self._schedule_tracked(
                TIMED_CLICK_PRESS_MS,
                lambda receipt=receipt: self._release_once(receipt),
            )
            synchronized_count = len(receipt.handles)
            synchronized_detail = (
                f"（同步 {synchronized_count} 個視窗）"
                if synchronized_count > 1
                else ""
            )
            self._status = (
                f"定時按下：已連點 {self._sent_count} 次{synchronized_detail}"
            )
            self._result(True, "press", self._status)
            return
        if receipt is not None:
            self._queue_release(generation, receipt, lease)
            return
        if lease is not None:
            lease.release()
        if generation != current_generation or not self._enabled:
            return
        self._enabled = False
        self._firing = False
        self._next_trigger_ms = None
        for handle in tuple(self._scheduled_handles):
            self._cancel_handle(handle)
        self._scheduled_handles.clear()
        self._status = "定時按下失敗：目標視窗無法唯一確認。"
        self._result(
            False,
            "press",
            self._status,
            "target_delivery_failed",
        )

    def _complete_release(
        self,
        generation: int,
        receipt: TimedClickPressReceipt,
        released: bool,
        lease: GameOperationLease | None,
    ) -> None:
        self._pending_release_ids.discard(id(receipt))
        if receipt in self._active_receipts:
            self._active_receipts.remove(receipt)
        tracked_lease = self._active_operation_leases.pop(id(receipt), None)
        if tracked_lease is not None:
            tracked_lease.release()
        if lease is not None:
            lease.release()
        with self._work_lock:
            current_generation = self._work_generation
        if generation != current_generation or not self._enabled:
            return
        if not released:
            self._enabled = False
            self._firing = False
            self._next_trigger_ms = None
            self._status = "定時按下失敗：滑鼠放開訊息未送達。"
            self._result(
                False,
                "release",
                self._status,
                "target_release_failed",
            )
            return
        if self._sent_count < self._repeat_count:
            target = self._target
            if target is None:
                self._enabled = False
                self._firing = False
                self._next_trigger_ms = None
                self._status = "定時按下失敗：按鈕位置不存在。"
                self._result(
                    False,
                    "press",
                    self._status,
                    "target_unavailable",
                )
                return
            self._schedule_tracked(
                self._repeat_interval_ms,
                lambda target=target: self._press_once(target),
            )
        elif not self._active_receipts and not self._scheduled_handles:
            self._firing = False
            self._status = (
                f"定時按下：今日已完成 {self._sent_count} 次，等待明日。"
            )
            self._result(True, "complete", self._status)
            self._schedule_poll()

    def _cancel_handle(self, handle: object | None) -> None:
        if handle is None:
            return
        try:
            self._cancel(handle)
        except Exception:
            pass

    def _schedule_tracked(
        self,
        delay_ms: int,
        callback: Callable[[], object],
    ) -> object:
        state: dict[str, object] = {}

        def run() -> object:
            handle = state.get("handle")
            if handle in self._scheduled_handles:
                self._scheduled_handles.remove(handle)
            return callback()

        handle = self._schedule(delay_ms, run)
        state["handle"] = handle
        self._scheduled_handles.append(handle)
        return handle

    def cancel(
        self,
        *,
        notify: bool = True,
        message: str = "定時按下：未啟用",
    ) -> GameTimeTimedClickResult:
        with self._work_lock:
            self._work_generation += 1
            generation = self._work_generation
        self._enabled = False
        self._firing = False
        self._next_trigger_ms = None
        self._cancel_handle(self._poll_handle)
        self._poll_handle = None
        for handle in tuple(self._scheduled_handles):
            self._cancel_handle(handle)
        self._scheduled_handles.clear()
        for receipt in tuple(self._active_receipts):
            if not self._queue_release(generation, receipt):
                lease = self._active_operation_leases.pop(id(receipt), None)
                if lease is not None:
                    lease.release()
        self._active_receipts.clear()
        self._cancel_handle(self._result_pump_handle)
        self._result_pump_handle = None
        with self._work_lock:
            needs_pump = bool(
                self._work_pending
                or self._work_inflight
                or not self._result_queue.empty()
            )
        if needs_pump:
            self._ensure_result_pump()
        self._status = message
        return self._result(
            True,
            "cancel",
            message,
            notify=notify,
        )

    def stop(self) -> None:
        self.cancel(notify=False)
        self._target = None

    @staticmethod
    def _poll_delay_ms(remaining_ms: int) -> int:
        if remaining_ms > 60_000:
            return min(1_000, remaining_ms - 60_000)
        if remaining_ms > 1_000:
            return min(100, remaining_ms - 1_000)
        if remaining_ms > 50:
            return max(1, remaining_ms - TIMED_CLICK_TRIGGER_WINDOW_MS)
        return 1

    def _schedule_poll(self) -> None:
        if not self._enabled or self._poll_handle is not None:
            return
        now_ms = self._absolute_time_ms()
        remaining_ms = (
            TIMED_CLICK_POLL_MS
            if now_ms is None or self._next_trigger_ms is None
            else max(1, self._next_trigger_ms - now_ms)
        )
        self._poll_handle = self._schedule(
            self._poll_delay_ms(remaining_ms),
            self.poll,
        )

    def poll(self) -> GameTimeTimedClickResult:
        self._poll_handle = None
        if (
            not self._enabled
            or self._target_time_ms is None
            or self._next_trigger_ms is None
        ):
            return self._result(
                False,
                "poll",
                self._status,
                "timed_click_disabled",
                notify=False,
            )
        now_ms = self._absolute_time_ms()
        if now_ms is None:
            self._enabled = False
            self._status = "定時按下：遊戲伺服器時間尚未校正。"
            return self._result(
                False,
                "poll",
                self._status,
                "server_time_uncalibrated",
            )
        remaining = self._next_trigger_ms - now_ms
        if remaining < -TIMED_CLICK_LATE_WINDOW_MS:
            missed_days = ((-remaining) // DAY_MS) + 1
            self._next_trigger_ms += missed_days * DAY_MS
            remaining = self._next_trigger_ms - now_ms
            self._status = "定時按下：今日時點已錯過，等待下一次。"
            result = self._result(True, "poll", self._status)
            self._schedule_poll()
            return result
        if remaining <= TIMED_CLICK_TRIGGER_WINDOW_MS:
            return self._fire(now_ms)
        self._status = f"定時按下：剩 {remaining} ms"
        result = self._result(
            True,
            "poll",
            self._status,
            notify=remaining <= 1_000,
        )
        self._schedule_poll()
        return result

    def _fire(self, now_ms: int) -> GameTimeTimedClickResult:
        target = self._target
        target_ms = self._target_time_ms
        scheduled_trigger_ms = self._next_trigger_ms
        if target is None or target_ms is None or scheduled_trigger_ms is None:
            self._enabled = False
            self._status = "定時按下失敗：按鈕位置不存在。"
            return self._result(
                False,
                "fire",
                self._status,
                "target_unavailable",
            )
        allowed = self._allowed_fingerprints()
        if target.fingerprint not in allowed:
            self._enabled = False
            self._status = "定時按下失敗：目標不屬於目前組別。"
            return self._result(
                False,
                "fire",
                self._status,
                "target_not_in_current_group",
            )
        self._poll_handle = None
        self._firing = True
        self._sent_count = 0
        self._next_trigger_ms = scheduled_trigger_ms + DAY_MS
        self._schedule_tracked(0, lambda target=target: self._press_once(target))
        delta = now_ms - scheduled_trigger_ms
        self._status = (
            f"定時按下：準備連點 {self._repeat_count} 次；"
            f"目前差值 {delta} ms。"
        )
        return self._result(True, "fire", self._status)

    def _press_once(self, target: TimedClickTarget) -> None:
        lease = (
            self._operation_gate.acquire(
                "timed-click",
                timeout_seconds=0,
            )
            if self._operation_gate is not None
            else None
        )
        if self._operation_gate is not None and lease is None:
            self._enabled = False
            self._firing = False
            self._next_trigger_ms = None
            self._status = "定時按下失敗：目前有其他遊戲操作。"
            self._result(
                False,
                "press",
                self._status,
                "operation_gate_closed",
            )
            return
        with self._work_lock:
            generation = self._work_generation
        if self._queue_press(generation, target, lease):
            return
        if lease is not None:
            lease.release()
        if not self._enabled:
            return
        self._enabled = False
        self._firing = False
        self._next_trigger_ms = None
        for handle in tuple(self._scheduled_handles):
            self._cancel_handle(handle)
        self._scheduled_handles.clear()
        self._status = "定時按下失敗：目標視窗無法唯一確認。"
        self._result(
            False,
            "press",
            self._status,
            "target_delivery_failed",
        )

    def _release_once(self, receipt: TimedClickPressReceipt) -> None:
        if (
            receipt not in self._active_receipts
            or id(receipt) in self._pending_release_ids
        ):
            return
        with self._work_lock:
            generation = self._work_generation
        if self._queue_release(generation, receipt):
            return
        self._complete_release(generation, receipt, False, None)
