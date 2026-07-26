"""提供角色詳細資料使用的獨立唯讀 SP2 快照。"""

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
    def from_summary(
        cls,
        summary: PlayerCharacterView,
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
        )


class CharacterDetailViewService:
    """建立角色詳細快照，不增加命魂或其他未確認欄位。"""

    def __init__(
        self,
        characters: CharacterViewService,
    ) -> None:
        if not isinstance(characters, CharacterViewService):
            raise TypeError("characters must be CharacterViewService.")
        self._characters = characters

    def all_with_identities(
        self,
    ) -> tuple[tuple[str, PlayerCharacterDetail], ...]:
        """供控制層安全綁定操作；角色識別不得傳給顯示內容。"""
        details: list[tuple[str, PlayerCharacterDetail]] = []
        for character_id, summary in self._characters.all_with_identities():
            details.append(
                (
                    character_id,
                    PlayerCharacterDetail.from_summary(summary),
                )
            )
        return tuple(details)

    def all(self) -> tuple[PlayerCharacterDetail, ...]:
        return tuple(detail for _character_id, detail in self.all_with_identities())
