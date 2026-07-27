"""提供角色詳細頁使用的已確認遊戲資料唯讀摘要。"""

from __future__ import annotations

from dataclasses import dataclass

from domain.character_game_data_store import CharacterGameDataStore


@dataclass(frozen=True, slots=True)
class CharacterGameDataView:
    pet_talent: str
    obsidian: str
    life_soul: str
    artifact: str


class CharacterGameDataViewService:
    def __init__(self, store: CharacterGameDataStore):
        if not isinstance(store, CharacterGameDataStore):
            raise TypeError("store must be CharacterGameDataStore.")
        self._store = store

    def get(self, character_id: str) -> CharacterGameDataView:
        record = next(
            (
                item
                for item in self._store.load()
                if item.character_id == character_id
            ),
            None,
        )
        if record is None:
            return CharacterGameDataView(
                pet_talent="尚未安全讀取",
                obsidian="尚未安全讀取",
                life_soul="尚未安全讀取",
                artifact="尚未安全讀取",
            )
        obsidian = (
            (
                f"已開啟至第 {record.obsidian.opened_page} 頁｜"
                f"尚餘 {record.obsidian.unlit_nodes} 個未點亮節點｜"
                f"最後更新 {record.obsidian.updated_at}"
            )
            if record.obsidian is not None
            else "尚未安全讀取"
        )
        life_soul = (
            (
                f"已讀取 {len(record.life_souls)}／"
                f"{record.cultivated_pet_count} 隻培養寵物"
            )
            if record.cultivated_pet_count
            else "尚未設定已培養寵物數量"
        )
        return CharacterGameDataView(
            pet_talent="尚未安全讀取",
            obsidian=obsidian,
            life_soul=life_soul,
            artifact="尚未安全讀取",
        )
