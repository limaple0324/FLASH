import pytest

from cards.models import GroupCard
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.character import Character
from domain.group import CharacterGroup


def _group() -> CharacterGroup:
    return CharacterGroup(
        group_id="14-windows",
        name="14支",
        characters=(
            Character(character_id="120-old", display_name="120古", level=120),
            Character(character_id="120-second", display_name="120次", level=120),
        ),
    )


def _activity() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=16,
    )


def _card(**changes) -> GroupCard:
    values = {
        "card_id": "guard-progress",
        "group": _group(),
        "activity": _activity(),
        "current_progress": "120古｜第2次所羅門",
    }
    values.update(changes)
    return GroupCard(**values)


def test_card_collects_confirmed_information_at_group_level():
    card = _card(
        affected_character_ids=("120-old",),
        daily_summary="已完成1次",
        requires_player_action=True,
        next_step="返回競技場繼續守紀",
    )

    assert card.group.name == "14支"
    assert card.activity.name == "守紀"
    assert card.affected_character_ids == ("120-old",)
    assert card.to_dict()["next_step"] == "返回競技場繼續守紀"


def test_card_can_describe_the_whole_group_without_creating_character_cards():
    card = _card()

    assert card.affected_character_ids == ()
    assert card.requires_player_action is False
    assert card.daily_summary is None
    assert card.next_step is None


def test_card_normalizes_known_text_without_generating_new_information():
    card = _card(
        card_id="  guard-progress  ",
        current_progress="  正在挑戰守紀  ",
        daily_summary="  已完成1次  ",
        next_step="  繼續目前流程  ",
    )

    assert card.card_id == "guard-progress"
    assert card.current_progress == "正在挑戰守紀"
    assert card.daily_summary == "已完成1次"
    assert card.next_step == "繼續目前流程"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("card_id", " "),
        ("current_progress", ""),
        ("daily_summary", "  "),
        ("next_step", "  "),
    ],
)
def test_card_rejects_empty_information(field, value):
    with pytest.raises(ValueError):
        _card(**{field: value})


def test_card_rejects_duplicate_or_foreign_affected_characters():
    with pytest.raises(ValueError):
        _card(affected_character_ids=("120-old", "120-old"))

    with pytest.raises(ValueError):
        _card(affected_character_ids=("not-in-group",))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("group", object()),
        ("activity", object()),
        ("requires_player_action", 1),
        ("affected_character_ids", (1,)),
    ],
)
def test_card_rejects_values_from_the_wrong_boundary(field, value):
    with pytest.raises(TypeError):
        _card(**{field: value})
