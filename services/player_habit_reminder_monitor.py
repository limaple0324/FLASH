"""在主迴圈中低頻檢查玩家習慣候選提醒。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domain.progress import TAIPEI_TIMEZONE
from services.player_habit_reminder_service import (
    PlayerHabitReminderService,
)


HABIT_REMINDER_CHECK_MS = 60_000


class PlayerHabitReminderMonitor:
    def __init__(
        self,
        service: PlayerHabitReminderService,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], object],
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(service, PlayerHabitReminderService):
            raise TypeError("service must be PlayerHabitReminderService.")
        if not callable(schedule) or not callable(cancel):
            raise TypeError("schedule and cancel must be callable.")
        self._service = service
        self._schedule = schedule
        self._cancel = cancel
        self._now_provider = now_provider or (
            lambda: datetime.now(TAIPEI_TIMEZONE)
        )
        self._after_id = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        try:
            self._tick()
        except Exception:
            self._running = False
            self._after_id = None
            raise
        return True

    def stop(self) -> bool:
        self._running = False
        if self._after_id is not None:
            after_id = self._after_id
            self._after_id = None
            try:
                self._cancel(after_id)
            except Exception:
                return False
        return True

    def _tick(self) -> None:
        self._after_id = None
        if not self._running:
            return
        self._service.refresh(self._now_provider())
        if self._running:
            self._after_id = self._schedule(
                HABIT_REMINDER_CHECK_MS,
                self._tick,
            )
