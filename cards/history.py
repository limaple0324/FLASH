"""只保留斷線與必要恢復提醒的精簡歷史。"""

from dataclasses import dataclass
from datetime import datetime

from cards.lifecycle import _require_aware
from cards.models import GroupCard
from cards.priority import CardPriorityReason


_RETAINED_REASONS = frozenset(
    {
        CardPriorityReason.DISCONNECTION,
        CardPriorityReason.RECOVERY,
    }
)


def should_retain(card: GroupCard) -> bool:
    if not isinstance(card, GroupCard):
        raise TypeError("card must be GroupCard.")
    return card.priority_reason in _RETAINED_REASONS


@dataclass(frozen=True, slots=True)
class CardHistoryRecord:
    recorded_at: datetime
    card_id: str
    priority_reason: CardPriorityReason
    group_id: str
    group_name: str
    activity_id: str
    activity_name: str
    current_progress: str
    affected_character_ids: tuple[str, ...]
    next_step: str | None

    def __post_init__(self) -> None:
        _require_aware(self.recorded_at, "recorded_at")
        if self.priority_reason not in _RETAINED_REASONS:
            raise ValueError("Only disconnection and recovery records may be retained.")

    @classmethod
    def from_card(
        cls,
        card: GroupCard,
        recorded_at: datetime,
    ) -> "CardHistoryRecord":
        if not should_retain(card):
            raise ValueError("This card must not be retained in history.")
        return cls(
            recorded_at=recorded_at,
            card_id=card.card_id,
            priority_reason=card.priority_reason,
            group_id=card.group.group_id,
            group_name=card.group.name,
            activity_id=card.activity.activity_id,
            activity_name=card.activity.name,
            current_progress=card.current_progress,
            affected_character_ids=card.affected_character_ids,
            next_step=card.next_step,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "recorded_at": self.recorded_at.isoformat(),
            "card_id": self.card_id,
            "priority_reason": self.priority_reason.value,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "activity_id": self.activity_id,
            "activity_name": self.activity_name,
            "current_progress": self.current_progress,
            "affected_character_ids": list(self.affected_character_ids),
            "next_step": self.next_step,
        }


class CardHistory:
    def __init__(self) -> None:
        self._records: list[CardHistoryRecord] = []

    @property
    def records(self) -> tuple[CardHistoryRecord, ...]:
        return tuple(self._records)

    def record(
        self,
        card: GroupCard,
        recorded_at: datetime,
    ) -> CardHistoryRecord | None:
        if not should_retain(card):
            return None
        record = CardHistoryRecord.from_card(card, recorded_at)
        self._records.append(record)
        return record
