"""Complete, immutable identity for one concrete Windows window instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class WindowInstanceSource(Protocol):
    handle: int
    process_id: int | None
    thread_id: int | None
    window_class: str | None
    rect: tuple[int, int, int, int]
    minimized: bool
    process_lifecycle_token: int | None


@dataclass(frozen=True, slots=True)
class WindowInstanceToken:
    """Every field required to distinguish a reused handle or process."""

    handle: int
    process_id: int
    thread_id: int
    window_class: str
    rect: tuple[int, int, int, int]
    minimized: bool
    process_lifecycle_token: int

    def __post_init__(self) -> None:
        if (
            not self._positive_integer(self.handle)
            or not self._positive_integer(self.process_id)
            or not self._positive_integer(self.thread_id)
            or not isinstance(self.window_class, str)
            or not self.window_class.strip()
            or not isinstance(self.rect, tuple)
            or len(self.rect) != 4
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in self.rect
            )
            or self.rect[2] <= self.rect[0]
            or self.rect[3] <= self.rect[1]
            or type(self.minimized) is not bool
            or not self._positive_integer(self.process_lifecycle_token)
        ):
            raise ValueError("window instance token is incomplete")

    @classmethod
    def from_window(
        cls,
        window: WindowInstanceSource,
    ) -> "WindowInstanceToken | None":
        try:
            return cls(
                handle=window.handle,
                process_id=window.process_id,
                thread_id=window.thread_id,
                window_class=window.window_class,
                rect=window.rect,
                minimized=window.minimized,
                process_lifecycle_token=window.process_lifecycle_token,
            )
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _positive_integer(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0


__all__ = ["WindowInstanceSource", "WindowInstanceToken"]
