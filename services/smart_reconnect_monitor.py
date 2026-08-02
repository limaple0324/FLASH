"""Background monitor for the registered SP1 smart reconnect boundary."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping

from core.sp1_boundaries import OperationResult, SmartReconnectBoundary
from services.logger_service import LoggerService


DEFAULT_SMART_RECONNECT_INTERVAL_MS = 2000
MINIMUM_SMART_RECONNECT_INTERVAL_MS = 500
MAXIMUM_SMART_RECONNECT_INTERVAL_MS = 60_000


def normalize_smart_reconnect_interval_ms(
    value: object,
    *,
    default: int = DEFAULT_SMART_RECONNECT_INTERVAL_MS,
) -> int:
    """Preserve the V0.2 millisecond setting while rejecting invalid values."""
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    if not (
        MINIMUM_SMART_RECONNECT_INTERVAL_MS
        <= normalized
        <= MAXIMUM_SMART_RECONNECT_INTERVAL_MS
    ):
        return default
    return normalized


class SmartReconnectMonitor:
    """Run safe reconnect scans until explicitly stopped with the application."""

    def __init__(
        self,
        boundary: SmartReconnectBoundary,
        *,
        logger: LoggerService | None = None,
        fallback_delay_seconds: int = 60,
        monitor_interval_ms: int = DEFAULT_SMART_RECONNECT_INTERVAL_MS,
    ):
        if fallback_delay_seconds <= 0:
            raise ValueError("fallback_delay_seconds must be positive")
        self._boundary = boundary
        self._logger = logger
        self._fallback_delay_seconds = fallback_delay_seconds
        self._monitor_interval_ms = normalize_smart_reconnect_interval_ms(
            monitor_interval_ms
        )
        self._stop_event = threading.Event()
        self._settings_changed_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_signature: tuple[object, ...] | None = None
        self._disconnected_without_progress_at: float | None = None
        self._disconnected_without_progress_reported = False

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def monitor_interval_ms(self) -> int:
        with self._lock:
            return self._monitor_interval_ms

    def set_monitor_interval_ms(self, value: object) -> bool:
        normalized = normalize_smart_reconnect_interval_ms(
            value,
            default=0,
        )
        if normalized <= 0:
            return False
        with self._lock:
            changed = normalized != self._monitor_interval_ms
            self._monitor_interval_ms = normalized
        if changed:
            self._settings_changed_event.set()
        return True

    @staticmethod
    def _safe_delay(result: OperationResult, fallback: int) -> int:
        details = result.details
        value = details.get("next_check_seconds") if details is not None else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return fallback
        return max(1, int(value))

    @staticmethod
    def _signature(result: OperationResult) -> tuple[object, ...]:
        details = result.details or {}
        state_counts = details.get("state_counts", {})
        failure_codes = details.get("failure_codes", [])
        return (
            result.code,
            tuple(sorted(state_counts.items()))
            if isinstance(state_counts, dict)
            else (),
            tuple(failure_codes) if isinstance(failure_codes, list) else (),
            details.get("clicked_windows"),
            details.get("restarted_windows"),
        )

    @staticmethod
    def _is_fully_connected_healthy(result: OperationResult) -> bool:
        """Only a complete, failure-free scan may use the short monitor cadence."""
        if not result.success or result.code != "reconnect.connected":
            return False
        details = result.details
        if details is None or details.get("all_connected") is not True:
            return False
        discovered = details.get("discovered_windows")
        if (
            isinstance(discovered, bool)
            or not isinstance(discovered, int)
            or discovered <= 0
        ):
            return False
        for name in (
            "validated_windows",
            "captured_windows",
            "recognized_windows",
            "connected_windows",
        ):
            value = details.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != discovered
            ):
                return False
        for name in (
            "actionable_windows",
            "clicked_windows",
            "restarted_windows",
            "unknown_windows",
        ):
            value = details.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value != 0:
                return False
        failure_codes = details.get("failure_codes")
        return (
            isinstance(failure_codes, (list, tuple))
            and not failure_codes
        )

    @staticmethod
    def _has_positive_count(
        details: Mapping[str, object],
        name: str,
    ) -> bool:
        value = details.get(name)
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        )

    def _report_stalled_reconnect(
        self,
        result: OperationResult,
        details: Mapping[str, object],
        now: float,
    ) -> None:
        state_counts = details.get("state_counts")
        disconnected = (
            state_counts.get("disconnected")
            if isinstance(state_counts, Mapping)
            else 0
        )
        detected = (
            isinstance(disconnected, int)
            and not isinstance(disconnected, bool)
            and disconnected > 0
        )
        started = self._has_positive_count(details, "clicked_windows") or (
            self._has_positive_count(details, "restarted_windows")
        )
        if not detected or started:
            self._disconnected_without_progress_at = None
            self._disconnected_without_progress_reported = False
            return
        if self._disconnected_without_progress_at is None:
            self._disconnected_without_progress_at = now
            return
        interval_seconds = self.monitor_interval_ms / 1000.0
        if (
            self._disconnected_without_progress_reported
            or now - self._disconnected_without_progress_at
            <= interval_seconds
        ):
            return
        self._disconnected_without_progress_reported = True
        if self._logger is not None:
            self._logger.error(
                "Smart reconnect detected a disconnection without starting "
                "recovery within one monitoring interval; "
                f"code={result.code}; "
                f"disconnected={disconnected}; "
                f"interval_seconds={interval_seconds}"
            )

    def run_once(self) -> tuple[OperationResult, float]:
        result = self._boundary.reconnect()
        delay = self._safe_delay(result, self._fallback_delay_seconds)
        details = result.details or {}
        if self._is_fully_connected_healthy(result):
            delay = max(0.001, self.monitor_interval_ms / 1000.0)
        self._report_stalled_reconnect(result, details, time.monotonic())
        signature = self._signature(result)
        if self._logger is not None and signature != self._last_signature:
            state_counts = details.get("state_counts", {})
            safe_states = (
                ",".join(
                    f"{state}:{count}"
                    for state, count in sorted(state_counts.items())
                    if isinstance(state, str)
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                )
                if isinstance(state_counts, dict)
                else ""
            )
            self._logger.info(
                "Smart reconnect state changed; "
                f"code={result.code}; "
                f"states={safe_states or 'none'}; "
                f"connected={details.get('connected_windows', 0)}; "
                f"actionable={details.get('actionable_windows', 0)}; "
                f"clicked={details.get('clicked_windows', 0)}; "
                f"restarted={details.get('restarted_windows', 0)}; "
                f"unknown={details.get('unknown_windows', 0)}; "
                f"next_check_seconds={delay}"
            )
        self._last_signature = signature
        return result, delay

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                _result, delay = self.run_once()
            except Exception as exc:
                delay = self._fallback_delay_seconds
                if self._logger is not None:
                    self._logger.error(
                        "Smart reconnect monitor cycle failed safely; "
                        f"error_type={type(exc).__name__}; "
                        f"next_check_seconds={delay}"
                    )
            if self._settings_changed_event.wait(delay):
                self._settings_changed_event.clear()
            if self._stop_event.is_set():
                break

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            execution_switch = getattr(
                self._boundary,
                "set_execution_enabled",
                None,
            )
            if callable(execution_switch):
                execution_switch(True)
            self._stop_event.clear()
            self._settings_changed_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="FLASH-SmartReconnect",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                if callable(execution_switch):
                    execution_switch(False)
                self._stop_event.set()
                self._settings_changed_event.set()
                return False
            return True

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        execution_switch = getattr(
            self._boundary,
            "set_execution_enabled",
            None,
        )
        with self._lock:
            if callable(execution_switch):
                execution_switch(False)
            thread = self._thread
            if thread is None:
                return True
            self._stop_event.set()
            self._settings_changed_event.set()
        if thread is not threading.current_thread():
            thread.join(max(0.0, timeout_seconds))
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

