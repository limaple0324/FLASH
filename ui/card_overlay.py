"""Right-bottom reminder placement rules."""

from dataclasses import dataclass

from cards.service import MAX_VISIBLE_CARDS


def _integer(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be int.")
    return value


def _positive(value: int, field: str) -> int:
    value = _integer(value, field)
    if value <= 0:
        raise ValueError(f"{field} must be positive.")
    return value


def _non_negative(value: int, field: str) -> int:
    value = _integer(value, field)
    if value < 0:
        raise ValueError(f"{field} must not be negative.")
    return value


@dataclass(frozen=True, slots=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        for field in ("left", "top", "right", "bottom"):
            _integer(getattr(self, field), field)
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("work area must have positive width and height.")


@dataclass(frozen=True, slots=True)
class CardSize:
    width: int
    height: int

    def __post_init__(self) -> None:
        _positive(self.width, "width")
        _positive(self.height, "height")


@dataclass(frozen=True, slots=True)
class CardPlacement:
    slot: int
    x: int
    y: int
    width: int
    height: int


def calculate_card_stack(
    work_area: WorkArea,
    card_size: CardSize,
    count: int,
    *,
    right_margin: int,
    bottom_margin: int,
    gap: int,
) -> tuple[CardPlacement, ...]:
    if not isinstance(work_area, WorkArea):
        raise TypeError("work_area must be WorkArea.")
    if not isinstance(card_size, CardSize):
        raise TypeError("card_size must be CardSize.")
    count = _non_negative(count, "count")
    right_margin = _non_negative(right_margin, "right_margin")
    bottom_margin = _non_negative(bottom_margin, "bottom_margin")
    gap = _non_negative(gap, "gap")
    if count > MAX_VISIBLE_CARDS:
        raise ValueError("Card stack cannot contain more than three cards.")
    if count == 0:
        return ()

    x = work_area.right - right_margin - card_size.width
    bottom_y = work_area.bottom - bottom_margin - card_size.height
    top_y = bottom_y - (count - 1) * (card_size.height + gap)
    if x < work_area.left or top_y < work_area.top:
        raise ValueError("Card stack does not fit inside the work area.")

    return tuple(
        CardPlacement(
            slot=slot,
            x=x,
            y=bottom_y - slot * (card_size.height + gap),
            width=card_size.width,
            height=card_size.height,
        )
        for slot in range(count)
    )
