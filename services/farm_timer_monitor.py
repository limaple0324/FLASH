"""在主介面排程中檢查農場成熟與逾時。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from services.farm_timer_service import FarmTimerService


class FarmTimerMonitor:
    def __init__(
        self,
        service: FarmTimerService,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
    ) -> None:
        if not isinstance(service, FarmTimerService):
            raise TypeError("service must be FarmTimerService.")
        if not callable(schedule) or not callable(cancel):
            raise TypeError("schedule and cancel must be callable.")
        self._service = service
        self._schedule = schedule
        self._cancel = cancel
        self._scheduled: object | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def _tick(self) -> None:
        self._scheduled = None
        if not self._running:
            return
        self._service.poll(datetime.now(timezone.utc))
        if self._running:
            self._scheduled = self._schedule(
                15000,
                self._tick,
            )

    def start(self) -> bool:
        if self._running:
            return False
        self._running = True
        self._tick()
        return True

    def stop(self) -> bool:
        if not self._running and self._scheduled is None:
            return True
        self._running = False
        scheduled = self._scheduled
        self._scheduled = None
        if scheduled is not None:
            self._cancel(scheduled)
        return True
