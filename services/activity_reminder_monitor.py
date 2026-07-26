"""Poll confirmed activity reminders from the desktop main loop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domain.progress import TAIPEI_TIMEZONE
from services.activity_reminder_service import ActivityReminderService


ACTIVITY_REMINDER_CHECK_MS = 15_000


class ActivityReminderMonitor:
    def __init__(
        self,
        service: ActivityReminderService,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(service, ActivityReminderService) and not callable(
            getattr(service, "poll", None)
        ):
            raise TypeError("service must provide poll(now).")
        if not callable(schedule) or not callable(cancel):
            raise TypeError("schedule and cancel must be callable.")
        self._service = service
        self._schedule = schedule
        self._cancel = cancel
        self._now = now or (lambda: datetime.now(TAIPEI_TIMEZONE))
        self._running = False
        self._after_id: object | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.poll()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self._cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def poll(self) -> None:
        if not self._running:
            return
        self._after_id = None
        self._service.poll(self._now())
        if self._running:
            self._after_id = self._schedule(
                ACTIVITY_REMINDER_CHECK_MS,
                self.poll,
            )
