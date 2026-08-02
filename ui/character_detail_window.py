"""Player-facing character detail and note editor."""

from __future__ import annotations

from collections.abc import Callable
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    X,
    Y,
    Button,
    Canvas,
    Entry,
    Frame,
    Label,
    Scrollbar,
    Toplevel,
)

from services.character_detail_view_service import PlayerCharacterDetail


BACKGROUND = "#C9A35D"
SURFACE = "#EAD3A0"
PRIMARY = "#8A5A24"
TEXT = "#2B1A0A"
MUTED = "#7A5321"
BORDER = "#80591F"


def _display_value(value: object) -> str:
    if value is None:
        return "尚未設定"
    if isinstance(value, str):
        return value.strip() or "尚未設定"
    return str(value)


class CharacterDetailWindow:
    def __init__(
        self,
        parent,
        detail: PlayerCharacterDetail,
        *,
        on_save_note: Callable[[str], PlayerCharacterDetail],
        on_clear_note: Callable[[], PlayerCharacterDetail],
        on_error: Callable[[Exception], object] | None = None,
        window_factory: Callable[[object], object] | None = None,
    ) -> None:
        if not isinstance(detail, PlayerCharacterDetail):
            raise TypeError("detail must be PlayerCharacterDetail.")
        if not callable(on_save_note) or not callable(on_clear_note):
            raise TypeError("note callbacks must be callable.")
        self.parent = parent
        self.detail = detail
        self.on_save_note = on_save_note
        self.on_clear_note = on_clear_note
        self.on_error = on_error
        self.window_factory = window_factory or Toplevel
        self.window = None
        self._note_entry: Entry | None = None
        self._value_labels: dict[str, Label] = {}

    def open(self):
        window = self.window_factory(self.parent)
        self.window = window
        window.title(f"輔｜{self.detail.display_name}")
        window.geometry("560x430")
        window.minsize(500, 390)
        window.configure(bg=BACKGROUND)

        viewport = Frame(window, bg=BACKGROUND)
        viewport.pack(fill=BOTH, expand=True)
        canvas = Canvas(
            viewport,
            bg=BACKGROUND,
            highlightthickness=0,
            bd=0,
            yscrollincrement=20,
        )
        scrollbar = Scrollbar(
            viewport,
            orient="vertical",
            command=canvas.yview,
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill=Y)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)

        body = Frame(canvas, bg=BACKGROUND, padx=24, pady=22)
        body_window = canvas.create_window(
            (0, 0),
            window=body,
            anchor="nw",
        )

        def refresh_scroll_region(_event=None) -> None:
            bounds = canvas.bbox("all")
            if bounds is not None:
                canvas.configure(scrollregion=bounds)

        def resize_body(event) -> None:
            canvas.itemconfigure(body_window, width=event.width)

        def scroll_body(event) -> str:
            delta = getattr(event, "delta", 0)
            if delta:
                canvas.yview_scroll(-int(delta / abs(delta)), "units")
            return "break"

        body.bind("<Configure>", refresh_scroll_region)
        canvas.bind("<Configure>", resize_body)
        window.bind("<MouseWheel>", scroll_body)
        Label(
            body,
            text=self.detail.display_name,
            font=("Microsoft JhengHei UI", 20, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            body,
            text="角色詳細資料",
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(2, 14))

        details = Frame(
            body,
            bg=SURFACE,
            padx=16,
            pady=12,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        details.pack(fill=X)
        for key, title, value in (
            ("group", "組別", self.detail.group),
            ("level", "等級", self.detail.level),
            ("importance", "分類", self.detail.importance),
            ("role", "定位", self.detail.role),
        ):
            row = Frame(details, bg=SURFACE)
            row.pack(fill=X, pady=4)
            Label(
                row,
                text=title,
                width=8,
                font=("Microsoft JhengHei UI", 10),
                bg=SURFACE,
                fg=MUTED,
                anchor="w",
            ).pack(side=LEFT)
            label = Label(
                row,
                text=_display_value(value),
                font=("Microsoft JhengHei UI", 10),
                bg=SURFACE,
                fg=TEXT,
                anchor="w",
            )
            label.pack(side=LEFT, fill=X, expand=True)
            self._value_labels[key] = label

        game_data = self.detail.game_data
        extended = Frame(
            body,
            bg=SURFACE,
            padx=16,
            pady=12,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        extended.pack(fill=X, pady=(14, 0))
        Label(
            extended,
            text="遊戲資料",
            font=("Microsoft JhengHei UI", 11, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X, pady=(0, 4))
        for title, value in (
            (
                "寵物天賦",
                game_data.pet_talent if game_data is not None else None,
            ),
            (
                "黑曜石",
                game_data.obsidian if game_data is not None else None,
            ),
            (
                "命魂",
                game_data.life_soul if game_data is not None else None,
            ),
            (
                "魂器",
                game_data.artifact if game_data is not None else None,
            ),
        ):
            row = Frame(extended, bg=SURFACE)
            row.pack(fill=X, pady=3)
            Label(
                row,
                text=title,
                width=8,
                font=("Microsoft JhengHei UI", 10),
                bg=SURFACE,
                fg=MUTED,
                anchor="w",
            ).pack(side=LEFT)
            Label(
                row,
                text=_display_value(value),
                font=("Microsoft JhengHei UI", 10),
                bg=SURFACE,
                fg=TEXT,
                anchor="w",
                justify=LEFT,
                wraplength=390,
            ).pack(side=LEFT, fill=X, expand=True)

        note_card = Frame(
            body,
            bg=SURFACE,
            padx=16,
            pady=14,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        note_card.pack(fill=X, pady=(14, 0))
        Label(
            note_card,
            text="備註",
            font=("Microsoft JhengHei UI", 11, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        self._note_entry = Entry(
            note_card,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        if self.detail.note:
            self._note_entry.insert(0, self.detail.note)
        self._note_entry.pack(fill=X, pady=(8, 12), ipady=8)
        actions = Frame(note_card, bg=SURFACE)
        actions.pack(fill=X)
        Button(
            actions,
            text="儲存備註",
            command=self._save,
            font=("Microsoft JhengHei UI", 10),
            bg=PRIMARY,
            fg="#FFFFFF",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
        ).pack(side=LEFT)
        Button(
            actions,
            text="清除",
            command=self._clear,
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=TEXT,
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
        ).pack(side=LEFT, padx=(8, 0))
        return window

    def _save(self) -> None:
        if self._note_entry is None:
            return
        note = self._note_entry.get().strip()
        if not note:
            self._clear()
            return
        self._apply(lambda: self.on_save_note(note))

    def _clear(self) -> None:
        self._apply(self.on_clear_note)

    def _apply(
        self,
        operation: Callable[[], PlayerCharacterDetail],
    ) -> None:
        try:
            detail = operation()
            if not isinstance(detail, PlayerCharacterDetail):
                raise TypeError("note operation must return PlayerCharacterDetail.")
        except Exception as error:
            if self.on_error is None:
                raise
            self.on_error(error)
            return
        self.detail = detail
        if self._note_entry is not None:
            self._note_entry.delete(0, END)
            if detail.note:
                self._note_entry.insert(0, detail.note)
