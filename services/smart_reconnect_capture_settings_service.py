"""Persistent capture-mode settings for smart reconnect."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from config.config_manager import ConfigManager


SMART_RECONNECT_CAPTURE_MODES_KEY = "smart_reconnect_capture_modes"
VISIBLE_CAPTURE_MODE = "visible"
OBSCURED_CAPTURE_MODE = "obscured"
MINIMIZED_CAPTURE_MODE = "minimized"
SMART_RECONNECT_CAPTURE_MODE_KEYS = (
    VISIBLE_CAPTURE_MODE,
    OBSCURED_CAPTURE_MODE,
    MINIMIZED_CAPTURE_MODE,
)


@dataclass(frozen=True, slots=True)
class SmartReconnectCaptureSettings:
    """Three independently controlled ways to inspect a game window."""

    visible: bool = True
    obscured: bool = True
    minimized: bool = True

    @classmethod
    def from_value(
        cls,
        value: object,
    ) -> "SmartReconnectCaptureSettings":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()

        def enabled(name: str) -> bool:
            item = value.get(name, True)
            return item if isinstance(item, bool) else True

        return cls(
            visible=enabled(VISIBLE_CAPTURE_MODE),
            obscured=enabled(OBSCURED_CAPTURE_MODE),
            minimized=enabled(MINIMIZED_CAPTURE_MODE),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            VISIBLE_CAPTURE_MODE: self.visible,
            OBSCURED_CAPTURE_MODE: self.obscured,
            MINIMIZED_CAPTURE_MODE: self.minimized,
        }

    def enabled(self, mode: str) -> bool:
        if mode not in SMART_RECONNECT_CAPTURE_MODE_KEYS:
            return False
        return bool(getattr(self, mode))


class SmartReconnectCaptureSettingsService:
    """Load and atomically persist the smart-reconnect capture selection."""

    def __init__(self, config: ConfigManager):
        self._config = config
        self._settings = SmartReconnectCaptureSettings.from_value(
            config.get(SMART_RECONNECT_CAPTURE_MODES_KEY)
        )
        normalized = self._settings.to_dict()
        if config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) != normalized:
            config.set(SMART_RECONNECT_CAPTURE_MODES_KEY, normalized)

    def snapshot(self) -> SmartReconnectCaptureSettings:
        return self._settings

    def update(
        self,
        value: object,
    ) -> SmartReconnectCaptureSettings:
        settings = SmartReconnectCaptureSettings.from_value(value)
        if settings == self._settings:
            return settings
        with self._config.transaction():
            self._config.set(
                SMART_RECONNECT_CAPTURE_MODES_KEY,
                settings.to_dict(),
            )
        self._settings = settings
        return settings
