"""將明確選定的提醒卡預覽方案接到既有浮層元件。"""

from __future__ import annotations

from dataclasses import dataclass

from services.card_overlay_layout_service import (
    CardOverlayLayoutService,
    CardViewStateSource,
    WorkAreaSource,
)
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


@dataclass(frozen=True, slots=True)
class SelectedCardPreview:
    """同一候選方案產生的定位來源與文字視覺設定。"""

    profile: CardPreviewProfile
    layout: CardOverlayLayoutService
    text: TkCardTextSettings


def select_card_preview(
    catalog: CardPreviewCatalog,
    profile_id: str,
    card_state: CardViewStateSource,
    work_area: WorkAreaSource,
) -> SelectedCardPreview:
    """Select one candidate explicitly; never infer or fall back to another."""
    if not isinstance(catalog, CardPreviewCatalog):
        raise TypeError("catalog must be CardPreviewCatalog.")

    profile = catalog.select(profile_id)
    layout = CardOverlayLayoutService(
        card_state,
        work_area,
        profile.card_size,
        right_margin=profile.right_margin,
        bottom_margin=profile.bottom_margin,
        gap=profile.gap,
    )
    return SelectedCardPreview(
        profile=profile,
        layout=layout,
        text=profile.text,
    )
