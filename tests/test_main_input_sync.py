import json
from pathlib import Path
from types import SimpleNamespace

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from config.config_manager import ConfigManager
from core.sp1_boundaries import SmartReconnectBoundary
from core.window_registry import CharacterWindowRecord
from domain.character import Character, CharacterImportance
from main import (
    GAME_TIME_AUTO_UPDATE_KEY,
    GAME_TIME_OFFSET_MS_KEY,
    INPUT_POLICY_KEY,
    SMART_RECONNECT_ENABLED_KEY,
    SMART_RECONNECT_CONSENT_KEY,
    SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY,
    SMART_RECONNECT_INTERVAL_MS_KEY,
    SMART_RECONNECT_INTERVAL_MIGRATION_KEY,
    SYNC_KEYS_COLLAPSED_KEY,
    TIMED_CLICK_SETTINGS_KEY,
    UI_THEME_CLASSIC_GOLD_MIGRATION_KEY,
    UI_THEME_KEY,
    _connected_sync_fingerprints,
    _rebuild_group_input_identity,
    _sync_scope_has_all_safe_windows,
    build_services,
    apply_auto_battle_after_game_launch,
    apply_smart_reconnect_auto_battle_setting,
    apply_smart_reconnect_snapshot_transition,
    normalize_smart_reconnect_auto_battle_enabled,
    resolve_registered_reconnect_roles,
    group_role_action_started_game,
    group_window_launch_started_game,
    stop_input_sync_pair,
)
from services.app_context import AppContext
from services.smart_reconnect_monitor import SmartReconnectMonitor
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentityService,
)
from services.ungrouped_window_service import UngroupedWindowService
from services.smart_reconnect_capture_settings_service import (
    SMART_RECONNECT_CAPTURE_MODES_KEY,
    SmartReconnectCaptureSettings,
    SmartReconnectCaptureSettingsService,
)
from services.group_role_status_service import (
    GroupRoleActionResult,
    GroupRoleStatusService,
)
from services.group_window_launch_service import GroupWindowLaunchResult
from services.game_operation_gate import GameOperationGate


class _SyncWindow:
    def __init__(self, fingerprint: str) -> None:
        self.launch_fingerprint = fingerprint


def test_registered_reconnect_roles_cross_check_all_confirmed_primary_records():
    names = (
        "120古",
        "120靈",
        "120射",
        "120福",
        "120獵",
        "亞洛",
        "160帥",
        "大排",
        "和尚",
        "餐廳",
    )
    characters = tuple(
        Character(
            f"id-{index}",
            name,
            120 if name.startswith("120") else 160,
            CharacterImportance.PRIMARY,
        )
        for index, name in enumerate(names)
    )
    registry = tuple(
        CharacterWindowRecord(
            character.character_id,
            character.display_name,
            role="主號",
        )
        for character in characters
    )

    result = resolve_registered_reconnect_roles(
        characters,
        registry,
        (),
    )

    assert {item.role_id for item in result} == set(names)
    assert all(
        item.importance is CharacterImportance.PRIMARY
        for item in result
    )


def test_registered_primary_requires_character_and_registry_identity_agreement():
    characters = (
        Character(
            "id-fu",
            "120福",
            120,
            CharacterImportance.PRIMARY,
        ),
    )
    mismatched = (
        CharacterWindowRecord("id-fu", "120古", role="主號"),
    )

    assert resolve_registered_reconnect_roles(
        characters,
        mismatched,
        (),
    ) == ()


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


def test_partial_connected_sync_tracks_three_roles_in_stable_scope_order():
    first = "a" * 64
    second = "b" * 64
    third = "c" * 64
    scope = (first, second, third)

    assert _connected_sync_fingerprints(
        scope,
        (_SyncWindow(third), _SyncWindow(first)),
    ) == (first, third)
    assert _connected_sync_fingerprints(
        scope,
        (_SyncWindow(second),),
    ) == (second,)
    assert _connected_sync_fingerprints(scope, ()) == ()
    assert _connected_sync_fingerprints(
        scope,
        (_SyncWindow(first), _SyncWindow(first), _SyncWindow(third)),
    ) == (third,)


def test_group_identity_failure_explains_cross_group_ambiguity():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def group_identity_failure_message("):
        source.index("    def clear_group_identity(")
    ]

    assert "共用捷徑延伸到其他組別" in function_source
    assert "無法唯一對應遊戲視窗" in function_source
    assert "維持安全停止" in function_source


