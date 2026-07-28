"""Safely synchronize card changes to overlay windows."""

from __future__ import annotations

from typing import Protocol

from cards.service import CardService
from services.card_overlay_layout_service import CardOverlayLayout


class OverlayLayoutSource(Protocol):
    def snapshot(self) -> CardOverlayLayout: ...


class OverlayLifecycle(Protocol):
    def sync(self, layout: CardOverlayLayout) -> None: ...
    def close_all(self) -> None: ...


class CardOverlaySyncService:
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

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._cards.subscribe(self._on_cards_changed)
        if self.refresh():
            return True
        self._cards.unsubscribe(self._on_cards_changed)
        self._running = False
        return False

    def stop(self) -> bool:
        if self._running:
            self._cards.unsubscribe(self._on_cards_changed)
            self._running = False
        try:
            self._lifecycle.close_all()
        except Exception as exc:
            self._last_error = exc
            return False
        self._last_error = None
        return True
