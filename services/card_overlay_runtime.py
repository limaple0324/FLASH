"""Build a stopped reminder overlay runtime."""

from __future__ import annotations

from collections.abc import Callable
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
    on_action: Callable[[str, str], object] | None = None,
) -> CardOverlaySyncService:
    lifecycle = build_windows_card_overlay_lifecycle(
        master,
        settings,
        window_factory=window_factory,
        widget_factory=widget_factory,
        on_close=cards.remove,
        on_action=on_action,
    )
    return CardOverlaySyncService(cards, layout, lifecycle)


def build_selected_card_overlay_runtime(
    master: Any,
    cards: CardService,
    selected: SelectedCardPreview,
    *,
    window_factory: WindowFactory | None = None,
    widget_factory: TkWidgetFactory | None = None,
    on_action: Callable[[str, str], object] | None = None,
) -> CardOverlaySyncService:
    """使用玩家已選定的同一套尺寸、定位及文字樣式建立浮層。"""
    if not isinstance(selected, SelectedCardPreview):
        raise TypeError("selected must be SelectedCardPreview.")
    return build_windows_card_overlay_runtime(
        master,
        cards,
        selected.layout,
        selected.text,
        window_factory=window_factory,
        widget_factory=widget_factory,
        on_action=on_action,
    )
