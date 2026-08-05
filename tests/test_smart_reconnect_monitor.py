import threading

import pytest

import services.smart_reconnect_monitor as smart_reconnect_monitor_module
from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_background_capture import CaptureSample
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.sp1_boundaries import OperationResult, ReconnectState
from services.smart_reconnect_monitor import SmartReconnectMonitor


class FakeBoundary:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.execution_enabled = False
        self.execution_changes = []

    @property
    def state(self):
        return ReconnectState.CONNECTED

    def check_connection(self):
        return OperationResult(True, "reconnect.connected")

    def reconnect(self):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return OperationResult(
            True,
            "reconnect.connected",
            details={"next_check_seconds": 5},
        )

    def set_execution_enabled(self, enabled):
        self.execution_enabled = bool(enabled)
        self.execution_changes.append(self.execution_enabled)


class RecordingLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


class NotifyingInfoLogger(RecordingLogger):
    def __init__(self):
        super().__init__()
        self.info_recorded = threading.Event()

    def info(self, message):
        super().info(message)
        self.info_recorded.set()


class BlockingBoundary(FakeBoundary):
    def __init__(self):
        super().__init__([])
        self.started = threading.Event()
        self.release = threading.Event()

    def reconnect(self):
        self.started.set()
        self.release.wait(2)
        return OperationResult(
            True,
            "reconnect.connected",
            details={"next_check_seconds": 5},
        )


def healthy_connected_details(*, next_check_seconds=5, windows=2):
    return {
        "all_connected": True,
        "discovered_windows": windows,
        "validated_windows": windows,
        "captured_windows": windows,
        "recognized_windows": windows,
        "connected_windows": windows,
        "actionable_windows": 0,
        "clicked_windows": 0,
        "restarted_windows": 0,
        "unknown_windows": 0,
        "next_check_seconds": next_check_seconds,
        "state_counts": {"connected": windows},
        "failure_codes": [],
    }


def test_run_once_uses_result_delay_and_logs_only_aggregate_state():
    boundary = FakeBoundary(
        [
            OperationResult(
                True,
                "reconnect.progressed",
                details={
                    "connected_windows": 1,
                    "actionable_windows": 1,
                    "clicked_windows": 1,
                    "unknown_windows": 0,
                    "next_check_seconds": 2,
                    "state_counts": {"connected": 1, "disconnected": 1},
                    "failure_codes": [],
                },
            )
        ]
    )
    logger = RecordingLogger()
    monitor = SmartReconnectMonitor(boundary, logger=logger)

    result, delay = monitor.run_once()

    assert result.code == "reconnect.progressed"
    assert delay == 2
    assert boundary.calls == 1
    assert len(logger.info_messages) == 1
    assert "states=connected:1,disconnected:1" in logger.info_messages[0]
    assert "connected=1" in logger.info_messages[0]
    assert "clicked=1" in logger.info_messages[0]
    assert "failure_codes=none" in logger.info_messages[0]
    assert "handle" not in logger.info_messages[0]
    assert "fingerprint" not in logger.info_messages[0]


def test_repeated_identical_state_does_not_spam_log():
    result = OperationResult(
        True,
        "reconnect.connected",
        details=healthy_connected_details(),
    )
    logger = RecordingLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary([result, result]),
        logger=logger,
    )

    monitor.run_once()
    monitor.run_once()

    assert len(logger.info_messages) == 1


def test_runtime_status_separates_waiting_recovery_from_real_failure():
    monitor = SmartReconnectMonitor(FakeBoundary([]))
    monitor._thread = type("AliveThread", (), {"is_alive": lambda self: True})()

    monitor._set_runtime_status(
        OperationResult(
            True,
            "reconnect.connected",
            details=healthy_connected_details(),
        )
    )
    assert monitor.runtime_status == "已開啟"

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "state_counts": {"line_selection": 1},
                "failure_codes": [],
            },
        )
    )
    assert monitor.runtime_status == "重連中"

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "state_counts": {"unknown": 1},
                "failure_codes": ["capture_failed"],
            },
        )
    )
    assert monitor.runtime_status == "重連失敗"


