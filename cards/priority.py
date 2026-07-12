"""提醒卡已確認的優先原因與層級。"""

from enum import Enum, IntEnum


class CardPriorityTier(IntEnum):
    HIGHEST = 0
    ACTIVITY = 1
    GENERAL = 2


class CardPriorityReason(str, Enum):
    DISCONNECTION = "斷線"
    RECOVERY = "恢復"
    TIME_LIMIT = "時間限制"
    LOSS_RISK = "即將造成損失"
    ACTIVITY = "活動"
    GENERAL = "一般資訊"


_HIGHEST_REASONS = frozenset(
    {
        CardPriorityReason.DISCONNECTION,
        CardPriorityReason.RECOVERY,
        CardPriorityReason.TIME_LIMIT,
        CardPriorityReason.LOSS_RISK,
    }
)


def priority_tier(reason: CardPriorityReason) -> CardPriorityTier:
    """只判定已確認的層級，不決定同層卡片的先後。"""

    if not isinstance(reason, CardPriorityReason):
        raise TypeError("reason must be CardPriorityReason.")
    if reason in _HIGHEST_REASONS:
        return CardPriorityTier.HIGHEST
    if reason is CardPriorityReason.ACTIVITY:
        return CardPriorityTier.ACTIVITY
    return CardPriorityTier.GENERAL
