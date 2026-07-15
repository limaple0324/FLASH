"""組合 Windows 提醒卡浮層元件，不提供或固定視覺設定。"""

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
    """Build the complete window/content lifecycle from caller-supplied settings."""
    presenter = TkCardContentPresenter(
        settings,
        widget_factory=widget_factory,
    )
    renderer = CardContentRenderer(presenter)
    windows = WindowsCardOverlayPort(
        master,
        renderer,
        window_factory=window_factory,
    )
    return CardOverlayWindowLifecycle(windows)
