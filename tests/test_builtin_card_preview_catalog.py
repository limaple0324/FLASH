from services.card_preview_selection_service import CardPreviewSelectionService
from ui.builtin_card_preview_catalog import (
    BUILTIN_CARD_PREVIEW_DISPLAY_NAME,
    BUILTIN_CARD_PREVIEW_PROFILE_ID,
    build_builtin_card_preview_catalog,
)
from ui.card_overlay import CardSize


def test_builtin_catalog_has_one_stable_replaceable_engineering_profile() -> None:
    catalog = build_builtin_card_preview_catalog()

    assert len(catalog.profiles) == 1
    profile = catalog.profiles[0]
    assert profile.profile_id == BUILTIN_CARD_PREVIEW_PROFILE_ID
    assert profile.display_name == BUILTIN_CARD_PREVIEW_DISPLAY_NAME
    assert profile.card_size == CardSize(width=360, height=120)
    assert profile.right_margin == 16
    assert profile.bottom_margin == 16
    assert profile.gap == 12
    assert profile.text.background == "#102030"
    assert profile.text.foreground == "#ffffff"
    assert profile.text.font_family == "Microsoft JhengHei UI"
    assert profile.text.font_size == 12
    assert profile.text.horizontal_padding == 12
    assert profile.text.vertical_padding == 8
    assert profile.text.line_spacing == 4


def test_builtin_catalog_does_not_select_or_enable_itself() -> None:
    service = CardPreviewSelectionService(
        build_builtin_card_preview_catalog()
    )

    assert service.snapshot().selected_profile_id is None
    assert service.snapshot().overlay_enabled is False
    assert service.selected_profile() is None
    assert service.available_choices()[0].selected is False
