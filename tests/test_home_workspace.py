import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from ui.home import HomeView
from workspace.models import WorkspaceState


def _known_state() -> WorkspaceState:
    return WorkspaceState(
        current_group=CharacterGroup(
            group_id="private-group-id",
            name="十四支",
        ),
        current_activity=ActivityDefinition(
            activity_id="private-activity-id",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
            max_completions=16,
            applicable_character_ids=("private-character-id",),
        ),
        next_step="完成下一個角色",
    )


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


def test_workspace_refresh_reads_a_new_snapshot_and_updates_existing_label():
    states = iter((WorkspaceState(), _known_state()))
    view = HomeView(
        None,
        {},
        workspace_state_provider=lambda: next(states),
    )
    label = _FakeLabel()
    view._workspace_label = label

    assert "等待設定組別" in view.refresh_workspace()
    refreshed = view.refresh_workspace()

    assert "目前組別：十四支" in refreshed
    assert "目前活動：守紀" in refreshed
    assert "下一步：完成下一個角色" in refreshed
    assert label.text == refreshed
    assert "private-group-id" not in refreshed
    assert "private-activity-id" not in refreshed
    assert "private-character-id" not in refreshed


def test_workspace_refresh_failure_keeps_the_last_visible_text_and_reports_error():
    calls = 0
    errors = []

    def provider() -> WorkspaceState:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _known_state()
        raise OSError(r"C:\private\workspace.json")

    view = HomeView(
        None,
        {},
        workspace_state_provider=provider,
        on_workspace_error=errors.append,
    )
    label = _FakeLabel()
    view._workspace_label = label

    previous = view.refresh_workspace()
    failed = view.refresh_workspace()

    assert failed == previous
    assert label.text == previous
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)


def test_workspace_refresh_rejects_an_invalid_provider_value_without_replacing_state():
    errors = []
    state = _known_state()
    view = HomeView(
        None,
        {},
        workspace_state=state,
        workspace_state_provider=lambda: object(),
        on_workspace_error=errors.append,
    )

    text = view.refresh_workspace()

    assert view.workspace_state is state
    assert "目前組別：十四支" in text
    assert len(errors) == 1
    assert isinstance(errors[0], TypeError)


def test_workspace_refresh_raises_when_no_error_boundary_is_available():
    view = HomeView(
        None,
        {},
        workspace_state_provider=lambda: object(),
    )

    with pytest.raises(TypeError, match="WorkspaceState"):
        view.refresh_workspace()
