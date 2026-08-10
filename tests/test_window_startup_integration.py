import sys
import uuid
import inspect
import threading
import time
import tkinter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    PowerShellShortcutFingerprintResolver,
)
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectObservationSnapshot,
    SmartReconnectShortcutObservation,
    SmartReconnectWindowObservation,
    WindowsSmartReconnectObservationBroker,
    _execute_observation_request,
    _observe_window,
)
from adapters.windows_window import WindowInfo
from config.config_manager import ConfigManager
from core.reconnect_policy import ReconnectScreenState
from core.smart_reconnect_authorization import ShortcutFileIdentity, ShortcutSeal
from core.sp1_boundaries import ExternalAdapter, OperationResult
from core.window_instance import WindowInstanceToken
from domain.group import CharacterGroup
from main import (
    TARGET_WINDOW_FINGERPRINT_KEY,
    TARGET_WINDOW_KEY,
    _normalize_window_fingerprint,
    _normalize_window_keywords,
    build_services,
    detect_target_window,
    format_window_status,
)
from services.app_context import AppContext
from services.role_id_template_service import RoleIdTemplateService
from services.character_game_data_capture_service import (
    CharacterGameDataCaptureService,
)
from services.group_configuration_service import GroupConfigurationService
from services.group_launch_service import GroupLaunchService
from services.game_operation_gate import GameOperationGate
from services.smart_reconnect_preparation_service import (
    SmartReconnectPreparationService,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentityService,
)
from services.target_window_contract_service import (
    TargetWindowContractService,
)
from workspace.service import WorkspaceService


class FakeAdapter:
    def __init__(self, result: OperationResult):
        self._result = result

    @property
    def name(self) -> str:
        return "fake_window"

    def health_check(self) -> OperationResult:
        return self._result

    def shutdown(self) -> None:
        return None


def test_normalize_window_keywords_accepts_string_and_list():
    assert _normalize_window_keywords("  game  ") == ["game"]
    assert _normalize_window_keywords([" game ", "", 123, "server"]) == ["game", "server"]
    assert _normalize_window_keywords({"game": True}) == []


def test_normalize_window_fingerprint_accepts_only_complete_sha256():
    assert _normalize_window_fingerprint("  " + "A" * 64 + "  ") == "a" * 64
    assert _normalize_window_fingerprint("a" * 63) is None
    assert _normalize_window_fingerprint("g" * 64) is None
    assert _normalize_window_fingerprint(123) is None


def test_build_services_connects_persisted_fingerprint_to_window_adapter(tmp_path):
    fingerprint = "6" * 64
    config = ConfigManager(tmp_path / "config" / "settings.json")
    config.update_values(
        {
            TARGET_WINDOW_KEY: ["Adobe Flash Player"],
            TARGET_WINDOW_FINGERPRINT_KEY: fingerprint.upper(),
        }
    )

    build_services(root=tmp_path)

    adapter = AppContext.get(ExternalAdapter)
    assert adapter is not None
    assert adapter._launch_fingerprint == fingerprint
    assert adapter._fingerprint_configured is True


def test_build_services_keeps_invalid_persisted_fingerprint_fail_closed(tmp_path):
    config = ConfigManager(tmp_path / "config" / "settings.json")
    config.update_values(
        {
            TARGET_WINDOW_KEY: ["Adobe Flash Player"],
            TARGET_WINDOW_FINGERPRINT_KEY: "invalid",
        }
    )

    build_services(root=tmp_path)

    adapter = AppContext.get(ExternalAdapter)
    result = adapter.health_check()
    assert result.success is False
    assert result.code == "window.identity_invalid"


def test_build_services_wires_process_observation_broker_into_reconnect_chain(
    tmp_path,
):
    build_services(root=tmp_path)
    role_templates = AppContext.get(RoleIdTemplateService)
    broker = AppContext.get(WindowsSmartReconnectObservationBroker)
    preparation = AppContext.get(SmartReconnectPreparationService)
    target_identity = AppContext.get(SmartReconnectTargetIdentityService)
    target_windows = AppContext.get(TargetWindowContractService)
    controller = AppContext.get(WindowsSmartReconnectController)

    assert role_templates is not None
    assert broker is not None
    assert preparation._observation_broker is broker
    assert preparation._role_identity_reader is None
    assert target_identity._observation_broker is broker
    assert target_windows._observation_broker is broker
    assert controller._observation_broker is broker
    assert broker._worker_operation is _execute_observation_request
    assert "RoleIdTemplateService().read" in inspect.getsource(_observe_window)


