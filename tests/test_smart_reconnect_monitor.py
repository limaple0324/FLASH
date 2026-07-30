import threading

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

    assert monitor.run_once()[1] == 1.5


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

    assert monitor.run_once()[1] == 30


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
        22,
        23,
        24,
    ]


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
