"""提醒卡預設 30 秒的顯示生命週期。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from cards.models import GroupCard
from cards.settings import DEFAULT_CARD_LIFETIME_SECONDS


DEFAULT_CARD_LIFETIME = timedelta(seconds=DEFAULT_CARD_LIFETIME_SECONDS)


def _require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value


@dataclass(frozen=True, slots=True)
class CardLifecycle:
    card: GroupCard
    shown_at: datetime
    lifetime: timedelta = DEFAULT_CARD_LIFETIME

    def __post_init__(self) -> None:
        if not isinstance(self.card, GroupCard):
            raise TypeError("card must be GroupCard.")
        _require_aware(self.shown_at, "shown_at")
        if not isinstance(self.lifetime, timedelta):
            raise TypeError("lifetime must be timedelta.")
        if self.lifetime <= timedelta(0):
            raise ValueError("lifetime must be positive.")

    @property
    def expires_at(self) -> datetime:
        return self.shown_at + self.lifetime

    def is_expired(self, now: datetime) -> bool:
        return _require_aware(now, "now") >= self.expires_at