def test_runtime_status_reproduces_real_partial_reconnect_sequence():
    monitor = SmartReconnectMonitor(
        FakeBoundary([]),
        fallback_delay_seconds=20,
    )
    monitor._thread = type(
        "AliveThread",
        (),
        {"is_alive": lambda self: True},
    )()
    sequence = (
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "connected_windows": 8,
                "unknown_windows": 2,
                "state_counts": {
                    "connected": 8,
                    "disconnected": 2,
                    "unknown": 2,
                },
                "failure_codes": ["screen_unknown"],
            },
        ),
        OperationResult(
            True,
            "reconnect.progressed_with_isolation",
            details={
                "connected_windows": 8,
                "unknown_windows": 2,
                "clicked_windows": 2,
                "state_counts": {
                    "connected": 8,
                    "disconnected": 2,
                    "unknown": 2,
                },
                "failure_codes": ["screen_unknown"],
            },
        ),
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "connected_windows": 0,
                "unknown_windows": 12,
                "state_counts": {"unknown": 12},
                "failure_codes": ["capture_failed", "screen_unknown"],
            },
        ),
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "connected_windows": 8,
                "unknown_windows": 2,
                "state_counts": {
                    "connected": 8,
                    "character_selection": 1,
                    "reconnecting": 1,
                    "unknown": 2,
                },
                "failure_codes": ["screen_unknown"],
            },
        ),
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "connected_windows": 10,
                "unknown_windows": 2,
                "state_counts": {"connected": 10, "unknown": 2},
                "failure_codes": ["screen_unknown"],
            },
        ),
    )

    for now, result in enumerate(sequence):
        monitor._set_runtime_status(result, now=float(now * 2))
        assert monitor.runtime_status == "重連中"


@pytest.mark.parametrize(
    "failure_code",
    (
        "capture_failed",
        "snapshot_identity_collision",
        "target_window_provider_failed",
        "click_delivery_uncertain",
    ),
)
def test_runtime_status_does_not_hide_real_safety_failure(failure_code):
    monitor = SmartReconnectMonitor(FakeBoundary([]))
    monitor._thread = type(
        "AliveThread",
        (),
        {"is_alive": lambda self: True},
    )()

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "state_counts": {"unknown": 1},
                "failure_codes": [failure_code],
            },
        ),
        now=100.0,
    )

    assert monitor.runtime_status == "重連失敗"


def test_repeated_recovery_state_does_not_refresh_progress_deadline():
    monitor = SmartReconnectMonitor(
        FakeBoundary([]),
        fallback_delay_seconds=10,
    )
    monitor._thread = type(
        "AliveThread",
        (),
        {"is_alive": lambda self: True},
    )()
    monitor._set_runtime_status(
        OperationResult(
            True,
            "reconnect.progressed_with_isolation",
            details={
                "connected_windows": 8,
                "clicked_windows": 1,
                "state_counts": {"connected": 8, "disconnected": 1},
                "failure_codes": ["screen_unknown"],
            },
        ),
        now=0.0,
    )

    failure = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "connected_windows": 0,
            "unknown_windows": 11,
            "state_counts": {"disconnected": 1, "unknown": 11},
            "failure_codes": ["capture_failed", "screen_unknown"],
        },
    )
    monitor._set_runtime_status(failure, now=9.0)
    assert monitor.runtime_status == "重連中"

    monitor._set_runtime_status(failure, now=11.0)
    assert monitor.runtime_status == "重連失敗"


def test_real_safety_failure_is_immediate_during_recovery_grace():
    monitor = SmartReconnectMonitor(
        FakeBoundary([]),
        fallback_delay_seconds=10,
    )
    monitor._thread = type(
        "AliveThread",
        (),
        {"is_alive": lambda self: True},
    )()
    monitor._set_runtime_status(
        OperationResult(
            True,
            "reconnect.progressed_with_isolation",
            details={
                "connected_windows": 8,
                "clicked_windows": 1,
                "state_counts": {"connected": 8, "disconnected": 1},
                "failure_codes": ["screen_unknown"],
            },
        ),
        now=0.0,
    )

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.waiting",
            details={
                "state_counts": {"disconnected": 1},
                "failure_codes": ["snapshot_identity_collision"],
            },
        ),
        now=1.0,
    )

    assert monitor.runtime_status == "重連失敗"


