"""每角色靈魂石文字紀錄的獨立編輯視窗。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class EditorWindow(Protocol):
    def title(self, text: str) -> Any: ...

    def transient(self, master: Any) -> Any: ...

    def protocol(self, name: str, callback: Callable[[], None]) -> Any: ...

    def destroy(self) -> Any: ...


WindowFactory = Callable[[Any], EditorWindow]
SaveHandler = Callable[[str], None]
ClearHandler = Callable[[], None]
ErrorReporter = Callable[[str, str], None]
WidgetFactory = Callable[..., Any]
EditorRenderer = Callable[
    [
        EditorWindow,
        str,
        str | None,
        SaveHandler,
        ClearHandler,
        Callable[[], None],
    ],
    None,
]


def _default_window_factory(master: Any) -> EditorWindow:
    import tkinter as tk

    return tk.Toplevel(master)


def _default_error_reporter(title: str, message: str) -> None:
    from tkinter import messagebox

    messagebox.showerror(title, message)


def render_soul_stone_editor(
    window: EditorWindow,
    display_name: str,
    initial_note: str | None,
    on_save: SaveHandler,
    on_clear: ClearHandler,
    on_close: Callable[[], None],
    *,
    frame_factory: WidgetFactory | None = None,
    label_factory: WidgetFactory | None = None,
    entry_factory: WidgetFactory | None = None,
    button_factory: WidgetFactory | None = None,
) -> None:
    """建立可替換視覺設定的單一文字紀錄介面。"""
    import tkinter as tk

    frame_factory = frame_factory or tk.Frame
    label_factory = label_factory or tk.Label
    entry_factory = entry_factory or tk.Entry
    button_factory = button_factory or tk.Button

    body = frame_factory(window, padx=24, pady=20)
    body.pack(fill=tk.BOTH, expand=True)
    label_factory(
        body,
        text=f"【{display_name}】靈魂石紀錄",
        font=("Microsoft JhengHei UI", 12, "bold"),
        anchor="w",
    ).pack(fill=tk.X)
    label_factory(
        body,
        text="可輸入要記住的靈魂石資訊。",
        font=("Microsoft JhengHei UI", 10),
        anchor="w",
    ).pack(fill=tk.X, pady=(6, 8))

    note_entry = entry_factory(
        body,
        font=("Microsoft JhengHei UI", 11),
        width=48,
    )
    if initial_note:
        note_entry.insert(0, initial_note)
    note_entry.pack(fill=tk.X)

    actions = frame_factory(body)
    actions.pack(fill=tk.X, pady=(20, 0))
    button_factory(
        actions,
        text="保存",
        width=12,
        command=lambda: on_save(note_entry.get()),
    ).pack(side=tk.LEFT)
    button_factory(
        actions,
        text="清除紀錄",
        width=12,
        command=on_clear,
    ).pack(side=tk.LEFT, padx=(8, 0))
    button_factory(
        actions,
        text="取消",
        width=12,
        command=on_close,
    ).pack(side=tk.RIGHT)


class SoulStoneEditorWindow:
    """管理單一編輯視窗，操作失敗時保留原視窗與原紀錄。"""

    def __init__(
        self,
        master: Any,
        on_save: SaveHandler,
        on_clear: ClearHandler,
        *,
        window_factory: WindowFactory | None = None,
        renderer: EditorRenderer | None = None,
        error_reporter: ErrorReporter | None = None,
    ) -> None:
        if not callable(on_save):
            raise TypeError("on_save must be callable.")
        if not callable(on_clear):
            raise TypeError("on_clear must be callable.")
        if window_factory is not None and not callable(window_factory):
            raise TypeError("window_factory must be callable.")
        if renderer is not None and not callable(renderer):
            raise TypeError("renderer must be callable.")
        if error_reporter is not None and not callable(error_reporter):
            raise TypeError("error_reporter must be callable.")
        self._master = master
        self._on_save = on_save
        self._on_clear = on_clear
        self._window_factory = window_factory or _default_window_factory
        self._renderer = renderer or render_soul_stone_editor
        self._error_reporter = error_reporter or _default_error_reporter
        self._window: EditorWindow | None = None

    @property
    def is_open(self) -> bool:
        return self._window is not None

    def open(self, display_name: str, initial_note: str | None = None) -> None:
        if not isinstance(display_name, str):
            raise TypeError("display_name must be a string.")
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("display_name must not be empty.")
        if initial_note is not None and not isinstance(initial_note, str):
            raise TypeError("initial_note must be a string or None.")
        if self._window is not None:
            raise RuntimeError("Soul stone editor window is already open.")

        window = self._window_factory(self._master)
        self._window = window
        try:
            window.title("輔｜靈魂石紀錄")
            window.transient(self._master)
            window.protocol("WM_DELETE_WINDOW", self.close)
            self._renderer(
                window,
                normalized_name,
                initial_note,
                self._save,
                self._clear,
                self.close,
            )
        except Exception:
            self._window = None
            window.destroy()
            raise

    def _save(self, note: str) -> None:
        try:
            self._on_save(note)
        except Exception:
            self._error_reporter(
                "無法保存靈魂石紀錄",
                "請確認已輸入內容後再試；原本紀錄已保留。",
            )
            return
        self.close()

    def _clear(self) -> None:
        try:
            self._on_clear()
        except Exception:
            self._error_reporter(
                "無法清除靈魂石紀錄",
                "原本紀錄已保留，請稍後再試。",
            )
            return
        self.close()

    def close(self) -> None:
        window = self._window
        if window is None:
            return
        self._window = None
        window.destroy()
