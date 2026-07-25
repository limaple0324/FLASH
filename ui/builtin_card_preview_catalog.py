"""正常啟動可選的保守提醒卡候選；不代表最終視覺定稿。"""

from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


BUILTIN_CARD_PREVIEW_PROFILE_ID = "builtin-basic"
BUILTIN_CARD_PREVIEW_DISPLAY_NAME = "基礎提醒卡（預覽）"


def build_builtin_card_preview_catalog() -> CardPreviewCatalog:
    """Build a replaceable engineering preview without selecting it for the player."""
    return CardPreviewCatalog(
        (
            CardPreviewProfile(
                profile_id=BUILTIN_CARD_PREVIEW_PROFILE_ID,
                display_name=BUILTIN_CARD_PREVIEW_DISPLAY_NAME,
                card_size=CardSize(width=360, height=120),
                right_margin=16,
                bottom_margin=16,
                gap=12,
                text=TkCardTextSettings(
                    background="#102030",
                    foreground="#ffffff",
                    font_family="Microsoft JhengHei UI",
                    font_size=12,
                    horizontal_padding=12,
                    vertical_padding=8,
                    line_spacing=4,
                ),
            ),
        )
    )
