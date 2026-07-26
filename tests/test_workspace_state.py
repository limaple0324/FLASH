import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from workspace.models import WorkspaceState


def _activity() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=16,
    )


def test_workspace_state_can_start_without_a_selection():
    state = WorkspaceState()

    assert state.to_dict() == {
        "current_group": None,
        "current_activity": None,
        "next_step": None,
    }


def test_workspace_state_reuses_group_and_activity_models_for_presentation():
    group = CharacterGroup(group_id="14-windows", name="14支")
    activity = _activity()
    state = WorkspaceState(
        current_group=group,
        current_activity=activity,
        next_step="  完成下一個角色  ",
    )

    assert state.current_group is group
    assert state.current_activity is activity
    assert state.next_step == "完成下一個角色"
    assert state.to_dict()["current_group"]["name"] == "14支"
    assert state.to_dict()["current_activity"]["name"] == "守紀"


@pytest.mark.parametrize("next_step", ["", "   "])
def test_workspace_state_rejects_an_empty_next_step(next_step):
    with pytest.raises(ValueError):
        WorkspaceState(next_step=next_step)


@pytest.mark.parametrize(
    ("field", "value"),
    [("current_group", object()), ("current_activity", object()), ("next_step", 1)],
)
def test_workspace_state_rejects_values_from_the_wrong_boundary(field, value):
    with pytest.raises(TypeError):
        WorkspaceState(**{field: value})
