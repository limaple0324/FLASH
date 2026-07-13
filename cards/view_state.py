"""提供介面使用的唯讀提醒卡呈現狀態。"""

from dataclasses import dataclass
from datetime import datetime

from cards.lifecycle import CardLifecycle
from cards.service import MAX_VISIBLE_CARDS


@dataclass(frozen=True, slots=True)
class CardViewItem:
    card_id: str
    group_id: str
    group_name: str
    activity_id: str
    activity_name: str
    current_progress: str
    affected_character_ids: tuple[str, ...]
    daily_summary: str | None
    requires_player_action: bool
    next_step: str | None
    priority_reason: str
    priority_level: int
    shown_at: datetime
    expires_at: datetime

    @classmethod
    def from_lifecycle(cls, entry: CardLifecycle) -> "CardViewItem":
        if not isinstance(entry, CardLifecycle):
            raise TypeError("entry must be CardLifecycle.")
        card = entry.card
        return cls(
            card_id=card.card_id,
            group_id=card.group.group_id,
            group_name=card.group.name,
            activity_id=card.activity.activity_id,
            activity_name=card.activity.name,
            current_progress=card.current_progress,
            affected_character_ids=card.affected_character_ids,
            daily_summary=card.daily_summary,
            requires_player_action=card.requires_player_action,
            next_step=card.next_step,
            priority_reason=card.priority_reason.value,
            priority_level=int(card.priority_tier),
            shown_at=entry.shown_at,
            expires_at=entry.expires_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "activity_id": self.activity_id,
            "activity_name": self.activity_name,
            "current_progress": self.current_progress,
            "affected_character_ids": list(self.affected_character_ids),
            "daily_summary": self.daily_summary,
            "requires_player_action": self.requires_player_action,
            "next_step": self.next_step,
            "priority_reason": self.priority_reason,
            "priority_level": self.priority_level,
            "shown_at": self.shown_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CardViewState:
    cards: tuple[CardViewItem, ...] = ()

    def __post_init__(self) -> None:
        cards = tuple(self.cards)
        if any(not isinstance(item, CardViewItem) for item in cards):
            raise TypeError("cards must contain only CardViewItem values.")
        if len(cards) > MAX_VISIBLE_CARDS:
            raise ValueError("Card view state cannot contain more than three cards.")
        object.__setattr__(self, "cards", cards)

    @property
    def is_empty(self) -> bool:
        return not self.cards

    def to_dict(self) -> dict[str, object]:
        return {"cards": [item.to_dict() for item in self.cards]}
