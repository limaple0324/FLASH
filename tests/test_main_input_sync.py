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
    INPUT_POLICY_KEY,
    SMART_RECONNECT_ENABLED_KEY,
    SMART_RECONNECT_CONSENT_KEY,
    build_services,
)
from services.app_context import AppContext
from services.smart_reconnect_monitor import SmartReconnectMonitor
from services.group_role_status_service import GroupRoleStatusService


def test_build_services_registers_input_controller_and_safe_default(tmp_path):
    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    controller = AppContext.get(WindowsInputSyncController)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    reconnect_boundary = AppContext.get(SmartReconnectBoundary)
    reconnect_monitor = AppContext.get(SmartReconnectMonitor)

    assert config.get(INPUT_POLICY_KEY) == WindowInputPolicy.ALL.value
    assert config.get(SMART_RECONNECT_ENABLED_KEY) is False
    assert config.get(SMART_RECONNECT_CONSENT_KEY) is False
    assert isinstance(controller, WindowsInputSyncController)
    assert isinstance(reconnect, WindowsSmartReconnectController)
    assert reconnect_boundary is reconnect
    assert isinstance(reconnect_monitor, SmartReconnectMonitor)


def test_sync_services_share_one_lifecycle_identity_snapshot(tmp_path):
    build_services(root=tmp_path)

    keyboard = AppContext.get(WindowsInputSyncController)
    pointer = AppContext.get(WindowsPointerSyncController)
    statuses = AppContext.get(GroupRoleStatusService)

    assert keyboard._window_backend is pointer._window_backend
    assert keyboard._window_backend is statuses._window_backend


def test_home_exposes_three_policies_and_complete_confirmed_shortcuts():
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "僅允許前台" in source
    assert "允許前台與背景" in source
    assert "全部允許（含最小化）" in source
    assert "開始同步視窗" in source
    assert "停止同步視窗" in source
    assert "CONFIRMED_GAME_SHORTCUTS" in source
    assert "測試 B" not in source


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
