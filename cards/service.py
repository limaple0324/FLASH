"""管理同時可見的組別級提醒卡。"""

from datetime import datetime, timezone

from cards.lifecycle import CardLifecycle, _require_aware
from cards.models import GroupCard


MAX_VISIBLE_CARDS = 3


class CardCapacityError(RuntimeError):
    """加入第四張新卡時回報，避免擅自淘汰既有提醒。"""


class CardService:
    def __init__(self) -> None:
        self._entries: list[CardLifecycle] = []

    @property
    def cards(self) -> tuple[GroupCard, ...]:
        return tuple(entry.card for entry in self._entries)

    @property
    def entries(self) -> tuple[CardLifecycle, ...]:
        return tuple(self._entries)

    def upsert(
        self,
        card: GroupCard,
        shown_at: datetime | None = None,
    ) -> GroupCard:
        if not isinstance(card, GroupCard):
            raise TypeError("card must be GroupCard.")

        for index, current in enumerate(self._entries):
            if current.card.card_id == card.card_id:
                self._entries[index] = CardLifecycle(card, current.shown_at)
                return card

        if len(self._entries) >= MAX_VISIBLE_CARDS:
            raise CardCapacityError("At most three cards can be visible.")
        shown_at = shown_at or datetime.now(timezone.utc)
        self._entries.append(CardLifecycle(card, shown_at))
        return card

    def remove(self, card_id: str) -> GroupCard | None:
        if not isinstance(card_id, str):
            raise TypeError("card_id must be str.")
        card_id = card_id.strip()
        if not card_id:
            raise ValueError("card_id must not be empty.")

        for index, entry in enumerate(self._entries):
            if entry.card.card_id == card_id:
                return self._entries.pop(index).card
        return None

    def remove_expired(self, now: datetime) -> tuple[GroupCard, ...]:
        _require_aware(now, "now")
        expired = tuple(entry.card for entry in self._entries if entry.is_expired(now))
        self._entries = [entry for entry in self._entries if not entry.is_expired(now)]
        return expired

    def complete(self, card_id: str) -> GroupCard | None:
        return self.remove(card_id)
