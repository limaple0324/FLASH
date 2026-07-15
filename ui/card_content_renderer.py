"""將提醒卡狀態轉為可替換呈現器使用的穩定內容。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from cards.view_state import CardViewItem


@dataclass(frozen=True, slots=True)
class CardContent:
    """已確認需要呈現的最小內容，不包含顏色、字型或排列。"""

    group_name: str
    activity_name: str
    current_progress: str
    next_step: str | None

    @classmethod
    def from_card(cls, card: CardViewItem) -> "CardContent":
        if not isinstance(card, CardViewItem):
            raise TypeError("card must be CardViewItem.")
        return cls(
            group_name=card.group_name,
            activity_name=card.activity_name,
            current_progress=card.current_progress,
            next_step=card.next_step,
        )


class CardContentPresenter(Protocol):
    """由後續視覺版本決定如何把內容放進浮動視窗。"""

    def render(self, window: Any, content: CardContent) -> None:
        """Render or update one card window's content."""


class CardContentRenderer:
    """符合浮動視窗殼層呼叫方式的可替換內容轉接器。"""

    def __init__(self, presenter: CardContentPresenter) -> None:
        if not callable(getattr(presenter, "render", None)):
            raise TypeError("presenter must provide a callable render method.")
        self._presenter = presenter

    def __call__(self, window: Any, card: CardViewItem) -> None:
        self._presenter.render(window, CardContent.from_card(card))
