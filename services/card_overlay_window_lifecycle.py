"""Synchronize reminder overlay window lifecycle."""

from __future__ import annotations

from typing import Protocol

from cards.service import MAX_VISIBLE_CARDS
from services.card_overlay_layout_service import (
    CardOverlayLayout,
    PositionedCard,
)


class CardOverlayWindowPort(Protocol):
    def open(self, item: PositionedCard) -> None: ...
    def update(self, item: PositionedCard) -> None: ...
    def close(self, card_id: str) -> None: ...


class CardOverlayWindowLifecycle:
    def __init__(self, windows: CardOverlayWindowPort) -> None:
        self._windows = windows
        self._visible: dict[str, PositionedCard] = {}

    @staticmethod
    def _validated_items(
        layout: CardOverlayLayout,
    ) -> tuple[PositionedCard, ...]:
        if not isinstance(layout, CardOverlayLayout):
            raise TypeError("layout must be CardOverlayLayout.")
        items = tuple(layout.cards)
        if any(not isinstance(item, PositionedCard) for item in items):
            raise TypeError("layout must contain only PositionedCard values.")
        if len(items) > MAX_VISIBLE_CARDS:
            raise ValueError("Overlay cannot contain more than three cards.")
        card_ids = tuple(item.card.card_id for item in items)
        if len(set(card_ids)) != len(card_ids):
            raise ValueError("Overlay card ids must be unique.")
        return items

    def sync(self, layout: CardOverlayLayout) -> None:
        items = self._validated_items(layout)
        incoming_ids = {item.card.card_id for item in items}
        for card_id in tuple(self._visible):
            if card_id not in incoming_ids:
                self._windows.close(card_id)
                del self._visible[card_id]
        for item in items:
            card_id = item.card.card_id
            current = self._visible.get(card_id)
            if current is None:
                self._windows.open(item)
                self._visible[card_id] = item
            elif current != item:
                self._windows.update(item)
                self._visible[card_id] = item

    def close_all(self) -> None:
        for card_id in tuple(self._visible):
            self._windows.close(card_id)
            del self._visible[card_id]
