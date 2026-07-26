"""Build a stopped reminder overlay runtime."""

from __future__ import annotations

from typing import Any

from cards.service import CardService
from services.card_overlay_assembly import build_windows_card_overlay_lifecycle
from services.card_overlay_sync_service import (
    CardOverlaySyncService,
    OverlayLayoutSource,
)
from ui.tk_card_presenter import TkCardTextSettings, TkWidgetFactory
from ui.windows_card_overlay import WindowFactory


def build_windows_card_overlay_runtime(
    master: Any,
    cards: CardService,
    layout: OverlayLayoutSource,
    settings: TkCardTextSettings,
    *,
    window_factory: WindowFactory | None = None,
    widget_factory: TkWidgetFactory | None = None,
) -> CardOverlaySyncService:
    lifecycle = build_windows_card_overlay_lifecycle(
        master,
        settings,
        window_factory=window_factory,
        widget_factory=widget_factory,
    )
    return CardOverlaySyncService(cards, layout, lifecycle)
