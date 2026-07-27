"""由桌面主迴圈排程器定期清除已到期的提醒卡。"""

from collections.abc import Callable
from datetime import datetime, timezone

from cards.service import CardService
from cards.models import GroupCard


CARD_EXPIRY_CHECK_MS = 1000


class CardExpiryMonitor:
    def __init__(
        self,
        cards: CardService,
        schedule: Callable[[int, Callable[[], None]], object],
        now: Callable[[], datetime] | None = None,
        on_pending_expired: Callable[[GroupCard], object] | None = None,
    ) -> None:
        if not isinstance(cards, CardService):
            raise TypeError("cards must be CardService.")
        if not callable(schedule):
            raise TypeError("schedule must be callable.")
        if now is not None and not callable(now):
            raise TypeError("now must be callable.")
        if on_pending_expired is not None and not callable(
            on_pending_expired
        ):
            raise TypeError("on_pending_expired must be callable.")
        self.cards = cards
        self._schedule = schedule
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._on_pending_expired = on_pending_expired
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
        pending_ids = {
            entry.card.card_id for entry in self.cards.pending_entries
        }
        expired = self.cards.remove_expired(self._now())
        if self._on_pending_expired is not None:
            for card in expired:
                if card.card_id in pending_ids:
                    self._on_pending_expired(card)
        self._schedule_next()
