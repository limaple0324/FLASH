from config.config_manager import ConfigManager
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from main import CURRENT_GROUP_NAME_KEY, build_services
from services.app_context import AppContext
from services.group_selection_service import GroupSelectionService
from workspace.service import WorkspaceService


def test_build_services_registers_groups_and_restores_player_choice(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("char-a", "角色甲", group="甲組")
    registry.register_character("char-b", "角色乙", group="乙組")
    WindowRegistryStore(tmp_path / "data" / "window_registry.json").save(registry)
    config = ConfigManager(tmp_path / "config" / "settings.json")
    config.set(CURRENT_GROUP_NAME_KEY, "乙組")

    build_services(root=tmp_path)

    choices = AppContext.get(GroupSelectionService).choices()
    workspace = AppContext.get(WorkspaceService).snapshot()
    assert tuple(choice.name for choice in choices) == ("乙組", "甲組")
    assert workspace.current_group is not None
    assert workspace.current_group.name == "乙組"
    assert workspace.next_step == "查看目前需要注意的內容"


def test_build_services_keeps_group_unset_when_no_confirmed_source(tmp_path) -> None:
    build_services(root=tmp_path)

    assert AppContext.get(GroupSelectionService).choices() == ()
    assert AppContext.get(WorkspaceService).snapshot().current_group is None
