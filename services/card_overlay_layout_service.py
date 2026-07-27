"""Combine reminder snapshots with safe Windows work-area placement."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Protocol

from cards.view_state import CardViewItem, CardViewState
from ui.card_overlay import CardPlacement, CardSize


class CardViewStateSource(Protocol):
    def snapshot(self) -> CardViewState: ...


class WorkAreaSource(Protocol):
    def read(self) -> WorkArea: ...


@dataclass(frozen=True, slots=True)
class PositionedCard:
    card: CardViewItem
    placement: CardPlacement


@dataclass(frozen=True, slots=True)
class CardOverlayLayout:
    cards: tuple[PositionedCard, ...] = ()


class CardOverlayLayoutService:
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

    @staticmethod
    def _estimated_height(card: CardViewItem, base: CardSize) -> int:
        display_scale = max(1.0, base.width / 160)
        available_width = max(1, base.width - round(16 * display_scale))
        character_width = max(1, round(12 * display_scale))
        characters_per_line = max(6, available_width // character_width)
        fields = (
            (card.activity_name,)
            if card.name_only
            else (
                card.group_name,
                card.activity_name,
                card.current_progress,
            )
        )
        baseline_lines = 1 if card.name_only else 3
        wrapped_lines = sum(
            max(1, ceil(len(text) / characters_per_line))
            for text in fields
        )
        line_height = max(17, round(17 * display_scale))
        action_height = (
            max(34, round(34 * display_scale))
            if card.actions
            else 0
        )
        return (
            base.height
            + max(0, wrapped_lines - baseline_lines) * line_height
            + action_height
        )

    def snapshot(self) -> CardOverlayLayout:
        state = self._card_state.snapshot()
        if not isinstance(state, CardViewState):
            raise TypeError("card state source must return CardViewState.")
        if state.is_empty:
            return CardOverlayLayout()
        work_area = self._work_area.read()
        sizes = tuple(
            CardSize(
                self._card_size.width,
                self._estimated_height(card, self._card_size),
            )
            for card in state.cards
        )
        x = work_area.right - self._right_margin - self._card_size.width
        current_bottom = work_area.bottom - self._bottom_margin
        placements_list: list[CardPlacement] = []
        for slot, size in enumerate(sizes):
            y = current_bottom - size.height
            if x < work_area.left or y < work_area.top:
                raise ValueError("Card stack does not fit inside the work area.")
            placements_list.append(
                CardPlacement(
                    slot=slot,
                    x=x,
                    y=y,
                    width=size.width,
                    height=size.height,
                )
            )
            current_bottom = y - self._gap
        placements = tuple(placements_list)
        return CardOverlayLayout(
            cards=tuple(
                PositionedCard(card=card, placement=placement)
                for card, placement in zip(
                    state.cards,
                    placements,
                    strict=True,
                )
            )
        )