def test_smart_reconnect_snapshot_transition_has_no_group_dependency() -> None:
    class Controller:
        def __init__(self):
            self.prepared = 0
            self.execution = []

        def prepare_execution_snapshot(self):
            self.prepared += 1
            return SimpleNamespace(
                success=True,
                message="快照完成",
            )

        def set_execution_enabled(self, value):
            self.execution.append(value)

    controller = Controller()
    started = apply_smart_reconnect_snapshot_transition(
        True,
        controller,
        object(),
        start_monitor=lambda _monitor: SimpleNamespace(success=True),
        stop_monitor=lambda _monitor: SimpleNamespace(success=True),
    )

    assert started.success is True
    assert controller.prepared == 1
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("def apply_smart_reconnect_snapshot_transition("):
        source.index("def resolve_group_role_progress_subject_id(")
    ]
    assert "group_selection_service" not in function_source
    assert "set_group_launch_plan" not in function_source
    assert "reopen_missing" not in function_source


def test_smart_reconnect_snapshot_failure_is_returned_without_starting() -> None:
    class Controller:
        def __init__(self):
            self.execution = []

        def prepare_execution_snapshot(self):
            return SimpleNamespace(
                success=False,
                message="目前遊戲視窗身分有衝突，沒有啟用智慧重連。",
            )

        def set_execution_enabled(self, value):
            self.execution.append(value)

    controller = Controller()
    starts = []

    result = apply_smart_reconnect_snapshot_transition(
        True,
        controller,
        object(),
        start_monitor=lambda monitor: starts.append(monitor),
        stop_monitor=lambda _monitor: SimpleNamespace(success=True),
    )

    assert result.success is False
    assert "身分有衝突" in result.message
    assert starts == []
    assert controller.execution == [False]


def test_smart_reconnect_stop_always_revokes_snapshot_authority() -> None:
    class Controller:
        def __init__(self):
            self.execution = []

        def prepare_execution_snapshot(self):
            return SimpleNamespace(success=True, message="")

        def set_execution_enabled(self, value):
            self.execution.append(value)

    controller = Controller()

    result = apply_smart_reconnect_snapshot_transition(
        False,
        controller,
        object(),
        start_monitor=lambda _monitor: SimpleNamespace(success=True),
        stop_monitor=lambda _monitor: SimpleNamespace(success=True),
    )

    assert result.success is True
    assert controller.execution == [False]


def test_smart_reconnect_auto_battle_setting_can_be_enabled_and_disabled():
    class Controller:
        def __init__(self):
            self.values = []

        def set_auto_battle_enabled(self, value):
            self.values.append(value)

    class Config:
        def __init__(self):
            self.values = {}

        def set(self, key, value):
            self.values[key] = value

    controller = Controller()
    config = Config()

    assert apply_smart_reconnect_auto_battle_setting(
        True,
        controller,
        config,
    )
    assert apply_smart_reconnect_auto_battle_setting(
        False,
        controller,
        config,
    )
    assert controller.values == [True, False]
    assert config.values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is False


def test_real_game_launch_enables_saves_and_reflects_auto_battle():
    class Controller:
        def __init__(self):
            self.values = []

        def set_auto_battle_enabled(self, value):
            self.values.append(value)

    class Config:
        def __init__(self):
            self.values = {}

        def set(self, key, value):
            self.values[key] = value

    class View:
        def __init__(self):
            self.values = []

        def set_smart_reconnect_auto_battle_enabled(self, value):
            self.values.append(value)

    controller = Controller()
    config = Config()
    view = View()

    assert not apply_auto_battle_after_game_launch(
        False,
        controller,
        config,
        view,
    )
    assert apply_auto_battle_after_game_launch(
        True,
        controller,
        config,
        view,
    )

    assert controller.values == [True]
    assert config.values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is True
    assert view.values == [True]


def _launch_auto_battle_outputs(result, predicate):
    controller = SimpleNamespace(values=[])
    controller.set_auto_battle_enabled = controller.values.append
    config = SimpleNamespace(values={})
    config.set = config.values.__setitem__
    view = SimpleNamespace(values=[])
    view.set_smart_reconnect_auto_battle_enabled = view.values.append

    applied = apply_auto_battle_after_game_launch(
        predicate(result),
        controller,
        config,
        view,
    )
    return applied, controller.values, config.values, view.values


