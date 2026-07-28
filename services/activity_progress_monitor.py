"""從桌面主迴圈檢查跨日、睡眠喚醒與系統時間調整。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domain.progress import TAIPEI_TIMEZONE
from services.activity_progress_service import ActivityProgressService


ACTIVITY_PROGRESS_CHECK_MS = 15_000


class ActivityProgressMonitor:
    def __init__(
        self,
        service: ActivityProgressService,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(service, ActivityProgressService):
            raise TypeError("service must be ActivityProgressService.")
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

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        try:
            self.poll()
        except Exception:
            self._running = False
            self._after_id = None
            raise
        return True

    def stop(self) -> bool:
        self._running = False
        if self._after_id is None:
            return True
        after_id = self._after_id
        self._after_id = None
        try:
            self._cancel(after_id)
        except Exception:
            return False
        return True

    def poll(self) -> None:
        if not self._running:
            return
        self._after_id = None
        self._service.reset_due(self._now())
        if self._running:
            self._after_id = self._schedule(
                ACTIVITY_PROGRESS_CHECK_MS,
                self.poll,
            )
