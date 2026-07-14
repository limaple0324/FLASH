from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cards.view_state import CardViewItem
from services.card_overlay_layout_service import CardOverlayLayout, PositionedCard
from services.card_overlay_window_lifecycle import CardOverlayWindowLifecycle
from ui.card_overlay import CardPlacement


def _item(card_id: str, *, progress: str = "守紀中斷", slot: int = 0) -> PositionedCard:
    shown_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    card = CardViewItem(
        card_id=card_id,
        group_id="14-windows",
        group_name="14支",
        activity_id="guard",
        activity_name="守紀",
        current_progress=progress,
        affected_character_ids=("120-old",),
        daily_summary="今日守紀尚未完成",
        requires_player_action=True,
        next_step="返回競技場繼續守紀",
        priority_reason="斷線",
        priority_level=1,
        shown_at=shown_at,
        expires_at=shown_at + timedelta(seconds=30),
    )
    return PositionedCard(
        card=card,
        placement=CardPlacement(
            slot=slot,
            x=1544,
            y=904 - slot * 132,
            width=360,
            height=120,
        ),
    )


class RecordingWindows:
    def __init__(self):
        self.operations = []

    def open(self, item):
        self.operations.append(("open", item.card.card_id, item))

    def update(self, item):
        self.operations.append(("update", item.card.card_id, item))

    def close(self, card_id):
        self.operations.append(("close", card_id, None))


def test_first_sync_opens_each_positioned_card_once():
    windows = RecordingWindows()
    lifecycle = CardOverlayWindowLifecycle(windows)
    first = _item("first")
    second = _item("second", slot=1)

    lifecycle.sync(CardOverlayLayout(cards=(first, second)))

    assert windows.operations == [
        ("open", "first", first),
        ("open", "second", second),
    ]
    assert lifecycle.visible_card_ids == ("first", "second")


def test_identical_layout_does_not_repeat_window_operations():
    windows = RecordingWindows()
    lifecycle = CardOverlayWindowLifecycle(windows)
    layout = CardOverlayLayout(cards=(_item("guard"),))
    lifecycle.sync(layout)
    windows.operations.clear()

    lifecycle.sync(layout)

    assert windows.operations == []


def test_sync_closes_removed_updates_changed_and_opens_new_cards():
    windows = RecordingWindows()
    lifecycle = CardOverlayWindowLifecycle(windows)
    first = _item("first")
    second = _item("second", slot=1)
    lifecycle.sync(CardOverlayLayout(cards=(first, second)))
    windows.operations.clear()
    changed_first = replace(
        first,
        card=replace(first.card, current_progress="已恢復登入"),
    )
    third = _item("third", slot=1)

    lifecycle.sync(CardOverlayLayout(cards=(changed_first, third)))

    assert windows.operations == [
        ("close", "second", None),
        ("update", "first", changed_first),
        ("open", "third", third),
    ]
    assert lifecycle.visible_card_ids == ("first", "third")


def test_empty_layout_and_close_all_close_visible_windows():
    windows = RecordingWindows()
    lifecycle = CardOverlayWindowLifecycle(windows)
    lifecycle.sync(CardOverlayLayout(cards=(_item("first"), _item("second", slot=1))))
    windows.operations.clear()

    lifecycle.sync(CardOverlayLayout())

    assert windows.operations == [
        ("close", "first", None),
        ("close", "second", None),
    ]
    assert lifecycle.visible_card_ids == ()
    lifecycle.close_all()
    assert len(windows.operations) == 2


def test_invalid_layout_is_rejected_before_any_window_operation():
    windows = RecordingWindows()
    lifecycle = CardOverlayWindowLifecycle(windows)
    duplicate = _item("same")

    with pytest.raises(ValueError, match="must be unique"):
        lifecycle.sync(CardOverlayLayout(cards=(duplicate, duplicate)))
    with pytest.raises(ValueError, match="more than three"):
        lifecycle.sync(
            CardOverlayLayout(
                cards=tuple(_item(str(index), slot=index) for index in range(4))
            )
        )

    assert windows.operations == []
    assert lifecycle.visible_card_ids == ()
