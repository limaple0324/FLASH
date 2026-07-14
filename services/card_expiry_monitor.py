"""由桌面主迴圈定期清除已到期的提醒卡。"""

from collections.abc import Callable
from datetime import datetime, timezone

from cards.service import CardService


CARD_EXPIRY_CHECK_MS = 1000


class CardExpiryMonitor:
    def __init__(
        self,
        cards: CardService,
        schedule: Callable[[int, Callable[[], None]], object],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cards, CardService):
            raise TypeError("cards must be CardService.")
        if not callable(schedule):
            raise TypeError("schedule must be callable.")
        if now is not None and not callable(now):
            raise TypeError("now must be callable.")
        self.cards = cards
        self._schedule = schedule
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._schedule_next()

    def stop(self) -> None:
        self._running = False

    def _schedule_next(self) -> None:
        self._schedule(CARD_EXPIRY_CHECK_MS, self._check)

    def _check(self) -> None:
        if not self._running:
            return
        self.cards.remove_expired(self._now())
        self._schedule_next()
