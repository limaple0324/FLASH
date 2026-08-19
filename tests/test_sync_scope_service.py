import hashlib
import json

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from services.group_configuration_service import GroupConfigurationService
from services.sync_scope_service import SyncScopeService


class Resolver:
    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
        }


class SharedResolver:
    def resolve(self, paths):
        return {path: "a" * 64 for path in paths}


class PartialResolver:
    def __init__(self, unavailable=()):
        self.unavailable = {path.resolve() for path in unavailable}

    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
            if path.resolve() not in self.unavailable
        }


def _shortcut(tmp_path, name):
    path = tmp_path / f"{name}.lnk"
    path.write_bytes(b"shortcut")
    return path


def test_current_group_scope_ignores_saved_cross_group_relations(tmp_path):
    a, b, c, d, e, f = [
        _shortcut(tmp_path, name)
        for name in ("甲", "乙", "丙", "丁", "戊", "己")
    ]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "第一組",
                        "launch_entries": [
                            {"path": str(path)}
                            for path in (a, b, c, d)
                        ],
                    },
                    {
                        "name": "第二組",
                        "launch_entries": [
                            {"path": str(path)}
                            for path in (b, e, f)
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    configuration = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    scope = SyncScopeService(configuration, Resolver()).scope("第一組")
    current_group = configuration.group("第一組")
    controller = current_group.main_entry.entry_id

    assert scope.ready is True
    assert scope.entry_ids == (
        controller,
        *(
            entry.entry_id
            for entry in current_group.entries
            if entry.entry_id != controller
        ),
    )
    assert len(scope.fingerprints) == 4
    assert len(configuration.expanded_sync_members(controller)) == 5


def test_configured_scope_still_keeps_every_group_for_reconnect(tmp_path):
    first = _shortcut(tmp_path, "甲")
    second = _shortcut(tmp_path, "乙")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("甲組", (first,))
    configuration.add_shortcuts("乙組", (second,))
    first_id = configuration.group("甲組").main_entry.entry_id
    second_id = configuration.group("乙組").main_entry.entry_id
    configuration.add_sync_relation(first_id, second_id)
    service = SyncScopeService(configuration, Resolver())

    assert service.scope("甲組").entry_ids == (first_id,)
    assert service.configured_scope().entry_ids == (first_id, second_id)


def test_configured_scope_isolates_one_bad_shortcut_without_blocking_reconnect(
    tmp_path,
):
    first = _shortcut(tmp_path, "可用角色")
    second = _shortcut(tmp_path, "失效角色")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("甲組", (first,))
    configuration.add_shortcuts("乙組", (second,))
    first_id = configuration.group("甲組").main_entry.entry_id
    second_id = configuration.group("乙組").main_entry.entry_id
    service = SyncScopeService(
        configuration,
        PartialResolver((second,)),
    )

    scope = service.configured_scope()

    assert scope.ready is True
    assert scope.failure_codes == ()
    assert scope.entry_ids == (first_id, second_id)
    assert scope.isolated_entry_ids == (second_id,)
    assert len(scope.entry_fingerprints) == 2
    assert scope.entry_fingerprints[0] == hashlib.sha256(
        str(first).encode()
    ).hexdigest()
    assert normalize_launch_fingerprint(scope.entry_fingerprints[1]) is not None
    assert scope.entry_fingerprints[1] != scope.entry_fingerprints[0]


def test_selected_group_controller_still_fails_closed_when_its_identity_is_missing(
    tmp_path,
):
    first = _shortcut(tmp_path, "主控")
    second = _shortcut(tmp_path, "跟隨")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("測試組", (first, second))
    service = SyncScopeService(
        configuration,
        PartialResolver((first,)),
    )

    selected = service.scope("測試組")
    configured = service.configured_scope()

    assert selected.ready is False
    assert selected.failure_codes == ("shortcut_identity_unresolved",)
    assert configured.ready is True
    assert configuration.group("測試組").main_entry.entry_id in (
        configured.isolated_entry_ids
    )


def test_scope_keeps_shared_launcher_digests_until_live_instance_binding(
    tmp_path,
):
    first, second = [_shortcut(tmp_path, name) for name in ("甲", "乙")]
    legacy = tmp_path / "shared-launcher.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "共用啟動檔組",
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
    configuration = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )

    scope = SyncScopeService(configuration, SharedResolver()).scope(
        "共用啟動檔組"
    )

    assert scope.ready is True
    assert len(scope.entry_ids) == 2
    assert scope.fingerprints == ("a" * 64, "a" * 64)


def test_reordering_launch_sequence_does_not_change_sync_controller(
    tmp_path,
):
    first, second, third = [
        _shortcut(tmp_path, name)
        for name in ("主控", "同步甲", "同步乙")
    ]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "排序組",
                        "master_locked": False,
                        "launch_entries": [
                            {"path": str(first), "role": "主窗口"},
                            {"path": str(second), "role": "同步窗口"},
                            {"path": str(third), "role": "同步窗口"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    configuration = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    group = configuration.group("排序組")
    controller = group.main_entry.entry_id
    proposed = tuple(
        entry.entry_id for entry in reversed(group.entries)
    )

    assert configuration.reorder_group_entries(
        "排序組",
        proposed,
    ) is True
    scope = SyncScopeService(configuration, Resolver()).scope("排序組")

    assert scope.ready is True
    assert scope.controller_entry_id == controller
    assert scope.entry_ids[0] == controller
    assert configuration.group("排序組").entries[0].entry_id != controller
