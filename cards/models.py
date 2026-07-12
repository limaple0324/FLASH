"""以組別彙整的提醒卡純資料模型。"""

from dataclasses import dataclass

from domain.activity import ActivityDefinition
from domain.group import CharacterGroup


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


@dataclass(frozen=True, slots=True)
class GroupCard:
    """保存已確認的卡片資訊，不在模型內產生提醒或決定優先度。"""

    card_id: str
    group: CharacterGroup
    activity: ActivityDefinition
    current_progress: str
    affected_character_ids: tuple[str, ...] = ()
    daily_summary: str | None = None
    requires_player_action: bool = False
    next_step: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.group, CharacterGroup):
            raise TypeError("group must be CharacterGroup.")
        if not isinstance(self.activity, ActivityDefinition):
            raise TypeError("activity must be ActivityDefinition.")
        if not isinstance(self.requires_player_action, bool):
            raise TypeError("requires_player_action must be bool.")

        affected_ids = tuple(
            _required_text(item, "affected_character_ids item")
            for item in self.affected_character_ids
        )
        if len(affected_ids) != len(set(affected_ids)):
            raise ValueError("affected_character_ids cannot contain duplicates.")
        unknown_ids = set(affected_ids) - set(self.group.character_ids)
        if unknown_ids:
            raise ValueError("affected characters must belong to the card group.")

        object.__setattr__(self, "card_id", _required_text(self.card_id, "card_id"))
        object.__setattr__(
            self,
            "current_progress",
            _required_text(self.current_progress, "current_progress"),
        )
        object.__setattr__(self, "affected_character_ids", affected_ids)
        object.__setattr__(
            self,
            "daily_summary",
            _optional_text(self.daily_summary, "daily_summary"),
        )
        object.__setattr__(
            self,
            "next_step",
            _optional_text(self.next_step, "next_step"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "group": self.group.to_dict(),
            "activity": self.activity.to_dict(),
            "current_progress": self.current_progress,
            "affected_character_ids": list(self.affected_character_ids),
            "daily_summary": self.daily_summary,
            "requires_player_action": self.requires_player_action,
            "next_step": self.next_step,
        }
