"""安全保存並套用玩家選擇的提醒卡顯示時間。"""

from __future__ import annotations

from collections.abc import Callable

from cards.service import CardService
from cards.settings import (
    CARD_LIFETIME_SECONDS_CONFIG_KEY,
    CardDisplaySettings,
    CardDisplaySettingsResolution,
)
from config.config_manager import ConfigManager


SettingsChanged = Callable[[CardDisplaySettingsResolution], None]


class CardDisplaySettingsService:
    """先完成驗證與保存，再讓後續新增的提醒卡使用新時間。"""

    def __init__(
        self,
        config: ConfigManager,
        cards: CardService,
        resolution: CardDisplaySettingsResolution,
        *,
        on_changed: SettingsChanged | None = None,
    ) -> None:
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager.")
        if not isinstance(cards, CardService):
            raise TypeError("cards must be CardService.")
        if not isinstance(resolution, CardDisplaySettingsResolution):
            raise TypeError("resolution must be CardDisplaySettingsResolution.")
        if on_changed is not None and not callable(on_changed):
            raise TypeError("on_changed must be callable.")

        self._config = config
        self._cards = cards
        self._resolution = resolution
        self._on_changed = on_changed

    def snapshot(self) -> CardDisplaySettingsResolution:
        return self._resolution

    def update_lifetime_seconds(
        self,
        lifetime_seconds: int,
    ) -> CardDisplaySettingsResolution:
        """驗證並保存秒數；保存失敗時維持原本執行狀態。"""

        settings = CardDisplaySettings(lifetime_seconds=lifetime_seconds)
        if (
            self._resolution.configured
            and not self._resolution.recovered_from_invalid
            and self._resolution.settings == settings
        ):
            return self._resolution

        previous_data = dict(self._config.data)
        try:
            self._config.set(
                CARD_LIFETIME_SECONDS_CONFIG_KEY,
                settings.lifetime_seconds,
            )
        except Exception:
            self._config.data.clear()
            self._config.data.update(previous_data)
            raise

        resolution = CardDisplaySettingsResolution(
            settings=settings,
            configured=True,
            recovered_from_invalid=False,
        )
        self._cards.settings = settings
        self._resolution = resolution
        if self._on_changed is not None:
            self._on_changed(resolution)
        return resolution
