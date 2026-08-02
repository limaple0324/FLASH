from datetime import datetime, timezone

import pytest

from cards.history import CardHistory, CardHistoryRecord, should_retain
from cards.models import GroupCard
from cards.priority import CardPriorityReason
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.character import Character
from domain.group import CharacterGroup


def _card(reason: CardPriorityReason) -> GroupCard:
    character = Character(character_id="120-old", display_name="120古", level=120)
    return GroupCard(
        card_id=f"guard-{reason.name.lower()}",
        group=CharacterGroup(
            group_id="14-windows",
            name="14支",
            characters=(character,),
        ),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress="120古｜守紀中斷",
        affected_character_ids=(character.character_id,),
        next_step="返回競技場繼續守紀",
        priority_reason=reason,
    )


@pytest.mark.parametrize(
    "reason",
    [CardPriorityReason.DISCONNECTION],
)
def test_history_retains_only_confirmed_disconnection(reason):
    card = _card(reason)
    recorded_at = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    history = CardHistory()

    record = history.record(card, recorded_at)

    assert should_retain(card) is True
    assert record is history.records[0]
    assert record.priority_reason is reason
    assert record.to_dict()["recorded_at"] == recorded_at.isoformat()


@pytest.mark.parametrize(
    "reason",
    [
        CardPriorityReason.TIME_LIMIT,
        CardPriorityReason.LOSS_RISK,
        CardPriorityReason.ACTIVITY,
        CardPriorityReason.RECOVERY,
        CardPriorityReason.GENERAL,
    ],
)
def test_general_and_temporary_reminders_leave_no_history(reason):
    card = _card(reason)
    history = CardHistory()

    assert should_retain(card) is False
    assert history.record(
        card,
        datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
    ) is None
    assert history.records == ()


def test_history_record_is_a_small_snapshot_not_the_whole_group_model():
    card = _card(CardPriorityReason.DISCONNECTION)
    record = CardHistory().record(
        card,
        datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
    )

    assert record.group_id == "14-windows"
    assert record.activity_name == "守紀"
    assert record.affected_character_ids == ("120-old",)
    assert "group" not in record.to_dict()
    assert "activity" not in record.to_dict()


def test_history_rejects_invalid_time_and_non_retained_direct_records():
    disconnection = _card(CardPriorityReason.DISCONNECTION)
    with pytest.raises(ValueError):
        CardHistory().record(disconnection, datetime(2026, 7, 13, 10, 0))

    with pytest.raises(ValueError):
        CardHistoryRecord.from_card(
            _card(CardPriorityReason.ACTIVITY),
            datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
        )


def test_history_policy_rejects_non_card_values():
    with pytest.raises(TypeError):
        should_retain(object())


def test_existing_recovery_record_remains_readable_without_allowing_new_one():
    payload = CardHistoryRecord(
        recorded_at=datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
        card_id="legacy-recovery",
        priority_reason=CardPriorityReason.RECOVERY,
        group_id="14-windows",
        group_name="14支",
        activity_id="recovery",
        activity_name="恢復",
        current_progress="已恢復",
        affected_character_ids=(),
        next_step=None,
    ).to_dict()

    record = CardHistoryRecord.from_dict(payload)

    assert record.priority_reason is CardPriorityReason.RECOVERY
    assert CardHistory().record(
        _card(CardPriorityReason.RECOVERY),
        datetime(2026, 7, 13, 10, 1, tzinfo=timezone.utc),
    ) is None
