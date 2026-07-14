import pytest

from ui.card_overlay import (
    CardPlacement,
    CardSize,
    WorkArea,
    calculate_card_stack,
)


def test_stack_anchors_to_work_area_bottom_right_and_grows_upward():
    placements = calculate_card_stack(
        WorkArea(left=0, top=0, right=1920, bottom=1040),
        CardSize(width=360, height=120),
        3,
        right_margin=16,
        bottom_margin=16,
        gap=12,
    )

    assert placements == (
        CardPlacement(slot=0, x=1544, y=904, width=360, height=120),
        CardPlacement(slot=1, x=1544, y=772, width=360, height=120),
        CardPlacement(slot=2, x=1544, y=640, width=360, height=120),
    )


def test_stack_uses_windows_work_area_instead_of_full_screen_bottom():
    placement = calculate_card_stack(
        WorkArea(left=0, top=0, right=1920, bottom=1000),
        CardSize(width=300, height=100),
        1,
        right_margin=10,
        bottom_margin=20,
        gap=0,
    )[0]

    assert placement.y == 880
    assert placement.y + placement.height == 980


def test_stack_supports_secondary_monitor_negative_coordinates():
    placement = calculate_card_stack(
        WorkArea(left=-1600, top=0, right=0, bottom=860),
        CardSize(width=320, height=100),
        1,
        right_margin=20,
        bottom_margin=20,
        gap=10,
    )[0]

    assert (placement.x, placement.y) == (-340, 740)


def test_empty_stack_has_no_positions_and_more_than_three_is_rejected():
    area = WorkArea(left=0, top=0, right=1920, bottom=1040)
    size = CardSize(width=360, height=120)

    assert calculate_card_stack(
        area,
        size,
        0,
        right_margin=16,
        bottom_margin=16,
        gap=12,
    ) == ()
    with pytest.raises(ValueError, match="more than three"):
        calculate_card_stack(
            area,
            size,
            4,
            right_margin=16,
            bottom_margin=16,
            gap=12,
        )


def test_stack_rejects_layout_that_would_leave_the_work_area():
    with pytest.raises(ValueError, match="does not fit"):
        calculate_card_stack(
            WorkArea(left=0, top=0, right=400, bottom=300),
            CardSize(width=360, height=120),
            3,
            right_margin=30,
            bottom_margin=30,
            gap=20,
        )
