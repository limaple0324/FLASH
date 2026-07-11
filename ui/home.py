"""SP1 Home UI foundation.

Player-facing presentation layer for the FLASH desktop entrypoint.
Keeps engineering diagnostics separated from the player home experience.
"""

from __future__ import annotations

from tkinter import BOTH, X, Button, Frame, Label


class HomeView:
    """First version of the player home screen."""

    def __init__(self, parent, status: dict[str, object], on_start=None):
        self.parent = parent
        self.status = status
        self.on_start = on_start

    def build(self):
        body = Frame(self.parent, padx=28, pady=24)
        body.pack(fill=BOTH, expand=True)

        Label(
            body,
            text="輔",
            font=("Microsoft JhengHei UI", 24, "bold"),
            anchor="w",
        ).pack(fill=X)

        Label(
            body,
            text="目前組別\n尚未設定",
            font=("Microsoft JhengHei UI", 12),
            anchor="w",
        ).pack(fill=X, pady=12)

        Button(
            body,
            text="啟動輔助",
            width=18,
            command=self.on_start,
        ).pack(pady=12)

        Label(
            body,
            text="目前狀態\n● 已準備完成",
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        Label(
            body,
            text="Workspace\n等待輔助工作",
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        Label(
            body,
            text="Card\n系統正常",
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        return body
