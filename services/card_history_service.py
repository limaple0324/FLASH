"""斷線與恢復提醒歷史的應用服務。"""

from datetime import datetime

from cards.history import CardHistory, CardHistoryRecord
from cards.history_store import CardHistoryStore
from cards.models import GroupCard


class CardHistoryService:
    def __init__(self, store: CardHistoryStore):
        self.store = store
        self._history = CardHistory(store.load())

    def all(self) -> tuple[CardHistoryRecord, ...]:
        return self._history.records

    def record(
        self,
        card: GroupCard,
        recorded_at: datetime,
    ) -> CardHistoryRecord | None:
        record = self._history.record(card, recorded_at)
        if record is not None:
            self.store.save(self._history.records)
        return record
