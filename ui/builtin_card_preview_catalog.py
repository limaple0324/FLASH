"""提供已由玩家確認的舊版提醒卡樣式。"""

from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


BUILTIN_CARD_PREVIEW_PROFILE_ID = "builtin-basic"
BUILTIN_CARD_PREVIEW_DISPLAY_NAME = "舊版棕色提醒卡"


def build_builtin_card_preview_catalog(
    display_scale: float = 1.0,
) -> CardPreviewCatalog:
    """依 Windows 顯示比例建立已確認的 160×75 基準樣式。"""
    if (
        isinstance(display_scale, bool)
        or not isinstance(display_scale, (int, float))
        or display_scale < 1
    ):
        raise ValueError("display_scale must be at least 1.")
    scale = float(display_scale)
    width = round(160 * scale)
    return CardPreviewCatalog(
        (
            CardPreviewProfile(
                profile_id=BUILTIN_CARD_PREVIEW_PROFILE_ID,
                display_name=BUILTIN_CARD_PREVIEW_DISPLAY_NAME,
                card_size=CardSize(
                    width=width,
                    height=round(75 * scale),
                ),
                right_margin=round(12 * scale),
                bottom_margin=round(12 * scale),
                gap=round(6 * scale),
                text=TkCardTextSettings(
                    background="#80591F",
                    foreground="#FFF2CF",
                    muted_foreground="#FFF2CF",
                    accent="#FFF2CF",
                    title_size=max(10, round(10 * scale)),
                    body_size=max(9, round(9 * scale)),
                    horizontal_padding=max(8, round(8 * scale)),
                    vertical_padding=max(5, round(5 * scale)),
                    card_width=width,
                ),
            ),
        )
    )