def test_main_calls_multiprocessing_freeze_support_before_run(monkeypatch):
    events = []
    monkeypatch.setattr(
        main_module.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze_support"),
    )
    monkeypatch.setattr(
        main_module,
        "run",
        lambda **_kwargs: events.append("run") or 0,
    )
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])

    with pytest.raises(SystemExit) as stopped:
        main_module.main()

    assert stopped.value.code == 0
    assert events == ["freeze_support", "run"]


def test_application_shutdown_stops_monitor_before_observation_broker():
    source = inspect.getsource(main_module._run_application)

    assert source.index("shutdown_smart_reconnect_monitor") < source.index(
        "shutdown_smart_reconnect_observation_broker"
    ) < source.index("shutdown_sync_controllers")
    broker_index = source.index("shutdown_smart_reconnect_observation_broker")
    assert source.index(
        "shutdown_smart_reconnect_monitor",
        broker_index,
    ) > broker_index


@pytest.mark.skipif(sys.platform != "win32", reason="只適用真實 Tk 介面")
def test_real_tk_build_services_timers_only_consume_published_broker_snapshot(
    tmp_path,
    monkeypatch,
):
    callbacks_scheduled = set()
    callbacks_completed = set()
    scheduled_callbacks = {}
    accelerated = set()
    original_after = tkinter.Misc.after

    def recording_after(widget, milliseconds, callback=None, *args):
        name = getattr(callback, "__name__", "")
        if name in {
            "poll_registered_obsidian_once",
            "auto_read_missing_role_id",
        }:
            callbacks_scheduled.add(name)
            scheduled_callback = callback
            scheduled_callbacks[name] = scheduled_callback

            def recorded_callback(*callback_args):
                callbacks_completed.add(name)
                return scheduled_callback(*callback_args)

            if name not in accelerated:
                accelerated.add(name)
                milliseconds = 1
            callback = recorded_callback
        return original_after(widget, milliseconds, callback, *args)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("Tk timer performed synchronous capture")

    monkeypatch.setattr(tkinter.Misc, "after", recording_after)
    monkeypatch.setattr(
        CharacterGameDataCaptureService,
        "read",
        forbidden_read,
    )
    monkeypatch.setattr(RoleIdTemplateService, "read_if_missing", forbidden_read)
    monkeypatch.setattr(main_module, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(
        main_module,
        "configure_tk_window_app_identity",
        lambda _window, _icon: None,
    )
    monkeypatch.setattr(main_module, "start_service", lambda _service: None)
    monkeypatch.setattr(
        main_module.SystemTrayController,
        "start",
        lambda _self: False,
    )
    paths, _logger = build_services(root=tmp_path)
    broker = AppContext.get(WindowsSmartReconnectObservationBroker)
    groups = AppContext.get(GroupConfigurationService)
    workspace = AppContext.get(WorkspaceService)
    shortcut = tmp_path / "timer-role.lnk"
    shortcut.write_bytes(b"timer-role")
    added = groups.add_shortcuts("timer-group", (shortcut,))
    assert len(added) == 1
    entry = added[0]
    assert groups.set_role_id(
        "timer-group",
        entry.entry_id,
        "already-known",
    ) is True
    workspace.set_current_group(CharacterGroup("timer-group", "timer-group"))
    fingerprint = "a" * 64
    observed_window = WindowInfo(
        handle=9001,
        title="Adobe Flash Player",
        visible=True,
        minimized=False,
        rect=(0, 0, 800, 600),
        process_id=9002,
        launch_fingerprint=fingerprint,
        thread_id=9003,
        window_class="FlashWindow",
        process_lifecycle_token=9004,
    )
    normalized_shortcut = str(shortcut.resolve())
    seal = ShortcutSeal(
        ShortcutFileIdentity(normalized_shortcut, 1, 1),
        "b" * 64,
        fingerprint,
    )
    serial = broker._next_request()
    assert broker._publish(
        serial,
        SmartReconnectObservationSnapshot(
            generation=0,
            windows=(
                SmartReconnectWindowObservation(
                    window=observed_window,
                    instance=WindowInstanceToken.from_window(observed_window),
                    sample=None,
                    recognition=ScreenRecognition(
                        ReconnectScreenState.CONNECTED,
                        1.0,
                        None,
                        "connected",
                    ),
                    fresh_capture=True,
                    capture_route="visible",
                    role_id="role-100",
                ),
            ),
            shortcuts=(
                SmartReconnectShortcutObservation(
                    normalized_shortcut,
                    fingerprint,
                    seal,
                ),
            ),
        ),
    ) is not None
    window = None
    heartbeat = []
    try:
        window = main_module.create_main_window({}, paths)
        assert groups.set_role_id("timer-group", entry.entry_id, "") is True
        runtime_calls = {
            "plan": 0,
            "launch_resolver": 0,
            "shortcut_resolver": 0,
            "stop_wait": 0,
            "gate_wait": 0,
            "identity_rebuild": 0,
        }
        runtime_events = []

        def counted(name, result):
            def call(*_args, **_kwargs):
                runtime_calls[name] += 1
                runtime_events.append(name)
                return result

            return call

        monkeypatch.setattr(
            GroupLaunchService,
            "plan",
            counted(
                "plan",
                SimpleNamespace(ready=False, targets=()),
            ),
        )
        monkeypatch.setattr(
            PowerShellLaunchFingerprintResolver,
            "resolve",
            counted("launch_resolver", {}),
        )
        monkeypatch.setattr(
            PowerShellShortcutFingerprintResolver,
            "resolve",
            counted("shortcut_resolver", {}),
        )
        monkeypatch.setattr(
            main_module,
            "stop_service",
            counted(
                "stop_wait",
                OperationResult(True, "test.stopped", "", {}),
            ),
        )
        monkeypatch.setattr(
            main_module,
            "_rebuild_group_input_identity",
            counted("identity_rebuild", True),
        )
        game_gate = AppContext.get(GameOperationGate)
        monkeypatch.setattr(
            game_gate,
            "close_and_wait",
            counted("gate_wait", True),
        )
        window.after(1, lambda: heartbeat.append("alive"))
        deadline = time.monotonic() + 0.5
        while (
            (
                callbacks_completed
                != {
                    "poll_registered_obsidian_once",
                    "auto_read_missing_role_id",
                }
                or not heartbeat
            )
            and time.monotonic() < deadline
        ):
            window.update()
            time.sleep(0.005)

        assert broker is not None
        assert callbacks_scheduled == {
            "poll_registered_obsidian_once",
            "auto_read_missing_role_id",
        }
        assert callbacks_completed == callbacks_scheduled
        assert heartbeat == ["alive"]
        assert broker._active == {}
        assert groups.group("timer-group").entries[0].role_id == "role-100"
        assert runtime_calls == {
            "plan": 0,
            "launch_resolver": 0,
            "shortcut_resolver": 0,
            "stop_wait": 0,
            "gate_wait": 0,
            "identity_rebuild": 0,
        }
        assert runtime_events == []

        assert groups.set_role_id("timer-group", entry.entry_id, "") is True
        automatic_ready = threading.Event()
        user_write_done = threading.Event()
        automatic_finished = threading.Event()
        user_failures = []
        race_heartbeat = []
        original_run_if_generation_current = (
            broker.run_if_generation_current
        )

        def barrier_run_if_generation_current(generation, callback):
            if (
                not automatic_ready.is_set()
                and any(
                    frame.function == "auto_read_missing_role_id"
                    for frame in inspect.stack()
                )
            ):
                automatic_ready.set()
                assert user_write_done.wait(2)
                result = original_run_if_generation_current(
                    generation,
                    callback,
                )
                automatic_finished.set()
                return result
            return original_run_if_generation_current(
                generation,
                callback,
            )

        monkeypatch.setattr(
            broker,
            "run_if_generation_current",
            barrier_run_if_generation_current,
        )

        def write_user_role_first():
            try:
                assert automatic_ready.wait(2)
                assert groups.set_role_id(
                    "timer-group",
                    entry.entry_id,
                    "user-role",
                ) is True
            except Exception as error:
                user_failures.append(error)
            finally:
                user_write_done.set()

        user_writer = threading.Thread(target=write_user_role_first)
        user_writer.start()
        window.after(1, lambda: race_heartbeat.append("alive"))
        window.after(
            1,
            scheduled_callbacks["auto_read_missing_role_id"],
        )
        deadline = time.monotonic() + 2.0
        while (
            (
                not automatic_finished.is_set()
                or not race_heartbeat
            )
            and time.monotonic() < deadline
        ):
            window.update()
            time.sleep(0.005)
        user_writer.join(2)

        assert user_writer.is_alive() is False
        assert user_failures == []
        assert automatic_ready.is_set()
        assert automatic_finished.is_set()
        assert race_heartbeat == ["alive"]
        assert groups.group("timer-group").entries[0].role_id == "user-role"
        assert runtime_events == []
        monkeypatch.setattr(
            broker,
            "run_if_generation_current",
            original_run_if_generation_current,
        )

        assert groups.set_role_id("timer-group", entry.entry_id, "") is True
        old_snapshot_returned = threading.Event()
        replacement_published = threading.Event()
        old_snapshot_released = threading.Event()
        invalidation_failures = []
        original_current_snapshot = broker.current_snapshot

        def barrier_current_snapshot():
            snapshot = original_current_snapshot()
            if (
                not old_snapshot_returned.is_set()
                and any(
                    frame.function == "auto_read_missing_role_id"
                    for frame in inspect.stack()
                )
            ):
                old_snapshot_returned.set()
                assert replacement_published.wait(2)
                old_snapshot_released.set()
            return snapshot

        monkeypatch.setattr(
            broker,
            "current_snapshot",
            barrier_current_snapshot,
        )

        def publish_replacement():
            try:
                assert old_snapshot_returned.wait(2)
                replacement_window = replace(
                    observed_window,
                    handle=9011,
                    process_id=9012,
                    thread_id=9013,
                    process_lifecycle_token=9014,
                )
                replacement_serial = broker._next_request()
                assert broker._publish(
                    replacement_serial,
                    SmartReconnectObservationSnapshot(
                        generation=0,
                        windows=(
                            SmartReconnectWindowObservation(
                                window=replacement_window,
                                instance=WindowInstanceToken.from_window(
                                    replacement_window
                                ),
                                sample=None,
                                recognition=ScreenRecognition(
                                    ReconnectScreenState.CONNECTED,
                                    1.0,
                                    None,
                                    "connected",
                                ),
                                fresh_capture=True,
                                capture_route="visible",
                                role_id=None,
                            ),
                        ),
                        shortcuts=(
                            SmartReconnectShortcutObservation(
                                normalized_shortcut,
                                fingerprint,
                                seal,
                            ),
                        ),
                    ),
                ) is not None
            except Exception as error:
                invalidation_failures.append(error)
            finally:
                replacement_published.set()

        invalidator = threading.Thread(target=publish_replacement)
        invalidator.start()
        window.after(
            1,
            scheduled_callbacks["auto_read_missing_role_id"],
        )
        deadline = time.monotonic() + 2.0
        while (
            not old_snapshot_released.is_set()
            and time.monotonic() < deadline
        ):
            window.update()
            time.sleep(0.005)
        invalidator.join(2)

        assert invalidator.is_alive() is False
        assert invalidation_failures == []
        assert old_snapshot_returned.is_set()
        assert old_snapshot_released.is_set()
        assert groups.group("timer-group").entries[0].role_id == ""
        assert runtime_events == []
    finally:
        if window is not None:
            window.destroy()
        main_module.shutdown_smart_reconnect_monitor()
        assert main_module.shutdown_smart_reconnect_observation_broker() is True
        main_module.shutdown_sync_controllers()
        AppContext.clear()


def test_timer_source_contains_no_direct_capture_or_role_read():
    source = inspect.getsource(main_module.create_main_window)
    obsidian = source[
        source.index("def poll_registered_obsidian_once"):
        source.index("def activity_progress_changed_handler")
    ]
    role_start = source.index("def auto_read_missing_role_id")
    role = source[
        role_start:
        source.index("\n    auto_read_missing_role_id()", role_start)
    ]

    assert "capture_service.read(" not in obsidian
    assert "role_id_template_service.read_if_missing(" not in role
    assert ".current_snapshot()" in obsidian
    assert ".current_snapshot()" in role
    assert "unique_window_for_group_entry" not in role
    assert "group_launch_service.plan" not in role


def test_unconfigured_window_detection_remains_unsafe():
    AppContext.clear()

    status = detect_target_window()

    assert status["configured"] is False
    assert status["safe"] is False
    assert status["code"] == "window.not_configured"
    assert "不會執行任何遊戲操作" in status["message"]


def test_ready_window_is_reported_but_input_is_not_enabled():
    AppContext.clear()
    adapter = FakeAdapter(
        OperationResult(
            success=True,
            code="window.ready",
            message="Target identified.",
            details={"title": "Game", "handle": 100, "rect": (0, 0, 800, 600)},
        )
    )
    AppContext.register(ExternalAdapter, adapter)

    status = detect_target_window()
    text = format_window_status({"target_window": status})

    assert status["configured"] is True
    assert status["safe"] is True
    assert status["code"] == "window.ready"
    assert status["details"]["handle"] == 100
    assert "同步輸入仍需玩家手動執行" in text


def test_ambiguous_window_keeps_operation_disabled():
    AppContext.clear()
    adapter = FakeAdapter(
        OperationResult(
            success=False,
            code="window.ambiguous",
            message="Multiple matches.",
            details={"count": 2},
        )
    )
    AppContext.register(ExternalAdapter, adapter)

    status = detect_target_window()
    text = format_window_status({"target_window": status})

    assert status["safe"] is False
    assert status["code"] == "window.ambiguous"
    assert "不可操作" in text


def test_duplicate_ui_instance_exits_before_application_services(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        "acquire_main_instance_lock",
        lambda: None,
    )
    monkeypatch.setattr(
        main_module,
        "_run_application",
        lambda **_kwargs: calls.append("started") or 9,
    )

    assert main_module.run() == 0
    assert calls == []


def test_ui_instance_lock_is_released_only_after_complete_run(monkeypatch):
    events: list[str] = []

    class Lock:
        def release(self) -> None:
            events.append("released")

    monkeypatch.setattr(
        main_module,
        "acquire_main_instance_lock",
        lambda: Lock(),
    )
    monkeypatch.setattr(
        main_module,
        "_run_application",
        lambda **_kwargs: events.append("application_complete") or 7,
    )

    assert main_module.run() == 7
    assert events == ["application_complete", "released"]


@pytest.mark.parametrize(
    "arguments",
    (
        {"self_check_only": True},
        {"target_desktop_verify_only": True},
        {"background_image_verify_path": Path("sample.png")},
    ),
)
def test_verification_commands_bypass_ui_instance_lock(monkeypatch, arguments):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        main_module,
        "acquire_main_instance_lock",
        lambda: pytest.fail("驗證命令不應取得主介面鎖"),
    )
    monkeypatch.setattr(
        main_module,
        "_run_application",
        lambda **kwargs: calls.append(kwargs) or 0,
    )

    assert main_module.run(**arguments) == 0
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="只適用 Windows 命名互斥鎖")
def test_real_named_instance_lock_rejects_second_handle_until_release():
    name = rf"Local\Limaple.Fu.Test.{uuid.uuid4()}"
    first = main_module.acquire_main_instance_lock(name)
    assert first is not None
    try:
        assert main_module.acquire_main_instance_lock(name) is None
    finally:
        first.release()
    restored = main_module.acquire_main_instance_lock(name)
    assert restored is not None
    restored.release()
