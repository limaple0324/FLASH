"""提供角色詳細頁使用的獨立唯讀資料快照。"""

from dataclasses import dataclass

from services.character_view_service import (
    CharacterViewService,
    PlayerCharacterView,
)
from services.soul_stone_service import SoulStoneService


@dataclass(frozen=True, slots=True)
class PlayerCharacterDetail:
    """只包含目前已確認的玩家可見角色資料。"""

    display_name: str
    group: str | None
    level: int | None
    importance: str | None
    role: str | None
    note: str | None
    soul_stone: str | None = None

    @classmethod
    def from_summary(
        cls,
        summary: PlayerCharacterView,
        *,
        soul_stone: str | None = None,
    ) -> "PlayerCharacterDetail":
        if not isinstance(summary, PlayerCharacterView):
            raise TypeError("summary must be PlayerCharacterView.")
        return cls(
            display_name=summary.display_name,
            group=summary.group,
            level=summary.level,
            importance=summary.importance,
            role=summary.role,
            note=summary.note,
            soul_stone=soul_stone,
        )


class CharacterDetailViewService:
    """建立與角色清單分離的詳細資料快照，不增加未確認欄位。"""

    def __init__(
        self,
        characters: CharacterViewService,
        soul_stones: SoulStoneService,
    ) -> None:
        if not isinstance(characters, CharacterViewService):
            raise TypeError("characters must be CharacterViewService.")
        if not isinstance(soul_stones, SoulStoneService):
            raise TypeError("soul_stones must be SoulStoneService.")
        self._characters = characters
        self._soul_stones = soul_stones

    def all(self) -> tuple[PlayerCharacterDetail, ...]:
        details: list[PlayerCharacterDetail] = []
        for character_id, summary in self._characters.all_with_identities():
            soul_stone = self._soul_stones.for_character(character_id)
            details.append(
                PlayerCharacterDetail.from_summary(
                    summary,
                    soul_stone=soul_stone.note if soul_stone is not None else None,
                )
            )
        return tuple(details)
