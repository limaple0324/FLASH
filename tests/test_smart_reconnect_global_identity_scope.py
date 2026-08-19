import hashlib

from main import build_configured_reconnect_plan
from services.group_configuration_service import GroupConfigurationService
from services.sync_scope_service import SyncScopeService


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


def test_global_reconnect_plan_survives_one_unresolved_saved_shortcut(tmp_path):
    usable = _shortcut(tmp_path, "可用角色")
    broken = _shortcut(tmp_path, "失效角色")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("可用組", (usable,))
    configuration.add_shortcuts("失效組", (broken,))
    service = SyncScopeService(
        configuration,
        PartialResolver((broken,)),
    )

    scope = service.configured_scope()
    plan = build_configured_reconnect_plan(
        scope,
        configuration.groups(),
        (),
        (),
    )

    assert scope.ready is True
    assert len(scope.isolated_entry_ids) == 1
    assert plan is not None
    assert plan.ready is True
    assert len(plan.targets) == 2
    assert plan.targets[0].fingerprint == hashlib.sha256(
        str(usable).encode()
    ).hexdigest()
    assert plan.targets[1].fingerprint == scope.entry_fingerprints[1]
    assert plan.targets[0].fingerprint != plan.targets[1].fingerprint
