"""Assemble the Windows reminder overlay lifecycle."""

from __future__ import annotations

from typing import Any

from services.card_overlay_window_lifecycle import CardOverlayWindowLifecycle
from ui.card_content_renderer import CardContentRenderer
from ui.tk_card_presenter import (
    TkCardContentPresenter,
    TkCardTextSettings,
    TkWidgetFactory,
)
from ui.windows_card_overlay import WindowFactory, WindowsCardOverlayPort


def build_windows_card_overlay_lifecycle(
    master: Any,
    settings: TkCardTextSettings,
    *,
    window_factory: WindowFactory | None = None,
    widget_factory: TkWidgetFactory | None = None,
) -> CardOverlayWindowLifecycle:
    presenter = TkCardContentPresenter(
        settings,
        widget_factory=widget_factory,
    )
    windows = WindowsCardOverlayPort(
        master,
        CardContentRenderer(presenter),
        window_factory=window_factory,
    )
    return CardOverlayWindowLifecycle(windows)
