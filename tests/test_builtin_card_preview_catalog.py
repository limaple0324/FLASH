from services.card_preview_selection_service import CardPreviewSelectionService
from ui.builtin_card_preview_catalog import (
    BUILTIN_CARD_PREVIEW_DISPLAY_NAME,
    BUILTIN_CARD_PREVIEW_PROFILE_ID,
    build_builtin_card_preview_catalog,
)
from ui.card_overlay import CardSize


def test_builtin_catalog_uses_confirmed_legacy_card_baseline() -> None:
    catalog = build_builtin_card_preview_catalog()

    assert len(catalog.profiles) == 1
    profile = catalog.profiles[0]
    assert profile.profile_id == BUILTIN_CARD_PREVIEW_PROFILE_ID
    assert profile.display_name == BUILTIN_CARD_PREVIEW_DISPLAY_NAME
    assert profile.card_size == CardSize(width=160, height=75)
    assert profile.right_margin == 12
    assert profile.bottom_margin == 12
    assert profile.gap == 6
    assert profile.text.background == "#80591F"
    assert profile.text.foreground == "#FFF2CF"
    assert profile.text.font_family == "Microsoft JhengHei UI"
    assert profile.text.title_size == 10
    assert profile.text.body_size == 9
    assert profile.text.horizontal_padding == 8
    assert profile.text.vertical_padding == 5


def test_builtin_catalog_scales_with_windows_display_ratio() -> None:
    profile = build_builtin_card_preview_catalog(1.5).profiles[0]

    assert profile.card_size == CardSize(width=240, height=112)
    assert profile.right_margin == 18
    assert profile.bottom_margin == 18
    assert profile.gap == 9


def test_builtin_catalog_does_not_select_or_enable_itself() -> None:
    service = CardPreviewSelectionService(
        build_builtin_card_preview_catalog()
    )

    assert service.snapshot().selected_profile_id is None
    assert service.snapshot().overlay_enabled is False
    assert service.selected_profile() is None
    assert service.available_choices()[0].selected is False
