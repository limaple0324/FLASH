"""不把遊戲細節寫死在介面的活動定義。"""

from dataclasses import dataclass
from enum import Enum


class ActivityType(str, Enum):
    DAILY = "每天"
    MULTI_STEP_DAILY = "每日多階段"
    LOOP = "循環"
    CALENDAR = "行事曆"
    PERMANENT = "常駐"


class ResetRule(str, Enum):
    DAILY_MIDNIGHT = "每日00:00"
    COOLDOWN = "依冷卻時間"
    CALENDAR = "依行事曆"
    NONE = "不重置"


@dataclass(frozen=True, slots=True)
class ActivityDefinition:
    activity_id: str
    name: str
    activity_type: ActivityType
    reset_rule: ResetRule
    max_completions: int | None = None
    applicable_character_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        activity_id = self.activity_id.strip()
        name = self.name.strip()
        character_ids = tuple(item.strip() for item in self.applicable_character_ids)
        if not activity_id:
            raise ValueError("activity_id must not be empty.")
        if not name:
            raise ValueError("name must not be empty.")
        if not isinstance(self.activity_type, ActivityType):
            raise TypeError("activity_type must be ActivityType.")
        if not isinstance(self.reset_rule, ResetRule):
            raise TypeError("reset_rule must be ResetRule.")
        if self.max_completions is not None:
            if (
                isinstance(self.max_completions, bool)
                or not isinstance(self.max_completions, int)
                or self.max_completions <= 0
            ):
                raise ValueError("max_completions must be a positive integer or None.")
        if any(not item for item in character_ids):
            raise ValueError("applicable_character_ids cannot contain empty values.")
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("applicable_character_ids cannot contain duplicates.")
        object.__setattr__(self, "activity_id", activity_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "applicable_character_ids", character_ids)

    @property
    def applies_to_all_characters(self) -> bool:
        return not self.applicable_character_ids

    def applies_to(self, character_id: str) -> bool:
        return self.applies_to_all_characters or character_id.strip() in self.applicable_character_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "activity_id": self.activity_id,
            "name": self.name,
            "activity_type": self.activity_type.value,
            "reset_rule": self.reset_rule.value,
            "max_completions": self.max_completions,
            "applicable_character_ids": list(self.applicable_character_ids),
        }
