import hashlib
import json

from services.group_configuration_service import GroupConfigurationService
from services.sync_scope_service import SyncScopeService


class Resolver:
    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
        }


def _shortcut(tmp_path, name):
    path = tmp_path / f"{name}.lnk"
    path.write_bytes(b"shortcut")
    return path


def test_scope_recursively_expands_cross_group_and_deduplicates(tmp_path):
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

    assert scope.ready is True
    assert scope.entry_ids == (
        configuration.group("第一組").entries[0].entry_id,
        *configuration.expanded_sync_members(
            configuration.group("第一組").entries[0].entry_id
        ),
    )
    assert len(scope.fingerprints) == 6
    assert len(scope.fingerprints) == len(set(scope.fingerprints))
