"""Tk reminder content presenter using the confirmed SP3 visual language."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

from ui.card_content_renderer import CardContent


@dataclass(frozen=True, slots=True)
class TkCardTextSettings:
    background: str
    foreground: str
    muted_foreground: str = ""
    accent: str = ""
    font_family: str = "Microsoft JhengHei UI"
    title_size: int = 10
    body_size: int = 9
    horizontal_padding: int = 8
    vertical_padding: int = 5
    card_width: int = 160
    font_size: int | None = None
    line_spacing: int = 0

    def __post_init__(self) -> None:
        if not self.muted_foreground:
            object.__setattr__(self, "muted_foreground", self.foreground)
        if not self.accent:
            object.__setattr__(self, "accent", self.foreground)
        if self.font_size is not None:
            if (
                isinstance(self.font_size, bool)
                or not isinstance(self.font_size, int)
                or self.font_size < 1
            ):
                raise ValueError("font_size must be a positive integer.")
            object.__setattr__(self, "title_size", self.font_size)
            object.__setattr__(self, "body_size", self.font_size)
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
            "card_width",
        ):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ValueError(f"{field} must be a positive integer.")
        if (
            isinstance(self.line_spacing, bool)
            or not isinstance(self.line_spacing, int)
            or self.line_spacing < 0
        ):
            raise ValueError("line_spacing must be a non-negative integer.")


class TkWidget(Protocol):
    def configure(self, **options: Any) -> Any: ...
    def pack(self, **options: Any) -> Any: ...
    def pack_forget(self) -> Any: ...


class TkWidgetFactory(Protocol):
    def frame(self, parent: Any, **options: Any) -> TkWidget: ...
    def label(self, parent: Any, **options: Any) -> TkWidget: ...
    def button(self, parent: Any, **options: Any) -> TkWidget: ...


class _DefaultTkWidgetFactory:
    def frame(self, parent: Any, **options: Any) -> TkWidget:
        import tkinter as tk

        return tk.Frame(parent, **options)

    def label(self, parent: Any, **options: Any) -> TkWidget:
        import tkinter as tk

        return tk.Label(parent, **options)

    def button(self, parent: Any, **options: Any) -> TkWidget:
        import tkinter as tk

        return tk.Button(parent, **options)


@dataclass(slots=True)
class _RenderedCard:
    frame: TkWidget
    close: TkWidget
    group: TkWidget
    title: TkWidget
    progress: TkWidget
    next_step: TkWidget
    actions: tuple[TkWidget, ...]


_WINDOW_STATE_ATTRIBUTE = "_fu_card_rendered"


class TkCardContentPresenter:
    def __init__(
        self,
        settings: TkCardTextSettings,
        *,
        widget_factory: TkWidgetFactory | None = None,
        on_close: Callable[[str], object] | None = None,
        on_action: Callable[[str, str], object] | None = None,
    ) -> None:
        if not isinstance(settings, TkCardTextSettings):
            raise TypeError("settings must be TkCardTextSettings.")
        self._settings = settings
        self._widgets = widget_factory or _DefaultTkWidgetFactory()
        if on_close is not None and not callable(on_close):
            raise TypeError("on_close must be callable.")
        self._on_close = on_close
        if on_action is not None and not callable(on_action):
            raise TypeError("on_action must be callable.")
        self._on_action = on_action

    def _create(self, window: Any) -> _RenderedCard:
        settings = self._settings
        display_scale = max(1.0, settings.card_width / 160)
        frame = self._widgets.frame(
            window,
            background=settings.background,
            highlightbackground=settings.accent,
            highlightthickness=2,
        )
        frame.pack(fill="both", expand=True)
        close = self._widgets.button(
            frame,
            text="×",
            background=settings.background,
            foreground=settings.muted_foreground,
            activebackground=settings.background,
            activeforeground=settings.foreground,
            relief="flat",
            borderwidth=0,
            font=(settings.font_family, settings.title_size, "bold"),
            cursor="hand2",
        )
        close.pack(
            side="right",
            anchor="n",
            padx=(round(4 * display_scale), round(10 * display_scale)),
            pady=(round(6 * display_scale), 0),
        )
        group = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.muted_foreground,
            font=(settings.font_family, settings.body_size),
            anchor="center",
            justify="center",
            wraplength=settings.card_width - 2 * settings.horizontal_padding,
        )
        title = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.foreground,
            font=(settings.font_family, settings.title_size, "bold"),
            anchor="center",
            justify="center",
            wraplength=settings.card_width - 2 * settings.horizontal_padding,
        )
        progress = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.foreground,
            font=(settings.font_family, settings.body_size),
            anchor="center",
            justify="center",
            wraplength=settings.card_width - 2 * settings.horizontal_padding,
        )
        next_step = self._widgets.label(
            frame,
            background=settings.background,
            foreground=settings.accent,
            font=(settings.font_family, settings.body_size),
            anchor="center",
            justify="center",
            wraplength=settings.card_width - 2 * settings.horizontal_padding,
        )
        action_buttons = tuple(
            self._widgets.button(
                frame,
                background=settings.background,
                foreground=settings.foreground,
                activebackground=settings.accent,
                activeforeground=settings.background,
                relief="solid",
                borderwidth=1,
                font=(settings.font_family, settings.body_size),
                cursor="hand2",
            )
            for _index in range(4)
        )
        for label in (group, title, progress, next_step):
            label.pack(
                fill="x",
                padx=settings.horizontal_padding,
                pady=(3, 0),
            )
        rendered = _RenderedCard(
            frame=frame,
            close=close,
            group=group,
            title=title,
            progress=progress,
            next_step=next_step,
            actions=action_buttons,
        )
        setattr(window, _WINDOW_STATE_ATTRIBUTE, rendered)
        return rendered

    def render(self, window: Any, content: CardContent) -> None:
        if not isinstance(content, CardContent):
            raise TypeError("content must be CardContent.")
        rendered = getattr(window, _WINDOW_STATE_ATTRIBUTE, None)
        if not isinstance(rendered, _RenderedCard):
            rendered = self._create(window)
        rendered.close.configure(
            command=(
                (lambda card_id=content.card_id: self._on_close(card_id))
                if self._on_close is not None
                else None
            )
        )
        rendered.title.configure(text=content.activity_name)
        for action_button in rendered.actions:
            action_button.pack_forget()
        for action_button, action in zip(
            rendered.actions,
            content.actions,
            strict=False,
        ):
            action_button.configure(
                text=action.label,
                command=(
                    (
                        lambda action_id=action.action_id,
                        card_id=content.card_id: self._on_action(
                            card_id,
                            action_id,
                        )
                    )
                    if self._on_action is not None
                    else None
                ),
            )
            action_button.pack(
                side="left",
                padx=(max(2, self._settings.horizontal_padding // 2), 0),
                pady=(max(4, self._settings.vertical_padding), 0),
            )
        if content.name_only:
            rendered.group.pack_forget()
            rendered.progress.pack_forget()
            rendered.next_step.pack_forget()
            rendered.title.pack(
                fill="x",
                padx=self._settings.horizontal_padding,
                pady=(
                    round(
                        22
                        * max(1.0, self._settings.card_width / 160)
                    ),
                    0,
                ),
            )
            return
        rendered.group.configure(text=content.group_name)
        rendered.progress.configure(text=content.current_progress)
        rendered.next_step.pack_forget()
        for label in (
            rendered.group,
            rendered.title,
            rendered.progress,
        ):
            label.pack(
                fill="x",
                padx=self._settings.horizontal_padding,
                pady=(max(3, self._settings.vertical_padding // 2), 0),
            )
