import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule


def test_daily_activity_can_describe_guard_without_ui_rules():
    activity = ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=16,
    )

    assert activity.applies_to_all_characters is True
    assert activity.applies_to("any-character") is True
    assert activity.to_dict()["max_completions"] == 16
    assert activity.to_dict()["reset_rule"] == "每日00:00"


def test_activity_can_limit_itself_to_selected_characters():
    activity = ActivityDefinition(
        activity_id="diamond-member",
        name="鑽石會員",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=1,
        applicable_character_ids=("level-160-a",),
    )

    assert activity.applies_to("level-160-a") is True
    assert activity.applies_to("level-120-a") is False


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_activity_rejects_invalid_maximum(maximum):
    with pytest.raises(ValueError):
        ActivityDefinition(
            activity_id="invalid",
            name="活動",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
            max_completions=maximum,
        )


def test_activity_types_cover_daily_loop_calendar_and_permanent_events():
    assert [item.value for item in ActivityType] == [
        "每天",
        "每日多階段",
        "循環",
        "行事曆",
        "常駐",
    ]
