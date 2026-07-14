"""同步提醒卡浮動視窗生命週期，不包含實際視窗樣式。"""

from __future__ import annotations

from typing import Protocol

from cards.service import MAX_VISIBLE_CARDS
from services.card_overlay_layout_service import (
    CardOverlayLayout,
    PositionedCard,
)


class CardOverlayWindowPort(Protocol):
    """由後續 Windows 介面實作真正的浮動視窗操作。"""

    def open(self, item: PositionedCard) -> None:
        """Open one card window."""

    def update(self, item: PositionedCard) -> None:
        """Update one existing card window's content or placement."""

    def close(self, card_id: str) -> None:
        """Close one card window."""


class CardOverlayWindowLifecycle:
    """Apply immutable layouts while avoiding duplicate window operations."""

    def __init__(self, windows: CardOverlayWindowPort) -> None:
        self._windows = windows
        self._visible: dict[str, PositionedCard] = {}

    @property
    def visible_card_ids(self) -> tuple[str, ...]:
        return tuple(self._visible)

    @staticmethod
    def _validated_items(layout: CardOverlayLayout) -> tuple[PositionedCard, ...]:
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
        """Open, update, or close windows until they match the supplied layout."""
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
