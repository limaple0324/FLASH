"""單一角色詳細資料的唯讀視窗外殼。"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from services.character_detail_view_service import PlayerCharacterDetail
from ui.home import format_player_character_detail


class DetailWindow(Protocol):
    def title(self, text: str) -> Any: ...

    def transient(self, master: Any) -> Any: ...

    def protocol(self, name: str, callback: Callable[[], None]) -> Any: ...

    def destroy(self) -> Any: ...


WindowFactory = Callable[[Any], DetailWindow]
DetailRenderer = Callable[
    [DetailWindow, PlayerCharacterDetail, Callable[[], None]],
    None,
]
WidgetFactory = Callable[..., Any]


def _default_window_factory(master: Any) -> DetailWindow:
    import tkinter as tk

    return tk.Toplevel(master)


def render_character_detail(
    window: DetailWindow,
    detail: PlayerCharacterDetail,
    on_close: Callable[[], None],
    *,
    on_edit_soul_stone: Callable[[], None] | None = None,
    frame_factory: WidgetFactory | None = None,
    label_factory: WidgetFactory | None = None,
    button_factory: WidgetFactory | None = None,
) -> None:
    """建立可替換視覺設定的基本中文唯讀內容。"""
    import tkinter as tk

    frame_factory = frame_factory or tk.Frame
    label_factory = label_factory or tk.Label
    button_factory = button_factory or tk.Button

    body = frame_factory(window, padx=24, pady=20)
    body.pack(fill=tk.BOTH, expand=True)
    label_factory(
        body,
        text=format_player_character_detail(detail),
        font=("Microsoft JhengHei UI", 12),
        justify="left",
        anchor="w",
    ).pack(fill=tk.X)
    if on_edit_soul_stone is not None:
        button_factory(
            body,
            text="編輯靈魂石",
            width=12,
            command=on_edit_soul_stone,
        ).pack(pady=(20, 0))
    button_factory(
        body,
        text="關閉",
        width=12,
        command=on_close,
    ).pack(pady=(8 if on_edit_soul_stone is not None else 20, 0))


class CharacterDetailWindow:
    """管理單一角色詳細視窗的建立與安全關閉。"""

    def __init__(
        self,
        master: Any,
        *,
        on_edit_soul_stone: Callable[[], None] | None = None,
        window_factory: WindowFactory | None = None,
        renderer: DetailRenderer | None = None,
    ) -> None:
        if on_edit_soul_stone is not None and not callable(on_edit_soul_stone):
            raise TypeError("on_edit_soul_stone must be callable.")
        if window_factory is not None and not callable(window_factory):
            raise TypeError("window_factory must be callable.")
        if renderer is not None and not callable(renderer):
            raise TypeError("renderer must be callable.")
        self._master = master
        self._on_edit_soul_stone = on_edit_soul_stone
        self._window_factory = window_factory or _default_window_factory
        self._renderer = renderer
        self._window: DetailWindow | None = None

    @property
    def is_open(self) -> bool:
        return self._window is not None

    def open(self, detail: PlayerCharacterDetail) -> None:
        if not isinstance(detail, PlayerCharacterDetail):
            raise TypeError("detail must be PlayerCharacterDetail.")
        if self._window is not None:
            raise RuntimeError("Character detail window is already open.")

        window = self._window_factory(self._master)
        self._window = window
        try:
            window.title("輔｜角色詳細資料")
            window.transient(self._master)
            window.protocol("WM_DELETE_WINDOW", self.close)
            if self._renderer is None:
                render_character_detail(
                    window,
                    detail,
                    self.close,
                    on_edit_soul_stone=self._on_edit_soul_stone,
                )
            else:
                self._renderer(window, detail, self.close)
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