def test_runtime_status_treats_safe_wait_and_rebinding_pause_as_reconnecting():
    monitor = SmartReconnectMonitor(FakeBoundary([]))
    monitor._thread = type("AliveThread", (), {"is_alive": lambda self: True})()

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.waiting",
            details={"failure_codes": []},
        )
    )
    assert monitor.runtime_status == "重連中"

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.waiting",
            details={"failure_codes": ["screen_unknown"]},
        )
    )
    assert monitor.runtime_status == "重連中"

    monitor._set_runtime_status(
        OperationResult(
            False,
            "reconnect.operation_paused",
            details={"failure_codes": ["operation_gate_closed"]},
        )
    )
    assert monitor.runtime_status == "重連中"


def test_fully_captured_partial_unknown_scan_reports_reconnecting() -> None:
    windows = (
        WindowInfo(
            1,
            "Adobe Flash Player",
            True,
            False,
            (0, 0, 2, 2),
            11,
            "ShockwaveFlash",
            "a" * 64,
            21,
            31,
        ),
        WindowInfo(
            2,
            "Adobe Flash Player",
            True,
            False,
            (0, 0, 2, 2),
            12,
            "ShockwaveFlash",
            "b" * 64,
            22,
            32,
        ),
    )

    class Windows:
        def list_windows(self):
            return list(windows)

        def foreground_handle(self):
            return windows[0].handle

        def top_window_at(self, _x, _y):
            return windows[0].handle

    class Capture:
        def capture(self, _handle):
            return CaptureSample(2, 2, bytes([0, 20, 80, 255] * 4), True)

    class Recognizer:
        def __init__(self):
            self.states = [
                ReconnectScreenState.CONNECTED,
                ReconnectScreenState.UNKNOWN,
            ]

        def recognize_capture(self, _sample):
            state = self.states.pop(0)
            return ScreenRecognition(state, 1.0, None, state.value)

    class Mouse:
        def is_window(self, _handle):
            return True

        def probe_responsive(self, _handle, _timeout):
            return True

        def click_relative(self, *_args):
            raise AssertionError("unknown screen must never deliver input")

    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(),
        capture_provider=Capture(),
        recognizer=Recognizer(),
        mouse_backend=Mouse(),
        primary_capture_is_trusted=True,
        primary_capture_is_fresh_without_visibility=True,
        require_expected_window_count=True,
        auto_battle_enabled=False,
    )
    monitor = SmartReconnectMonitor(controller)
    monitor._thread = type(
        "AliveThread",
        (),
        {"is_alive": lambda self: True},
    )()

    result, _delay = monitor.run_once()

    assert result.code == "reconnect.waiting"
    assert result.details["captured_windows"] == 2
    assert result.details["state_counts"] == {
        "connected": 1,
        "unknown": 1,
    }
    assert result.details["failure_codes"] == ["screen_unknown"]
    assert monitor.runtime_status == "重連中"


def test_status_change_log_includes_actual_failure_codes():
    logger = RecordingLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary(
            [
                OperationResult(
                    False,
                    "reconnect.waiting",
                    details={
                        "failure_codes": ["capture_failed"],
                        "next_check_seconds": 3,
                    },
                )
            ]
        ),
        logger=logger,
    )

    monitor.run_once()

    assert "failure_codes=capture_failed" in logger.info_messages[0]


def test_only_complete_healthy_connected_scan_uses_saved_monitoring_interval():
    monitor = SmartReconnectMonitor(
        FakeBoundary(
            [
                OperationResult(
                    True,
                    "reconnect.connected",
                    details=healthy_connected_details(next_check_seconds=45),
                )
            ]
        ),
        monitor_interval_ms=1500,
    )

    assert monitor.run_once()[1] == 2.0


def test_passive_waiting_respects_controller_delay_or_safe_fallback():
    monitor = SmartReconnectMonitor(
        FakeBoundary(
            [
                OperationResult(False, "reconnect.waiting", details={}),
                OperationResult(
                    False,
                    "reconnect.waiting",
                    details={"next_check_seconds": 17},
                ),
            ]
        ),
        fallback_delay_seconds=60,
        monitor_interval_ms=1500,
    )

    assert monitor.run_once()[1] == 60
    assert monitor.run_once()[1] == 17


