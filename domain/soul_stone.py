"""每個角色各自保存的靈魂石文字紀錄。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class SoulStoneRecord:
    """只保存已確認的角色身分與玩家文字紀錄。"""

    character_id: str
    note: str

    def __post_init__(self) -> None:
        if not isinstance(self.character_id, str):
            raise TypeError("character_id must be a string.")
        if not isinstance(self.note, str):
            raise TypeError("note must be a string.")
        character_id = self.character_id.strip()
        note = self.note.strip()
        if not character_id:
            raise ValueError("character_id must not be empty.")
        if not note:
            raise ValueError("note must not be empty.")
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "note", note)

    def to_dict(self) -> dict[str, str]:
        return {
            "character_id": self.character_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SoulStoneRecord:
        if set(payload) != {"character_id", "note"}:
            raise ValueError("Soul stone record fields are invalid.")
        character_id = payload["character_id"]
        note = payload["note"]
        if not isinstance(character_id, str) or not isinstance(note, str):
            raise ValueError("Soul stone record fields must be strings.")
        return cls(character_id=character_id, note=note)
