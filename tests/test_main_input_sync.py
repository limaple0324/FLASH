from pathlib import Path

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from config.config_manager import ConfigManager
from core.sp1_boundaries import SmartReconnectBoundary
from main import (
    INPUT_POLICY_KEY,
    SMART_RECONNECT_ENABLED_KEY,
    build_services,
)
from services.app_context import AppContext
from services.smart_reconnect_monitor import SmartReconnectMonitor


def test_build_services_registers_input_controller_and_safe_default(tmp_path):
    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    controller = AppContext.get(WindowsInputSyncController)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    reconnect_boundary = AppContext.get(SmartReconnectBoundary)
    reconnect_monitor = AppContext.get(SmartReconnectMonitor)

    assert config.get(INPUT_POLICY_KEY) == WindowInputPolicy.ALL.value
    assert config.get(SMART_RECONNECT_ENABLED_KEY) is True
    assert isinstance(controller, WindowsInputSyncController)
    assert isinstance(reconnect, WindowsSmartReconnectController)
    assert reconnect_boundary is reconnect
    assert isinstance(reconnect_monitor, SmartReconnectMonitor)


def test_home_exposes_three_confirmed_input_policies_and_only_b_c():
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "僅允許前台" in source
    assert "允許前台與背景" in source
    assert "全部允許（含最小化）" in source
    assert "測試 B（背包）" in source
    assert "測試 C（人物）" in source


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
