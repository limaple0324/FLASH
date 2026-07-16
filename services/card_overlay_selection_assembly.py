"""組裝玩家方案選擇與完整 Windows 提醒卡浮層鏈路。"""

from __future__ import annotations

from typing import Any

from cards.service import CardService
from services.card_overlay_layout_service import (
    CardOverlayLayoutService,
    CardViewStateSource,
    WorkAreaSource,
)
from services.card_overlay_runtime import build_selected_card_overlay_runtime
from services.card_overlay_selection_coordinator import (
    CardOverlaySelectionCoordinator,
)
from services.card_preview_adapter import SelectedCardPreview
from services.card_preview_selection_service import CardPreviewSelectionService
from ui.card_preview_settings import CardPreviewProfile
from ui.tk_card_presenter import TkWidgetFactory
from ui.windows_card_overlay import WindowFactory


def build_windows_card_overlay_selection_coordinator(
    master: Any,
    cards: CardService,
    selection: CardPreviewSelectionService,
    card_state: CardViewStateSource,
    work_area: WorkAreaSource,
    *,
    window_factory: WindowFactory | None = None,
    widget_factory: TkWidgetFactory | None = None,
) -> CardOverlaySelectionCoordinator:
    """Build a stopped coordinator without choosing or defaulting a profile."""

    def build_runtime(profile: CardPreviewProfile):
        layout = CardOverlayLayoutService(
            card_state,
            work_area,
            profile.card_size,
            right_margin=profile.right_margin,
            bottom_margin=profile.bottom_margin,
            gap=profile.gap,
        )
        selected = SelectedCardPreview(
            profile=profile,
            layout=layout,
            text=profile.text,
        )
        return build_selected_card_overlay_runtime(
            master,
            cards,
            selected,
            window_factory=window_factory,
            widget_factory=widget_factory,
        )

    return CardOverlaySelectionCoordinator(selection, build_runtime)
