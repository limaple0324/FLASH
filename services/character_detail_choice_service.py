"""將內部角色識別安全綁定為玩家介面可使用的選擇命令。"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)


CharacterDetailHandler = Callable[[str, PlayerCharacterDetail], None]


@dataclass(frozen=True, slots=True)
class PlayerCharacterDetailChoice:
    """只向介面提供玩家可見快照與不帶參數的選擇命令。"""

    detail: PlayerCharacterDetail
    select: Callable[[], None]

    def __post_init__(self) -> None:
        if not isinstance(self.detail, PlayerCharacterDetail):
            raise TypeError("detail must be PlayerCharacterDetail.")
        if not callable(self.select):
            raise TypeError("select must be callable.")


class CharacterDetailChoiceService:
    """建立不外露角色識別、但能精確選中角色的介面命令。"""

    def __init__(
        self,
        details: CharacterDetailViewService,
        on_select: CharacterDetailHandler,
    ) -> None:
        if not isinstance(details, CharacterDetailViewService):
            raise TypeError("details must be CharacterDetailViewService.")
        if not callable(on_select):
            raise TypeError("on_select must be callable.")
        self._details = details
        self._on_select = on_select

    def all(self) -> tuple[PlayerCharacterDetailChoice, ...]:
        return tuple(
            PlayerCharacterDetailChoice(
                detail=detail,
                select=partial(self._on_select, character_id, detail),
            )
            for character_id, detail in self._details.all_with_identities()
        )
