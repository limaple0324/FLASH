"""每角色靈魂石紀錄的唯讀應用服務。"""

from __future__ import annotations

from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore


class SoulStoneService:
    """啟動時載入紀錄，提供不外露可變狀態的查詢。"""

    def __init__(self, store: SoulStoneStore):
        self.store = store
        loaded = store.load()
        self._records = {
            record.character_id: record
            for record in loaded
        }

    def all(self) -> tuple[SoulStoneRecord, ...]:
        return tuple(
            self._records[character_id]
            for character_id in sorted(self._records)
        )

    def for_character(self, character_id: str) -> SoulStoneRecord | None:
        if not isinstance(character_id, str):
            raise TypeError("character_id must be a string.")
        normalized = character_id.strip()
        if not normalized:
            raise ValueError("character_id must not be empty.")
        return self._records.get(normalized)
