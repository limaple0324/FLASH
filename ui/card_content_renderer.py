"""Stable reminder content independent from window styling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from cards.view_state import CardViewItem


@dataclass(frozen=True, slots=True)
class CardContent:
    card_id: str
    group_name: str
    activity_name: str
    current_progress: str
    next_step: str | None
    name_only: bool

    @classmethod
    def from_card(cls, card: CardViewItem) -> "CardContent":
        if not isinstance(card, CardViewItem):
            raise TypeError("card must be CardViewItem.")
        return cls(
            card_id=card.card_id,
            group_name=card.group_name,
            activity_name=card.activity_name,
            current_progress=card.current_progress,
            next_step=card.next_step,
            name_only=card.name_only,
        )


class CardContentPresenter(Protocol):
    def render(self, window: Any, content: CardContent) -> None: ...


class CardContentRenderer:
    def __init__(self, presenter: CardContentPresenter) -> None:
        if not callable(getattr(presenter, "render", None)):
            raise TypeError("presenter must provide a callable render method.")
        self._presenter = presenter

    def __call__(self, window: Any, card: CardViewItem) -> None:
        self._presenter.render(window, CardContent.from_card(card))
