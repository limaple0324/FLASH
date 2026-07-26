import json

import pytest

from services.group_configuration_service import (
    GroupConfigurationService,
    SyncCycleError,
)
from services.group_launch_service import SavedWindowPlacement


def _shortcut(tmp_path, name):
    path = tmp_path / f"{name}.lnk"
    path.write_bytes(b"shortcut")
    return path


def _legacy(tmp_path):
    first = _shortcut(tmp_path, "100古")
    second = _shortcut(tmp_path, "100靈")
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "14支",
                        "launch_entries": [
                            {"path": str(first), "role": "主窗口"},
                            {"path": str(second), "role": "同步窗口"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path, first, second


def test_imports_to_new_owned_copy_without_modifying_legacy(tmp_path):
    legacy, first, second = _legacy(tmp_path)
    original = legacy.read_bytes()
    owned = tmp_path / "data" / "groups.json"

    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )

    assert legacy.read_bytes() == original
    assert owned.is_file()
    assert tuple(entry.display_name for entry in service.group("14支").entries) == (
        first.stem,
        second.stem,
    )


def test_add_and_remove_shortcuts_updates_only_owned_copy(tmp_path):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "data" / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    added_path = _shortcut(tmp_path, "120古")

    added = service.add_shortcuts("14支", [added_path, added_path])

    assert len(added) == 1
    assert added[0].display_name == "120古"
    assert added[0].role == "同步窗口"
    assert service.remove_shortcut("14支", added[0].entry_id) is True
    assert service.remove_shortcut("14支", added[0].entry_id) is False


def test_removing_main_promotes_next_entry(tmp_path):
    legacy, _first, _second = _legacy(tmp_path)
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    group = service.group("14支")

    assert service.remove_shortcut("14支", group.entries[0].entry_id) is True
    remaining = service.group("14支").entries
    assert len(remaining) == 1
    assert remaining[0].role == "主窗口"


def test_recursive_sync_expands_all_levels_and_deduplicates_paths(tmp_path):
    legacy, _first, second = _legacy(tmp_path)
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    c_path, d_path, e_path, f_path = [
        _shortcut(tmp_path, name)
        for name in ("丙", "丁", "戊", "己")
    ]
    service.add_shortcuts("14支", [c_path, d_path])
    service.add_shortcuts("延伸組", [second, e_path, f_path])
    first_group = service.group("14支").entries
    second_group = service.group("延伸組").entries
    a, b, c, d = (entry.entry_id for entry in first_group)
    _same_b, e, f = (entry.entry_id for entry in second_group)
    service.add_sync_relation(c, e)

    assert service.expanded_sync_members(a) == (b, e, f, c, d)
    assert service.expanded_sync_members(a).count(e) == 1


def test_cycle_is_rejected_before_save_and_original_graph_remains(tmp_path):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    a, b = (
        entry.entry_id
        for entry in service.group("14支").entries
    )
    before = owned.read_bytes()

    with pytest.raises(
        SyncCycleError,
        match="無法加入：會形成重複控制",
    ):
        service.add_sync_relation(b, a)

    assert owned.read_bytes() == before
    assert service.expanded_sync_members(a) == (b,)
    assert service.expanded_sync_members(b) == ()


def test_group_sync_choices_and_explicit_relations_are_player_safe(
    tmp_path,
):
    first = _shortcut(tmp_path, "甲")
    second = _shortcut(tmp_path, "乙")
    service = GroupConfigurationService(tmp_path / "groups.json")
    service.add_shortcuts("第一組", (first,))
    service.add_shortcuts("第二組", (second,))

    choices = service.available_sync_members("第一組")
    second_id = service.group("第二組").entries[0].entry_id
    assert [(item.label, item.entry_id) for item in choices] == [
        ("第二組｜乙", second_id)
    ]

    first_id = service.group("第一組").entries[0].entry_id
    service.add_sync_relation(first_id, second_id)
    relations = service.explicit_sync_members("第一組")
    assert [(item.label, item.entry_id) for item in relations] == [
        ("第二組｜乙", second_id)
    ]


def test_group_create_rename_move_and_delete_preserve_player_order(
    tmp_path,
):
    service = GroupConfigurationService(tmp_path / "groups.json")

    assert service.create_group("第一組") is True
    assert service.create_group("第二組") is True
    assert service.create_group("第二組") is False
    assert service.rename_group("第二組", "新名稱") is True
    assert service.move_group("新名稱", -1) is True
    assert tuple(group.name for group in service.groups()) == (
        "新名稱",
        "第一組",
    )
    assert service.delete_group("新名稱") is True
    assert service.delete_group("新名稱") is False
    assert tuple(group.name for group in service.groups()) == ("第一組",)


def test_saved_window_layout_survives_owned_configuration_reload(tmp_path):
    legacy, first, _second = _legacy(tmp_path)
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    payload["groups"][0]["launch_entries"][0].update(
        {
            "x": -2191,
            "y": -523,
            "width": 916,
            "height": 629,
            "delay_ms": 200,
        }
    )
    legacy.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    owned = tmp_path / "groups.json"

    first_service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    first_entry = first_service.group("14支").entries[0]
    second_service = GroupConfigurationService(owned)
    second_entry = second_service.group("14支").entries[0]

    assert first_entry.shortcut_path == first
    assert first_entry.placement is not None
    assert second_entry.placement == first_entry.placement
    assert (
        second_entry.placement.x,
        second_entry.placement.y,
        second_entry.placement.width,
        second_entry.placement.height,
        second_entry.placement.delay_ms,
    ) == (-2191, -523, 916, 629, 200)


def test_existing_owned_copy_is_enriched_from_legacy_saved_layout(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    legacy_payload = json.loads(legacy.read_text(encoding="utf-8"))
    legacy_payload["groups"][0]["launch_entries"][0].update(
        {
            "x": -3000,
            "y": 250,
            "width": 916,
            "height": 629,
            "delay_ms": 150,
        }
    )
    legacy.write_text(
        json.dumps(legacy_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    owned = tmp_path / "groups.json"
    first_service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    owned_payload = json.loads(owned.read_text(encoding="utf-8"))
    for entry in owned_payload["groups"][0]["launch_entries"]:
        for key in ("x", "y", "width", "height", "delay_ms"):
            entry.pop(key, None)
    owned.write_text(
        json.dumps(owned_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    enriched = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    placement = enriched.group("14支").entries[0].placement

    assert placement is not None
    assert (
        placement.x,
        placement.y,
        placement.width,
        placement.height,
        placement.delay_ms,
    ) == (-3000, 250, 916, 629, 150)
    assert first_service.group("14支") is not None


def test_record_current_positions_requires_complete_group_and_saves_all(
    tmp_path,
):
    legacy, first, second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )

    assert service.update_saved_placements(
        "14支",
        {
            first: SavedWindowPlacement(10, 20, 900, 600, 0),
        },
    ) is False
    assert service.update_saved_placements(
        "14支",
        {
            first: SavedWindowPlacement(10, 20, 900, 600, 0),
            second: SavedWindowPlacement(-500, 30, 916, 629, 100),
        },
    ) is True

    reloaded = GroupConfigurationService(owned).group("14支")
    assert tuple(entry.placement for entry in reloaded.entries) == (
        SavedWindowPlacement(10, 20, 900, 600, 0),
        SavedWindowPlacement(-500, 30, 916, 629, 100),
    )


def test_set_main_entry_reorders_group_and_clear_keeps_empty_group(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    before = service.group("14支").entries

    assert service.set_main_entry("14支", before[1].entry_id) is True
    reordered = service.group("14支").entries
    assert reordered[0].entry_id == before[1].entry_id
    assert reordered[0].role == "主窗口"
    assert reordered[1].role == "同步窗口"
    assert service.clear_group("14支") is True
    assert service.group("14支").entries == ()
    assert service.clear_group("14支") is False


def test_import_same_name_replaces_owned_group_without_duplicate(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    replacement = _shortcut(tmp_path, "新角色")
    imported = tmp_path / "import.json"
    imported.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "14支",
                        "launch_entries": [
                            {
                                "path": str(replacement),
                                "x": 50,
                                "y": 60,
                                "width": 900,
                                "height": 600,
                                "delay_ms": 20,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = imported.read_bytes()

    names = service.import_configuration(imported)

    assert names == ("14支",)
    assert tuple(group.name for group in service.groups()).count("14支") == 1
    entries = service.group("14支").entries
    assert tuple(entry.display_name for entry in entries) == ("新角色",)
    assert entries[0].placement == SavedWindowPlacement(
        50,
        60,
        900,
        600,
        20,
    )
    assert imported.read_bytes() == before


def test_export_and_import_round_trip_preserves_group_order_and_layout(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    third = _shortcut(tmp_path, "第三角色")
    service.add_shortcuts("第二組", (third,))
    export_path = service.export_configuration(
        tmp_path / "export.json"
    )
    restored = GroupConfigurationService(tmp_path / "restored.json")

    names = restored.import_configuration(export_path)

    assert names == ("14支", "第二組")
    assert tuple(group.name for group in restored.groups()) == (
        "14支",
        "第二組",
    )
    assert (
        restored.group("14支").entries[0].shortcut_path
        == service.group("14支").entries[0].shortcut_path
    )
