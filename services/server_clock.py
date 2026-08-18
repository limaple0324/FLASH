"""唯讀遊戲伺服器時間的單次校正時鐘。"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from adapters.windows_launch_fingerprint import normalize_launch_fingerprint


PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class ServerTimeSourceIdentity:
    handle: int
    process_id: int
    thread_id: int
    lifecycle: int
    fingerprint: str

    def __post_init__(self) -> None:
        fingerprint = normalize_launch_fingerprint(self.fingerprint)
        if fingerprint is None:
            raise ValueError("fingerprint must be a complete SHA-256 value")
        for name in ("handle", "process_id", "thread_id", "lifecycle"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        object.__setattr__(self, "fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class ServerTimeSample:
    protocol_version: int
    source_instance_identity: ServerTimeSourceIdentity
    server_now_ms: int | float
    sample_local_flash_timer: int | float
    sample_sequence: int


@dataclass(frozen=True, slots=True)
class ServerClockSnapshot:
    state: str
    server_base_ms: int | None
    local_base_monotonic_ns: int | None
    calibration_count: int


class ServerClock:
    """每個「輔」程序生命週期只接受第一筆合法伺服器時間。"""

    UNCALIBRATED = "UNCALIBRATED"
    CALIBRATED = "CALIBRATED"

    def __init__(
        self,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        source_validator: Callable[[ServerTimeSourceIdentity], bool] | None = None,
    ) -> None:
        self._monotonic_ns = monotonic_ns
        self._source_validator = source_validator
        self._lock = threading.Lock()
        self._state = self.UNCALIBRATED
        self._server_base_ms: int | None = None
        self._local_base_monotonic_ns: int | None = None
        self._calibration_count = 0

    @property
    def calibration_count(self) -> int:
        return self._calibration_count

    def snapshot(self) -> ServerClockSnapshot:
        with self._lock:
            return ServerClockSnapshot(
                self._state,
                self._server_base_ms,
                self._local_base_monotonic_ns,
                self._calibration_count,
            )

    @staticmethod
    def _valid_number(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        )

    def _valid_sample(self, sample: ServerTimeSample) -> bool:
        if not isinstance(sample, ServerTimeSample):
            return False
        if sample.protocol_version != PROTOCOL_VERSION:
            return False
        if not self._valid_number(sample.server_now_ms):
            return False
        if not self._valid_number(sample.sample_local_flash_timer):
            return False
        if (
            isinstance(sample.sample_sequence, bool)
            or not isinstance(sample.sample_sequence, int)
            or sample.sample_sequence < 0
        ):
            return False
        if self._source_validator is not None:
            try:
                if not self._source_validator(sample.source_instance_identity):
                    return False
            except Exception:
                return False
        return True

    def calibrate_once(self, sample: ServerTimeSample) -> bool:
        if not self._valid_sample(sample):
            return False
        with self._lock:
            if self._state != self.UNCALIBRATED:
                return False
            self._server_base_ms = int(round(float(sample.server_now_ms)))
            self._local_base_monotonic_ns = int(self._monotonic_ns())
            self._calibration_count = 1
            self._state = self.CALIBRATED
            return True

    def now_ms(self) -> int | None:
        with self._lock:
            if (
                self._state != self.CALIBRATED
                or self._server_base_ms is None
                or self._local_base_monotonic_ns is None
            ):
                return None
            elapsed_ms = (
                int(self._monotonic_ns()) - self._local_base_monotonic_ns
            ) // 1_000_000
            return self._server_base_ms + elapsed_ms

