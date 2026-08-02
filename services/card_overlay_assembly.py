"""Assemble the Windows reminder overlay lifecycle."""

from __future__ import annotations

from collections.abc import Callable
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
    on_close: Callable[[str], object] | None = None,
    on_action: Callable[[str, str], object] | None = None,
) -> CardOverlayWindowLifecycle:
    presenter = TkCardContentPresenter(
        settings,
        widget_factory=widget_factory,
        on_close=on_close,
        on_action=on_action,
    )
    windows = WindowsCardOverlayPort(
        master,
        CardContentRenderer(presenter),
        window_factory=window_factory,
    )
    return CardOverlayWindowLifecycle(windows)
