import hashlib

from adapters.windows_window import WindowInfo
from core.window_registry import WindowRegistry
from main import build_configured_reconnect_plan
from services.group_configuration_service import GroupConfigurationService
from services.sync_scope_service import SyncScopeService
from services.target_window_contract_service import TargetWindowContractService


class PartialResolver:
    def __init__(self, unavailable=()):
        self.unavailable = {path.resolve() for path in unavailable}

    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
            if path.resolve() not in self.unavailable
        }


class WindowBackend:
    def __init__(self, windows=(), foreground=None):
        self.windows = tuple(windows)
        self.foreground = foreground

    def list_windows(self):
        return self.windows

    def foreground_handle(self):
        return self.foreground


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


def test_one_bad_saved_shortcut_does_not_remove_a_live_manual_flash_target(
    tmp_path,
):
    usable = _shortcut(tmp_path, "手動開啟角色")
    broken = _shortcut(tmp_path, "失效舊角色")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("可用組", (usable,))
    configuration.add_shortcuts("失效組", (broken,))
    usable_entry_id = configuration.group("可用組").main_entry.entry_id
    broken_entry_id = configuration.group("失效組").main_entry.entry_id
    resolver = PartialResolver((broken,))
    scope_service = SyncScopeService(configuration, resolver)
    usable_fingerprint = hashlib.sha256(str(usable).encode()).hexdigest()
    manual_window = WindowInfo(
        handle=11,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 900, 600),
        process_id=101,
        window_class="ShockwaveFlash",
        launch_fingerprint=usable_fingerprint,
        thread_id=1001,
        process_lifecycle_token=100001,
    )
    contract = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        WindowBackend((manual_window,), foreground=11),
    )

    resolved = contract.configured_reconnect_targets()
    plan = build_configured_reconnect_plan(
        contract.configured_scope(),
        configuration.groups(),
        (),
        (),
    )

    assert resolved.global_failure_codes == ()
    assert tuple(window.handle for window in resolved.windows) == (11,)
    assert resolved.sync_entry_ids == (usable_entry_id,)
    assert len(resolved.target_failure_evidence) == 1
    assert resolved.target_failure_evidence[0].entry_id == broken_entry_id
    assert resolved.target_failure_evidence[0].failure_codes == ("window_offline",)
    assert plan is not None
    assert plan.ready is True
