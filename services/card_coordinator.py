"""協調可見提醒卡與斷線／恢復歷史。"""

from datetime import datetime, timezone

from cards.models import GroupCard
from cards.service import CardService
from services.card_history_service import CardHistoryService


class CardCoordinator:
    def __init__(
        self,
        cards: CardService,
        history: CardHistoryService,
    ) -> None:
        self.cards = cards
        self.history = history

    def show(
        self,
        card: GroupCard,
        shown_at: datetime | None = None,
    ) -> GroupCard:
        previous = next(
            (current for current in self.cards.cards if current.card_id == card.card_id),
            None,
        )
        result = self.cards.upsert(card, shown_at=shown_at)
        if previous is None or previous.priority_reason is not card.priority_reason:
            entry = next(
                item for item in self.cards.entries if item.card.card_id == card.card_id
            )
            recorded_at = (
                entry.shown_at
                if previous is None
                else shown_at or datetime.now(timezone.utc)
            )
            self.history.record(card, recorded_at)
        return result
