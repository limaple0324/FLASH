"""SP1 Home UI foundation.

Player-facing presentation layer for the FLASH desktop entrypoint.
Keeps engineering diagnostics separated from the player home experience.
"""

from __future__ import annotations

from collections.abc import Callable
from tkinter import BOTH, X, Button, Frame, Label

from cards.view_state import CardViewState
from services.card_preview_selection_service import CardPreviewChoice


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
        card_view_state_provider: Callable[[], CardViewState] | None = None,
        card_preview_choices_provider: Callable[[], tuple[CardPreviewChoice, ...]] | None = None,
        on_card_preview_select: Callable[[str], object] | None = None,
    ):
        self.parent = parent
        self.status = status
        self.on_start = on_start
        self.card_view_state = card_view_state
        self.card_view_state_provider = card_view_state_provider
        self.card_preview_choices_provider = card_preview_choices_provider
        self.on_card_preview_select = on_card_preview_select
        self._card_label = None
        self._card_preview_buttons: dict[str, Button] = {}

    def refresh_cards(self) -> str:
        """重新讀取唯讀快照，並更新既有的首頁提醒文字。"""
        if self.card_view_state_provider is not None:
            self.card_view_state = self.card_view_state_provider()
        text = _card_text(self.status, self.card_view_state)
        if self._card_label is not None:
            self._card_label.configure(text=text)
        return text

    def refresh_card_preview_choices(self) -> tuple[CardPreviewChoice, ...]:
        """Refresh the read-only candidate labels after an explicit selection."""
        if self.card_preview_choices_provider is None:
            return ()
        choices = self.card_preview_choices_provider()
        for choice in choices:
            button = self._card_preview_buttons.get(choice.profile_id)
            if button is not None:
                marker = "✓ " if choice.selected else ""
                button.configure(text=f"{marker}{choice.display_name}")
        return choices

    def select_card_preview(self, profile_id: str) -> None:
        if self.on_card_preview_select is None:
            return
        self.on_card_preview_select(profile_id)
        self.refresh_card_preview_choices()

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

        self._card_label = Label(
            body,
            text=self.refresh_cards(),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        )
        self._card_label.pack(fill=X, pady=12)

        choices = self.refresh_card_preview_choices()
        if choices and self.on_card_preview_select is not None:
            Label(
                body,
                text="提醒卡樣式",
                font=("Microsoft JhengHei UI", 11),
                anchor="w",
            ).pack(fill=X, pady=(12, 4))
            for choice in choices:
                marker = "✓ " if choice.selected else ""
                button = Button(
                    body,
                    text=f"{marker}{choice.display_name}",
                    command=lambda profile_id=choice.profile_id: self.select_card_preview(
                        profile_id
                    ),
                )
                button.pack(fill=X, pady=2)
                self._card_preview_buttons[choice.profile_id] = button

        return body
