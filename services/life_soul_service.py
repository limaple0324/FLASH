"""每角色命魂紀錄的查詢與安全保存服務。"""

from __future__ import annotations

from domain.life_soul import LifeSoulRecord
from domain.life_soul_store import LifeSoulStore


class LifeSoulService:
    """啟動時載入紀錄，保存成功後才替換執行中的狀態。"""

    def __init__(self, store: LifeSoulStore):
        self.store = store
        loaded = store.load()
        self._records = {
            record.character_id: record
            for record in loaded
        }

    def all(self) -> tuple[LifeSoulRecord, ...]:
        return tuple(
            self._records[character_id]
            for character_id in sorted(self._records)
        )

    def for_character(self, character_id: str) -> LifeSoulRecord | None:
        normalized = self._normalize_character_id(character_id)
        return self._records.get(normalized)

    def set_for_character(
        self,
        character_id: str,
        note: str,
    ) -> LifeSoulRecord:
        """新增或修改角色紀錄，並在套用至記憶體前完成保存。"""

        record = LifeSoulRecord(character_id=character_id, note=note)
        candidate = dict(self._records)
        candidate[record.character_id] = record
        self.store.save(self._ordered(candidate))
        self._records = candidate
        return record

    def clear_for_character(self, character_id: str) -> bool:
        """清除角色紀錄；不存在時不建立檔案或重複寫入。"""

        normalized = self._normalize_character_id(character_id)
        if normalized not in self._records:
            return False
        candidate = dict(self._records)
        del candidate[normalized]
        self.store.save(self._ordered(candidate))
        self._records = candidate
        return True

    @staticmethod
    def _normalize_character_id(character_id: str) -> str:
        if not isinstance(character_id, str):
            raise TypeError("character_id must be a string.")
        normalized = character_id.strip()
        if not normalized:
            raise ValueError("character_id must not be empty.")
        return normalized

    @staticmethod
    def _ordered(
        records: dict[str, LifeSoulRecord],
    ) -> tuple[LifeSoulRecord, ...]:
        return tuple(records[character_id] for character_id in sorted(records))
