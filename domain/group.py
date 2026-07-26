"""以組別為主要呈現單位的角色集合。"""

from dataclasses import dataclass

from domain.character import Character


@dataclass(frozen=True, slots=True)
class CharacterGroup:
    group_id: str
    name: str
    characters: tuple[Character, ...] = ()

    def __post_init__(self) -> None:
        group_id = self.group_id.strip()
        name = self.name.strip()
        characters = tuple(self.characters)
        if not group_id:
            raise ValueError("group_id must not be empty.")
        if not name:
            raise ValueError("name must not be empty.")
        if any(not isinstance(item, Character) for item in characters):
            raise TypeError("characters must contain Character values.")
        character_ids = [item.character_id for item in characters]
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("A character cannot appear twice in one group.")
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "characters", characters)

    @property
    def character_ids(self) -> tuple[str, ...]:
        return tuple(item.character_id for item in self.characters)

    def add_character(self, character: Character) -> "CharacterGroup":
        if character.character_id in self.character_ids:
            return self
        return CharacterGroup(
            group_id=self.group_id,
            name=self.name,
            characters=(*self.characters, character),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "characters": [item.to_dict() for item in self.characters],
        }
