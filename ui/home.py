"""SP1 Home UI foundation.

Player-facing presentation layer for the FLASH desktop entrypoint.
Keeps engineering diagnostics separated from the player home experience.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from tkinter import BOTH, X, Button, Entry, Frame, Label

from cards.view_state import CardViewState
from services.card_preview_selection_service import CardPreviewChoice
from services.character_detail_view_service import PlayerCharacterDetail
from services.character_view_service import PlayerCharacterView


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


def format_group_characters(status: dict[str, object]) -> str:
    """顯示所有已登記角色，但不外露視窗與識別資訊。"""
    characters = _characters(status)
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in characters:
        name = item.get("display_name")
        if not isinstance(name, str) or not name.strip():
            continue
        group = item.get("group")
        group_name = (
            group.strip()
            if isinstance(group, str) and group.strip()
            else "未分組"
        )
        grouped.setdefault(group_name, []).append(item)

    if not grouped:
        return "目前沒有可顯示的組別與角色資料。"

    lines: list[str] = []
    for group_name in sorted(grouped, key=lambda value: (value == "未分組", value)):
        lines.append(f"【{group_name}】")
        for item in grouped[group_name]:
            name = str(item["display_name"]).strip()
            lines.append(f"• {name}")
            role = item.get("role")
            if isinstance(role, str) and role.strip():
                lines.append(f"  定位：{role.strip()}")
            note = item.get("note")
            if isinstance(note, str) and note.strip():
                lines.append(f"  備註：{note.strip()}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_player_characters(
    characters: Iterable[PlayerCharacterView],
) -> str:
    """顯示已合併的角色資料，不外露固定識別或視窗技術資訊。"""
    grouped: dict[str, list[PlayerCharacterView]] = {}
    for item in characters:
        group_name = item.group.strip() if item.group and item.group.strip() else "未分組"
        grouped.setdefault(group_name, []).append(item)

    if not grouped:
        return "目前沒有可顯示的組別與角色資料。"

    lines: list[str] = []
    for group_name in sorted(grouped, key=lambda value: (value == "未分組", value)):
        lines.append(f"【{group_name}】")
        for item in grouped[group_name]:
            lines.append(f"• {item.display_name}")
            if item.level is not None:
                lines.append(f"  等級：{item.level}")
            if item.importance:
                lines.append(f"  分類：{item.importance}")
            if item.role:
                lines.append(f"  定位：{item.role}")
            if item.note:
                lines.append(f"  備註：{item.note}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_player_character_detail(detail: PlayerCharacterDetail) -> str:
    """將單一角色詳細快照轉成簡潔中文，不補造缺少的資料。"""
    if not isinstance(detail, PlayerCharacterDetail):
        raise TypeError("detail must be PlayerCharacterDetail.")

    def value_or_unset(value: str | None) -> str:
        return value.strip() if value and value.strip() else "尚未設定"

    level = (
        str(detail.level)
        if isinstance(detail.level, int) and not isinstance(detail.level, bool)
        else "尚未設定"
    )
    return "\n".join(
        (
            f"【{detail.display_name}】",
            f"組別：{value_or_unset(detail.group)}",
            f"等級：{level}",
            f"分類：{value_or_unset(detail.importance)}",
            f"定位：{value_or_unset(detail.role)}",
            f"備註：{value_or_unset(detail.note)}",
            f"靈魂石：{value_or_unset(detail.soul_stone)}",
        )
    )


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
        on_card_preview_clear: Callable[[], object] | None = None,
        on_card_preview_error: Callable[[str, Exception], object] | None = None,
        card_display_seconds_provider: Callable[[], int] | None = None,
        on_card_display_seconds_update: Callable[[int], object] | None = None,
        on_card_display_seconds_error: Callable[[Exception], object] | None = None,
        on_show_group_characters: Callable[[], object] | None = None,
    ):
        self.parent = parent
        self.status = status
        self.on_start = on_start
        self.card_view_state = card_view_state
        self.card_view_state_provider = card_view_state_provider
        self.card_preview_choices_provider = card_preview_choices_provider
        self.on_card_preview_select = on_card_preview_select
        self.on_card_preview_clear = on_card_preview_clear
        self.on_card_preview_error = on_card_preview_error
        self.card_display_seconds_provider = card_display_seconds_provider
        self.on_card_display_seconds_update = on_card_display_seconds_update
        self.on_card_display_seconds_error = on_card_display_seconds_error
        self.on_show_group_characters = on_show_group_characters
        self._card_label = None
        self._card_preview_buttons: dict[str, Button] = {}
        self._card_preview_clear_button: Button | None = None
        self._card_preview_clear_visible = False
        self._card_display_seconds_entry: Entry | None = None

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
        self._set_card_preview_clear_visible(any(choice.selected for choice in choices))
        return choices

    def select_card_preview(self, profile_id: str) -> None:
        if self.on_card_preview_select is None:
            return
        try:
            self.on_card_preview_select(profile_id)
        except Exception as exc:
            self._report_card_preview_error("select", exc)
            return
        self.refresh_card_preview_choices()

    def clear_card_preview(self) -> None:
        if self.on_card_preview_clear is None:
            return
        try:
            self.on_card_preview_clear()
        except Exception as exc:
            self._report_card_preview_error("clear", exc)
            return
        self.refresh_card_preview_choices()

    def _report_card_preview_error(self, action: str, error: Exception) -> None:
        if self.on_card_preview_error is None:
            raise error
        self.on_card_preview_error(action, error)

    def _set_card_preview_clear_visible(self, visible: bool) -> None:
        button = self._card_preview_clear_button
        if button is None or visible == self._card_preview_clear_visible:
            return
        if visible:
            button.pack(fill=X, pady=(8, 2))
        else:
            button.pack_forget()
        self._card_preview_clear_visible = visible

    def refresh_card_display_seconds(self) -> int | None:
        if self.card_display_seconds_provider is None:
            return None
        seconds = self.card_display_seconds_provider()
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
            raise ValueError("card display seconds must be a positive integer.")
        if self._card_display_seconds_entry is not None:
            self._card_display_seconds_entry.delete(0, "end")
            self._card_display_seconds_entry.insert(0, str(seconds))
        return seconds

    def update_card_display_seconds(self) -> None:
        entry = self._card_display_seconds_entry
        if entry is None or self.on_card_display_seconds_update is None:
            return
        try:
            seconds = int(entry.get().strip())
            if seconds < 1:
                raise ValueError("card display seconds must be positive.")
            self.on_card_display_seconds_update(seconds)
        except Exception as exc:
            self._report_card_display_seconds_error(exc)
        finally:
            self.refresh_card_display_seconds()

    def _report_card_display_seconds_error(self, error: Exception) -> None:
        if self.on_card_display_seconds_error is None:
            raise error
        self.on_card_display_seconds_error(error)

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

        if self.on_show_group_characters is not None:
            Button(
                body,
                text="查看組別角色",
                width=18,
                command=self.on_show_group_characters,
            ).pack(pady=(0, 8))

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
            if self.on_card_preview_clear is not None:
                self._card_preview_clear_button = Button(
                    body,
                    text="關閉提醒卡預覽",
                    command=self.clear_card_preview,
                )
                self._set_card_preview_clear_visible(
                    any(choice.selected for choice in choices)
                )

        if (
            self.card_display_seconds_provider is not None
            and self.on_card_display_seconds_update is not None
        ):
            Label(
                body,
                text="提醒卡顯示時間（秒）",
                font=("Microsoft JhengHei UI", 11),
                anchor="w",
            ).pack(fill=X, pady=(12, 4))
            self._card_display_seconds_entry = Entry(body)
            self._card_display_seconds_entry.pack(fill=X, pady=2)
            self.refresh_card_display_seconds()
            Button(
                body,
                text="儲存顯示時間",
                command=self.update_card_display_seconds,
            ).pack(fill=X, pady=(2, 8))

        return body
