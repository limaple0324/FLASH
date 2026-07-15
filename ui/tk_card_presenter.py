"""可調整的 Tk 提醒卡文字呈現器，不代表最終視覺定稿。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ui.card_content_renderer import CardContent


CARD_CONTENT_FIELDS = (
    "group_name",
    "activity_name",
    "current_progress",
    "next_step",
)
_WINDOW_STATE_ATTRIBUTE = "_fu_card_text_state"


def _non_empty_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text.")


def _non_negative_integer(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class TkCardTextSettings:
    """集中保存所有暫定文字視覺參數，方便後續整體替換。"""

    background: str
    foreground: str
    font_family: str
    font_size: int
    horizontal_padding: int
    vertical_padding: int
    line_spacing: int
    field_order: tuple[str, ...] = CARD_CONTENT_FIELDS

    def __post_init__(self) -> None:
        _non_empty_text(self.background, "background")
        _non_empty_text(self.foreground, "foreground")
        _non_empty_text(self.font_family, "font_family")
        if not isinstance(self.font_size, int) or isinstance(self.font_size, bool):
            raise ValueError("font_size must be a positive integer.")
        if self.font_size <= 0:
            raise ValueError("font_size must be a positive integer.")
        _non_negative_integer(self.horizontal_padding, "horizontal_padding")
        _non_negative_integer(self.vertical_padding, "vertical_padding")
        _non_negative_integer(self.line_spacing, "line_spacing")

        order = tuple(self.field_order)
        if len(order) != len(CARD_CONTENT_FIELDS) or set(order) != set(
            CARD_CONTENT_FIELDS
        ):
            raise ValueError("field_order must contain each card content field once.")
        object.__setattr__(self, "field_order", order)


class TkWidget(Protocol):
    def configure(self, **options: Any) -> Any: ...

    def pack(self, **options: Any) -> Any: ...

    def pack_forget(self) -> Any: ...


class TkWidgetFactory(Protocol):
    def frame(self, parent: Any, **options: Any) -> TkWidget: ...

    def label(self, parent: Any, **options: Any) -> TkWidget: ...


class _DefaultTkWidgetFactory:
    def frame(self, parent: Any, **options: Any) -> TkWidget:
        import tkinter as tk

        return tk.Frame(parent, **options)

    def label(self, parent: Any, **options: Any) -> TkWidget:
        import tkinter as tk

        return tk.Label(parent, **options)


@dataclass(slots=True)
class _RenderedCard:
    frame: TkWidget
    labels: dict[str, TkWidget]


class TkCardContentPresenter:
    """在同一浮動視窗中建立或更新四項提醒卡文字。"""

    def __init__(
        self,
        settings: TkCardTextSettings,
        *,
        widget_factory: TkWidgetFactory | None = None,
    ) -> None:
        if not isinstance(settings, TkCardTextSettings):
            raise TypeError("settings must be TkCardTextSettings.")
        self._settings = settings
        self._widgets = widget_factory or _DefaultTkWidgetFactory()

    def _create(self, window: Any) -> _RenderedCard:
        settings = self._settings
        frame = self._widgets.frame(window, background=settings.background)
        frame.pack(
            fill="both",
            expand=True,
        )
        labels = {
            field: self._widgets.label(
                frame,
                background=settings.background,
                foreground=settings.foreground,
                font=(settings.font_family, settings.font_size),
                anchor="w",
                justify="left",
            )
            for field in CARD_CONTENT_FIELDS
        }
        rendered = _RenderedCard(frame=frame, labels=labels)
        setattr(window, _WINDOW_STATE_ATTRIBUTE, rendered)
        return rendered

    def render(self, window: Any, content: CardContent) -> None:
        if not isinstance(content, CardContent):
            raise TypeError("content must be CardContent.")
        rendered = getattr(window, _WINDOW_STATE_ATTRIBUTE, None)
        if not isinstance(rendered, _RenderedCard):
            rendered = self._create(window)

        for field in CARD_CONTENT_FIELDS:
            label = rendered.labels[field]
            label.configure(text=getattr(content, field) or "")
            label.pack_forget()

        visible_fields = tuple(
            field
            for field in self._settings.field_order
            if getattr(content, field) is not None
        )
        for index, field in enumerate(visible_fields):
            top_gap = self._settings.vertical_padding if index == 0 else 0
            bottom_gap = (
                self._settings.vertical_padding
                if index == len(visible_fields) - 1
                else self._settings.line_spacing
            )
            rendered.labels[field].pack(
                fill="x",
                anchor="w",
                padx=self._settings.horizontal_padding,
                pady=(top_gap, bottom_gap),
            )
