from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import main as main_module
from config.config_manager import ConfigManager
from core.sp1_boundaries import ExternalAdapter, OperationResult
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from services.app_context import AppContext
from services.background_image_service import BackgroundImageService
from services.event_bus import EventBus
from services.target_window_state_service import TargetWindowStateService
from services.target_window_contract_service import (
    ResolvedTargetWindows,
    TargetWindowContractService,
)


class RecordingAdapter:
    def __init__(self, *, shutdown_error: Exception | None = None):
        self.shutdown_calls = 0
        self._shutdown_error = shutdown_error

    @property
    def name(self) -> str:
        return "recording_adapter"

    def health_check(self) -> OperationResult:
        return OperationResult(
            success=True,
            code="window.ready",
            message="Target identified without enabling input.",
        )

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._shutdown_error is not None:
            raise self._shutdown_error


class RecordingLogger:
    def __init__(self):
        self.info_messages: list[str] = []
        self.error_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def error(self, message: str) -> None:
        self.error_messages.append(message)


@pytest.fixture(autouse=True)
def clear_app_context():
    AppContext.clear()
    yield
    AppContext.clear()


def prepare_run(monkeypatch, tmp_path, adapter, *, startup_error: Exception | None = None):
    paths = main_module.PathManager(root=tmp_path)
    logger = RecordingLogger()
    registry = WindowRegistry()
    store = WindowRegistryStore(paths.data_dir() / main_module.REGISTRY_FILENAME)
    event_bus = EventBus(logger=logger)
    target_window_state_service = TargetWindowStateService(event_bus, logger)

    AppContext.register(WindowRegistryStore, store)
    AppContext.register(WindowRegistry, registry)
    AppContext.register(ExternalAdapter, adapter)
    AppContext.register(EventBus, event_bus)
    AppContext.register(TargetWindowStateService, target_window_state_service)

    monkeypatch.setattr(main_module, "build_services", lambda root=None: (paths, logger))

    def start():
        if startup_error is not None:
            raise startup_error
        return {"self_check_passed": True, "self_check": []}

    monkeypatch.setattr(
        main_module,
        "Bootstrap",
        lambda context: SimpleNamespace(start=start),
    )
    return logger


def test_run_shuts_down_adapter_after_self_check_exit(monkeypatch, tmp_path):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)

    assert main_module.run(self_check_only=True, root=tmp_path) == 0
    assert adapter.shutdown_calls == 1


def test_target_desktop_verification_writes_safe_report_and_shuts_down(
    monkeypatch, tmp_path
):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)
    payload = {
        "passed": True,
        "discovered_windows": 14,
        "unique_fingerprints": 14,
        "input_sent": False,
    }
    verification = SimpleNamespace(passed=True, to_dict=lambda: payload)
    monkeypatch.setattr(
        main_module.TargetDesktopVerifier,
        "for_real_windows",
        lambda: SimpleNamespace(verify=lambda: verification),
    )
    monkeypatch.setattr(
        main_module,
        "create_main_window",
        lambda *_args: pytest.fail("Headless verification must not create a window."),
    )

    exit_code = main_module.run(
        target_desktop_verify_only=True,
        root=tmp_path,
    )

    assert exit_code == 0
    assert adapter.shutdown_calls == 1
    report_path = tmp_path / "data" / main_module.TARGET_DESKTOP_REPORT_FILENAME
    assert report_path.exists()
    assert '"input_sent": false' in report_path.read_text(encoding="utf-8")


def test_target_desktop_verification_uses_distinct_failure_exit_code(
    monkeypatch, tmp_path
):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)
    verification = SimpleNamespace(
        passed=False,
        to_dict=lambda: {
            "passed": False,
            "failure_codes": ["window_count_mismatch"],
            "input_sent": False,
        },
    )
    monkeypatch.setattr(
        main_module.TargetDesktopVerifier,
        "for_real_windows",
        lambda: SimpleNamespace(verify=lambda: verification),
    )

    assert (
        main_module.run(
            target_desktop_verify_only=True,
            root=tmp_path,
        )
        == 3
    )
    assert adapter.shutdown_calls == 1


def test_target_desktop_verification_sanitizes_unexpected_failure(
    monkeypatch, tmp_path
):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)
    monkeypatch.setattr(
        main_module.TargetDesktopVerifier,
        "for_real_windows",
        lambda: SimpleNamespace(
            verify=lambda: (_ for _ in ()).throw(
                RuntimeError("raw launcher arguments must not escape")
            )
        ),
    )

    exit_code = main_module.run(
        target_desktop_verify_only=True,
        root=tmp_path,
    )

    report_path = tmp_path / "data" / main_module.TARGET_DESKTOP_REPORT_FILENAME
    report = report_path.read_text(encoding="utf-8")
    assert exit_code == 4
    assert "verifier_execution_failed" in report
    assert "raw launcher arguments" not in report
    assert adapter.shutdown_calls == 1


