"""已確認的每角色遊戲延伸資料快照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ObsidianSnapshot:
    """黑曜石陣只保存玩家已確認的頁數與未點亮數量。"""

    opened_page: int
    unlit_nodes: int
    updated_at: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.opened_page, bool)
            or not isinstance(self.opened_page, int)
            or not 1 <= self.opened_page <= 10
        ):
            raise ValueError("opened_page must be between 1 and 10.")
        if (
            isinstance(self.unlit_nodes, bool)
            or not isinstance(self.unlit_nodes, int)
            or self.unlit_nodes < 0
        ):
            raise ValueError("unlit_nodes must be a non-negative integer.")
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, "updated_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "opened_page": self.opened_page,
            "unlit_nodes": self.unlit_nodes,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ObsidianSnapshot":
        return cls(
            opened_page=payload.get("opened_page"),  # type: ignore[arg-type]
            unlit_nodes=payload.get("unlit_nodes"),  # type: ignore[arg-type]
            updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class LifeSoul:
    name: str
    level: int
    effect: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        if (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or self.level < 0
        ):
            raise ValueError("level must be a non-negative integer.")
        object.__setattr__(self, "effect", _required_text(self.effect, "effect"))

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "level": self.level, "effect": self.effect}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LifeSoul":
        return cls(
            name=payload.get("name"),  # type: ignore[arg-type]
            level=payload.get("level"),  # type: ignore[arg-type]
            effect=payload.get("effect"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PetLifeSoulSnapshot:
    pet_name: str
    souls: tuple[LifeSoul, ...]
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pet_name",
            _required_text(self.pet_name, "pet_name"),
        )
        if any(not isinstance(item, LifeSoul) for item in self.souls):
            raise TypeError("souls must contain only LifeSoul values.")
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, "updated_at"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "pet_name": self.pet_name,
            "souls": [item.to_dict() for item in self.souls],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "PetLifeSoulSnapshot":
        raw_souls = payload.get("souls")
        if not isinstance(raw_souls, list) or any(
            not isinstance(item, Mapping) for item in raw_souls
        ):
            raise ValueError("souls must be a list of objects.")
        return cls(
            pet_name=payload.get("pet_name"),  # type: ignore[arg-type]
            souls=tuple(LifeSoul.from_dict(item) for item in raw_souls),
            updated_at=payload.get("updated_at"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CharacterGameData:
    """不猜測寵物天賦／魂器欄位；只保存已確認的黑曜石與命魂資料。"""

    character_id: str
    cultivated_pet_count: int = 0
    obsidian: ObsidianSnapshot | None = None
    life_souls: tuple[PetLifeSoulSnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "character_id",
            _required_text(self.character_id, "character_id"),
        )
        if (
            isinstance(self.cultivated_pet_count, bool)
            or not isinstance(self.cultivated_pet_count, int)
            or self.cultivated_pet_count < 0
        ):
            raise ValueError(
                "cultivated_pet_count must be a non-negative integer."
            )
        if self.obsidian is not None and not isinstance(
            self.obsidian,
            ObsidianSnapshot,
        ):
            raise TypeError("obsidian must be ObsidianSnapshot or None.")
        if any(
            not isinstance(item, PetLifeSoulSnapshot)
            for item in self.life_souls
        ):
            raise TypeError(
                "life_souls must contain only PetLifeSoulSnapshot values."
            )
        if len(self.life_souls) > self.cultivated_pet_count:
            raise ValueError(
                "read pet count cannot exceed cultivated pet count."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "character_id": self.character_id,
            "cultivated_pet_count": self.cultivated_pet_count,
            "obsidian": (
                self.obsidian.to_dict() if self.obsidian is not None else None
            ),
            "life_souls": [item.to_dict() for item in self.life_souls],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CharacterGameData":
        raw_obsidian = payload.get("obsidian")
        if raw_obsidian is not None and not isinstance(raw_obsidian, Mapping):
            raise ValueError("obsidian must be an object or null.")
        raw_life_souls = payload.get("life_souls")
        if not isinstance(raw_life_souls, list) or any(
            not isinstance(item, Mapping) for item in raw_life_souls
        ):
            raise ValueError("life_souls must be a list of objects.")
        return cls(
            character_id=payload.get("character_id"),  # type: ignore[arg-type]
            cultivated_pet_count=payload.get(  # type: ignore[arg-type]
                "cultivated_pet_count",
                0,
            ),
            obsidian=(
                ObsidianSnapshot.from_dict(raw_obsidian)
                if raw_obsidian is not None
                else None
            ),
            life_souls=tuple(
                PetLifeSoulSnapshot.from_dict(item)
                for item in raw_life_souls
            ),
        )
