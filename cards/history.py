"""只保留斷線與必要恢復提醒的精簡歷史。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from cards.lifecycle import _require_aware
from cards.models import GroupCard
from cards.priority import CardPriorityReason


_RETAINED_REASONS = frozenset(
    {
        CardPriorityReason.DISCONNECTION,
        CardPriorityReason.RECOVERY,
    }
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


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
        if not isinstance(self.priority_reason, CardPriorityReason):
            raise TypeError("priority_reason must be CardPriorityReason.")
        if self.priority_reason not in _RETAINED_REASONS:
            raise ValueError("Only disconnection and recovery records may be retained.")
        affected_ids = tuple(
            _required_text(item, "affected_character_ids item")
            for item in self.affected_character_ids
        )
        if len(affected_ids) != len(set(affected_ids)):
            raise ValueError("affected_character_ids cannot contain duplicates.")
        object.__setattr__(self, "card_id", _required_text(self.card_id, "card_id"))
        object.__setattr__(self, "group_id", _required_text(self.group_id, "group_id"))
        object.__setattr__(self, "group_name", _required_text(self.group_name, "group_name"))
        object.__setattr__(self, "activity_id", _required_text(self.activity_id, "activity_id"))
        object.__setattr__(self, "activity_name", _required_text(self.activity_name, "activity_name"))
        object.__setattr__(
            self,
            "current_progress",
            _required_text(self.current_progress, "current_progress"),
        )
        object.__setattr__(self, "affected_character_ids", affected_ids)
        if self.next_step is not None:
            object.__setattr__(self, "next_step", _required_text(self.next_step, "next_step"))

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

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CardHistoryRecord":
        if not isinstance(payload, Mapping):
            raise ValueError("History record must be an object.")
        recorded_value = payload.get("recorded_at")
        reason_value = payload.get("priority_reason")
        affected_value = payload.get("affected_character_ids")
        next_step = payload.get("next_step")
        if not isinstance(recorded_value, str):
            raise ValueError("recorded_at must be an ISO datetime string.")
        if not isinstance(reason_value, str):
            raise ValueError("priority_reason must be a string.")
        if not isinstance(affected_value, list):
            raise ValueError("affected_character_ids must be a list.")
        if next_step is not None and not isinstance(next_step, str):
            raise ValueError("next_step must be a string or null.")
        try:
            recorded_at = datetime.fromisoformat(recorded_value)
            priority_reason = CardPriorityReason(reason_value)
        except ValueError as exc:
            raise ValueError("History time or priority reason is invalid.") from exc
        return cls(
            recorded_at=recorded_at,
            card_id=_required_text(payload.get("card_id"), "card_id"),
            priority_reason=priority_reason,
            group_id=_required_text(payload.get("group_id"), "group_id"),
            group_name=_required_text(payload.get("group_name"), "group_name"),
            activity_id=_required_text(payload.get("activity_id"), "activity_id"),
            activity_name=_required_text(payload.get("activity_name"), "activity_name"),
            current_progress=_required_text(
                payload.get("current_progress"),
                "current_progress",
            ),
            affected_character_ids=tuple(affected_value),
            next_step=next_step,
        )


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
