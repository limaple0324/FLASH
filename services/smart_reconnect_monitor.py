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
FULLY_CONNECTED_STABLE_SECONDS = 30 * 60
RECOVERY_POLL_SECONDS = 2.0
SMART_RECONNECT_MODE_BALANCED = "balanced"
SMART_RECONNECT_MODE_HIGH_PERFORMANCE = "high_performance"
SMART_RECONNECT_MONITOR_MODES = (
    SMART_RECONNECT_MODE_BALANCED,
    SMART_RECONNECT_MODE_HIGH_PERFORMANCE,
)
SMART_RECONNECT_STATUS_ENABLED = "已開啟"
SMART_RECONNECT_STATUS_RECONNECTING = "重連中"
SMART_RECONNECT_STATUS_FAILED = "重連失敗"
_RECOVERY_RESULT_CODES = frozenset(
    {
        "reconnect.waiting",
        "reconnect.progressed",
        "reconnect.progressed_with_isolation",
    }
)
_TRANSIENT_RECOVERY_FAILURE_CODES = frozenset(
    {"capture_failed", "screen_unknown"}
)


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


def normalize_smart_reconnect_mode(
    value: object,
    *,
    default: str = SMART_RECONNECT_MODE_BALANCED,
) -> str:
    if value in SMART_RECONNECT_MONITOR_MODES:
        return str(value)
    return default


