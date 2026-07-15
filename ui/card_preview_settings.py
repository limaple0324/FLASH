"""可切換的提醒卡預覽設定；不代表任何方案已定稿。"""

from __future__ import annotations

from dataclasses import dataclass

from ui.card_overlay import CardSize
from ui.tk_card_presenter import TkCardTextSettings


def _non_empty_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text.")


def _non_negative_integer(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class CardPreviewProfile:
    """一套可供玩家比較的卡片尺寸、定位與文字視覺參數。"""

    profile_id: str
    display_name: str
    card_size: CardSize
    right_margin: int
    bottom_margin: int
    gap: int
    text: TkCardTextSettings

    def __post_init__(self) -> None:
        _non_empty_text(self.profile_id, "profile_id")
        _non_empty_text(self.display_name, "display_name")
        if not isinstance(self.card_size, CardSize):
            raise TypeError("card_size must be CardSize.")
        _non_negative_integer(self.right_margin, "right_margin")
        _non_negative_integer(self.bottom_margin, "bottom_margin")
        _non_negative_integer(self.gap, "gap")
        if not isinstance(self.text, TkCardTextSettings):
            raise TypeError("text must be TkCardTextSettings.")


@dataclass(frozen=True, slots=True)
class CardPreviewCatalog:
    """保存候選方案；呼叫端必須用識別碼明確選擇，不提供預設方案。"""

    profiles: tuple[CardPreviewProfile, ...]

    def __post_init__(self) -> None:
        profiles = tuple(self.profiles)
        if not profiles:
            raise ValueError("profiles must contain at least one preview profile.")
        if any(not isinstance(profile, CardPreviewProfile) for profile in profiles):
            raise TypeError("profiles must contain only CardPreviewProfile values.")
        identifiers = tuple(profile.profile_id for profile in profiles)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("profile_id must be unique within a preview catalog.")
        object.__setattr__(self, "profiles", profiles)

    def select(self, profile_id: str) -> CardPreviewProfile:
        _non_empty_text(profile_id, "profile_id")
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise KeyError(profile_id)
