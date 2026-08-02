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
        return CharacterGameDataView(
            pet_talent=self._pet_talent_summary(record),
            obsidian=self._obsidian_summary(record),
            life_soul=self._life_soul_summary(record),
            artifact=self._artifact_summary(record),
        )

    @staticmethod
    def _pet_talent_summary(record) -> str:
        snapshot = record.pet_talent
        if snapshot is None:
            return "尚未安全讀取"
        pages = tuple(sorted(snapshot.pages, key=lambda item: item.page_number))
        status = f"已讀取 {len(pages)}／4 頁"
        if snapshot.complete:
            status += "｜四頁已完整讀取"
        lines = [status]
        for page in pages:
            lines.append(
                f"第 {page.page_number} 頁｜{page.observed_text}｜"
                f"最後更新 {page.updated_at}"
            )
        return "\n".join(lines)

    @staticmethod
    def _obsidian_summary(record) -> str:
        snapshot = record.obsidian
        if snapshot is None:
            return "尚未安全讀取"
        parts = [f"已開啟至第 {snapshot.opened_page} 頁"]
        if snapshot.stage is not None:
            parts.append(f"階段 {snapshot.stage}")
        if snapshot.opened_nodes is not None:
            parts.append(f"已開 {snapshot.opened_nodes} 格")
        parts.append(f"尚餘 {snapshot.unlit_nodes} 個未點亮節點")
        parts.append(f"最後更新 {snapshot.updated_at}")
        return "｜".join(parts)

    @staticmethod
    def _life_soul_summary(record) -> str:
        if not record.cultivated_pet_count:
            return "尚未設定已培養寵物數量"
        summary = (
            f"已讀取 {record.read_pet_count}／"
            f"{record.cultivated_pet_count} 隻培養寵物"
        )
        if not record.life_souls:
            return summary
        pet_lines: list[str] = []
        for pet in record.life_souls:
            souls = "；".join(
                f"{soul.name}｜等級 {soul.level}｜{soul.effect}"
                for soul in pet.souls
            )
            pet_lines.append(
                f"{pet.pet_name}｜第 {pet.page_number} 頁｜"
                f"{souls or '尚未讀到命魂'}｜"
                f"最後更新 {pet.updated_at}"
            )
        return "\n".join((summary, *pet_lines))

    @staticmethod
    def _artifact_summary(record) -> str:
        snapshot = record.artifact
        if snapshot is None:
            return "尚未安全讀取"
        title = snapshot.page_name
        if snapshot.level is not None:
            title += f"｜等級 {snapshot.level}"
        lines = [title]
        lines.extend(snapshot.summary_lines)
        if snapshot.rune_text:
            lines.append("符文文字：" + "；".join(snapshot.rune_text))
        lines.append(f"最後更新 {snapshot.updated_at}")
        return "\n".join(lines)