def test_unknown_or_capture_failure_respects_controller_delay():
    monitor = SmartReconnectMonitor(
        FakeBoundary(
            [
                OperationResult(
                    False,
                    "reconnect.waiting",
                    details={
                        "all_connected": False,
                        "discovered_windows": 14,
                        "validated_windows": 14,
                        "captured_windows": 13,
                        "recognized_windows": 13,
                        "connected_windows": 5,
                        "actionable_windows": 0,
                        "clicked_windows": 0,
                        "restarted_windows": 0,
                        "unknown_windows": 1,
                        "next_check_seconds": 30,
                        "state_counts": {"connected": 5, "unknown": 1},
                        "failure_codes": ["capture_failed"],
                    },
                )
            ]
        ),
        monitor_interval_ms=1500,
    )

    assert monitor.run_once()[1] == 2


def test_disconnected_without_progress_reports_one_error_after_interval(
    monkeypatch,
):
    waiting = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "state_counts": {"disconnected": 1},
            "clicked_windows": 0,
            "restarted_windows": 0,
            "next_check_seconds": 2,
        },
    )
    logger = RecordingLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary([waiting, waiting, waiting]),
        logger=logger,
        monitor_interval_ms=2000,
    )
    moments = iter((0.0, 2.1, 4.2))
    monkeypatch.setattr(
        smart_reconnect_monitor_module.time,
        "monotonic",
        lambda: next(moments),
    )

    monitor.run_once()
    monitor.run_once()
    monitor.run_once()

    assert len(logger.error_messages) == 1
    assert "without starting recovery" in logger.error_messages[0]
    assert "disconnected=1" in logger.error_messages[0]


def test_reconnect_progress_clears_stalled_disconnect_monitoring(
    monkeypatch,
):
    waiting = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "state_counts": {"disconnected": 1},
            "clicked_windows": 0,
            "restarted_windows": 0,
            "next_check_seconds": 2,
        },
    )
    progressed = OperationResult(
        True,
        "reconnect.progressed",
        details={
            "state_counts": {"disconnected": 1},
            "clicked_windows": 1,
            "restarted_windows": 0,
            "next_check_seconds": 2,
        },
    )
    logger = RecordingLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary([waiting, progressed]),
        logger=logger,
        monitor_interval_ms=2000,
    )
    moments = iter((0.0, 2.1))
    monkeypatch.setattr(
        smart_reconnect_monitor_module.time,
        "monotonic",
        lambda: next(moments),
    )

    monitor.run_once()
    monitor.run_once()

    assert logger.error_messages == []


def test_connected_code_without_complete_health_evidence_keeps_controller_delay():
    incomplete_or_failed = [
        {"next_check_seconds": 21},
        {
            **healthy_connected_details(next_check_seconds=22),
            "unknown_windows": 1,
        },
        {
            **healthy_connected_details(next_check_seconds=23),
            "failure_codes": ["capture_failed"],
        },
        {
            **healthy_connected_details(next_check_seconds=24),
            "captured_windows": 1,
        },
    ]
    monitor = SmartReconnectMonitor(
        FakeBoundary(
            [
                OperationResult(
                    True,
                    "reconnect.connected",
                    details=details,
                )
                for details in incomplete_or_failed
            ]
        ),
        monitor_interval_ms=1500,
    )

    assert [monitor.run_once()[1] for _ in incomplete_or_failed] == [
        21,
        2,
        2,
        2,
    ]


def test_full_health_waits_30_minutes_before_downgrade(monkeypatch):
    healthy = OperationResult(
        True,
        "reconnect.connected",
        details=healthy_connected_details(next_check_seconds=5, windows=3),
    )
    monitor = SmartReconnectMonitor(
        FakeBoundary([healthy, healthy, healthy]),
        fallback_delay_seconds=60,
        monitor_interval_ms=2000,
    )
    times = iter((100.0, 120.0, 2000.0))
    monkeypatch.setattr(
        smart_reconnect_monitor_module.time,
        "monotonic",
        lambda: next(times),
    )

    assert monitor.run_once()[1] == 2
    assert monitor.run_once()[1] == 2
    assert monitor.run_once()[1] == 60


