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
        details={
            "connected_windows": 2,
            "actionable_windows": 0,
            "clicked_windows": 0,
            "unknown_windows": 0,
            "next_check_seconds": 5,
            "state_counts": {"connected": 2},
            "failure_codes": [],
        },
    )
    logger = RecordingLogger()
    monitor = SmartReconnectMonitor(
        FakeBoundary([result, result]),
        logger=logger,
    )

    monitor.run_once()
    monitor.run_once()

    assert len(logger.info_messages) == 1


def test_passive_waiting_uses_saved_monitoring_interval():
    monitor = SmartReconnectMonitor(
        FakeBoundary(
            [
                OperationResult(False, "reconnect.waiting", details={}),
                OperationResult(
                    False,
                    "reconnect.waiting",
                    details={"next_check_seconds": 0},
                ),
            ]
        ),
        monitor_interval_ms=1500,
    )

    assert monitor.run_once()[1] == 1.5
    assert monitor.run_once()[1] == 1.5


def test_monitoring_interval_can_change_without_restarting_monitor():
    monitor = SmartReconnectMonitor(FakeBoundary([]))

    assert monitor.monitor_interval_ms == 1000
    assert monitor.set_monitor_interval_ms(2500) is True
    assert monitor.monitor_interval_ms == 2500
    assert monitor.set_monitor_interval_ms(0) is False
    assert monitor.monitor_interval_ms == 2500


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