def test_successful_group_launch_enables_auto_battle() -> None:
    result = GroupWindowLaunchResult(
        True,
        "14支",
        total_count=14,
        launched_count=2,
        restored_count=12,
        action="launch",
    )

    applied, controller_values, config_values, view_values = (
        _launch_auto_battle_outputs(
            result,
            group_window_launch_started_game,
        )
    )

    assert applied is True
    assert controller_values == [True]
    assert config_values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is True
    assert view_values == [True]


def test_partial_failed_group_launch_does_not_enable_auto_battle() -> None:
    result = GroupWindowLaunchResult(
        False,
        "14支",
        total_count=14,
        launched_count=2,
        restored_count=11,
        failure_code="group_window_place_failed",
        action="launch",
    )

    outputs = _launch_auto_battle_outputs(
        result,
        group_window_launch_started_game,
    )

    assert outputs == (False, [], {}, [])


def test_restore_only_group_operation_does_not_enable_auto_battle() -> None:
    result = GroupWindowLaunchResult(
        True,
        "14支",
        total_count=14,
        launched_count=0,
        restored_count=14,
        action="restore",
    )

    outputs = _launch_auto_battle_outputs(
        result,
        group_window_launch_started_game,
    )

    assert outputs == (False, [], {}, [])


def test_single_role_launch_result_requires_successful_real_launch() -> None:
    launched = GroupRoleActionResult(True, action="launched")
    activated = GroupRoleActionResult(True, action="activated")
    failed = GroupRoleActionResult(
        False,
        action="launched",
        failure_code="role_launch_failed",
    )

    applied, controller_values, config_values, view_values = (
        _launch_auto_battle_outputs(
            launched,
            group_role_action_started_game,
        )
    )
    assert applied is True
    assert controller_values == [True]
    assert config_values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is True
    assert view_values == [True]
    assert _launch_auto_battle_outputs(
        activated,
        group_role_action_started_game,
    ) == (False, [], {}, [])
    assert _launch_auto_battle_outputs(
        failed,
        group_role_action_started_game,
    ) == (False, [], {}, [])


def test_only_two_real_launch_results_feed_the_shared_auto_battle_helper():
    source = Path("main.py").read_text(encoding="utf-8")
    group_completion = source[
        source.index("    def complete_group_window_launch("):
        source.index("    def start_group_window_operation(")
    ]
    single_role = source[
        source.index("    def activate_or_launch_group_role("):
        source.index("    home_view = HomeView(")
    ]

    assert "group_window_launch_started_game(result)" in group_completion
    assert "apply_auto_battle_after_game_launch" in group_completion
    assert "group_role_action_started_game(result)" in single_role
    assert "apply_auto_battle_after_game_launch" in single_role


def test_smart_reconnect_enable_callback_never_reads_group_or_launch_plan():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def change_smart_reconnect("):
        source.index("    def change_smart_reconnect_auto_battle(")
    ]

    assert "group_selection_service" not in function_source
    assert "group_launch_service" not in function_source
    assert "current_workspace_group_name" not in function_source
    assert "selected_group_plan" not in function_source
    assert "set_group_launch_plan" not in function_source


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
    assert config.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY) is True
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
    assert reconnect.auto_battle_enabled is True
    assert isinstance(reconnect_monitor, SmartReconnectMonitor)
    assert reconnect_monitor.monitor_interval_ms == 2000
    assert (
        capture_settings_service.snapshot()
        == SmartReconnectCaptureSettings()
    )
    assert reconnect.capture_settings == SmartReconnectCaptureSettings()


def test_build_services_wires_registered_primary_and_unique_ungrouped_shortcut(
    tmp_path,
):
    build_services(root=tmp_path)

    reconnect = AppContext.get(WindowsSmartReconnectController)
    target_identity = AppContext.get(SmartReconnectTargetIdentityService)
    ungrouped = AppContext.get(UngroupedWindowService)
    shortcut_provider = reconnect._ungrouped_shortcut_provider

    assert callable(reconnect._registered_role_provider)
    assert reconnect._target_identity_provider.__self__ is target_identity
    assert (
        reconnect._target_identity_provider.__func__
        is SmartReconnectTargetIdentityService.target_for
    )
    assert reconnect._verified_slot_recorder.__self__ is target_identity
    assert (
        reconnect._verified_slot_recorder.__func__
        is SmartReconnectTargetIdentityService.remember_verified_slot
    )
    assert reconnect._verified_line_recorder.__self__ is target_identity
    assert (
        reconnect._verified_line_recorder.__func__
        is SmartReconnectTargetIdentityService.remember_verified_line
    )
    assert shortcut_provider is not None
    assert shortcut_provider.__self__ is ungrouped
    assert shortcut_provider.__func__ is UngroupedWindowService.shortcut_for


