import json

import pytest

from services.group_configuration_service import (
    GroupConfigurationService,
    GroupHotkeyConflictError,
    GroupMasterLockedError,
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
                        "master_locked": False,
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
    fixed_group_id = service.group("第二組").group_id
    assert service.rename_group("第二組", "新名稱") is True
    assert service.group("新名稱").group_id == fixed_group_id
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


def test_set_main_entry_preserves_launch_order_and_clear_keeps_empty_group(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    before = service.group("14支").entries

    assert service.set_main_entry("14支", before[1].entry_id) is True
    updated = service.group("14支").entries
    assert tuple(entry.entry_id for entry in updated) == tuple(
        entry.entry_id for entry in before
    )
    assert updated[0].role == "同步窗口"
    assert updated[1].role == "主窗口"
    assert service.group("14支").main_entry == updated[1]
    assert service.clear_group("14支") is True
    assert service.group("14支").entries == ()
    assert service.clear_group("14支") is False


def test_reorder_entries_is_atomic_persistent_and_keeps_main_identity(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    before = service.group("14支")
    proposed = tuple(
        entry.entry_id for entry in reversed(before.entries)
    )
    main_id = before.main_entry.entry_id

    assert service.reorder_group_entries("14支", proposed) is True
    reordered = service.group("14支")
    reloaded = GroupConfigurationService(owned).group("14支")

    assert tuple(entry.entry_id for entry in reordered.entries) == proposed
    assert tuple(entry.entry_id for entry in reloaded.entries) == proposed
    assert reordered.main_entry.entry_id == main_id
    assert reloaded.main_entry.entry_id == main_id
    assert reordered.entry_order_customized is True
    assert service.reorder_group_entries("14支", proposed) is False
    assert service.reorder_group_entries(
        "14支",
        (proposed[0], "unknown"),
    ) is False
    assert tuple(
        entry.entry_id for entry in service.group("14支").entries
    ) == proposed
    assert service.remove_shortcut("14支", proposed[0]) is True
    assert service.group("14支").main_entry.entry_id == main_id


def test_master_lock_defaults_safe_and_blocks_group_role_edits(
    tmp_path,
):
    first = _shortcut(tmp_path, "甲")
    second = _shortcut(tmp_path, "乙")
    legacy = tmp_path / "legacy-locked.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "上鎖組",
                        "launch_entries": [
                            {"path": str(first)},
                            {"path": str(second)},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    group = service.group("上鎖組")

    assert group.master_locked is True
    with pytest.raises(
        GroupMasterLockedError,
        match=GroupMasterLockedError.player_message,
    ):
        service.add_shortcuts(
            "上鎖組",
            (_shortcut(tmp_path, "丙"),),
        )
    with pytest.raises(GroupMasterLockedError):
        service.remove_shortcut(
            "上鎖組",
            group.entries[1].entry_id,
        )
    with pytest.raises(GroupMasterLockedError):
        service.set_main_entry(
            "上鎖組",
            group.entries[1].entry_id,
        )
    with pytest.raises(GroupMasterLockedError):
        service.reorder_group_entries(
            "上鎖組",
            tuple(
                entry.entry_id
                for entry in reversed(group.entries)
            ),
        )
    with pytest.raises(GroupMasterLockedError):
        service.clear_group("上鎖組")
    with pytest.raises(GroupMasterLockedError):
        service.update_saved_placements(
            "上鎖組",
            {
                first: SavedWindowPlacement(0, 0, 900, 600, 0),
                second: SavedWindowPlacement(900, 0, 900, 600, 0),
            },
        )


def test_unlock_allows_role_edits_and_lock_state_survives_reload(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    before = service.group("14支").entries

    assert service.set_master_locked("14支", True) is True
    assert service.set_master_locked("14支", True) is False
    assert GroupConfigurationService(owned).group(
        "14支"
    ).master_locked is True
    assert service.set_master_locked("14支", False) is True
    assert service.set_main_entry(
        "14支",
        before[1].entry_id,
    ) is True
    assert GroupConfigurationService(owned).group(
        "14支"
    ).master_locked is False


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
    first_group_name = service.groups()[0].name
    service.set_launch_hotkey(first_group_name, "F3")
    service.set_master_locked(first_group_name, True)
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
    assert restored.group(first_group_name).launch_hotkey == "F3"
    assert restored.group(first_group_name).master_locked is True
    assert (
        restored.group(first_group_name).group_id
        == service.group(first_group_name).group_id
    )
    assert (
        restored.group("14支").entries[0].shortcut_path
        == service.group("14支").entries[0].shortcut_path
    )


def test_legacy_launch_hotkey_display_is_imported_without_modifying_source(
    tmp_path,
):
    shortcut = _shortcut(tmp_path, "legacy")
    legacy = tmp_path / "legacy-hotkey.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "legacy",
                        "launch_hotkey_display": "f4",
                        "launch_entries": [{"path": str(shortcut)}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    original = legacy.read_bytes()

    service = GroupConfigurationService(
        tmp_path / "owned.json",
        legacy_config_path=legacy,
    )

    assert service.group("legacy").launch_hotkey == "F4"
    assert legacy.read_bytes() == original


def test_each_group_keeps_one_unique_launch_hotkey(tmp_path):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    service.create_group("120")

    assert service.set_launch_hotkey("14支", "f2") is True
    assert service.group("14支").launch_hotkey == "F2"
    assert service.launch_hotkeys() == {"14支": "F2"}
    with pytest.raises(
        GroupHotkeyConflictError,
        match=GroupHotkeyConflictError.player_message,
    ):
        service.set_launch_hotkey("120", "F2")

    restored = GroupConfigurationService(owned)
    assert restored.group("14支").launch_hotkey == "F2"
    assert restored.group("120").launch_hotkey == ""


def test_import_rejects_reserved_feature_hotkey_without_changing_owned(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    imported_shortcut = _shortcut(tmp_path, "120古")
    imported = tmp_path / "reserved-hotkey.json"
    imported.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "120",
                        "launch_hotkey": "F1",
                        "launch_entries": [
                            {"path": str(imported_shortcut)}
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = owned.read_bytes()

    with pytest.raises(ValueError, match="reserved hotkey"):
        service.import_configuration(
            imported,
            reserved_hotkeys=("F1",),
        )

    assert owned.read_bytes() == before
    assert service.group("120") is None


def test_sync_offset_delay_base_point_and_role_id_survive_reload(tmp_path):
    legacy, _first, _second = _legacy(tmp_path)
    owned = tmp_path / "groups.json"
    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    entry = service.group("14支").entries[1]

    assert service.set_sync_base_point("14支", (100, 200)) is True
    assert service.set_sync_target_settings(
        "14支",
        entry.entry_id,
        offset_enabled=True,
        offset_x=-35,
        offset_y=12,
        delay_ms=450,
    ) is True
    assert service.set_role_id(
        "14支",
        entry.entry_id,
        "  001|角色_甲 ",
    ) is True

    restored = GroupConfigurationService(owned).group("14支")
    restored_entry = restored.entries[1]
    assert restored.sync_base_point == (100, 200)
    assert restored_entry.sync_settings.offset_enabled is True
    assert restored_entry.sync_settings.offset_x == -35
    assert restored_entry.sync_settings.offset_y == 12
    assert restored_entry.sync_settings.delay_ms == 450
    assert restored_entry.role_id == "001|角色_甲"


def test_legacy_launch_delay_seeds_sync_delay_and_clear_resets_only_sync(
    tmp_path,
):
    legacy, _first, _second = _legacy(tmp_path)
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    payload["groups"][0]["launch_entries"][1].update(
        {
            "x": 0,
            "y": 0,
            "width": 916,
            "height": 629,
            "delay_ms": 8_000,
        }
    )
    legacy.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    service = GroupConfigurationService(
        tmp_path / "owned.json",
        legacy_config_path=legacy,
    )
    entry = service.group("14支").entries[1]

    assert entry.sync_settings.delay_ms == 5_000
    assert service.clear_sync_target_settings(
        "14支",
        entry.entry_id,
    ) is True
    cleared = service.group("14支").entries[1]
    assert cleared.sync_settings.delay_ms == 0
    assert cleared.placement.delay_ms == 8_000


def test_v02_migration_preserves_safe_unknown_fields_and_source_hash(
    tmp_path,
):
    shortcut = _shortcut(tmp_path, "中文角色")
    legacy = tmp_path / "sync_launch_config.json"
    legacy.write_text(
        json.dumps(
            {
                "app_state": {
                    "machine_id": "保留的舊版機器識別",
                    "active_group_name": "中文組",
                    "window_geometry": "900x600+10+20",
                    "section_visibility": {
                        "group": True,
                        "token": "不得搬移",
                    },
                },
                "future_root": {"enabled": True},
                "groups": [
                    {
                        "name": "中文組",
                        "custom_key_display": "F2",
                        "sync_keyboard_enabled": True,
                        "keyboard_key_displays": ["C", "CTRL"],
                        "future_group": {"mode": "保留"},
                        "launch_entries": [
                            {
                                "path": str(shortcut),
                                "role": "主窗口",
                                "x": -1200,
                                "y": 50,
                                "width": 916,
                                "height": 629,
                                "delay_ms": 250,
                                "future_entry": {
                                    "value": 7,
                                    "password": "不得搬移",
                                },
                            }
                        ],
                    },
                    {
                        "name": "空白組",
                        "future_group": "仍保留",
                        "launch_entries": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    owned = tmp_path / "data" / "group_configuration.json"

    service = GroupConfigurationService(
        owned,
        legacy_config_path=legacy,
    )
    payload = json.loads(owned.read_text(encoding="utf-8"))

    assert legacy.read_bytes() == before
    assert payload["schema_version"] == 2
    assert payload["future_root"] == {"enabled": True}
    assert payload["app_state"]["active_group_name"] == "中文組"
    assert payload["app_state"]["window_geometry"] == "900x600+10+20"
    assert payload["app_state"]["section_visibility"] == {"group": True}
    assert (
        payload["app_state"]["machine_id"]
        == "保留的舊版機器識別"
    )
    assert tuple(group.name for group in service.groups()) == (
        "中文組",
        "空白組",
    )
    assert payload["groups"][0]["custom_key_display"] == "F2"
    assert payload["groups"][0]["sync_keyboard_enabled"] is True
    assert payload["groups"][0]["keyboard_key_displays"] == ["C", "CTRL"]
    assert payload["groups"][0]["future_group"] == {"mode": "保留"}
    assert payload["groups"][0]["launch_entries"][0]["future_entry"] == {
        "value": 7
    }
    assert payload["groups"][1]["future_group"] == "仍保留"
    assert service.migration_backup_path is not None
    backup_payload = json.loads(
        service.migration_backup_path.read_text(encoding="utf-8")
    )
    assert backup_payload == payload


def test_schema_one_migration_creates_exact_recovery_backup(
    tmp_path,
):
    shortcut = _shortcut(tmp_path, "舊設定角色")
    owned = tmp_path / "group_configuration.json"
    owned.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owned_extra": {"keep": "yes"},
                "groups": [
                    {
                        "name": "舊設定",
                        "future_group": 8,
                        "launch_entries": [
                            {
                                "path": str(shortcut),
                                "future_entry": "保留",
                            }
                        ],
                    }
                ],
                "sync_edges": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = owned.read_bytes()

    service = GroupConfigurationService(owned)
    payload = json.loads(owned.read_text(encoding="utf-8"))

    assert service.migration_backup_path is not None
    assert service.migration_backup_path.read_bytes() == before
    assert payload["schema_version"] == 2
    assert payload["owned_extra"] == {"keep": "yes"}
    assert payload["groups"][0]["future_group"] == 8
    assert payload["groups"][0]["launch_entries"][0]["future_entry"] == "保留"


def test_corrupt_owned_configuration_recovers_from_last_valid_backup(
    tmp_path,
):
    shortcut = _shortcut(tmp_path, "可回復角色")
    owned = tmp_path / "group_configuration.json"
    backup = tmp_path / "group_configuration.json.bak"
    valid = {
        "schema_version": 2,
        "groups": [
            {
                "name": "可回復組",
                "launch_entries": [{"path": str(shortcut)}],
            }
        ],
        "sync_edges": {},
    }
    backup.write_text(
        json.dumps(valid, ensure_ascii=False),
        encoding="utf-8",
    )
    owned.write_text("{broken", encoding="utf-8")

    service = GroupConfigurationService(owned)

    assert service.recovered_from_backup is True
    assert service.corrupt_backup_path is not None
    assert service.corrupt_backup_path.read_text(encoding="utf-8") == "{broken"
    assert service.group("可回復組") is not None
    assert json.loads(owned.read_text(encoding="utf-8"))["schema_version"] == 2


def test_failed_schema_migration_keeps_original_and_recovery_copy(
    tmp_path,
    monkeypatch,
):
    shortcut = _shortcut(tmp_path, "失敗回復角色")
    owned = tmp_path / "group_configuration.json"
    owned.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "groups": [
                    {
                        "name": "失敗回復組",
                        "launch_entries": [{"path": str(shortcut)}],
                    }
                ],
                "sync_edges": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    before = owned.read_bytes()
    original_writer = GroupConfigurationService._write_json_atomic.__func__

    def fail_owned_write(cls, path, payload):
        if path == owned:
            raise OSError("simulated migration failure")
        return original_writer(cls, path, payload)

    monkeypatch.setattr(
        GroupConfigurationService,
        "_write_json_atomic",
        classmethod(fail_owned_write),
    )

    with pytest.raises(OSError, match="simulated migration failure"):
        GroupConfigurationService(owned)

    backups = tuple(tmp_path.glob("group_configuration.json.pre-migration*"))
    assert owned.read_bytes() == before
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_export_import_keeps_safe_root_unknown_fields(tmp_path):
    legacy, _first, _second = _legacy(tmp_path)
    legacy_payload = json.loads(legacy.read_text(encoding="utf-8"))
    legacy_payload["future_root"] = {"value": "保留"}
    legacy.write_text(
        json.dumps(legacy_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    source = GroupConfigurationService(
        tmp_path / "source.json",
        legacy_config_path=legacy,
    )
    exported = source.export_configuration(tmp_path / "exported.json")
    restored = GroupConfigurationService(tmp_path / "restored.json")

    restored.import_configuration(exported)
    payload = json.loads(restored.path.read_text(encoding="utf-8"))

    assert payload["future_root"] == {"value": "保留"}
