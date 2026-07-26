import json

import pytest

from services.group_configuration_service import (
    GroupConfigurationService,
    SyncCycleError,
)


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
