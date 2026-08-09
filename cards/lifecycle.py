"""提醒卡預設 30 秒的顯示生命週期。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import inf
from typing import TYPE_CHECKING

from cards.models import GroupCard
from cards.settings import DEFAULT_CARD_LIFETIME_SECONDS
from domain.character import CharacterImportance, character_importance_rank

if TYPE_CHECKING:
    from decision.models import DecisionCategory


DEFAULT_CARD_LIFETIME = timedelta(seconds=DEFAULT_CARD_LIFETIME_SECONDS)


def _require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value


@dataclass(frozen=True, slots=True)
class CardPresentationOrder:
    """已決策卡片的共同排序資料，不從舊五層原因回推。"""

    category: "DecisionCategory"
    remaining_time: timedelta | None
    character_importance: CharacterImportance
    event_id: str

    def __post_init__(self) -> None:
        from decision.models import DecisionCategory

        if not isinstance(self.category, DecisionCategory):
            raise TypeError("category must be DecisionCategory.")
        if self.category is DecisionCategory.QUIET:
            raise ValueError("category must be a visible DecisionCategory.")
        if self.remaining_time is not None:
            if not isinstance(self.remaining_time, timedelta):
                raise TypeError("remaining_time must be timedelta or None.")
            if self.remaining_time < timedelta(0):
                raise ValueError("remaining_time cannot be negative.")
        if not isinstance(self.character_importance, CharacterImportance):
            raise TypeError("character_importance must be CharacterImportance.")
        event_id = self.event_id.strip()
        if not event_id:
            raise ValueError("event_id must not be empty.")
        object.__setattr__(self, "event_id", event_id)

    @classmethod
    def general(cls, event_id: str) -> "CardPresentationOrder":
        from decision.models import DecisionCategory

        return cls(
            category=DecisionCategory.GENERAL_INFORMATION,
            remaining_time=None,
            character_importance=CharacterImportance.SECONDARY,
            event_id=event_id,
        )

    def sort_key(self) -> tuple[int, float, int, str]:
        remaining_seconds = (
            self.remaining_time.total_seconds()
            if self.remaining_time is not None
            else inf
        )
        return (
            int(self.category),
            remaining_seconds,
            character_importance_rank(self.character_importance),
            self.event_id,
        )


@dataclass(frozen=True, slots=True)
class CardLifecycle:
    card: GroupCard
    shown_at: datetime
    lifetime: timedelta = DEFAULT_CARD_LIFETIME
    presentation_order: CardPresentationOrder | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.card, GroupCard):
            raise TypeError("card must be GroupCard.")
        _require_aware(self.shown_at, "shown_at")
        if not isinstance(self.lifetime, timedelta):
            raise TypeError("lifetime must be timedelta.")
        if self.lifetime <= timedelta(0):
            raise ValueError("lifetime must be positive.")
        presentation_order = self.presentation_order
        if presentation_order is None:
            presentation_order = CardPresentationOrder.general(self.card.card_id)
        if not isinstance(presentation_order, CardPresentationOrder):
            raise TypeError("presentation_order must be CardPresentationOrder.")
        if presentation_order.event_id != self.card.card_id:
            raise ValueError("presentation_order event_id must match card_id.")
        object.__setattr__(self, "presentation_order", presentation_order)

    @property
    def expires_at(self) -> datetime:
        return self.shown_at + self.lifetime

    def is_expired(self, now: datetime) -> bool:
        return _require_aware(now, "now") >= self.expires_at
