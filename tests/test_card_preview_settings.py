import pytest

from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _text(*, background: str = "#102030") -> TkCardTextSettings:
    return TkCardTextSettings(
        background=background,
        foreground="#f0f0f0",
        font_family="Microsoft JhengHei UI",
        font_size=12,
        horizontal_padding=14,
        vertical_padding=10,
        line_spacing=4,
    )


def _profile(
    profile_id: str = "compact",
    *,
    display_name: str = "精簡預覽",
    width: int = 320,
    background: str = "#102030",
) -> CardPreviewProfile:
    return CardPreviewProfile(
        profile_id=profile_id,
        display_name=display_name,
        card_size=CardSize(width=width, height=150),
        right_margin=16,
        bottom_margin=16,
        gap=10,
        text=_text(background=background),
    )


def test_profile_keeps_size_placement_and_text_settings_together() -> None:
    profile = _profile()

    assert profile.card_size == CardSize(width=320, height=150)
    assert profile.right_margin == 16
    assert profile.bottom_margin == 16
    assert profile.gap == 10
    assert profile.text.background == "#102030"


@pytest.mark.parametrize("field", ("profile_id", "display_name"))
def test_profile_rejects_blank_identity_fields(field: str) -> None:
    values = {
        "profile_id": "compact",
        "display_name": "精簡預覽",
    }
    values[field] = "  "

    with pytest.raises(ValueError, match=field):
        CardPreviewProfile(
            **values,
            card_size=CardSize(width=320, height=150),
            right_margin=16,
            bottom_margin=16,
            gap=10,
            text=_text(),
        )


@pytest.mark.parametrize("field", ("right_margin", "bottom_margin", "gap"))
def test_profile_rejects_negative_placement_values(field: str) -> None:
    values = {"right_margin": 16, "bottom_margin": 16, "gap": 10}
    values[field] = -1

    with pytest.raises(ValueError, match=field):
        CardPreviewProfile(
            profile_id="compact",
            display_name="精簡預覽",
            card_size=CardSize(width=320, height=150),
            text=_text(),
            **values,
        )


def test_catalog_keeps_multiple_distinct_candidates() -> None:
    compact = _profile()
    roomy = _profile(
        "roomy",
        display_name="寬鬆預覽",
        width=380,
        background="#203040",
    )

    catalog = CardPreviewCatalog((compact, roomy))

    assert catalog.profiles == (compact, roomy)
    assert catalog.select("compact") is compact
    assert catalog.select("roomy") is roomy


def test_catalog_requires_explicit_selection() -> None:
    catalog = CardPreviewCatalog((_profile(),))

    with pytest.raises(KeyError):
        catalog.select("not-selected")


def test_catalog_rejects_duplicate_identifiers() -> None:
    with pytest.raises(ValueError, match="unique"):
        CardPreviewCatalog((_profile(), _profile(display_name="另一個名稱")))


def test_catalog_is_immutable_snapshot() -> None:
    source = [_profile()]
    catalog = CardPreviewCatalog(source)
    source.append(_profile("roomy"))

    assert len(catalog.profiles) == 1
