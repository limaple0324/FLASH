"""Per-role synchronization adjustment values proven by the legacy app."""

from __future__ import annotations

from dataclasses import dataclass


MAX_SYNC_DELAY_MS = 5_000
MAX_SYNC_OFFSET_PX = 20_000


def clamp_sync_delay_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(MAX_SYNC_DELAY_MS, max(0, value))


def clamp_sync_offset_px(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(MAX_SYNC_OFFSET_PX, max(-MAX_SYNC_OFFSET_PX, value))


@dataclass(frozen=True, slots=True)
class SyncTargetSettings:
    """A target's optional pixel correction and independent dispatch delay."""

    offset_enabled: bool = False
    offset_x: int = 0
    offset_y: int = 0
    delay_ms: int = 0

    @classmethod
    def normalized(
        cls,
        *,
        offset_enabled: object = False,
        offset_x: object = 0,
        offset_y: object = 0,
        delay_ms: object = 0,
    ) -> "SyncTargetSettings":
        return cls(
            offset_enabled=(
                offset_enabled if isinstance(offset_enabled, bool) else False
            ),
            offset_x=clamp_sync_offset_px(offset_x),
            offset_y=clamp_sync_offset_px(offset_y),
            delay_ms=clamp_sync_delay_ms(delay_ms),
        )

