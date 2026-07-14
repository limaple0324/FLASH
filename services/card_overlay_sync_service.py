"""將提醒卡資料變更安全同步至浮動視窗生命週期。"""

from __future__ import annotations

from typing import Protocol

from cards.service import CardService
from services.card_overlay_layout_service import CardOverlayLayout


class OverlayLayoutSource(Protocol):
    def snapshot(self) -> CardOverlayLayout:
        """Return the current positioned card layout."""


class OverlayLifecycle(Protocol):
    def sync(self, layout: CardOverlayLayout) -> None:
        """Synchronize visible windows to the layout."""

    def close_all(self) -> None:
        """Close every visible overlay window."""


class CardOverlaySyncService:
    """Listen for card changes without letting UI failures break card state."""

    def __init__(
        self,
        cards: CardService,
        layout: OverlayLayoutSource,
        lifecycle: OverlayLifecycle,
    ) -> None:
        if not isinstance(cards, CardService):
            raise TypeError("cards must be CardService.")
        self._cards = cards
        self._layout = layout
        self._lifecycle = lifecycle
        self._running = False
        self._last_error: Exception | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def refresh(self) -> bool:
        """Try one synchronization and retain failures for diagnostics."""
        try:
            layout = self._layout.snapshot()
            if not isinstance(layout, CardOverlayLayout):
                raise TypeError("layout source must return CardOverlayLayout.")
            self._lifecycle.sync(layout)
        except Exception as exc:
            self._last_error = exc
            return False

        self._last_error = None
        return True

    def _on_cards_changed(self) -> None:
        if self._running:
            self.refresh()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._cards.subscribe(self._on_cards_changed)
        self.refresh()

    def stop(self) -> None:
        if not self._running:
            return
        self._cards.unsubscribe(self._on_cards_changed)
        self._running = False
        self._lifecycle.close_all()
