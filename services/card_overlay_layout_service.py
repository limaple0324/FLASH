"""組合提醒卡快照與 Windows 工作區的唯讀浮層定位。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cards.view_state import CardViewItem, CardViewState
from ui.card_overlay import CardPlacement, CardSize, WorkArea, calculate_card_stack


class CardViewStateSource(Protocol):
    def snapshot(self) -> CardViewState:
        """Return the current immutable visible-card state."""


class WorkAreaSource(Protocol):
    def read(self) -> WorkArea:
        """Return the current Windows work area."""


@dataclass(frozen=True, slots=True)
class PositionedCard:
    card: CardViewItem
    placement: CardPlacement


@dataclass(frozen=True, slots=True)
class CardOverlayLayout:
    cards: tuple[PositionedCard, ...] = ()


class CardOverlayLayoutService:
    """Create positions without opening windows or choosing card appearance."""

    def __init__(
        self,
        card_state: CardViewStateSource,
        work_area: WorkAreaSource,
        card_size: CardSize,
        *,
        right_margin: int,
        bottom_margin: int,
        gap: int,
    ) -> None:
        self._card_state = card_state
        self._work_area = work_area
        self._card_size = card_size
        self._right_margin = right_margin
        self._bottom_margin = bottom_margin
        self._gap = gap

    def snapshot(self) -> CardOverlayLayout:
        state = self._card_state.snapshot()
        if not isinstance(state, CardViewState):
            raise TypeError("card state source must return CardViewState.")
        if state.is_empty:
            return CardOverlayLayout()

        placements = calculate_card_stack(
            self._work_area.read(),
            self._card_size,
            len(state.cards),
            right_margin=self._right_margin,
            bottom_margin=self._bottom_margin,
            gap=self._gap,
        )
        return CardOverlayLayout(
            cards=tuple(
                PositionedCard(card=card, placement=placement)
                for card, placement in zip(state.cards, placements, strict=True)
            )
        )