def test_monitoring_interval_can_change_without_restarting_monitor():
    monitor = SmartReconnectMonitor(FakeBoundary([]))

    assert monitor.monitor_interval_ms == 2000
    assert monitor.set_monitor_interval_ms(2500) is True
    assert monitor.monitor_interval_ms == 2500
    assert monitor.set_monitor_interval_ms(0) is False
    assert monitor.monitor_interval_ms == 2500
    assert monitor.set_monitor_interval_ms(499) is False
    assert monitor.set_monitor_interval_ms(60001) is False


def test_start_and_stop_are_idempotent():
    boundary = FakeBoundary([])
    monitor = SmartReconnectMonitor(boundary)

    assert monitor.start() is True
    assert boundary.execution_enabled is True
    assert monitor.start() is False
    assert monitor.stop() is True
    assert boundary.execution_enabled is False
    assert monitor.stop() is True
    assert monitor.running is False
    assert boundary.execution_changes[0] is True
    assert boundary.execution_changes[-1] is False


def test_start_reports_reconnecting_while_first_scan_is_warming_up():
    boundary = BlockingBoundary()
    monitor = SmartReconnectMonitor(boundary)

    assert monitor.start() is True
    assert boundary.started.wait(1) is True
    assert monitor.runtime_status == "重連中"

    boundary.release.set()
    assert monitor.stop(timeout_seconds=1) is True


def test_first_capture_failure_after_start_fails_without_fake_grace():
    failure = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "connected_windows": 0,
            "unknown_windows": 12,
            "state_counts": {"unknown": 12},
            "failure_codes": ["capture_failed", "screen_unknown"],
            "next_check_seconds": 60,
        },
    )
    logger = NotifyingInfoLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary([failure]),
        logger=logger,
        fallback_delay_seconds=60,
    )

    assert monitor.start() is True
    assert logger.info_recorded.wait(1) is True
    assert monitor.runtime_status == "重連失敗"
    assert monitor.stop(timeout_seconds=1) is True


def test_first_connected_count_sets_baseline_before_real_increase_progresses():
    initial_failure = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "connected_windows": 5,
            "unknown_windows": 7,
            "state_counts": {"connected": 5, "unknown": 7},
            "failure_codes": ["capture_failed", "screen_unknown"],
            "next_check_seconds": 60,
        },
    )
    increased = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "connected_windows": 6,
            "unknown_windows": 6,
            "state_counts": {"connected": 6, "unknown": 6},
            "failure_codes": ["capture_failed", "screen_unknown"],
            "next_check_seconds": 60,
        },
    )

    class GatedIncreaseBoundary(FakeBoundary):
        def __init__(self):
            super().__init__([initial_failure, increased])
            self.allow_second_result = threading.Event()

        def reconnect(self):
            if self.calls == 1:
                self.allow_second_result.wait(2)
            return super().reconnect()

    boundary = GatedIncreaseBoundary()
    logger = NotifyingInfoLogger()
    monitor = SmartReconnectMonitor(
        boundary,
        logger=logger,
        fallback_delay_seconds=60,
    )

    assert monitor.start() is True
    assert logger.info_recorded.wait(1) is True
    assert monitor.runtime_status == "重連失敗"

    logger.info_recorded.clear()
    boundary.allow_second_result.set()
    assert monitor.set_monitor_interval_ms(2500) is True
    assert logger.info_recorded.wait(1) is True
    assert monitor.runtime_status == "重連中"
    assert monitor.stop(timeout_seconds=1) is True


