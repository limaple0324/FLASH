"""Build a stopped reminder overlay runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cards.service import CardService
from services.card_overlay_sync_service import (
    CardOverlaySyncService,
    OverlayLayoutSource,
)
from services.card_overlay_window_lifecycle import CardOverlayWindowLifecycle
from ui.card_content_renderer import CardContent
from ui.tk_card_presenter import (
    TkCardContentPresenter,
    TkCardTextSettings,
    TkWidgetFactory,
)
from ui.windows_card_overlay import WindowFactory, WindowsCardOverlayPort


def build_windows_card_overlay_runtime(
    master: Any,
    cards: CardService,
    layout: OverlayLayoutSource,
    settings: TkCardTextSettings,
    *,
    window_factory: WindowFactory | None = None,
    widget_factory: TkWidgetFactory | None = None,
    on_action: Callable[[str, str], object] | None = None,
) -> CardOverlaySyncService:
    presenter = TkCardContentPresenter(
        settings,
        widget_factory=widget_factory,
        on_close=cards.remove,
        on_action=on_action,
    )
    windows = WindowsCardOverlayPort(
        master,
        lambda window, card: presenter.render(
            window,
            CardContent.from_card(card),
        ),
        window_factory=window_factory,
    )
    lifecycle = CardOverlayWindowLifecycle(windows)
    return CardOverlaySyncService(cards, layout, lifecycle)
