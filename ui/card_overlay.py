"""Right-bottom reminder placement rules."""

from dataclasses import dataclass


def _integer(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be int.")
    return value


def _positive(value: int, field: str) -> int:
    value = _integer(value, field)
    if value <= 0:
        raise ValueError(f"{field} must be positive.")
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
