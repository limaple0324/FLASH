"""提供角色詳細資料使用的獨立唯讀 SP2 快照。"""

from dataclasses import dataclass

from services.character_view_service import (
    CharacterViewService,
    PlayerCharacterView,
)
from services.character_game_data_view_service import (
    CharacterGameDataView,
    CharacterGameDataViewService,
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
    game_data: CharacterGameDataView | None = None

    @classmethod
    def from_summary(
        cls,
        summary: PlayerCharacterView,
        game_data: CharacterGameDataView | None = None,
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
            game_data=game_data,
        )


class CharacterDetailViewService:
    """建立角色詳細快照，只加入玩家已確認的延伸資料摘要。"""

    def __init__(
        self,
        characters: CharacterViewService,
        game_data: CharacterGameDataViewService | None = None,
    ) -> None:
        if not isinstance(characters, CharacterViewService):
            raise TypeError("characters must be CharacterViewService.")
        self._characters = characters
        if game_data is not None and not isinstance(
            game_data,
            CharacterGameDataViewService,
        ):
            raise TypeError(
                "game_data must be CharacterGameDataViewService or None."
            )
        self._game_data = game_data

    def all_with_identities(
        self,
        group_name: str | None = None,
    ) -> tuple[tuple[str, PlayerCharacterDetail], ...]:
        """供控制層安全綁定操作；角色識別不得傳給顯示內容。"""
        details: list[tuple[str, PlayerCharacterDetail]] = []
        for character_id, summary in self._characters.all_with_identities(
            group_name
        ):
            details.append(
                (
                    character_id,
                    PlayerCharacterDetail.from_summary(
                        summary,
                        (
                            self._game_data.get(character_id)
                            if self._game_data is not None
                            else None
                        ),
                    ),
                )
            )
        return tuple(details)

    def all(
        self,
        group_name: str | None = None,
    ) -> tuple[PlayerCharacterDetail, ...]:
        return tuple(
            detail
            for _character_id, detail
            in self.all_with_identities(group_name)
        )

    def get_by_identity(self, character_id: str) -> PlayerCharacterDetail:
        """Resolve the latest safe snapshot for an already-bound internal id."""
        for current_id, detail in self.all_with_identities():
            if current_id == character_id:
                return detail
        raise KeyError("character detail is no longer available")
