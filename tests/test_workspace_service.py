import pytest

from domain.group import CharacterGroup
from main import build_services
from services.app_context import AppContext
from services.group_selection_service import GroupSelectionService
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


def test_service_starts_empty_and_can_use_a_known_initial_state():
    empty = WorkspaceService()
    initial = WorkspaceState(next_step="選擇組別")
    restored = WorkspaceService(initial)

    assert empty.snapshot() == WorkspaceState()
    assert restored.snapshot() is initial


def test_service_updates_only_the_requested_workspace_fields():
    group = CharacterGroup(group_id="14-windows", name="14支")
    service = WorkspaceService()

    first = service.set_current_group(group)
    second = service.set_next_step("完成下一個角色")

    assert first.current_group is group
    assert second.current_group is group
    assert second == WorkspaceState(group, None, "完成下一個角色")


def test_service_keeps_the_previous_state_when_an_update_is_invalid():
    initial = WorkspaceState(next_step="選擇組別")
    service = WorkspaceService(initial)

    with pytest.raises(ValueError):
        service.set_next_step("   ")

    assert service.snapshot() is initial


def test_service_rejects_an_invalid_initial_state():
    with pytest.raises(TypeError):
        WorkspaceService(object())


def test_build_services_registers_clean_sp3_workspace_without_legacy_groups(
    tmp_path,
):
    build_services(root=tmp_path)

    service = AppContext.get(WorkspaceService)
    groups = AppContext.get(GroupSelectionService)

    assert isinstance(service, WorkspaceService)
    assert service.snapshot() == WorkspaceState(next_step="選擇組別")
    assert groups.choices() == ()
