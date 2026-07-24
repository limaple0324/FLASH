"""玩家可調整的提醒卡顯示設定。"""

from dataclasses import dataclass
from datetime import timedelta


DEFAULT_CARD_LIFETIME_SECONDS = 30


@dataclass(frozen=True, slots=True)
class CardDisplaySettings:
    """提醒卡生命週期設定；介面與磁碟保存由後續小步驟接入。"""

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
