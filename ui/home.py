"""SP1 Home UI foundation.

Player-facing presentation layer for the FLASH desktop entrypoint.
Keeps engineering diagnostics separated from the player home experience.
"""

from __future__ import annotations

from tkinter import BOTH, X, Button, Frame, Label

from cards.view_state import CardViewState


def _characters(status: dict[str, object]) -> list[dict[str, object]]:
    registry = status.get("window_registry", {})
    if not isinstance(registry, dict):
        return []
    characters = registry.get("characters", [])
    if not isinstance(characters, list):
        return []
    return [item for item in characters if isinstance(item, dict)]


def _group_text(status: dict[str, object]) -> str:
    characters = _characters(status)
    if not characters:
        return "目前組別\n尚未設定"

    groups = sorted({
        str(item.get("group")).strip()
        for item in characters
        if isinstance(item.get("group"), str) and str(item.get("group")).strip()
    })
    names = [
        str(item.get("display_name")).strip()
        for item in characters
        if isinstance(item.get("display_name"), str) and str(item.get("display_name")).strip()
    ]
    title = "、".join(groups) if groups else "未分組"
    preview = "、".join(names[:3])
    if len(names) > 3:
        preview += f" 等 {len(names)} 個角色"
    return f"目前組別\n{title}\n{preview}"


def _status_text(status: dict[str, object]) -> str:
    if not bool(status.get("self_check_passed", False)):
        return "目前狀態\n● 需要檢查"
    target = status.get("target_window", {})
    if isinstance(target, dict) and bool(target.get("safe", False)):
        return "目前狀態\n● 已找到遊戲視窗"
    return "目前狀態\n● 已準備完成"


def _workspace_text(status: dict[str, object]) -> str:
    characters = _characters(status)
    if characters:
        return f"工作區\n已載入 {len(characters)} 個角色"
    return "工作區\n等待設定組別"


def _card_text(
    status: dict[str, object],
    card_view_state: CardViewState | None = None,
) -> str:
    if card_view_state is not None:
        count = len(card_view_state.cards)
        if card_view_state.is_empty:
            return "提醒卡（0）\n目前沒有提醒"

        card = card_view_state.cards[0]
        next_step = card.next_step or "尚未提供"
        return (
            f"提醒卡（{count}）\n"
            f"{card.group_name}｜{card.activity_name}\n"
            f"進度：{card.current_progress}\n"
            f"下一步：{next_step}"
        )

    if not bool(status.get("self_check_passed", False)):
        return "提醒卡\n自我檢查發現問題"
    target = status.get("target_window", {})
    if isinstance(target, dict) and target.get("configured") is False:
        return "提醒卡\n尚未設定遊戲主視窗"
    return "提醒卡\n系統正常"


class HomeView:
    """First version of the player home screen."""

    def __init__(
        self,
        parent,
        status: dict[str, object],
        on_start=None,
        card_view_state: CardViewState | None = None,
    ):
        self.parent = parent
        self.status = status
        self.on_start = on_start
        self.card_view_state = card_view_state

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
            text=_group_text(self.status),
            font=("Microsoft JhengHei UI", 12),
            anchor="w",
        ).pack(fill=X, pady=12)

        Button(
            body,
            text="查看目前狀態",
            width=18,
            command=self.on_start,
        ).pack(pady=12)

        Label(
            body,
            text=_status_text(self.status),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        Label(
            body,
            text=_workspace_text(self.status),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        Label(
            body,
            text=_card_text(self.status, self.card_view_state),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        return body
