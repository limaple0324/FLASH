"""建立可啟停的 Windows 提醒卡浮層執行階段。"""

from __future__ import annotations

from typing import Any

from cards.service import CardService
from services.card_overlay_assembly import build_windows_card_overlay_lifecycle
from services.card_preview_adapter import SelectedCardPreview
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
    """Connect card events to the assembled overlay without starting it early."""
    lifecycle = build_windows_card_overlay_lifecycle(
        master,
        settings,
        window_factory=window_factory,
        widget_factory=widget_factory,
    )
    return CardOverlaySyncService(cards, layout, lifecycle)


def build_selected_card_overlay_runtime(
    master: Any,
    cards: CardService,
    selected: SelectedCardPreview,
    *,
    window_factory: WindowFactory | None = None,
    widget_factory: TkWidgetFactory | None = None,
) -> CardOverlaySyncService:
    """Build a stopped runtime from one explicitly selected preview profile."""
    if not isinstance(selected, SelectedCardPreview):
        raise TypeError("selected must be SelectedCardPreview.")
    return build_windows_card_overlay_runtime(
        master,
        cards,
        selected.layout,
        selected.text,
        window_factory=window_factory,
        widget_factory=widget_factory,
    )
