"""Windows 提醒卡浮動視窗殼層，不決定卡片內容與視覺樣式。"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from cards.view_state import CardViewItem
from services.card_overlay_layout_service import PositionedCard


class CardWindow(Protocol):
    """浮動視窗殼層所需的最小 Tk 視窗能力。"""

    def overrideredirect(self, enabled: bool) -> Any: ...

    def attributes(self, option: str, value: Any) -> Any: ...

    def geometry(self, geometry: str) -> Any: ...

    def destroy(self) -> Any: ...


CardRenderer = Callable[[CardWindow, CardViewItem], None]
WindowFactory = Callable[[Any], CardWindow]


def _default_window_factory(master: Any) -> CardWindow:
    import tkinter as tk

    return tk.Toplevel(master)


class WindowsCardOverlayPort:
    """以置頂無邊框 Tk 視窗實作提醒卡視窗生命週期介面。"""

    def __init__(
        self,
        master: Any,
        renderer: CardRenderer,
        *,
        window_factory: WindowFactory | None = None,
    ) -> None:
        if not callable(renderer):
            raise TypeError("renderer must be callable.")
        if window_factory is not None and not callable(window_factory):
            raise TypeError("window_factory must be callable.")
        self._master = master
        self._renderer = renderer
        self._window_factory = window_factory or _default_window_factory
        self._windows: dict[str, CardWindow] = {}

    @property
    def open_card_ids(self) -> tuple[str, ...]:
        return tuple(self._windows)

    @staticmethod
    def _validated_item(item: PositionedCard) -> PositionedCard:
        if not isinstance(item, PositionedCard):
            raise TypeError("item must be PositionedCard.")
        return item

    @staticmethod
    def _geometry(item: PositionedCard) -> str:
        placement = item.placement
        return (
            f"{placement.width}x{placement.height}"
            f"{placement.x:+d}{placement.y:+d}"
        )

    def _apply(self, window: CardWindow, item: PositionedCard) -> None:
        window.geometry(self._geometry(item))
        self._renderer(window, item.card)

    def open(self, item: PositionedCard) -> None:
        item = self._validated_item(item)
        card_id = item.card.card_id
        if card_id in self._windows:
            raise ValueError(f"Card window is already open: {card_id}")

        window = self._window_factory(self._master)
        try:
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            self._apply(window, item)
        except Exception:
            window.destroy()
            raise
        self._windows[card_id] = window

    def update(self, item: PositionedCard) -> None:
        item = self._validated_item(item)
        card_id = item.card.card_id
        try:
            window = self._windows[card_id]
        except KeyError as error:
            raise KeyError(f"Card window is not open: {card_id}") from error
        self._apply(window, item)

    def close(self, card_id: str) -> None:
        window = self._windows.pop(card_id, None)
        if window is not None:
            window.destroy()
