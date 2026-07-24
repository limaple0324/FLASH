"""目前組別角色的唯讀選擇視窗。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from services.character_detail_view_service import PlayerCharacterDetail


class CharacterListWindowHandle(Protocol):
    def title(self, text: str) -> Any: ...

    def transient(self, master: Any) -> Any: ...

    def protocol(self, name: str, callback: Callable[[], None]) -> Any: ...

    def destroy(self) -> Any: ...


WindowFactory = Callable[[Any], CharacterListWindowHandle]
CharacterSelector = Callable[[PlayerCharacterDetail], None]
ListRenderer = Callable[
    [
        CharacterListWindowHandle,
        tuple[PlayerCharacterDetail, ...],
        CharacterSelector,
        Callable[[], None],
    ],
    None,
]
WidgetFactory = Callable[..., Any]


def _default_window_factory(master: Any) -> CharacterListWindowHandle:
    import tkinter as tk

    return tk.Toplevel(master)


def _choice_text(detail: PlayerCharacterDetail) -> str:
    parts = [detail.display_name]
    if detail.level is not None:
        parts.append(f"{detail.level} 級")
    if detail.importance:
        parts.append(detail.importance)
    if detail.role:
        parts.append(detail.role)
    return "｜".join(parts)


def render_character_list(
    window: CharacterListWindowHandle,
    details: tuple[PlayerCharacterDetail, ...],
    on_select: CharacterSelector,
    on_close: Callable[[], None],
    *,
    frame_factory: WidgetFactory | None = None,
    label_factory: WidgetFactory | None = None,
    button_factory: WidgetFactory | None = None,
) -> None:
    """顯示分組角色按鈕，不外露固定識別或視窗技術資料。"""
    import tkinter as tk

    frame_factory = frame_factory or tk.Frame
    label_factory = label_factory or tk.Label
    button_factory = button_factory or tk.Button

    body = frame_factory(window, padx=24, pady=20)
    body.pack(fill=tk.BOTH, expand=True)
    if not details:
        label_factory(
            body,
            text="目前沒有可顯示的組別與角色資料。",
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=tk.X)
    else:
        grouped: dict[str, list[PlayerCharacterDetail]] = {}
        for detail in details:
            group = detail.group.strip() if detail.group and detail.group.strip() else "未分組"
            grouped.setdefault(group, []).append(detail)
        for group in sorted(grouped, key=lambda value: (value == "未分組", value)):
            characters = grouped[group]
            label_factory(
                body,
                text=f"【{group}】",
                font=("Microsoft JhengHei UI", 11, "bold"),
                anchor="w",
            ).pack(fill=tk.X, pady=(8, 2))
            for detail in characters:
                button_factory(
                    body,
                    text=_choice_text(detail),
                    command=lambda selected=detail: on_select(selected),
                    anchor="w",
                ).pack(fill=tk.X, pady=2)

    button_factory(
        body,
        text="關閉",
        width=12,
        command=on_close,
    ).pack(pady=(20, 0))


class CharacterListWindow:
    """管理單一角色清單視窗與明確的角色點選事件。"""

    def __init__(
        self,
        master: Any,
        on_select: CharacterSelector,
        *,
        window_factory: WindowFactory | None = None,
        renderer: ListRenderer | None = None,
    ) -> None:
        if not callable(on_select):
            raise TypeError("on_select must be callable.")
        if window_factory is not None and not callable(window_factory):
            raise TypeError("window_factory must be callable.")
        if renderer is not None and not callable(renderer):
            raise TypeError("renderer must be callable.")
        self._master = master
        self._on_select = on_select
        self._window_factory = window_factory or _default_window_factory
        self._renderer = renderer or render_character_list
        self._window: CharacterListWindowHandle | None = None

    @property
    def is_open(self) -> bool:
        return self._window is not None

    def open(self, details: Iterable[PlayerCharacterDetail]) -> None:
        snapshots = tuple(details)
        if any(not isinstance(item, PlayerCharacterDetail) for item in snapshots):
            raise TypeError("details must contain PlayerCharacterDetail values.")
        if self._window is not None:
            raise RuntimeError("Character list window is already open.")

        window = self._window_factory(self._master)
        self._window = window
        try:
            window.title("輔｜組別角色")
            window.transient(self._master)
            window.protocol("WM_DELETE_WINDOW", self.close)
            self._renderer(window, snapshots, self._on_select, self.close)
        except Exception:
            self._window = None
            window.destroy()
            raise

    def close(self) -> None:
        window = self._window
        if window is None:
            return
        self._window = None
        window.destroy()