def test_stop_and_restart_do_not_reuse_previous_recovery_progress():
    progressed = OperationResult(
        True,
        "reconnect.progressed_with_isolation",
        details={
            "connected_windows": 8,
            "clicked_windows": 1,
            "state_counts": {"connected": 8, "disconnected": 1},
            "failure_codes": ["screen_unknown"],
            "next_check_seconds": 60,
        },
    )
    failure = OperationResult(
        False,
        "reconnect.waiting",
        details={
            "connected_windows": 9,
            "unknown_windows": 3,
            "state_counts": {"connected": 9, "unknown": 3},
            "failure_codes": ["capture_failed", "screen_unknown"],
            "next_check_seconds": 60,
        },
    )
    logger = NotifyingInfoLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary([progressed, failure]),
        logger=logger,
        fallback_delay_seconds=60,
    )

    assert monitor.start() is True
    assert logger.info_recorded.wait(1) is True
    assert monitor.runtime_status == "重連中"
    assert monitor.stop(timeout_seconds=1) is True

    logger.info_recorded.clear()
    assert monitor.start() is True
    assert logger.info_recorded.wait(1) is True
    assert monitor.runtime_status == "重連失敗"
    assert monitor.stop(timeout_seconds=1) is True


def test_one_cycle_exception_marks_failure_then_monitor_continues_and_stops():
    class FailsOnceBoundary(FakeBoundary):
        def __init__(self):
            super().__init__([])
            self.failed = threading.Event()
            self.recovered = threading.Event()

        def reconnect(self):
            self.calls += 1
            if self.calls == 1:
                self.failed.set()
                raise RuntimeError("單輪測試失敗")
            self.recovered.set()
            return OperationResult(
                True,
                "reconnect.connected",
                details=healthy_connected_details(),
            )

    class NotifyingLogger(RecordingLogger):
        def __init__(self):
            super().__init__()
            self.error_recorded = threading.Event()

        def error(self, message):
            super().error(message)
            self.error_recorded.set()

    boundary = FailsOnceBoundary()
    logger = NotifyingLogger()
    monitor = SmartReconnectMonitor(boundary, logger=logger)

    assert monitor.start() is True
    assert boundary.failed.wait(1) is True
    assert logger.error_recorded.wait(1) is True
    assert monitor.runtime_status == "重連失敗"

    assert monitor.set_monitor_interval_ms(2500) is True
    assert boundary.recovered.wait(1) is True
    assert monitor.stop(timeout_seconds=1) is True
    assert monitor.running is False
    assert boundary.calls >= 2


def test_start_failure_recloses_controller_execution(monkeypatch):
    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

        def is_alive(self):
            return False

    boundary = FakeBoundary([])
    monitor = SmartReconnectMonitor(boundary)
    monkeypatch.setattr(threading, "Thread", FailingThread)

    assert monitor.start() is False
    assert monitor.running is False
    assert boundary.execution_enabled is False
    assert boundary.execution_changes == [True, False]


def test_stop_blocks_concurrent_start_until_execution_is_closed(monkeypatch):
    native_thread = threading.Thread

    class StoppedThread:
        def is_alive(self):
            return False

        def join(self, _timeout):
            pass

    class ImmediateThread(StoppedThread):
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    class BlockingDisableBoundary(FakeBoundary):
        def __init__(self):
            super().__init__([])
            self.disable_started = threading.Event()
            self.allow_disable = threading.Event()

        def set_execution_enabled(self, enabled):
            super().set_execution_enabled(enabled)
            if not enabled:
                self.disable_started.set()
                self.allow_disable.wait(1)

    boundary = BlockingDisableBoundary()
    monitor = SmartReconnectMonitor(boundary)
    monitor._thread = StoppedThread()
    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    stop_results = []
    start_results = []
    start_finished = threading.Event()

    stop_worker = native_thread(
        target=lambda: stop_results.append(monitor.stop()),
    )
    stop_worker.start()
    assert boundary.disable_started.wait(1) is True

    def start_monitor():
        start_results.append(monitor.start())
        start_finished.set()

    start_worker = native_thread(target=start_monitor)
    start_worker.start()
    assert start_finished.wait(0.2) is False

    boundary.allow_disable.set()
    stop_worker.join(1)
    start_worker.join(1)

    assert stop_results == [True]
    assert start_results == [True]


def test_stop_timeout_remains_running_and_disables_all_execution():
    boundary = BlockingBoundary()
    monitor = SmartReconnectMonitor(boundary)

    assert monitor.start() is True
    assert boundary.started.wait(1) is True
    assert monitor.stop(timeout_seconds=0) is False
    assert monitor.running is True
    assert boundary.execution_enabled is False

    boundary.release.set()
    assert monitor.stop(timeout_seconds=1) is True
    assert monitor.running is False
