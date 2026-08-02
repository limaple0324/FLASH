import json

from core.window_registry import WindowRegistry
from services.group_configuration_service import GroupConfigurationService
from services.group_selection_service import (
    GroupSelectionService,
    PlayerGroupChoice,
    default_legacy_group_config_path,
)


def _legacy_file(tmp_path):
    path = tmp_path / "sync_launch_config_v02.json"
    path.write_text(
        json.dumps(
            {
                "app_state": {"active_group_name": "120"},
                "groups": [
                    {"name": "14支", "launch_entries": [{}] * 14},
                    {"name": "120", "launch_entries": [{}] * 5},
                    {"name": "160", "launch_entries": [{}] * 5},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_choices_import_only_safe_names_and_counts(tmp_path) -> None:
    service = GroupSelectionService(
        WindowRegistry(),
        legacy_config_path=_legacy_file(tmp_path),
    )

    assert tuple((item.name, item.character_count) for item in service.choices()) == (
        ("14支", 14),
        ("120", 5),
        ("160", 5),
    )
    assert all(item.group_id.startswith("group-") for item in service.choices())
    assert service.initial_choice().name == "120"


def test_registry_groups_are_available_without_old_config(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("a", "角色甲", group="甲組")
    registry.register_character("b", "角色乙", group="甲組")
    registry.register_character("c", "角色丙", group="乙組")

    service = GroupSelectionService(
        registry,
        legacy_config_path=tmp_path / "missing.json",
    )

    assert tuple((item.name, item.character_count) for item in service.choices()) == (
        ("乙組", 1),
        ("甲組", 2),
    )


def test_configured_choice_wins_over_legacy_active_group(tmp_path) -> None:
    service = GroupSelectionService(
        WindowRegistry(),
        legacy_config_path=_legacy_file(tmp_path),
    )

    assert service.initial_choice("160").name == "160"
    assert service.find("不存在") is None


def test_configured_group_keeps_fixed_id_after_rename(tmp_path) -> None:
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.create_group("原名稱")
    fixed_group_id = configuration.group("原名稱").group_id
    configuration.rename_group("原名稱", "新名稱")
    service = GroupSelectionService(
        WindowRegistry(),
        legacy_config_path=configuration.path,
        configuration=configuration,
    )

    assert service.find("新名稱").group_id == fixed_group_id


def test_malformed_legacy_config_fails_closed(tmp_path) -> None:
    path = tmp_path / "sync_launch_config_v02.json"
    path.write_text("{broken", encoding="utf-8")

    service = GroupSelectionService(WindowRegistry(), legacy_config_path=path)

    assert service.choices() == ()
    assert service.initial_choice() is None


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
