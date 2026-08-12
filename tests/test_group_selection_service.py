from core.window_registry import WindowRegistry
from services.group_configuration_service import GroupConfigurationService
from services.group_selection_service import (
    GroupSelectionService,
    PlayerGroupChoice,
    default_legacy_group_config_path,
)


def test_registry_groups_are_available_without_old_config(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("a", "角色甲", group="甲組")
    registry.register_character("b", "角色乙", group="甲組")
    registry.register_character("c", "角色丙", group="乙組")

    service = GroupSelectionService(registry)

    assert tuple((item.name, item.character_count) for item in service.choices()) == (
        ("乙組", 1),
        ("甲組", 2),
    )


def test_configured_choice_wins_when_it_exists(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("a", "甲", group="120")
    registry.register_character("b", "乙", group="160")
    service = GroupSelectionService(registry)

    assert service.initial_choice("160").name == "160"
    assert service.find("不存在") is None


def test_configured_group_keeps_fixed_id_after_rename(tmp_path) -> None:
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.create_group("原名稱")
    fixed_group_id = configuration.group("原名稱").group_id
    configuration.rename_group("原名稱", "新名稱")
    service = GroupSelectionService(
        WindowRegistry(),
        configuration=configuration,
    )

    assert service.find("新名稱").group_id == fixed_group_id


def test_default_legacy_path_prefers_confirmed_filename(
    tmp_path,
    monkeypatch,
) -> None:
    directory = tmp_path / "輔V0.2"
    directory.mkdir()
    confirmed = directory / "sync_launch_config.json"
    older_alias = directory / "sync_launch_config_v02.json"
    confirmed.write_text("{}", encoding="utf-8")
    older_alias.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert default_legacy_group_config_path() == confirmed


def test_default_legacy_path_supports_older_filename(
    tmp_path,
    monkeypatch,
) -> None:
    directory = tmp_path / "輔V0.2"
    directory.mkdir()
    older_alias = directory / "sync_launch_config_v02.json"
    older_alias.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert default_legacy_group_config_path() == older_alias


def test_workspace_group_uses_safe_choice() -> None:
    choice = PlayerGroupChoice("group-1", "120", 5)

    group = GroupSelectionService.workspace_group(choice)

    assert group.group_id == "group-1"
    assert group.name == "120"
    assert group.characters == ()
