from datetime import datetime, timedelta, timezone

import pytest

from adapters.windows_work_area import WorkAreaUnavailableError
from cards.view_state import CardViewItem, CardViewState
from services.card_overlay_layout_service import (
    CardOverlayLayout,
    CardOverlayLayoutService,
)
from ui.card_overlay import CardPlacement, CardSize, WorkArea


def _item(card_id: str) -> CardViewItem:
    shown_at = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)
    return CardViewItem(
        card_id=card_id,
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
    def __init__(self, *cards: CardViewItem):
        self.state = CardViewState(cards=cards)

    def snapshot(self):
        return self.state


class FixedWorkArea:
    def __init__(self, area=WorkArea(0, 0, 1920, 1040), *, error=None):
        self.area = area
        self.error = error
        self.calls = 0

    def read(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.area


def _service(cards, work_area, *, size=CardSize(360, 120)):
    return CardOverlayLayoutService(
        cards,
        work_area,
        size,
        right_margin=16,
        bottom_margin=16,
        gap=12,
    )


def test_empty_state_returns_empty_layout_without_reading_windows():
    work_area = FixedWorkArea()

    layout = _service(FixedCardState(), work_area).snapshot()

    assert layout == CardOverlayLayout()
    assert work_area.calls == 0


def test_visible_order_is_paired_with_bottom_to_top_slots():
    first = _item("first")
    second = _item("second")

    layout = _service(FixedCardState(first, second), FixedWorkArea()).snapshot()

    assert tuple(item.card.card_id for item in layout.cards) == ("first", "second")
    assert tuple(item.placement for item in layout.cards) == (
        CardPlacement(slot=0, x=1544, y=904, width=360, height=120),
        CardPlacement(slot=1, x=1544, y=772, width=360, height=120),
    )


def test_layout_uses_current_work_area_and_configurable_card_size():
    work_area = FixedWorkArea(WorkArea(-1600, 0, 0, 860))
    service = CardOverlayLayoutService(
        FixedCardState(_item("farm")),
        work_area,
        CardSize(320, 100),
        right_margin=20,
        bottom_margin=20,
        gap=10,
    )

    placement = service.snapshot().cards[0].placement

    assert placement == CardPlacement(slot=0, x=-340, y=740, width=320, height=100)
    assert work_area.calls == 1


def test_unavailable_windows_area_is_not_replaced_with_guessed_coordinates():
    error = WorkAreaUnavailableError("Windows did not return a work area.")

    with pytest.raises(WorkAreaUnavailableError, match="did not return"):
        _service(FixedCardState(_item("guard")), FixedWorkArea(error=error)).snapshot()


def test_layout_rejects_cards_that_do_not_fit_current_work_area():
    area = FixedWorkArea(WorkArea(0, 0, 350, 200))

    with pytest.raises(ValueError, match="does not fit"):
        _service(FixedCardState(_item("guard")), area).snapshot()