class SmartReconnectMonitor:
    """Run safe reconnect scans until explicitly stopped with the application."""

    def __init__(
        self,
        boundary: SmartReconnectBoundary,
        *,
        logger: LoggerService | None = None,
        fallback_delay_seconds: int = 60,
        monitor_interval_ms: int = DEFAULT_SMART_RECONNECT_INTERVAL_MS,
        monitor_mode: str = SMART_RECONNECT_MODE_BALANCED,
    ):
        if fallback_delay_seconds <= 0:
            raise ValueError("fallback_delay_seconds must be positive")
        self._boundary = boundary
        self._logger = logger
        self._fallback_delay_seconds = fallback_delay_seconds
        self._monitor_interval_ms = normalize_smart_reconnect_interval_ms(
            monitor_interval_ms
        )
        self._monitor_mode = normalize_smart_reconnect_mode(
            monitor_mode,
        )
        self._stop_event = threading.Event()
        self._settings_changed_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_signature: tuple[object, ...] | None = None
        self._disconnected_without_progress_at: float | None = None
        self._disconnected_without_progress_reported = False
        self._fully_connected_stable_from: float | None = None
        self._runtime_status: str | None = None
        self._runtime_recovery_progress_at: float | None = None
        self._runtime_connected_high_watermark: int | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def monitor_interval_ms(self) -> int:
        with self._lock:
            return self._monitor_interval_ms

    @property
    def monitor_mode(self) -> str:
        with self._lock:
            return self._monitor_mode

    @property
    def runtime_status(self) -> str | None:
        with self._lock:
            return self._runtime_status if self.running else None

    def _set_runtime_status(
        self,
        result: OperationResult,
        *,
        now: float | None = None,
    ) -> None:
        if now is None:
            now = time.monotonic()
        details = result.details or {}
        failure_codes = details.get("failure_codes")
        normalized_failure_codes = (
            frozenset(str(item) for item in failure_codes)
            if isinstance(failure_codes, (list, tuple))
            else frozenset()
        )
        safely_paused_for_rebinding = (
            result.code == "reconnect.operation_paused"
            and normalized_failure_codes == {"operation_gate_closed"}
        )
        safe_unknown_wait = (
            result.code == "reconnect.waiting"
            and normalized_failure_codes.issubset({"screen_unknown"})
        )
        terminal_failure_codes = (
            normalized_failure_codes
            - _TRANSIENT_RECOVERY_FAILURE_CODES
        )
        recovering = self._contains_recovery_states(details)
        execution_progress = any(
            self._has_positive_count(details, name)
            for name in ("clicked_windows", "restarted_windows")
        )
        connected_windows = details.get("connected_windows")
        connected_count = (
            connected_windows
            if isinstance(connected_windows, int)
            and not isinstance(connected_windows, bool)
            and connected_windows >= 0
            else None
        )
        with self._lock:
            previous_progress_at = self._runtime_recovery_progress_at
            previous_connected_high = self._runtime_connected_high_watermark
        if connected_count is None:
            connected_increased = False
            observed_connected_high = previous_connected_high
        elif previous_connected_high is None:
            connected_increased = False
            observed_connected_high = connected_count
        else:
            connected_increased = connected_count > previous_connected_high
            observed_connected_high = max(
                previous_connected_high,
                connected_count,
            )
        controller_progressed = result.code in {
            "reconnect.progressed",
            "reconnect.progressed_with_isolation",
        }
        recovery_progress = bool(
            result.code in _RECOVERY_RESULT_CODES
            and (
                controller_progressed
                or execution_progress
                or connected_increased
            )
        )
        recent_recovery_progress = bool(
            previous_progress_at is not None
            and now - previous_progress_at <= self._fallback_delay_seconds
        )
        transient_failure_during_recovery = bool(
            result.code == "reconnect.waiting"
            and normalized_failure_codes
            and normalized_failure_codes.issubset(
                _TRANSIENT_RECOVERY_FAILURE_CODES
            )
            and recent_recovery_progress
        )
        is_full_health = self._is_fully_connected_healthy(result)
        next_connected_high = observed_connected_high
        if is_full_health:
            status = SMART_RECONNECT_STATUS_ENABLED
            next_progress_at = None
        elif safely_paused_for_rebinding:
            status = SMART_RECONNECT_STATUS_RECONNECTING
            next_progress_at = previous_progress_at
        elif terminal_failure_codes:
            status = SMART_RECONNECT_STATUS_FAILED
            next_progress_at = previous_progress_at
        elif recovery_progress:
            status = SMART_RECONNECT_STATUS_RECONNECTING
            next_progress_at = now
        elif (
            safe_unknown_wait
            or transient_failure_during_recovery
            or (
                result.code in _RECOVERY_RESULT_CODES
                and recovering
                and not normalized_failure_codes
            )
        ):
            status = SMART_RECONNECT_STATUS_RECONNECTING
            next_progress_at = previous_progress_at
        elif not result.success or normalized_failure_codes:
            status = SMART_RECONNECT_STATUS_FAILED
            next_progress_at = previous_progress_at
        else:
            status = SMART_RECONNECT_STATUS_ENABLED
            next_progress_at = None
        with self._lock:
            if self.running:
                self._runtime_status = status
                self._runtime_recovery_progress_at = next_progress_at
                self._runtime_connected_high_watermark = next_connected_high

    def set_monitor_mode(self, value: object) -> bool:
        normalized = normalize_smart_reconnect_mode(value)
        with self._lock:
            changed = normalized != self._monitor_mode
            self._monitor_mode = normalized
        if changed:
            self._settings_changed_event.set()
        return True

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
            tuple(failure_codes)
            if isinstance(failure_codes, (list, tuple))
            else (),
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

    def _recovery_poll_seconds(self) -> float:
        if self.monitor_mode == SMART_RECONNECT_MODE_HIGH_PERFORMANCE:
            return max(1.0, self.monitor_interval_ms / 1000.0)
        return RECOVERY_POLL_SECONDS

    @staticmethod
    def _has_open_windows(details: Mapping[str, object]) -> bool:
        discovered = details.get("discovered_windows")
        return (
            isinstance(discovered, int)
            and not isinstance(discovered, bool)
            and discovered > 0
        )

    @staticmethod
    def _contains_disconnected_windows(details: Mapping[str, object]) -> bool:
        state_counts = details.get("state_counts")
        disconnected = (
            state_counts.get("disconnected")
            if isinstance(state_counts, Mapping)
            else 0
        )
        return (
            isinstance(disconnected, int)
            and not isinstance(disconnected, bool)
            and disconnected > 0
        )

    @staticmethod
    def _contains_recovery_states(details: Mapping[str, object]) -> bool:
        state_counts = details.get("state_counts")
        if not isinstance(state_counts, Mapping):
            return False
        recovery_states = {
            "disconnected",
            "login_start",
            "force_login_start",
            "force_login_timeout",
            "line_selection",
            "character_selection",
            "post_login_activity",
            "post_login_recommendation",
            "post_login_auto_dungeon",
            "reconnecting",
        }
        return any(
            state in recovery_states
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
            for state, count in state_counts.items()
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
        details = result.details or {}
        now = time.monotonic()
        self._set_runtime_status(result, now=now)

        delay = self._safe_delay(result, self._fallback_delay_seconds)
        is_full_health = self._is_fully_connected_healthy(result)
        has_open_windows = self._has_open_windows(details)
        recovery_interval = self._recovery_poll_seconds()
        if self._contains_disconnected_windows(details):
            delay = recovery_interval
            self._fully_connected_stable_from = None
        elif has_open_windows:
            if is_full_health:
                if self._fully_connected_stable_from is None:
                    self._fully_connected_stable_from = now
                    delay = recovery_interval
                elif now - self._fully_connected_stable_from >= FULLY_CONNECTED_STABLE_SECONDS:
                    if self.monitor_mode == SMART_RECONNECT_MODE_BALANCED:
                        delay = max(
                            float(self._fallback_delay_seconds),
                            delay,
                        )
                    else:
                        delay = recovery_interval
                else:
                    delay = recovery_interval
            else:
                self._fully_connected_stable_from = None
                delay = recovery_interval
        else:
            self._fully_connected_stable_from = None
        self._report_stalled_reconnect(result, details, now)
        signature = self._signature(result)
        if self._logger is not None and signature != self._last_signature:
            state_counts = details.get("state_counts", {})
            failure_codes = details.get("failure_codes", ())
            safe_failure_codes = (
                ",".join(
                    str(code)
                    for code in failure_codes
                    if isinstance(code, str) and code
                )
                if isinstance(failure_codes, (list, tuple))
                else ""
            )
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
                f"failure_codes={safe_failure_codes or 'none'}; "
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
                with self._lock:
                    self._runtime_status = SMART_RECONNECT_STATUS_FAILED
                    self._runtime_recovery_progress_at = None
                    self._runtime_connected_high_watermark = None
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
            self._runtime_status = SMART_RECONNECT_STATUS_RECONNECTING
            self._runtime_recovery_progress_at = None
            self._runtime_connected_high_watermark = None
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
                self._runtime_status = None
                self._runtime_recovery_progress_at = None
                self._runtime_connected_high_watermark = None
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
                    self._runtime_status = None
                    self._runtime_recovery_progress_at = None
                    self._runtime_connected_high_watermark = None
        return stopped

