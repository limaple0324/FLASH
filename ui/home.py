"""SP3 player home and confirmed feature pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from tkinter import (
    BOTH,
    DISABLED,
    LEFT,
    NORMAL,
    RIGHT,
    X,
    Y,
    Button,
    Entry,
    Frame,
    Label,
    OptionMenu,
    StringVar,
)

from cards.view_state import CardViewState
from core.target_window_observation import TargetWindowObservation
from domain.game_shortcuts import (
    CONFIRMED_GAME_SHORTCUTS,
)
from presentation.target_window_status import target_window_summary
from services.character_detail_choice_service import PlayerCharacterDetailChoice
from services.character_detail_view_service import PlayerCharacterDetail
from services.character_view_service import PlayerCharacterView
from services.group_selection_service import PlayerGroupChoice
from services.activity_schedule_view_service import PlayerActivitySchedule
from workspace.models import WorkspaceState


INPUT_POLICY_LABELS = {
    "foreground_only": "僅允許前台",
    "foreground_background": "允許前台與背景",
    "all": "全部允許（含最小化）",
}

BACKGROUND = "#F3F6FA"
SURFACE = "#FFFFFF"
SIDEBAR = "#17324D"
SIDEBAR_ACTIVE = "#2D6EA8"
SIDEBAR_GROUP = "#203E5B"
SIDEBAR_MUTED = "#B8C9D9"
PRIMARY = "#2474C6"
PRIMARY_HOVER = "#1E64AB"
TEXT = "#182433"
MUTED = "#617083"
BORDER = "#DCE4ED"
SUCCESS = "#26845B"
WARNING = "#B36A18"


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

    groups = sorted(
        {
            str(item.get("group")).strip()
            for item in characters
            if isinstance(item.get("group"), str)
            and str(item.get("group")).strip()
        }
    )
    names = [
        str(item.get("display_name")).strip()
        for item in characters
        if isinstance(item.get("display_name"), str)
        and str(item.get("display_name")).strip()
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
        if card_view_state.is_empty:
            return "目前沒有需要提醒的內容"
        first = card_view_state.cards[0]
        next_step = first.next_step or "尚未提供"
        return (
            f"{first.group_name}｜{first.activity_name}\n"
            f"{first.current_progress}\n下一步：{next_step}"
        )
    if not bool(status.get("self_check_passed", False)):
        return "提醒卡\n自我檢查發現問題"
    target = status.get("target_window", {})
    if isinstance(target, dict) and target.get("configured") is False:
        return "提醒卡\n尚未設定遊戲主視窗"
    return "提醒卡\n系統正常"


def _workspace_state_text(state: WorkspaceState) -> str:
    if not isinstance(state, WorkspaceState):
        raise TypeError("state must be WorkspaceState.")
    group = state.current_group.name if state.current_group is not None else "尚未選擇"
    activity = (
        state.current_activity.name
        if state.current_activity is not None
        else "等待可信遊戲進度"
    )
    next_step = state.next_step or "尚未提供"
    return f"目前組別：{group}\n目前活動：{activity}\n下一步：{next_step}"


def _activity_schedule_text(state: PlayerActivitySchedule | None) -> str:
    if state is None or not state.activities:
        return "今天沒有已登記的固定活動"
    lines = []
    for activity in state.activities:
        eligibility = (
            f"｜{activity.eligibility_text}" if activity.eligibility_text else ""
        )
        lines.append(f"{activity.time_text}　{activity.name}{eligibility}")
    return "\n".join(lines)


def _safe_character_lines(
    characters: Iterable[PlayerCharacterView],
) -> tuple[str, ...]:
    lines: list[str] = []
    for character in characters:
        if not isinstance(character, PlayerCharacterView):
            raise TypeError("characters must contain PlayerCharacterView values.")
        level = str(character.level) if character.level is not None else "等級未設定"
        role = character.role or "定位未設定"
        note = f"｜備註：{character.note}" if character.note else ""
        lines.append(f"{character.display_name}｜{level}｜{role}{note}")
    return tuple(lines)


def _safe_character_detail_line(detail: PlayerCharacterDetail) -> str:
    if not isinstance(detail, PlayerCharacterDetail):
        raise TypeError("detail must be PlayerCharacterDetail.")
    level = str(detail.level) if detail.level is not None else "等級未設定"
    role = detail.role or "定位未設定"
    note = f"｜備註：{detail.note}" if detail.note else ""
    return f"{detail.display_name}｜{level}｜{role}{note}"


class HomeView:
    """Usable SP3 shell that keeps confirmed capabilities in separate pages."""

    def __init__(
        self,
        parent,
        status: dict[str, object],
        on_start=None,
        *,
        input_policy: str = "all",
        on_input_policy_change=None,
        keyboard_sync_enabled: bool = False,
        on_keyboard_sync_change: Callable[[bool], object] | None = None,
        group_choices: Iterable[PlayerGroupChoice] = (),
        current_group_name: str | None = None,
        on_group_change: Callable[[str], object] | None = None,
        workspace_state: WorkspaceState | None = None,
        workspace_state_provider: Callable[[], WorkspaceState] | None = None,
        activity_schedule: PlayerActivitySchedule | None = None,
        activity_schedule_provider: (
            Callable[[], PlayerActivitySchedule] | None
        ) = None,
        card_view_state: CardViewState | None = None,
        card_view_state_provider: Callable[[], CardViewState] | None = None,
        target_window_state: TargetWindowObservation | None = None,
        target_window_state_provider: (
            Callable[[], TargetWindowObservation] | None
        ) = None,
        characters: Iterable[PlayerCharacterView] = (),
        character_choices: Iterable[PlayerCharacterDetailChoice] = (),
        smart_reconnect_enabled: bool = False,
        on_smart_reconnect_change: Callable[[bool], object] | None = None,
        card_display_seconds_provider: Callable[[], int] | None = None,
        on_card_display_seconds_update: Callable[[int], object] | None = None,
        on_refresh_error: Callable[[Exception], object] | None = None,
    ):
        self.parent = parent
        self.status = status
        self.on_start = on_start
        self.input_policy = (
            input_policy if input_policy in INPUT_POLICY_LABELS else "all"
        )
        self.on_input_policy_change = on_input_policy_change
        self.keyboard_sync_enabled = bool(keyboard_sync_enabled)
        self.on_keyboard_sync_change = on_keyboard_sync_change
        self.group_choices = tuple(group_choices)
        if any(
            not isinstance(choice, PlayerGroupChoice)
            for choice in self.group_choices
        ):
            raise TypeError("group_choices must contain PlayerGroupChoice values.")
        self.current_group_name = (
            current_group_name.strip()
            if isinstance(current_group_name, str) and current_group_name.strip()
            else None
        )
        self.on_group_change = on_group_change
        self.workspace_state = workspace_state or WorkspaceState()
        self.workspace_state_provider = workspace_state_provider
        self.activity_schedule = activity_schedule
        self.activity_schedule_provider = activity_schedule_provider
        self.card_view_state = card_view_state
        self.card_view_state_provider = card_view_state_provider
        self.target_window_state = target_window_state
        self.target_window_state_provider = target_window_state_provider
        self.characters = tuple(characters)
        if any(
            not isinstance(character, PlayerCharacterView)
            for character in self.characters
        ):
            raise TypeError("characters must contain PlayerCharacterView values.")
        self.character_choices = tuple(character_choices)
        if any(
            not isinstance(choice, PlayerCharacterDetailChoice)
            for choice in self.character_choices
        ):
            raise TypeError(
                "character_choices must contain PlayerCharacterDetailChoice values."
            )
        self.smart_reconnect_enabled = bool(smart_reconnect_enabled)
        self.on_smart_reconnect_change = on_smart_reconnect_change
        self.card_display_seconds_provider = card_display_seconds_provider
        self.on_card_display_seconds_update = on_card_display_seconds_update
        self.on_refresh_error = on_refresh_error
        self._pages: dict[str, Frame] = {}
        self._navigation_buttons: dict[str, Button] = {}
        self._workspace_label: Label | None = None
        self._activity_schedule_label: Label | None = None
        self._card_label: Label | None = None
        self._target_label: Label | None = None
        self._group_value_label: Label | None = None
        self._group_variable: StringVar | None = None
        self._card_seconds_entry: Entry | None = None
        self._keyboard_sync_label: Label | None = None
        self._keyboard_sync_button: Button | None = None
        self._smart_reconnect_label: Label | None = None
        self._smart_reconnect_button: Button | None = None

    @staticmethod
    def _button(parent, text: str, command=None, *, primary: bool = False):
        background = PRIMARY if primary else SURFACE
        foreground = "#FFFFFF" if primary else TEXT
        active_background = PRIMARY_HOVER if primary else BACKGROUND
        return Button(
            parent,
            text=text,
            command=command,
            font=("Microsoft JhengHei UI", 10),
            bg=background,
            fg=foreground,
            activebackground=active_background,
            activeforeground=foreground,
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
        )

    @staticmethod
    def _card(parent, *, padx: int = 18, pady: int = 16) -> Frame:
        return Frame(
            parent,
            bg=SURFACE,
            padx=padx,
            pady=pady,
            highlightbackground=BORDER,
            highlightthickness=1,
        )

    def build(self):
        root = Frame(self.parent, bg=BACKGROUND)
        root.pack(fill=BOTH, expand=True)

        body = Frame(root, bg=BACKGROUND)
        body.pack(fill=BOTH, expand=True)
        sidebar = Frame(body, bg=SIDEBAR, width=176, padx=12, pady=16)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)
        content = Frame(body, bg=BACKGROUND, padx=22, pady=20)
        content.pack(side=LEFT, fill=BOTH, expand=True)

        self._build_group_summary(sidebar)

        page_specs = (
            ("home", "首頁"),
            ("groups", "組別與視窗"),
            ("sync", "同步與重連"),
            ("characters", "角色資料"),
            ("settings", "設定"),
        )
        for key, label in page_specs:
            button = Button(
                sidebar,
                text=label,
                command=lambda selected=key: self.show_page(selected),
                anchor="w",
                font=("Microsoft JhengHei UI", 11),
                bg=SIDEBAR,
                fg="#EAF2F8",
                activebackground=SIDEBAR_ACTIVE,
                activeforeground="#FFFFFF",
                relief="flat",
                bd=0,
                padx=14,
                pady=10,
                cursor="hand2",
            )
            button.pack(fill=X, pady=2)
            self._navigation_buttons[key] = button

        self._pages["home"] = self._build_home_page(content)
        self._pages["groups"] = self._build_groups_page(content)
        self._pages["sync"] = self._build_sync_page(content)
        self._pages["characters"] = self._build_characters_page(content)
        self._pages["settings"] = self._build_settings_page(content)
        self.show_page("home")
        return root

    def _build_group_summary(self, parent) -> None:
        group_card = Frame(
            parent,
            bg=SIDEBAR_GROUP,
            highlightbackground="#2A4B69",
            highlightthickness=1,
        )
        group_card.pack(fill=X, pady=(0, 16))
        accent = Frame(group_card, bg=PRIMARY, width=4)
        accent.pack(side=LEFT, fill=Y)
        accent.pack_propagate(False)
        details = Frame(group_card, bg=SIDEBAR_GROUP, padx=12, pady=10)
        details.pack(side=LEFT, fill=X, expand=True)
        Label(
            details,
            text="目前組別",
            font=("Microsoft JhengHei UI", 9),
            bg=SIDEBAR_GROUP,
            fg=SIDEBAR_MUTED,
            anchor="w",
        ).pack(fill=X)

        current_group = self.current_group_name or "尚未選擇組別"
        self._group_value_label = Label(
            details,
            text=current_group,
            font=("Microsoft JhengHei UI", 14, "bold"),
            bg=SIDEBAR_GROUP,
            fg="#FFFFFF",
            anchor="w",
            justify=LEFT,
            wraplength=118,
        )
        self._group_value_label.pack(fill=X, pady=(3, 0))

    def _page_heading(self, parent, title: str, subtitle: str) -> None:
        Label(
            parent,
            text=title,
            font=("Microsoft JhengHei UI", 20, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            parent,
            text=subtitle,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(2, 16))

    def _build_home_page(self, parent) -> Frame:
        page = Frame(parent, bg=BACKGROUND)
        self._page_heading(page, "今天要做什麼", "只顯示現在需要注意的內容")

        summary_row = Frame(page, bg=BACKGROUND)
        summary_row.pack(fill=X)
        workspace_card = self._card(summary_row)
        workspace_card.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 8))
        Label(
            workspace_card,
            text="目前工作區",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X)
        self._workspace_label = Label(
            workspace_card,
            text=_workspace_state_text(self.workspace_state),
            justify=LEFT,
            font=("Microsoft JhengHei UI", 11),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        )
        self._workspace_label.pack(fill=X, pady=(8, 0))

        target_card = self._card(summary_row)
        target_card.pack(side=LEFT, fill=BOTH, expand=True, padx=(8, 0))
        Label(
            target_card,
            text="遊戲視窗",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X)
        target_text = (
            target_window_summary(self.target_window_state)
            if self.target_window_state is not None
            else "尚未完成視窗檢查"
        )
        self._target_label = Label(
            target_card,
            text=f"● {target_text}",
            font=("Microsoft JhengHei UI", 11),
            bg=SURFACE,
            fg=SUCCESS if self.target_window_state and self.target_window_state.safe else WARNING,
            anchor="w",
        )
        self._target_label.pack(fill=X, pady=(8, 0))

        Label(
            page,
            text="今日已登記活動",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X, pady=(20, 8))
        schedule_card = self._card(page, pady=12)
        schedule_card.pack(fill=X)
        self._activity_schedule_label = Label(
            schedule_card,
            text=_activity_schedule_text(self.activity_schedule),
            justify=LEFT,
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        )
        self._activity_schedule_label.pack(fill=X)

        Label(
            page,
            text="需要注意",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X, pady=(20, 8))
        reminder = self._card(page)
        reminder.pack(fill=X)
        self._card_label = Label(
            reminder,
            text=_card_text(self.status, self.card_view_state),
            justify=LEFT,
            font=("Microsoft JhengHei UI", 11),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        )
        self._card_label.pack(side=LEFT, fill=X, expand=True)
        self._button(
            reminder,
            "重新查看",
            self._refresh_from_player_action,
            primary=True,
        ).pack(side=RIGHT)

        Label(
            page,
            text="沒有可信新資訊時保持安靜，不猜測活動或完成進度。",
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(12, 0))
        return page

    def _build_groups_page(self, parent) -> Frame:
        page = Frame(parent, bg=BACKGROUND)
        self._page_heading(
            page,
            "組別與遊戲視窗",
            "沿用現有組別名稱；不讀取或顯示登入參數",
        )
        selector = self._card(page)
        selector.pack(fill=X)
        Label(
            selector,
            text="目前組別",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X)

        names = tuple(choice.name for choice in self.group_choices)
        initial = (
            self.current_group_name
            if self.current_group_name in names
            else (names[0] if names else "尚未建立組別")
        )
        self._group_variable = StringVar(master=self.parent, value=initial)
        menu = OptionMenu(
            selector,
            self._group_variable,
            *(names or ("尚未建立組別",)),
            command=self._select_group,
        )
        menu.configure(
            font=("Microsoft JhengHei UI", 11),
            bg=BACKGROUND,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        menu["menu"].configure(font=("Microsoft JhengHei UI", 10))
        menu.pack(fill=X, pady=(8, 0))
        if not names:
            menu.configure(state=DISABLED)

        Label(
            page,
            text="可用組別",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X, pady=(20, 8))
        list_card = self._card(page, padx=8, pady=8)
        list_card.pack(fill=BOTH, expand=True)
        list_card.grid_columnconfigure(0, weight=1)
        list_card.grid_columnconfigure(1, weight=1)
        if not self.group_choices:
            Label(
                list_card,
                text="目前沒有可用組別。",
                font=("Microsoft JhengHei UI", 10),
                bg=SURFACE,
                fg=MUTED,
                padx=18,
                pady=14,
            ).grid(row=0, column=0, columnspan=2, sticky="ew")
        for index, choice in enumerate(self.group_choices):
            row = Frame(
                list_card,
                bg=SURFACE,
                padx=10,
                pady=7,
                highlightbackground=BORDER,
                highlightthickness=1,
            )
            row.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=4,
                pady=4,
            )
            Label(
                row,
                text=choice.name,
                font=("Microsoft JhengHei UI", 11, "bold"),
                bg=SURFACE,
                fg=TEXT,
                anchor="w",
            ).pack(side=LEFT, fill=X, expand=True)
            Label(
                row,
                text=f"{choice.character_count} 個視窗設定",
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=MUTED,
            ).pack(side=LEFT, padx=8)
            self._button(
                row,
                "選擇",
                lambda name=choice.name: self._select_group(name),
            ).pack(side=RIGHT)
        return page

    def _build_sync_page(self, parent) -> Frame:
        page = Frame(parent, bg=BACKGROUND)
        self._page_heading(
            page,
            "同步與重新連線",
            "常用操作集中在這裡，執行前仍會重新驗證所有視窗",
        )

        input_card = self._card(page)
        input_card.pack(fill=X)
        Label(
            input_card,
            text="同步輸入",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            input_card,
            text="允許範圍",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(12, 4))

        label_to_policy = {
            label: policy for policy, label in INPUT_POLICY_LABELS.items()
        }
        policy_variable = StringVar(
            master=self.parent,
            value=INPUT_POLICY_LABELS[self.input_policy],
        )
        self._input_policy_variable = policy_variable

        def policy_changed(label: str) -> None:
            policy = label_to_policy.get(label)
            if policy is not None and self.on_input_policy_change is not None:
                self.on_input_policy_change(policy)

        policy_menu = OptionMenu(
            input_card,
            policy_variable,
            *INPUT_POLICY_LABELS.values(),
            command=policy_changed,
        )
        policy_menu.configure(
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        policy_menu.pack(fill=X)
        shortcut_labels = tuple(
            shortcut.player_label for shortcut in CONFIRMED_GAME_SHORTCUTS
        )
        shortcut_variable = StringVar(
            master=self.parent,
            value=shortcut_labels[0],
        )
        self._shortcut_variable = shortcut_variable
        shortcut_menu = OptionMenu(
            input_card,
            shortcut_variable,
            *shortcut_labels,
        )
        shortcut_menu.configure(
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            anchor="w",
        )
        shortcut_menu["menu"].configure(
            font=("Microsoft JhengHei UI", 10),
        )
        shortcut_menu.pack(fill=X, pady=(12, 0))

        actions = Frame(input_card, bg=SURFACE)
        actions.pack(fill=X, pady=(10, 0))
        self._keyboard_sync_button = self._button(
            actions,
            "",
            self._toggle_keyboard_sync,
            primary=True,
        )
        self._keyboard_sync_button.pack(side=LEFT)
        self._keyboard_sync_label = Label(
            actions,
            text="",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        )
        self._keyboard_sync_label.pack(side=LEFT, padx=12)
        Label(
            actions,
            text=(
                f"已確認 {len(CONFIRMED_GAME_SHORTCUTS)} 組快捷鍵；"
                "請從上方清單查看"
            ),
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=RIGHT)
        self._refresh_keyboard_sync_controls()

        reconnect_card = self._card(page)
        reconnect_card.pack(fill=X, pady=(14, 0))
        Label(
            reconnect_card,
            text="斷線重新連線",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        self._smart_reconnect_label = Label(
            reconnect_card,
            text="",
            font=("Microsoft JhengHei UI", 11),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        )
        self._smart_reconnect_label.pack(fill=X, pady=(10, 0))
        self._smart_reconnect_button = self._button(
            reconnect_card,
            "",
            self._toggle_smart_reconnect,
            primary=True,
        )
        self._smart_reconnect_button.pack(anchor="w", pady=(10, 0))
        Label(
            reconnect_card,
            text="啟用後會自動判定與重試；不需逐次按重連按鈕。",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(8, 0))
        self._refresh_smart_reconnect_controls()
        return page

    def _toggle_keyboard_sync(self) -> None:
        desired = not self.keyboard_sync_enabled
        if self.on_keyboard_sync_change is None:
            return
        accepted = self.on_keyboard_sync_change(desired)
        if accepted is False:
            return
        self.keyboard_sync_enabled = desired
        self._refresh_keyboard_sync_controls()

    def _refresh_keyboard_sync_controls(self) -> None:
        if self._keyboard_sync_button is not None:
            self._keyboard_sync_button.configure(
                text=(
                    "停止鍵盤同步"
                    if self.keyboard_sync_enabled
                    else "開始鍵盤同步"
                ),
                bg=(
                    WARNING if self.keyboard_sync_enabled else PRIMARY
                ),
            )
        if self._keyboard_sync_label is not None:
            self._keyboard_sync_label.configure(
                text=(
                    "● 已啟用｜在目前遊戲主視窗按鍵時同步"
                    if self.keyboard_sync_enabled
                    else "● 尚未啟用"
                ),
                fg=SUCCESS if self.keyboard_sync_enabled else MUTED,
            )

    def set_keyboard_sync_enabled(self, enabled: bool) -> None:
        self.keyboard_sync_enabled = bool(enabled)
        self._refresh_keyboard_sync_controls()

    def _toggle_smart_reconnect(self) -> None:
        desired = not self.smart_reconnect_enabled
        if self.on_smart_reconnect_change is None:
            return
        accepted = self.on_smart_reconnect_change(desired)
        if accepted is False:
            return
        self.smart_reconnect_enabled = desired
        self._refresh_smart_reconnect_controls()

    def _refresh_smart_reconnect_controls(self) -> None:
        if self._smart_reconnect_button is not None:
            self._smart_reconnect_button.configure(
                text=(
                    "停止智慧重連"
                    if self.smart_reconnect_enabled
                    else "啟用智慧重連"
                ),
                bg=(
                    WARNING if self.smart_reconnect_enabled else PRIMARY
                ),
            )
        if self._smart_reconnect_label is not None:
            self._smart_reconnect_label.configure(
                text=(
                    "● 自動監看中｜失敗每 60 秒重試"
                    if self.smart_reconnect_enabled
                    else "● 安全停止｜不會點擊遊戲視窗"
                ),
                fg=SUCCESS if self.smart_reconnect_enabled else MUTED,
            )

    def _build_characters_page(self, parent) -> Frame:
        page = Frame(parent, bg=BACKGROUND)
        self._page_heading(
            page,
            "角色資料",
            "顯示角色、等級、定位與備註，不顯示內部識別資訊",
        )
        card = self._card(page, padx=0, pady=4)
        card.pack(fill=X)
        rows: tuple[tuple[str, Callable[[], None] | None], ...]
        if self.character_choices:
            rows = tuple(
                (_safe_character_detail_line(choice.detail), choice.select)
                for choice in self.character_choices
            )
        else:
            rows = tuple((line, None) for line in _safe_character_lines(self.characters))
        if not rows:
            rows = (("目前沒有可顯示的角色資料。", None),)
        for line, select in rows:
            row = Frame(card, bg=SURFACE, padx=18, pady=6)
            row.pack(fill=X)
            Label(
                row,
                text=line,
                font=("Microsoft JhengHei UI", 10),
                bg=SURFACE,
                fg=TEXT if self.characters or self.character_choices else MUTED,
                anchor="w",
            ).pack(side=LEFT, fill=X, expand=True)
            if select is not None:
                self._button(row, "查看", select).pack(side=RIGHT)
        return page

    def _build_settings_page(self, parent) -> Frame:
        page = Frame(parent, bg=BACKGROUND)
        self._page_heading(
            page,
            "設定",
            "只保留玩家需要調整與查看的內容",
        )
        card = self._card(page)
        card.pack(fill=X)
        Label(
            card,
            text="提醒顯示時間",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            card,
            text="設定提醒卡在畫面上保留的秒數。",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(6, 12))
        settings_row = Frame(card, bg=SURFACE)
        settings_row.pack(fill=X)
        initial_seconds = (
            self.card_display_seconds_provider()
            if self.card_display_seconds_provider is not None
            else 30
        )
        self._card_seconds_entry = Entry(
            settings_row,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            width=10,
        )
        self._card_seconds_entry.insert(0, str(initial_seconds))
        self._card_seconds_entry.pack(side=LEFT, ipady=8)
        Label(
            settings_row,
            text="秒",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            padx=8,
        ).pack(side=LEFT)
        self._button(
            settings_row,
            "儲存",
            self._save_card_display_seconds,
            primary=True,
        ).pack(side=LEFT, padx=(8, 0))

        status_card = self._card(page)
        status_card.pack(fill=X, pady=(14, 0))
        Label(
            status_card,
            text="系統狀態",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            status_card,
            text="查看目前視窗、安全檢查與資料保存狀態。",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(6, 12))
        self._button(
            status_card,
            "查看目前狀態",
            self._refresh_from_player_action,
        ).pack(anchor="w")
        return page

    def _save_card_display_seconds(self) -> None:
        if (
            self._card_seconds_entry is None
            or self.on_card_display_seconds_update is None
        ):
            return
        raw = self._card_seconds_entry.get().strip()
        try:
            seconds = int(raw)
            if seconds < 1:
                raise ValueError("seconds must be positive")
            self.on_card_display_seconds_update(seconds)
            confirmed = (
                self.card_display_seconds_provider()
                if self.card_display_seconds_provider is not None
                else seconds
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._card_seconds_entry.delete(0, "end")
        self._card_seconds_entry.insert(0, str(confirmed))

    def show_page(self, name: str) -> None:
        if name not in self._pages:
            raise KeyError(f"Unknown home page: {name}")
        for page_name, page in self._pages.items():
            if page_name == name:
                page.pack(fill=BOTH, expand=True)
            else:
                page.pack_forget()
        for button_name, button in self._navigation_buttons.items():
            button.configure(
                bg=SIDEBAR_ACTIVE if button_name == name else SIDEBAR,
                fg="#FFFFFF" if button_name == name else "#EAF2F8",
            )

    def _select_group(self, name: str) -> None:
        if name not in {choice.name for choice in self.group_choices}:
            return
        if self.on_group_change is not None:
            self.on_group_change(name)
        self.current_group_name = name
        if self._group_variable is not None:
            self._group_variable.set(name)
        if self._group_value_label is not None:
            self._group_value_label.configure(text=name)
        self.refresh_workspace()

    def _refresh_from_player_action(self) -> None:
        if self.on_start is not None:
            self.on_start()
        self.refresh_workspace()
        self.refresh_activity_schedule()
        self.refresh_cards()
        self.refresh_target_window()

    def refresh_workspace(self) -> str:
        previous = self.workspace_state
        try:
            state = (
                self.workspace_state_provider()
                if self.workspace_state_provider is not None
                else previous
            )
            text = _workspace_state_text(state)
        except Exception as error:
            self._report_refresh_error(error)
            return _workspace_state_text(previous)
        self.workspace_state = state
        if self._workspace_label is not None:
            self._workspace_label.configure(text=text)
        return text

    def refresh_activity_schedule(self) -> str:
        previous = self.activity_schedule
        try:
            state = (
                self.activity_schedule_provider()
                if self.activity_schedule_provider is not None
                else previous
            )
            if state is not None and not isinstance(state, PlayerActivitySchedule):
                raise TypeError(
                    "activity schedule provider must return PlayerActivitySchedule."
                )
            text = _activity_schedule_text(state)
        except Exception as error:
            self._report_refresh_error(error)
            return _activity_schedule_text(previous)
        self.activity_schedule = state
        if self._activity_schedule_label is not None:
            self._activity_schedule_label.configure(text=text)
        return text

    def refresh_cards(self) -> str:
        previous = self.card_view_state
        try:
            state = (
                self.card_view_state_provider()
                if self.card_view_state_provider is not None
                else previous
            )
            if state is not None and not isinstance(state, CardViewState):
                raise TypeError("card provider must return CardViewState.")
            text = _card_text(self.status, state)
        except Exception as error:
            self._report_refresh_error(error)
            return _card_text(self.status, previous)
        self.card_view_state = state
        if self._card_label is not None:
            self._card_label.configure(text=text)
        return text

    def refresh_target_window(self) -> str:
        previous = self.target_window_state
        try:
            state = (
                self.target_window_state_provider()
                if self.target_window_state_provider is not None
                else previous
            )
            if state is not None and not isinstance(
                state,
                TargetWindowObservation,
            ):
                raise TypeError(
                    "target window provider must return TargetWindowObservation."
                )
            text = (
                target_window_summary(state)
                if state is not None
                else "尚未完成視窗檢查"
            )
        except Exception as error:
            self._report_refresh_error(error)
            return (
                target_window_summary(previous)
                if previous is not None
                else "尚未完成視窗檢查"
            )
        self.target_window_state = state
        if self._target_label is not None:
            self._target_label.configure(
                text=f"● {text}",
                fg=SUCCESS if state is not None and state.safe else WARNING,
            )
        return text

    def _report_refresh_error(self, error: Exception) -> None:
        if self.on_refresh_error is None:
            raise error
        self.on_refresh_error(error)
