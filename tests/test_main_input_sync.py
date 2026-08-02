import json
from pathlib import Path

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from config.config_manager import ConfigManager
from core.sp1_boundaries import SmartReconnectBoundary
from main import (
    GAME_TIME_AUTO_UPDATE_KEY,
    GAME_TIME_OFFSET_MS_KEY,
    INPUT_POLICY_KEY,
    SMART_RECONNECT_ENABLED_KEY,
    SMART_RECONNECT_CONSENT_KEY,
    SMART_RECONNECT_INTERVAL_MS_KEY,
    SMART_RECONNECT_INTERVAL_MIGRATION_KEY,
    SYNC_KEYS_COLLAPSED_KEY,
    TIMED_CLICK_SETTINGS_KEY,
    UI_THEME_CLASSIC_GOLD_MIGRATION_KEY,
    UI_THEME_KEY,
    _sync_scope_has_all_safe_windows,
    build_services,
    stop_input_sync_pair,
)
from services.app_context import AppContext
from services.smart_reconnect_monitor import SmartReconnectMonitor
from services.smart_reconnect_capture_settings_service import (
    SMART_RECONNECT_CAPTURE_MODES_KEY,
    SmartReconnectCaptureSettings,
    SmartReconnectCaptureSettingsService,
)
from services.group_role_status_service import GroupRoleStatusService
from services.game_operation_gate import GameOperationGate


class _SyncWindow:
    def __init__(self, fingerprint: str) -> None:
        self.launch_fingerprint = fingerprint


def test_sync_scope_requires_every_safe_window_with_matching_identity():
    first = "a" * 64
    second = "b" * 64

    assert _sync_scope_has_all_safe_windows(
        (first, second),
        (_SyncWindow(first), _SyncWindow(second)),
    )
    assert not _sync_scope_has_all_safe_windows(
        (first, second),
        (_SyncWindow(first),),
    )
    assert not _sync_scope_has_all_safe_windows(
        (first, second),
        (_SyncWindow(first), _SyncWindow(first)),
    )


def test_build_services_registers_input_controller_and_safe_default(tmp_path):
    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    controller = AppContext.get(WindowsInputSyncController)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    reconnect_boundary = AppContext.get(SmartReconnectBoundary)
    reconnect_monitor = AppContext.get(SmartReconnectMonitor)
    capture_settings_service = AppContext.get(
        SmartReconnectCaptureSettingsService
    )

    assert config.get(INPUT_POLICY_KEY) == WindowInputPolicy.ALL.value
    assert config.get(SMART_RECONNECT_ENABLED_KEY) is False
    assert config.get(SMART_RECONNECT_CONSENT_KEY) is False
    assert config.get(SMART_RECONNECT_INTERVAL_MS_KEY) == 2000
    assert config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is True
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == {
        "visible": True,
        "obscured": True,
        "minimized": True,
    }
    assert config.get(GAME_TIME_OFFSET_MS_KEY) == 0
    assert config.get(GAME_TIME_AUTO_UPDATE_KEY) is True
    assert config.get(SYNC_KEYS_COLLAPSED_KEY) is True
    assert config.get(UI_THEME_KEY) == "classic_gold"
    assert config.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is True
    assert config.get(TIMED_CLICK_SETTINGS_KEY) == {
        "target_time": "",
        "lead_ms": 120,
        "repeat_count": 2,
        "repeat_interval_ms": 250,
    }
    assert isinstance(controller, WindowsInputSyncController)
    assert isinstance(reconnect, WindowsSmartReconnectController)
    assert reconnect_boundary is reconnect
    assert isinstance(reconnect_monitor, SmartReconnectMonitor)
    assert reconnect_monitor.monitor_interval_ms == 2000
    assert (
        capture_settings_service.snapshot()
        == SmartReconnectCaptureSettings()
    )
    assert reconnect.capture_settings == SmartReconnectCaptureSettings()


