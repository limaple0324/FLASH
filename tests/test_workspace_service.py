import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


def _activity() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=16,
    )


def test_service_starts_empty_and_can_use_a_known_initial_state():
    empty = WorkspaceService()
    initial = WorkspaceState(next_step="選擇組別")
    restored = WorkspaceService(initial)

    assert empty.state == WorkspaceState()
    assert restored.state is initial


def test_service_updates_only_the_requested_workspace_field():
    group = CharacterGroup(group_id="14-windows", name="14支")
    activity = _activity()
    service = WorkspaceService()

    first = service.set_current_group(group)
    second = service.set_current_activity(activity)
    third = service.set_next_step("完成下一個角色")

    assert first.current_group is group
    assert first.current_activity is None
    assert second.current_group is group
    assert second.current_activity is activity
    assert third == WorkspaceState(group, activity, "完成下一個角色")


def test_service_can_clear_one_field_without_changing_the_others():
    group = CharacterGroup(group_id="dimension", name="魔心次元組")
    service = WorkspaceService(WorkspaceState(group, _activity(), "繼續守紀"))

    state = service.set_current_activity(None)

    assert state.current_group is group
    assert state.current_activity is None
    assert state.next_step == "繼續守紀"


def test_service_clear_returns_to_an_empty_workspace():
    service = WorkspaceService(WorkspaceState(next_step="選擇組別"))

    cleared = service.clear()

    assert cleared == WorkspaceState()
    assert service.state is cleared


def test_service_keeps_the_previous_state_when_an_update_is_invalid():
    initial = WorkspaceState(next_step="選擇組別")
    service = WorkspaceService(initial)

    with pytest.raises(ValueError):
        service.set_next_step("   ")

    assert service.state is initial


def test_service_rejects_an_invalid_initial_state():
    with pytest.raises(TypeError):
        WorkspaceService(object())
