"""Tk reminder content presenter using the confirmed SP3 visual language."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ui.card_content_renderer import CardContent


@dataclass(frozen=True, slots=True)
class TkCardTextSettings:
    background: str
    foreground: str
    muted_foreground: str
    accent: str
    font_family: str = "Microsoft JhengHei UI"
    title_size: int = 12
    body_size: int = 10
    horizontal_padding: int = 18
    vertical_padding: int = 14

    def __post_init__(self) -> None:
        for field in (
            "background",
            "foreground",
            "muted_foreground",
            "accent",
            "font_family",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-empty text.")
        for field in (
            "title_size",
            "body_size",
            "horizontal_padding",
            "vertical_padding",
        ):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ValueError(f"{field} must be a positive integer.")


class TkWidget(Protocol):
    def configure(self, **options: Any) -> Any: ...
    def pack(self, **options: Any) -> Any: ...


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
    group: TkWidget
    title: TkWidget
    progress: TkWidget
    next_step: TkWidget


_WINDOW_STATE_ATTRIBUTE = "_fu_card_rendered"


class TkCardContentPresenter:
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
        frame = self._widgets.frame(
            window,
            background=settings.background,
            highlightbackground=settings.accent,
            highlightthickness=2,
        )
        frame.pack(fill="both", expand=True)
        group = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.muted_foreground,
            font=(settings.font_family, settings.body_size),
            anchor="w",
        )
        title = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.foreground,
            font=(settings.font_family, settings.title_size, "bold"),
            anchor="w",
        )
        progress = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.foreground,
            font=(settings.font_family, settings.body_size),
            anchor="w",
        )
        next_step = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.accent,
            font=(settings.font_family, settings.body_size),
            anchor="w",
        )
        for label in (group, title, progress, next_step):
            label.pack(
                fill="x",
                padx=settings.horizontal_padding,
                pady=(3, 0),
            )
        rendered = _RenderedCard(
            frame=frame,
            group=group,
            title=title,
            progress=progress,
            next_step=next_step,
        )
        setattr(window, _WINDOW_STATE_ATTRIBUTE, rendered)
        return rendered

    def render(self, window: Any, content: CardContent) -> None:
        if not isinstance(content, CardContent):
            raise TypeError("content must be CardContent.")
        rendered = getattr(window, _WINDOW_STATE_ATTRIBUTE, None)
        if not isinstance(rendered, _RenderedCard):
            rendered = self._create(window)
        rendered.group.configure(text=content.group_name)
        rendered.title.configure(text=content.activity_name)
        rendered.progress.configure(text=content.current_progress)
        rendered.next_step.configure(
            text=f"下一步：{content.next_step or '尚未提供'}"
        )
