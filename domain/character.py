"""SP2 角色身分與角色重要度。"""

from dataclasses import dataclass
from enum import Enum


class CharacterImportance(str, Enum):
    PRIMARY = "主號"
    SECONDARY = "次要"
    RESERVE = "備用"


CHARACTER_IMPORTANCE_ORDER = {
    CharacterImportance.PRIMARY: 0,
    CharacterImportance.SECONDARY: 1,
    CharacterImportance.RESERVE: 2,
}


def character_importance_rank(importance: CharacterImportance) -> int:
    """Return the project-wide role order used by every character decision."""
    if not isinstance(importance, CharacterImportance):
        raise TypeError("importance must be CharacterImportance.")
    return CHARACTER_IMPORTANCE_ORDER[importance]


def character_priority_key(character: "Character") -> tuple[int, int, str]:
    """Return the shared default order: role, higher level, stable identity."""
    if not isinstance(character, Character):
        raise TypeError("character must be Character.")
    return (
        character_importance_rank(character.importance),
        -character.level,
        character.character_id,
    )


@dataclass(frozen=True, slots=True)
class Character:
    """不包含暫時視窗資訊的穩定角色資料。"""

    character_id: str
    display_name: str
    level: int
    importance: CharacterImportance = CharacterImportance.SECONDARY

    def __post_init__(self) -> None:
        character_id = self.character_id.strip()
        display_name = self.display_name.strip()
        if not character_id:
            raise ValueError("character_id must not be empty.")
        if not display_name:
            raise ValueError("display_name must not be empty.")
        if isinstance(self.level, bool) or not isinstance(self.level, int) or self.level <= 0:
            raise ValueError("level must be a positive integer.")
        if not isinstance(self.importance, CharacterImportance):
            raise TypeError("importance must be CharacterImportance.")
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "display_name", display_name)

    def to_dict(self) -> dict[str, object]:
        return {
            "character_id": self.character_id,
            "display_name": self.display_name,
            "level": self.level,
            "importance": self.importance.value,
        }