def test_build_services_preserves_explicit_saved_auto_battle_off(tmp_path):
    build_services(root=tmp_path)
    config = AppContext.get(ConfigManager)
    config.set(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY, False)

    build_services(root=tmp_path)
    reconnect = AppContext.get(WindowsSmartReconnectController)

    assert reconnect.auto_battle_enabled is False


def test_invalid_legacy_auto_battle_value_self_heals_to_on(tmp_path):
    build_services(root=tmp_path)
    config = AppContext.get(ConfigManager)
    config.set(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY, "invalid")

    build_services(root=tmp_path)
    reloaded = AppContext.get(ConfigManager)
    reconnect = AppContext.get(WindowsSmartReconnectController)

    assert normalize_smart_reconnect_auto_battle_enabled(None) is True
    assert normalize_smart_reconnect_auto_battle_enabled(False) is False
    assert reloaded.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY) is True
    assert reconnect.auto_battle_enabled is True


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


def test_sync_toggle_returns_direct_card_feedback_for_every_branch():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def change_keyboard_sync("):
        source.index("    def change_smart_reconnect(")
    ]

    assert "-> SyncToggleViewResult:" in function_source
    assert function_source.count("SyncToggleViewResult(") >= 7
    assert "同步中｜同步左鍵、拖曳與已確認快捷鍵" in function_source
    assert "同步已停止；背景清理仍在完成中。" in function_source
    assert "未能啟動；同步沒有啟用。" in function_source
    assert "messagebox.showerror" not in function_source


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


def test_group_configuration_change_does_not_stop_or_rebind_reconnect_snapshot():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index(
            "    def stop_group_automation_for_configuration_change("
        ):
        source.index("    def finish_group_management(")
    ]

    assert "smart_reconnect_monitor" not in function_source
    assert "set_group_launch_plan" not in function_source
    assert "SMART_RECONNECT_ENABLED_KEY" not in function_source


def test_failed_committed_group_input_rebuild_leaves_zero_input() -> None:
    allowed = ["old-input"]
    choice = object()

    def fail_after_partial_rebuild(_choice) -> None:
        allowed[:] = ["partial-new-input"]
        raise RuntimeError("input rebuild interrupted")

    def clear_input() -> None:
        allowed.clear()

    assert _rebuild_group_input_identity(
        choice,
        fail_after_partial_rebuild,
        clear_input,
    ) is False
    assert allowed == []


def test_empty_committed_group_clears_input_without_trying_to_apply() -> None:
    allowed = ["old-input"]
    apply_calls = []

    assert _rebuild_group_input_identity(
        None,
        lambda choice: apply_calls.append(choice),
        allowed.clear,
    ) is True
    assert apply_calls == []
    assert allowed == []


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
    apply_index = change_group.index("_rebuild_group_input_identity(")
    publication_index = change_group.index(
        "current_group_publication_service.execute("
    )
    reopen_index = change_group.rindex(
        "reopen_group_operation_gate()"
    )

    assert (
        close_index
        < stop_index
        < apply_index
        < publication_index
        < reopen_index
    )
    assert "publication_plan_for_choice(" in change_group
    assert "config.set(CURRENT_GROUP_NAME_KEY" not in change_group
    assert "workspace_service.set_current_group(" not in change_group


def test_group_change_allows_selection_but_clears_identity_when_unresolved():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]

    apply_index = change_group.index("_rebuild_group_input_identity(")
    clear_index = change_group.index("clear_group_identity,", apply_index)
    publication_index = change_group.index(
        "current_group_publication_service.execute("
    )

    assert "identity_ready = _rebuild_group_input_identity(" in change_group
    assert apply_index < clear_index < publication_index
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
        change_group.index(
            "except CurrentGroupPublicationNotificationError as error:"
        ),
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
    reopen_index = rollback.index("reopen_group_operation_gate()")

    assert restore_index < clear_index < reopen_index
    assert "if rollback_ready:" in rollback
    assert "restore_published_group(" not in change_group
    assert "config.set(CURRENT_GROUP_NAME_KEY" not in change_group
    assert "workspace_service.set_current_group(" not in change_group


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