def test_background_image_runtime_verification_uses_real_service_without_ui(
    monkeypatch,
    tmp_path,
) -> None:
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)
    config = ConfigManager(tmp_path / "config" / "settings.json")
    service = BackgroundImageService(config, tmp_path / "data")
    AppContext.register(BackgroundImageService, service)
    source = tmp_path / "月球原圖.png"
    Image.new("RGB", (14, 9), "#203040").save(source)
    before = source.read_bytes()
    monkeypatch.setattr(
        main_module,
        "create_main_window",
        lambda *_args: pytest.fail("背景圖片驗證不得開啟主畫面。"),
    )

    exit_code = main_module.run(
        background_image_verify_path=source,
        root=tmp_path,
    )

    assert exit_code == 0
    assert source.read_bytes() == before
    report_path = (
        tmp_path
        / "data"
        / main_module.BACKGROUND_IMAGE_VERIFY_REPORT_FILENAME
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["source_unchanged"] is True
    assert report["managed_copy_created"] is True
    assert report["original_size"] == [14, 9]
    assert tuple((tmp_path / "data" / "backgrounds").glob("*.png")) == ()
    assert adapter.shutdown_calls == 1


def test_run_shuts_down_adapter_after_normal_window_close(monkeypatch, tmp_path):
    adapter = RecordingAdapter()
    prepare_run(monkeypatch, tmp_path, adapter)
    window = SimpleNamespace(mainloop=lambda: None)
    monkeypatch.setattr(main_module, "create_main_window", lambda status, paths: window)

    assert main_module.run(root=tmp_path) == 0
    assert adapter.shutdown_calls == 1


def test_build_services_uses_global_reconnect_and_grouped_sync_targets(
    monkeypatch,
    tmp_path,
):
    main_module.build_services(root=tmp_path)
    contract = AppContext.get(TargetWindowContractService)
    reconnect = AppContext.get(main_module.WindowsSmartReconnectController)
    keyboard = AppContext.get(main_module.WindowsInputSyncController)
    pointer = AppContext.get(main_module.WindowsPointerSyncController)
    unscoped = reconnect._target_windows_provider()
    assert isinstance(unscoped, ResolvedTargetWindows)
    assert unscoped.failure_codes == ("group_name_invalid",)
    strict_targets = ("strict-target",)
    strict_contract = ResolvedTargetWindows(strict_targets)
    monkeypatch.setattr(
        contract,
        "reconnect_targets",
        lambda _group_name: strict_contract,
    )

    assert reconnect._target_windows_provider() is strict_contract
    assert reconnect._require_expected_window_count is False
    # 未驗證的測試替身不得進入同步目標集合。
    assert keyboard._target_windows_provider() == ()
    assert pointer._target_windows_provider() == ()


def test_run_shuts_down_adapter_after_startup_failure(monkeypatch, tmp_path):
    adapter = RecordingAdapter()
    logger = prepare_run(
        monkeypatch,
        tmp_path,
        adapter,
        startup_error=RuntimeError("original startup failure"),
    )

    assert main_module.run(self_check_only=True, root=tmp_path) == 1
    assert adapter.shutdown_calls == 1
    assert any("original startup failure" in message for message in logger.error_messages)


def test_shutdown_failure_does_not_replace_successful_exit(monkeypatch, tmp_path):
    adapter = RecordingAdapter(shutdown_error=RuntimeError("cleanup failure"))
    logger = prepare_run(monkeypatch, tmp_path, adapter)

    assert main_module.run(self_check_only=True, root=tmp_path) == 0
    assert adapter.shutdown_calls == 1
    assert any("cleanup failure" in message for message in logger.error_messages)


def test_shutdown_failure_does_not_hide_startup_failure(monkeypatch, tmp_path):
    adapter = RecordingAdapter(shutdown_error=RuntimeError("cleanup failure"))
    logger = prepare_run(
        monkeypatch,
        tmp_path,
        adapter,
        startup_error=RuntimeError("original startup failure"),
    )

    assert main_module.run(self_check_only=True, root=tmp_path) == 1
    assert adapter.shutdown_calls == 1
    assert any("original startup failure" in message for message in logger.error_messages)
    assert any("cleanup failure" in message for message in logger.error_messages)


def test_sync_controller_shutdown_reports_a_live_background_queue():
    class Controller:
        def close(self, timeout_seconds):
            assert timeout_seconds == 1.0
            return False

    logger = RecordingLogger()
    AppContext.register(main_module.WindowsInputSyncController, Controller())

    assert main_module.shutdown_sync_controllers(logger) is False
    assert any(
        "did not stop cleanly" in message
        for message in logger.error_messages
    )


def test_event_subscription_shutdown_reports_detach_failure():
    class StateService:
        def close(self):
            return False

    logger = RecordingLogger()
    AppContext.register(TargetWindowStateService, StateService())

    assert main_module.shutdown_event_subscriptions(logger) is False
    assert any(
        "listeners were not detached" in message
        for message in logger.error_messages
    )


def test_obsidian_polling_reschedules_after_a_safe_cycle_failure_and_cancels_on_close():
    source = Path("main.py").read_text(encoding="utf-8")
    window_setup = source[
        source.index("def create_main_window("):
        source.index("    def schedule_registered_obsidian_poll(")
    ]
    polling = source[
        source.index("    def schedule_registered_obsidian_poll("):
        source.index("    def activity_progress_changed_handler(")
    ]
    role_read_scheduler = source[
        source.index("    def auto_read_missing_role_id("):
        source.index("    auto_read_missing_role_id()")
    ]
    role_read_helper = source[
        source.index("def auto_read_missing_role_id_once("):
        source.index("class ConnectedSyncTargetContractProvider")
    ]
    closing = source[
        source.index("    def close_window("):
        source.index("    window.protocol(")
    ]

    assert "if closing:" in polling
    assert "def current_sync_target_windows()" in window_setup
    assert "target_window_contract_service.reconnect_targets(" in window_setup
    assert "auto_read_missing_role_id_once(" in role_read_scheduler
    assert (
        "smart_reconnect_controller.observe_screen_states("
        in role_read_helper
    )
    assert "ReconnectScreenState.CONNECTED" in role_read_helper
    assert "candidate_index = game_data_read_cursor % len(candidates)" in polling
    assert "result = capture_service.read(selected.handle)" in polling
    assert "except Exception as error:" in polling
    assert "stage={stage}; candidate_index={candidate_index}" in polling
    assert "finally:" in polling
    assert "schedule_registered_obsidian_poll()" in polling
    assert "window.after_cancel(game_data_read_after_id)" in closing
