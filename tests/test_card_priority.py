import pytest

from cards.models import GroupCard
from cards.priority import CardPriorityReason, CardPriorityTier, priority_tier
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup


def _card(reason: CardPriorityReason = CardPriorityReason.ACTIVITY) -> GroupCard:
    return GroupCard(
        card_id="dimension-status",
        group=CharacterGroup(group_id="dimension", name="魔心次元組"),
        activity=ActivityDefinition(
            activity_id="dimension",
            name="次元",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress="次元進行中",
        priority_reason=reason,
    )


@pytest.mark.parametrize(
    "reason",
    [
        CardPriorityReason.DISCONNECTION,
        CardPriorityReason.RECOVERY,
        CardPriorityReason.TIME_LIMIT,
        CardPriorityReason.LOSS_RISK,
    ],
)
def test_confirmed_urgent_reasons_share_the_highest_tier(reason):
    assert priority_tier(reason) is CardPriorityTier.HIGHEST
    assert _card(reason).priority_tier is CardPriorityTier.HIGHEST


def test_activity_and_general_information_stay_below_urgent_cards():
    assert priority_tier(CardPriorityReason.ACTIVITY) is CardPriorityTier.ACTIVITY
    assert priority_tier(CardPriorityReason.GENERAL) is CardPriorityTier.GENERAL
    assert CardPriorityTier.HIGHEST < CardPriorityTier.ACTIVITY < CardPriorityTier.GENERAL


def test_group_card_defaults_to_activity_priority_without_guessing_urgency():
    card = _card()

    assert card.priority_reason is CardPriorityReason.ACTIVITY
    assert card.to_dict()["priority_reason"] == "活動"
    assert card.to_dict()["priority_tier"] == "ACTIVITY"


def test_priority_rules_reject_unknown_values():
    with pytest.raises(TypeError):
        priority_tier("斷線")

    with pytest.raises(TypeError):
        _card("斷線")