def test_existing_theme_migrates_to_gold_once_without_overriding_later_choice(
    tmp_path,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = config_dir / "settings.json"
    settings.write_text(
        json.dumps({UI_THEME_KEY: "clear_blue"}, ensure_ascii=False),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    assert config.get(UI_THEME_KEY) == "classic_gold"
    assert config.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is True

    config.set(UI_THEME_KEY, "forest_green")
    build_services(root=tmp_path)

    reloaded = AppContext.get(ConfigManager)
    assert reloaded.get(UI_THEME_KEY) == "forest_green"
    assert reloaded.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is True


def test_stop_sync_pair_reports_actual_partial_cleanup_without_false_success():
    class Monitor:
        def __init__(self, *, fail_stop=False):
            self.enabled = True
            self.fail_stop = fail_stop

        def start(self):
            self.enabled = True
            return True

        def stop(self):
            if self.fail_stop:
                return False
            self.enabled = False
            return True

    keyboard = Monitor(fail_stop=True)
    mouse = Monitor()

    assert stop_input_sync_pair(keyboard, mouse) is False
    assert keyboard.enabled is True
    assert mouse.enabled is False

    keyboard.fail_stop = False
    mouse.start()
    assert stop_input_sync_pair(keyboard, mouse) is True
    assert keyboard.enabled is False
    assert mouse.enabled is False


def test_smart_reconnect_monitor_restores_saved_interval(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {SMART_RECONNECT_INTERVAL_MS_KEY: 2750},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    monitor = AppContext.get(SmartReconnectMonitor)
    assert config.get(SMART_RECONNECT_INTERVAL_MS_KEY) == 2750
    assert config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is True
    assert monitor.monitor_interval_ms == 2750


def test_smart_reconnect_controller_restores_saved_capture_modes(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    expected = {
        "visible": True,
        "obscured": False,
        "minimized": True,
    }
    (config_dir / "settings.json").write_text(
        json.dumps(
            {SMART_RECONNECT_CAPTURE_MODES_KEY: expected},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    service = AppContext.get(SmartReconnectCaptureSettingsService)
    controller = AppContext.get(WindowsSmartReconnectController)
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == expected
    assert service.snapshot().to_dict() == expected
    assert controller.capture_settings.to_dict() == expected


def test_old_default_reconnect_interval_migrates_once_to_balanced_default(
    tmp_path,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {SMART_RECONNECT_INTERVAL_MS_KEY: 1000},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    monitor = AppContext.get(SmartReconnectMonitor)
    assert config.get(SMART_RECONNECT_INTERVAL_MS_KEY) == 2000
    assert config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is True
    assert monitor.monitor_interval_ms == 2000


def test_sync_services_share_lifecycle_backend_with_separate_target_contracts(
    tmp_path,
):
    build_services(root=tmp_path)

    keyboard = AppContext.get(WindowsInputSyncController)
    pointer = AppContext.get(WindowsPointerSyncController)
    statuses = AppContext.get(GroupRoleStatusService)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    gate = AppContext.get(GameOperationGate)

    assert keyboard._window_backend is pointer._window_backend
    assert keyboard._window_backend is statuses._window_backend
    assert keyboard._target_windows_provider is pointer._target_windows_provider
    assert keyboard._target_windows_provider is not reconnect._target_windows_provider
    assert statuses._target_snapshot_provider is not None
    assert keyboard._operation_gate is gate
    assert pointer._operation_gate is gate
    assert reconnect._operation_gate is gate
    assert statuses._operation_gate is gate


def test_main_window_polling_uses_a_throttled_current_group_handle_cache():
    source = Path("main.py").read_text(encoding="utf-8")

    assert source.count(
        "target_handles_provider=current_target_handles"
    ) == 2
    assert source.count(
        'execution_enabled_provider=lambda: bool('
    ) == 2
    assert 'sync_session_state["enabled"] = False' in source
    assert 'sync_source_handle_cache: dict[str, object]' in source
    assert '"expires_at": now + 0.25' in source
    assert "target_windows_provider=current_sync_target_windows" in source
    assert source.count("operation_record_store.append_deferred(") >= 3


def test_home_exposes_three_policies_and_complete_confirmed_shortcuts():
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "僅允許前台" in source
    assert "允許前台與背景" in source
    assert "全部允許（含最小化）" in source
    assert "開始同步視窗" in source
    assert "停止同步視窗" in source
    assert "CONFIRMED_GAME_SHORTCUTS" in source
    assert "測試 B" not in source


def test_sync_key_collapsed_state_is_loaded_saved_and_wired_to_home():
    source = Path("main.py").read_text(encoding="utf-8")

    assert 'SYNC_KEYS_COLLAPSED_KEY = "sync_keys_collapsed"' in source
    assert "def change_sync_keys_collapsed(" in source
    assert "config.set(SYNC_KEYS_COLLAPSED_KEY, bool(collapsed))" in source
    assert "sync_keys_collapsed=configured_sync_keys_collapsed" in source
    assert (
        "on_sync_keys_collapsed_change=change_sync_keys_collapsed"
        in source
    )


def test_smart_reconnect_interval_uses_legacy_key_and_is_saved():
    source = Path("main.py").read_text(encoding="utf-8")

    assert (
        'SMART_RECONNECT_INTERVAL_MS_KEY = "disconnect_detect_interval_ms"'
        in source
    )
    assert "def change_smart_reconnect_interval(" in source
    assert (
        "config.set(SMART_RECONNECT_INTERVAL_MS_KEY, normalized)"
        in source
    )
    assert "smart_reconnect_interval_ms=(" in source
    assert (
        "on_smart_reconnect_interval_change=("
        in source
    )


def test_smart_reconnect_capture_modes_use_one_formal_persistent_entrypoint():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "SmartReconnectCaptureSettingsService(config)" in source
    assert "def change_smart_reconnect_capture_modes(" in source
    assert (
        "smart_reconnect_capture_settings_service.update(modes)"
        in source
    )
    assert "smart_reconnect_controller.set_capture_settings(settings)" in source
    assert "smart_reconnect_capture_modes=(" in source
    assert "on_smart_reconnect_capture_modes_change=(" in source


def test_group_member_continuous_click_never_falls_back_to_one_window():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "block_physical_fallback=lambda source:" in source
    assert (
        "pointer_sync_controller.source_must_block_physical_fallback("
        in source
    )


def test_group_change_and_group_edit_stop_continuous_click_immediately():
    source = Path("main.py").read_text(encoding="utf-8")

    assert source.count("auto_click_service.stop()") >= 2


def test_group_change_stops_all_automation_before_publishing_new_group():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]

    close_index = change_group.index(
        "close_group_operation_gate()"
    )
    stop_index = change_group.index(
        "stop_group_automation_for_configuration_change()"
    )
    apply_index = change_group.index("apply_group_identity(choice)")
    workspace_index = change_group.index(
        "workspace_service.set_current_group("
    )
    config_index = change_group.index(
        "config.set(CURRENT_GROUP_NAME_KEY"
    )
    reopen_index = change_group.rindex(
        "reopen_group_operation_gate()"
    )

    assert (
        close_index
        < stop_index
        < apply_index
        < config_index
        < workspace_index
        < reopen_index
    )


def test_group_change_allows_selection_but_clears_identity_when_unresolved():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]

    apply_index = change_group.index("apply_group_identity(choice)")
    clear_index = change_group.index("clear_group_identity()")
    config_index = change_group.index("config.set(CURRENT_GROUP_NAME_KEY")
    workspace_index = change_group.index(
        "workspace_service.set_current_group("
    )

    assert "identity_ready = apply_group_identity(choice) is not None" in change_group
    assert "if not identity_ready:" in change_group
    assert apply_index < clear_index < config_index < workspace_index
    assert "同步與智慧重連已保持停用" in change_group


def test_failed_group_change_clears_unbound_identity_before_reopening_gate():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]
    rollback_start = change_group.index(
        "        except Exception:",
        change_group.index("selected_workspace_group = "),
    )
    rollback_end = change_group.index(
        "        reopen_group_operation_gate()",
        rollback_start,
    ) + len("        reopen_group_operation_gate()")
    rollback = change_group[
        rollback_start:
        rollback_end
    ]

    restore_index = rollback.index("restore_group_identity(old_choice)")
    clear_index = rollback.index("clear_group_identity()")
    publish_index = rollback.index("restore_published_group(")
    reopen_index = rollback.index("reopen_group_operation_gate()")

    assert restore_index < clear_index < publish_index < reopen_index
    assert "if rollback_ready and publication_restored:" in rollback


def test_role_identity_refresh_only_rebinds_current_group_and_reopens_gate():
    source = Path("main.py").read_text(encoding="utf-8")
    refresh = source[
        source.index("    def refresh_group_sync_identity("):
        source.index("    def capture_sync_base_point(")
    ]

    assert "config.get(CURRENT_GROUP_NAME_KEY" in refresh
    assert "!= group_name" in refresh
    assert "close_group_operation_gate()" in refresh
    assert "finally:" in refresh
    assert "reopen_group_operation_gate()" in refresh


def test_input_verifier_has_a_bounded_delay_for_real_foreground_testing():
    source = Path("scripts/verify_input_sync_sp1.py").read_text(
        encoding="utf-8"
    )

    assert "--delay-seconds" in source
    assert "between 0 and 30" in source
    assert "--activate-one-for-foreground-test" in source
    assert 'window.window_class == "ShockwaveFlash"' in source
    assert "_SnapshotWindowBackend(validated_windows)" in source
    assert "resolve_fingerprints=True" in source


def test_input_verifier_restores_its_temporary_minimized_subset():
    source = Path("scripts/verify_input_sync_sp1.py").read_text(
        encoding="utf-8"
    )

    assert "--minimize-count-for-test" in source
    assert "resolve_fingerprints=True" in source
    assert "finally:" in source
    assert "_restore_flash_windows(restore_handles)" in source
