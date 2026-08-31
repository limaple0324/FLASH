"""Fresh game-owned UTC anchors, advanced only by QPC with bounded slewing.

The acquisition bracket describes observation timing, NOT server/network accuracy.
Wall time is permitted only as a reader sanity filter, never as a clock source.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

DAY_MS = 86_400_000
UTC8_MS = 28_800_000
SAMPLE_MAX_AGE_NS = 30_000_000_000
HEALTH_MAX_GAP_NS = 3_000_000_000
MAX_SLEW_ERROR_MS = 250.0
SLEW_MS_PER_SECOND = 50.0


@dataclass(frozen=True)
class SourceIdentity:
    hwnd: int
    pid: int
    tid: int
    created: int
    launch_fingerprint: str
    image_sha256: str
    normalized_target: str = ""


@dataclass(frozen=True)
class ClockSample:
    identity: SourceIdentity
    server_ms: float
    anchor_ns: int
    observation_bracket_ns: int
    read_latency_ns: int
    transition_ms: float
    profile: str
    response_interval_ms: float = 0.0
    quality_observations: tuple = ()


class GameClock:
    def __init__(self, monotonic_ns=time.perf_counter_ns):
        self._now = monotonic_ns
        self.sample: ClockSample | None = None
        self.reason = "尚未校正"
        self.invalidation_version = 0
        self._last_now = None
        self._invalid_before_ns = 0
        self._last_anchor_ns = 0
        self._last_identity = None
        self._last_server_ms = 0.0
        self._model_ns = 0
        self._model_ms = 0.0
        self.last_correction_ms = None

    def invalidate(self, reason="來源已失效", now_ns=None):
        now = self._now() if now_ns is None else now_ns
        # Old queued anchors cannot revive a clock after a fault, even after a
        # QPC rollback. A different, explicitly selected source still needs a NEW edge.
        self._invalid_before_ns = max(self._invalid_before_ns, now, self._last_now or 0)
        # A fault is an edge, not the absence of a sample. Other consumers can
        # observe it before the acquisition poll; repeated invalid reads are inert.
        if self.sample is not None:
            self.invalidation_version += 1
        self.sample = None
        self.reason = reason

    def _check_time(self, now):
        previous = self._last_now
        self._last_now = max(now, previous or 0)
        if previous is not None and now < previous:
            self.invalidate("單調時鐘逆序", now)
            return False
        if self.sample is not None:
            if previous is not None and now - previous > HEALTH_MAX_GAP_NS:
                self.invalidate("時鐘更新中斷", now)
                return False
            if now - self.sample.anchor_ns > SAMPLE_MAX_AGE_NS:
                self.invalidate("校時樣本已過期", now)
                return False
        return True

    def _advance(self, now):
        elapsed_ms = (now - self._model_ns) / 1_000_000
        advanced = self._model_ms + elapsed_ms
        target = self.sample.server_ms + (now - self.sample.anchor_ns) / 1_000_000
        limit = elapsed_ms * SLEW_MS_PER_SECOND / 1000
        self._model_ms = advanced + max(-limit, min(limit, target - advanced))
        self._model_ns = now
        return self._model_ms

    def calibrate_once(self, sample: ClockSample) -> bool:
        """Compatibility name; continuous callers use accept_sample."""
        return self.accept_sample(sample)

    def accept_sample(self, sample: ClockSample) -> bool:
        now = self._now()
        if not self._check_time(now) or not isinstance(sample, ClockSample):
            return False
        identity = sample.identity
        if (not isinstance(identity, SourceIdentity)
                or min(identity.hwnd, identity.pid, identity.tid, identity.created) <= 0
                or not identity.launch_fingerprint or not identity.image_sha256
                or not math.isfinite(sample.server_ms)
                or not 1_000_000_000_000 <= sample.server_ms <= 2_200_000_000_000
                or not 0 < sample.anchor_ns <= now
                or now - sample.anchor_ns > SAMPLE_MAX_AGE_NS
                or sample.anchor_ns <= max(self._last_anchor_ns, self._invalid_before_ns)
                or not 0 < sample.observation_bracket_ns <= 25_000_000
                or not 0 <= sample.read_latency_ns <= 20_000_000
                or not 1000 <= sample.transition_ms <= 30_000
                or not math.isfinite(sample.response_interval_ms)
                or not 0 <= sample.response_interval_ms <= 100
                or not sample.profile):
            return False
        if (identity == self._last_identity and sample.server_ms <= self._last_server_ms):
            return False
        if self.sample is not None and identity != self.sample.identity:
            self.invalidate("來源身分已改變", now)
            return False
        target = sample.server_ms + (now - sample.anchor_ns) / 1_000_000
        if self.sample is not None:
            current = self._advance(now)
            self.last_correction_ms = target - current  # independent PRE-correction residual
            if abs(self.last_correction_ms) > MAX_SLEW_ERROR_MS:
                self.invalidate("新樣本偏差超過250ms，等待重新校正", now)
                return False
        else:
            self._model_ns, self._model_ms = now, target
            self.last_correction_ms = None
        self.sample = sample
        self._last_anchor_ns = sample.anchor_ns
        self._last_identity, self._last_server_ms = identity, sample.server_ms
        self.reason = "持續校正"
        return True

    def utc_ms(self) -> int | None:
        now = self._now()
        if not self._check_time(now) or self.sample is None:
            return None
        return int(self._advance(now))

    def time_of_day_ms(self) -> int | None:
        value = self.utc_ms()
        return None if value is None else (value + UTC8_MS) % DAY_MS

    def text(self) -> str | None:
        value = self.time_of_day_ms()
        if value is None:
            return None
        return f"{value // 3600000:02d}:{value // 60000 % 60:02d}:{value // 1000 % 60:02d}.{value % 1000:03d}"
