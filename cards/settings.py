"""玩家可調整的提醒卡顯示設定。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta


DEFAULT_CARD_LIFETIME_SECONDS = 30
CARD_LIFETIME_SECONDS_CONFIG_KEY = "card_lifetime_seconds"


@dataclass(frozen=True, slots=True)
class CardDisplaySettings:
    """提醒卡生命週期設定；玩家操作介面由後續小步驟接入。"""

    lifetime_seconds: int = DEFAULT_CARD_LIFETIME_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.lifetime_seconds, bool) or not isinstance(
            self.lifetime_seconds,
            int,
        ):
            raise TypeError("lifetime_seconds must be int.")
        if self.lifetime_seconds < 1:
            raise ValueError("lifetime_seconds must be at least 1.")
        try:
            timedelta(seconds=self.lifetime_seconds)
        except OverflowError as exc:
            raise ValueError("lifetime_seconds is too large.") from exc

    @property
    def lifetime(self) -> timedelta:
        return timedelta(seconds=self.lifetime_seconds)


@dataclass(frozen=True, slots=True)
class CardDisplaySettingsResolution:
    """保留啟動時的設定來源，供自我檢查與玩家診斷使用。"""

    settings: CardDisplaySettings
    configured: bool
    recovered_from_invalid: bool


def resolve_card_display_settings(
    values: Mapping[str, object],
) -> CardDisplaySettingsResolution:
    if not isinstance(values, Mapping):
        raise TypeError("values must be a mapping.")
    if CARD_LIFETIME_SECONDS_CONFIG_KEY not in values:
        return CardDisplaySettingsResolution(
            settings=CardDisplaySettings(),
            configured=False,
            recovered_from_invalid=False,
        )

    try:
        settings = CardDisplaySettings(
            lifetime_seconds=values[CARD_LIFETIME_SECONDS_CONFIG_KEY],
        )
    except (TypeError, ValueError):
        return CardDisplaySettingsResolution(
            settings=CardDisplaySettings(),
            configured=True,
            recovered_from_invalid=True,
        )
    return CardDisplaySettingsResolution(
        settings=settings,
        configured=True,
        recovered_from_invalid=False,
    )
