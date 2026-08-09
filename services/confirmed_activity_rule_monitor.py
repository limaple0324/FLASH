"""由主視窗排程輪詢已定案活動，不進行遊戲操作。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domain.progress import TAIPEI_TIMEZONE
from services.confirmed_activity_rule_service import ConfirmedActivityRuleService


CONFIRMED_ACTIVITY_RULE_CHECK_MS = 15_000


class ConfirmedActivityRuleMonitor:
    def __init__(
        self,
        service: ConfirmedActivityRuleService,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(service, ConfirmedActivityRuleService):
            raise TypeError("service must be ConfirmedActivityRuleService.")
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
        self._service.poll(self._now())
        if self._running:
            self._after_id = self._schedule(
                CONFIRMED_ACTIVITY_RULE_CHECK_MS,
                self.poll,
            )
