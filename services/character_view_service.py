"""把 SP1 視窗角色資料與 SP2 穩定角色資料轉成唯讀玩家資料。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.window_registry import WindowRegistry
from domain.character import Character


@dataclass(frozen=True, slots=True)
class PlayerCharacterView:
    """不含角色識別碼與視窗技術資料的唯讀快照。"""

    display_name: str
    group: str | None
    level: int | None
    importance: str | None
    role: str | None
    note: str | None


class CharacterViewService:
    """依固定角色身分組合資料，不用顯示名稱進行猜測。"""

    def __init__(
        self,
        registry: WindowRegistry,
        characters: Iterable[Character],
    ) -> None:
        self._registry = registry
        self._characters: dict[str, Character] = {}
        for character in characters:
            if character.character_id in self._characters:
                raise ValueError(
                    f"Duplicate stable character ID: {character.character_id}"
                )
            self._characters[character.character_id] = character

    def all_with_identities(
        self,
    ) -> tuple[tuple[str, PlayerCharacterView], ...]:
        """提供內部服務安全配對；角色識別不得傳給顯示層。"""
        snapshots: list[tuple[str, PlayerCharacterView]] = []
        for record in self._registry.all():
            character = self._characters.get(record.character_id)
            snapshots.append(
                (
                    record.character_id,
                    PlayerCharacterView(
                        display_name=record.display_name,
                        group=record.group,
                        level=character.level if character is not None else None,
                        importance=(
                            character.importance.value
                            if character is not None
                            else None
                        ),
                        role=record.role,
                        note=record.note,
                    ),
                )
            )
        return tuple(snapshots)

    def all(self) -> tuple[PlayerCharacterView, ...]:
        return tuple(
            snapshot
            for _character_id, snapshot in self.all_with_identities()
        )
