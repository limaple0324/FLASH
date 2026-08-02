from datetime import datetime, timedelta, timezone

import pytest

from cards.view_state import CardViewItem, CardViewState
from services.card_preview_adapter import select_card_preview
from ui.card_overlay import CardPlacement, CardSize, WorkArea
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _text(background: str) -> TkCardTextSettings:
    return TkCardTextSettings(
        background=background,
        foreground="#ffffff",
        font_family="Microsoft JhengHei UI",
        font_size=12,
        horizontal_padding=12,
        vertical_padding=8,
        line_spacing=4,
    )


def _profile(
    profile_id: str,
    *,
    width: int,
    height: int,
    right_margin: int,
    bottom_margin: int,
    gap: int,
    background: str,
) -> CardPreviewProfile:
    return CardPreviewProfile(
        profile_id=profile_id,
        display_name=f"{profile_id} 預覽",
        card_size=CardSize(width, height),
        right_margin=right_margin,
        bottom_margin=bottom_margin,
        gap=gap,
        text=_text(background),
    )


def _card() -> CardViewItem:
    shown_at = datetime(2026, 7, 16, tzinfo=timezone.utc)
    return CardViewItem(
        card_id="guard",
        group_id="14-windows",
        group_name="14支",
        activity_id="guard",
        activity_name="守紀",
        current_progress="守紀中斷",
        affected_character_ids=("120-old",),
        daily_summary="今日守紀尚未完成",
        requires_player_action=True,
        next_step="返回競技場繼續守紀",
        priority_reason="斷線",
        priority_level=1,
        shown_at=shown_at,
        expires_at=shown_at + timedelta(seconds=30),
    )


class FixedCardState:
    def __init__(self, state: CardViewState) -> None:
        self.state = state

    def snapshot(self) -> CardViewState:
        return self.state


class FixedWorkArea:
    def read(self) -> WorkArea:
        return WorkArea(0, 0, 1920, 1040)


def _catalog() -> CardPreviewCatalog:
    return CardPreviewCatalog(
        (
            _profile(
                "compact",
                width=320,
                height=100,
                right_margin=12,
                bottom_margin=14,
                gap=8,
                background="#102030",
            ),
            _profile(
                "roomy",
                width=400,
                height=160,
                right_margin=24,
                bottom_margin=28,
                gap=16,
                background="#304050",
            ),
        )
    )


def test_selected_profile_drives_layout_and_text_from_same_candidate() -> None:
    selected = select_card_preview(
        _catalog(),
        "roomy",
        FixedCardState(CardViewState(cards=(_card(),))),
        FixedWorkArea(),
    )

    assert selected.profile.profile_id == "roomy"
    assert selected.text is selected.profile.text
    assert selected.text.background == "#304050"
    assert selected.layout.snapshot().cards[0].placement == CardPlacement(
        slot=0,
        x=1496,
        y=852,
        width=400,
        height=160,
    )


def test_different_selection_changes_both_layout_and_text() -> None:
    selected = select_card_preview(
        _catalog(),
        "compact",
        FixedCardState(CardViewState(cards=(_card(),))),
        FixedWorkArea(),
    )

    assert selected.text.background == "#102030"
    assert selected.layout.snapshot().cards[0].placement.width == 320


def test_unknown_profile_does_not_fall_back() -> None:
    with pytest.raises(KeyError):
        select_card_preview(
            _catalog(),
            "not-selected",
            FixedCardState(CardViewState()),
            FixedWorkArea(),
        )


def test_adapter_rejects_non_catalog_input() -> None:
    with pytest.raises(TypeError, match="catalog"):
        select_card_preview(
            object(),
            "compact",
            FixedCardState(CardViewState()),
            FixedWorkArea(),
        )


def test_empty_card_state_stays_empty_after_selection() -> None:
    selected = select_card_preview(
        _catalog(),
        "compact",
        FixedCardState(CardViewState()),
        FixedWorkArea(),
    )

    assert selected.layout.snapshot().cards == ()
