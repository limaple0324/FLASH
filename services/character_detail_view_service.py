"""提供角色詳細頁使用的獨立唯讀資料快照。"""

from dataclasses import dataclass

from services.character_view_service import (
    CharacterViewService,
    PlayerCharacterView,
)


@dataclass(frozen=True, slots=True)
class PlayerCharacterDetail:
    """只包含目前已確認的玩家可見角色資料。"""

    display_name: str
    group: str | None
    level: int | None
    importance: str | None
    role: str | None
    note: str | None

    @classmethod
    def from_summary(cls, summary: PlayerCharacterView) -> "PlayerCharacterDetail":
        if not isinstance(summary, PlayerCharacterView):
            raise TypeError("summary must be PlayerCharacterView.")
        return cls(
            display_name=summary.display_name,
            group=summary.group,
            level=summary.level,
            importance=summary.importance,
            role=summary.role,
            note=summary.note,
        )


class CharacterDetailViewService:
    """建立與角色清單分離的詳細資料快照，不增加未確認欄位。"""

    def __init__(self, characters: CharacterViewService) -> None:
        if not isinstance(characters, CharacterViewService):
            raise TypeError("characters must be CharacterViewService.")
        self._characters = characters

    def all(self) -> tuple[PlayerCharacterDetail, ...]:
        return tuple(
            PlayerCharacterDetail.from_summary(summary)
            for summary in self._characters.all()
        )
