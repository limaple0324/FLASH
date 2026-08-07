from config.config_manager import ConfigManager
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character_store import CharacterStore
from main import CURRENT_GROUP_NAME_KEY, build_services
from services.app_context import AppContext
from services.character_note_service import CharacterNoteService
from services.character_view_service import CharacterViewService
from services.current_group_publication_service import (
    CurrentGroupPublicationService,
)
from services.group_character_registration_service import (
    GroupCharacterRegistrationService,
)
from services.group_configuration_service import GroupConfigurationService
from services.group_selection_service import GroupSelectionService
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)
from workspace.service import WorkspaceService


def test_build_services_registers_groups_and_restores_player_choice(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("char-a", "角色甲", group="甲組")
    registry.register_character("char-b", "角色乙", group="乙組")
    coordinator = IdentityDataTransactionCoordinator()
    WindowRegistryStore(
        tmp_path / "data" / "window_registry.json",
        coordinator,
    ).save(registry)
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


def test_build_services_injects_one_shared_identity_coordinator(tmp_path) -> None:
    build_services(root=tmp_path)

    coordinator = AppContext.get(IdentityDataTransactionCoordinator)
    assert coordinator is not None
    assert AppContext.get(WindowRegistryStore).coordinator is coordinator
    assert AppContext.get(CharacterStore).coordinator is coordinator
    assert AppContext.get(GroupConfigurationService).coordinator is coordinator
    assert AppContext.get(WorkspaceService).coordinator is coordinator
    assert (
        AppContext.get(GroupCharacterRegistrationService)._coordinator
        is coordinator
    )
    assert AppContext.get(CharacterViewService)._coordinator is coordinator
    assert AppContext.get(CharacterNoteService)._coordinator is coordinator
    assert (
        AppContext.get(CurrentGroupPublicationService)._coordinator
        is coordinator
    )


def test_group_selection_and_ungrouped_join_use_explicit_view_results() -> None:
    main_source = Path("main.py").read_text(encoding="utf-8")
    home_source = Path("ui/home.py").read_text(encoding="utf-8")
    join_source = main_source[
        main_source.index("    def add_ungrouped_window_to_group("):
        main_source.index("    def remove_group_shortcut(")
    ]
    select_source = home_source[
        home_source.index("    def _select_group("):
        home_source.index("    @staticmethod", home_source.index("    def _select_group("))
    ]

    assert "-> GroupManagementViewResult:" in join_source
    assert join_source.count("GroupManagementViewResult(") >= 4
    assert "refreshed, added = finish_group_management(mutation)" in join_source
    assert "if not refreshed.success:" in join_source
    assert "return refreshed" in join_source
    assert "if refreshed.message:" in join_source
    assert "if not result.success:" in home_source[
        home_source.index("    def _add_ungrouped_window("):
        home_source.index("    def _show_ungrouped_status(")
    ]
    assert "_show_group_selection_message" in select_source
    assert "_refresh_group_selection_controls()" in select_source
    assert 'text="目前使用" if current else "選擇"' in home_source
from pathlib import Path
