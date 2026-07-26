"""SP3 player home and confirmed feature pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from tkinter import (
    BOTH,
    DISABLED,
    LEFT,
    NORMAL,
    RIGHT,
    X,
    Y,
    Button,
    Canvas,
    Checkbutton,
    Entry,
    Frame,
    Label,
    OptionMenu,
    Scrollbar,
    IntVar,
    StringVar,
)
from tkinter import messagebox

from PIL import Image, ImageTk

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
from services.group_configuration_service import (
    GroupConfigurationEntry,
    GroupSyncMemberChoice,
)
from services.group_role_status_service import GroupRoleStatus
from services.background_image_service import BackgroundImageResult
from services.sync_operation_record_store import (
    OperationRecordSearchResult,
)
from services.activity_schedule_view_service import PlayerActivitySchedule
from workspace.models import WorkspaceState


INPUT_POLICY_LABELS = {
    "foreground_only": "僅允許前台",
    "foreground_background": "允許前台與背景",
    "all": "全部允許（含最小化）",
}

UI_THEME_LABELS = {
    "clear_blue": "俐落藍",
    "soft_violet": "柔和紫",
    "classic_gold": "舊版金色",
    "minimal_mono": "極簡黑白",
}

UI_THEME_PALETTES = {
    "clear_blue": {
        "background": "#F3F6FA",
        "surface": "#FFFFFF",
        "sidebar": "#17324D",
        "sidebar_active": "#2D6EA8",
        "sidebar_group": "#203E5B",
        "sidebar_muted": "#B8C9D9",
        "primary": "#2474C6",
        "primary_hover": "#1E64AB",
        "text": "#182433",
        "muted": "#617083",
        "border": "#DCE4ED",
        "success": "#26845B",
        "warning": "#B36A18",
    },
    "soft_violet": {
        "background": "#F6F1F7",
        "surface": "#FFFBFF",
        "sidebar": "#51445E",
        "sidebar_active": "#826795",
        "sidebar_group": "#655472",
        "sidebar_muted": "#D7CADD",
        "primary": "#79588F",
        "primary_hover": "#674877",
        "text": "#2D2532",
        "muted": "#74677A",
        "border": "#E5DCE8",
        "success": "#3A8066",
        "warning": "#A8662B",
    },
    "classic_gold": {
        "background": "#E3C47F",
        "surface": "#F4E5B8",
        "sidebar": "#745323",
        "sidebar_active": "#A3742E",
        "sidebar_group": "#89642B",
        "sidebar_muted": "#F0DFB0",
        "primary": "#916522",
        "primary_hover": "#765019",
        "text": "#3B2A13",
        "muted": "#725A35",
        "border": "#B78D47",
        "success": "#39704B",
        "warning": "#9B4B1E",
    },
    "minimal_mono": {
        "background": "#F4F4F4",
        "surface": "#FFFFFF",
        "sidebar": "#222222",
        "sidebar_active": "#454545",
        "sidebar_group": "#303030",
        "sidebar_muted": "#C8C8C8",
        "primary": "#202020",
        "primary_hover": "#3A3A3A",
        "text": "#1C1C1C",
        "muted": "#666666",
        "border": "#D7D7D7",
        "success": "#26714C",
        "warning": "#9A551E",
    },
}


def theme_palette(name: object) -> dict[str, str]:
    key = name if isinstance(name, str) else ""
    selected = UI_THEME_PALETTES.get(key, UI_THEME_PALETTES["clear_blue"])
    return dict(selected)


def _apply_theme_palette(name: object) -> str:
    key = (
        name
        if isinstance(name, str) and name in UI_THEME_PALETTES
        else "clear_blue"
    )
    palette = UI_THEME_PALETTES[key]
    global BACKGROUND, SURFACE, SIDEBAR, SIDEBAR_ACTIVE
    global SIDEBAR_GROUP, SIDEBAR_MUTED, PRIMARY, PRIMARY_HOVER
    global TEXT, MUTED, BORDER, SUCCESS, WARNING
    BACKGROUND = palette["background"]
    SURFACE = palette["surface"]
    SIDEBAR = palette["sidebar"]
    SIDEBAR_ACTIVE = palette["sidebar_active"]
    SIDEBAR_GROUP = palette["sidebar_group"]
    SIDEBAR_MUTED = palette["sidebar_muted"]
    PRIMARY = palette["primary"]
    PRIMARY_HOVER = palette["primary_hover"]
    TEXT = palette["text"]
    MUTED = palette["muted"]
    BORDER = palette["border"]
    SUCCESS = palette["success"]
    WARNING = palette["warning"]
    return key


_apply_theme_palette("clear_blue")


def _cover_geometry(
    source_size: tuple[int, int],
    viewport_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Return resized dimensions and centered crop origin for cover layout."""
    source_width, source_height = source_size
    viewport_width, viewport_height = viewport_size
    if min(source_width, source_height, viewport_width, viewport_height) < 1:
        raise ValueError("image and viewport dimensions must be positive")
    scale = max(
        viewport_width / source_width,
        viewport_height / source_height,
    )
    resized_width = max(viewport_width, round(source_width * scale))
    resized_height = max(viewport_height, round(source_height * scale))
    crop_x = max(0, (resized_width - viewport_width) // 2)
    crop_y = max(0, (resized_height - viewport_height) // 2)
    return resized_width, resized_height, crop_x, crop_y


@dataclass(frozen=True, slots=True)
class GroupManagementViewResult:
    success: bool
    current_group_name: str | None
    message: str = ""


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
        if first.name_only:
            return first.activity_name
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
        group_choices_provider: (
            Callable[[], tuple[PlayerGroupChoice, ...]] | None
        ) = None,
        current_group_name: str | None = None,
        on_group_change: Callable[[str], object] | None = None,
        on_launch_group: Callable[[str], object] | None = None,
        on_restore_group: Callable[[str], object] | None = None,
        on_record_group_positions: Callable[[str], object] | None = None,
        on_create_group: (
            Callable[[str], GroupManagementViewResult] | None
        ) = None,
        on_rename_group: (
            Callable[[str, str], GroupManagementViewResult] | None
        ) = None,
        on_delete_group: (
            Callable[[str], GroupManagementViewResult] | None
        ) = None,
        on_move_group: (
            Callable[[str, int], GroupManagementViewResult] | None
        ) = None,
        group_entries_provider: (
            Callable[[str], tuple[GroupConfigurationEntry, ...]] | None
        ) = None,
        on_add_group_shortcuts: Callable[[str], object] | None = None,
        on_remove_group_shortcut: (
            Callable[[str, str], object] | None
        ) = None,
        on_set_group_main: (
            Callable[[str, str], object] | None
        ) = None,
        on_clear_group: (
            Callable[[str], GroupManagementViewResult] | None
        ) = None,
        group_sync_choices_provider: (
            Callable[[str], tuple[GroupSyncMemberChoice, ...]] | None
        ) = None,
        group_sync_relations_provider: (
            Callable[[str], tuple[GroupSyncMemberChoice, ...]] | None
        ) = None,
        on_add_group_sync_relation: (
            Callable[[str, str], object] | None
        ) = None,
        on_remove_group_sync_relation: (
            Callable[[str, str], object] | None
        ) = None,
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
        reconnect_failure_messages_provider: (
            Callable[[], tuple[str, ...]] | None
        ) = None,
        group_role_status_provider: (
            Callable[[], tuple[GroupRoleStatus, ...]] | None
        ) = None,
        on_group_role_action: Callable[[str], object] | None = None,
        operation_record_lines_provider: (
            Callable[[], tuple[str, ...]] | None
        ) = None,
        operation_record_files_provider: (
            Callable[[], tuple[Path, ...]] | None
        ) = None,
        on_open_operation_record_file: (
            Callable[[Path], object] | None
        ) = None,
        operation_record_search: (
            Callable[[str, str], tuple[OperationRecordSearchResult, ...]]
            | None
        ) = None,
        auto_click_running: bool = False,
        on_auto_click_change: (
            Callable[[bool, int, str, bool, int], object] | None
        ) = None,
        card_display_seconds_provider: Callable[[], int] | None = None,
        on_card_display_seconds_update: Callable[[int], object] | None = None,
        theme_name: str = "clear_blue",
        on_theme_change: Callable[[str], object] | None = None,
        background_image_path: Path | None = None,
        on_select_background_image: (
            Callable[[], BackgroundImageResult | None] | None
        ) = None,
        on_clear_background_image: (
            Callable[[], BackgroundImageResult] | None
        ) = None,
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
        self.group_choices_provider = group_choices_provider
        self.current_group_name = (
            current_group_name.strip()
            if isinstance(current_group_name, str) and current_group_name.strip()
            else None
        )
        self.on_group_change = on_group_change
        self.on_launch_group = on_launch_group
        self.on_restore_group = on_restore_group
        self.on_record_group_positions = on_record_group_positions
        self.on_create_group = on_create_group
        self.on_rename_group = on_rename_group
        self.on_delete_group = on_delete_group
        self.on_move_group = on_move_group
        self.group_entries_provider = group_entries_provider
        self.on_add_group_shortcuts = on_add_group_shortcuts
        self.on_remove_group_shortcut = on_remove_group_shortcut
        self.on_set_group_main = on_set_group_main
        self.on_clear_group = on_clear_group
        self.group_sync_choices_provider = group_sync_choices_provider
        self.group_sync_relations_provider = group_sync_relations_provider
        self.on_add_group_sync_relation = on_add_group_sync_relation
        self.on_remove_group_sync_relation = on_remove_group_sync_relation
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
        self.reconnect_failure_messages_provider = (
            reconnect_failure_messages_provider
        )
        self.group_role_status_provider = group_role_status_provider
        self.on_group_role_action = on_group_role_action
        self.operation_record_lines_provider = (
            operation_record_lines_provider
        )
        self.operation_record_files_provider = (
            operation_record_files_provider
        )
        self.on_open_operation_record_file = (
            on_open_operation_record_file
        )
        self.operation_record_search = operation_record_search
        self.auto_click_running = bool(auto_click_running)
        self.on_auto_click_change = on_auto_click_change
        self.card_display_seconds_provider = card_display_seconds_provider
        self.on_card_display_seconds_update = on_card_display_seconds_update
        self.theme_name = (
            theme_name
            if theme_name in UI_THEME_PALETTES
            else "clear_blue"
        )
        self.on_theme_change = on_theme_change
        self.background_image_path = (
            Path(background_image_path).resolve(strict=False)
            if background_image_path is not None
            else None
        )
        self.on_select_background_image = on_select_background_image
        self.on_clear_background_image = on_clear_background_image
        self.on_refresh_error = on_refresh_error
        self._root: Frame | None = None
        self._active_page = "home"
        self._mousewheel_binding_id: str | None = None
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
        self._reconnect_failure_card: Frame | None = None
        self._reconnect_failure_label: Label | None = None
        self._home_role_rows_frame: Frame | None = None
        self._last_group_role_statuses: (
            tuple[GroupRoleStatus, ...] | None
        ) = None
        self._home_activity_heading: Label | None = None
        self._auto_click_interval_entry: Entry | None = None
        self._auto_click_button_variable: StringVar | None = None
        self._auto_click_forever_variable: IntVar | None = None
        self._auto_click_count_entry: Entry | None = None
        self._auto_click_status_label: Label | None = None
        self._auto_click_toggle_button: Button | None = None
        self._group_entries_frame: Frame | None = None
        self._group_setting_message_label: Label | None = None
        self._group_launch_button: Button | None = None
        self._group_restore_button: Button | None = None
        self._group_record_button: Button | None = None
        self._group_launch_status_label: Label | None = None
        self._group_name_entry: Entry | None = None
        self._group_sync_choice_variable: StringVar | None = None
        self._group_sync_choice_ids: dict[str, str] = {}
        self._group_sync_relations_frame: Frame | None = None
        self._operation_records_label: Label | None = None
        self._operation_record_files_frame: Frame | None = None
        self._operation_record_date_entry: Entry | None = None
        self._operation_record_role_entry: Entry | None = None
        self._operation_record_search_frame: Frame | None = None
        self._page_canvas: Canvas | None = None
        self._page_canvas_window: int | None = None
        self._theme_variable: StringVar | None = None
        self._background_status_label: Label | None = None
        self._background_canvas_item: int | None = None
        self._background_page_labels: dict[str, Label] = {}
        self._background_source_image: Image.Image | None = None
        self._background_loaded_path: Path | None = None
        self._background_photo = None
        self._background_resize_id: str | None = None
        self._background_render_size: tuple[int, int] | None = None

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
        active_page = self._active_page
        self._cancel_background_resize()
        if self._mousewheel_binding_id is not None:
            try:
                self.parent.unbind(
                    "<MouseWheel>",
                    self._mousewheel_binding_id,
                )
            except Exception:
                pass
            self._mousewheel_binding_id = None
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
        self._pages.clear()
        self._navigation_buttons.clear()
        self._background_page_labels.clear()
        self._last_group_role_statuses = None
        self.theme_name = _apply_theme_palette(self.theme_name)
        root = Frame(self.parent, bg=BACKGROUND)
        root.pack(fill=BOTH, expand=True)
        self._root = root

        body = Frame(root, bg=BACKGROUND)
        body.pack(fill=BOTH, expand=True)
        sidebar = Frame(body, bg=SIDEBAR, width=176, padx=12, pady=16)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)
        content_shell = Frame(body, bg=BACKGROUND, padx=22, pady=20)
        content_shell.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar = Scrollbar(content_shell, orient="vertical")
        scrollbar.pack(side=RIGHT, fill=Y)
        canvas = Canvas(
            content_shell,
            bg=BACKGROUND,
            highlightthickness=0,
            bd=0,
            yscrollcommand=scrollbar.set,
        )
        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.configure(command=canvas.yview)
        self._background_canvas_item = canvas.create_image(
            0,
            0,
            anchor="nw",
            state="hidden",
        )
        content = Frame(canvas, bg=BACKGROUND)
        canvas_window = canvas.create_window(
            (0, 0),
            window=content,
            anchor="nw",
        )
        self._page_canvas = canvas
        self._page_canvas_window = canvas_window
        self._background_photo = None
        self._background_render_size = None
        self._set_background_path(self.background_image_path)
        content.bind("<Configure>", self._sync_page_scroll_region)
        canvas.bind("<Configure>", self._resize_page_content)
        self._mousewheel_binding_id = self.parent.bind(
            "<MouseWheel>",
            self._on_page_mousewheel,
            add="+",
        )

        self._build_group_summary(sidebar)

        page_specs = (
            ("home", "首頁"),
            ("groups", "組別與視窗"),
            ("sync", "同步與重連"),
            ("characters", "角色資料"),
            ("records", "紀錄"),
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
        self._pages["records"] = self._build_records_page(content)
        self._pages["settings"] = self._build_settings_page(content)
        for page_name, page in self._pages.items():
            background_label = Label(
                page,
                bg=BACKGROUND,
                bd=0,
                highlightthickness=0,
                anchor="nw",
            )
            background_label.place(x=0, y=0, relwidth=1, relheight=1)
            background_label.lower()
            self._background_page_labels[page_name] = background_label
        self.show_page(
            active_page if active_page in self._pages else "home"
        )
        return root

    def _sync_page_scroll_region(self, _event=None) -> None:
        canvas = self._page_canvas
        if canvas is not None:
            canvas.configure(scrollregion=canvas.bbox("all"))

    def _resize_page_content(self, event) -> None:
        canvas = self._page_canvas
        canvas_window = self._page_canvas_window
        if canvas is None or canvas_window is None:
            return
        canvas.itemconfigure(canvas_window, width=max(1, int(event.width)))
        self._schedule_background_resize(
            max(1, int(event.width)),
            max(1, int(getattr(event, "height", 1))),
        )
        self._sync_page_scroll_region()

    def _cancel_background_resize(self) -> None:
        if self._background_resize_id is None:
            return
        try:
            self.parent.after_cancel(self._background_resize_id)
        except Exception:
            pass
        self._background_resize_id = None

    def _schedule_background_resize(self, width: int, height: int) -> None:
        if (
            self._background_source_image is None
            or self._background_canvas_item is None
            or width < 2
            or height < 2
        ):
            return
        requested_size = (width, height)
        if requested_size == self._background_render_size:
            return
        self._cancel_background_resize()
        self._background_resize_id = self.parent.after(
            120,
            lambda: self._render_background(requested_size),
        )

    def _render_background(self, viewport_size: tuple[int, int]) -> None:
        self._background_resize_id = None
        canvas = self._page_canvas
        item = self._background_canvas_item
        source = self._background_source_image
        if canvas is None or item is None or source is None:
            return
        width, height = viewport_size
        try:
            resized_width, resized_height, crop_x, crop_y = _cover_geometry(
                source.size,
                viewport_size,
            )
            resized = source.resize(
                (resized_width, resized_height),
                Image.Resampling.LANCZOS,
            )
            try:
                display = resized.crop(
                    (crop_x, crop_y, crop_x + width, crop_y + height)
                )
                try:
                    photo = ImageTk.PhotoImage(display, master=self.parent)
                finally:
                    display.close()
            finally:
                resized.close()
            canvas.itemconfigure(item, image=photo, state="normal")
            canvas.coords(item, 0, 0)
            canvas.tag_lower(item)
            for label in self._background_page_labels.values():
                label.configure(image=photo)
                label.lower()
            self._background_photo = photo
            self._background_render_size = viewport_size
        except Exception:
            canvas.itemconfigure(item, image="", state="hidden")
            self._background_photo = None
            self._background_render_size = None
            self._refresh_background_status(
                "受管背景副本目前無法顯示，請重新選擇圖片。"
            )

    def _set_background_path(self, path: Path | None) -> bool:
        normalized = (
            Path(path).resolve(strict=False) if path is not None else None
        )
        self.background_image_path = normalized
        if (
            normalized == self._background_loaded_path
            and self._background_source_image is not None
        ):
            canvas = self._page_canvas
            if canvas is not None:
                self._schedule_background_resize(
                    max(1, int(canvas.winfo_width())),
                    max(1, int(canvas.winfo_height())),
                )
            return True

        self._cancel_background_resize()
        if self._background_source_image is not None:
            self._background_source_image.close()
        self._background_source_image = None
        self._background_loaded_path = None
        self._background_photo = None
        self._background_render_size = None

        canvas = self._page_canvas
        item = self._background_canvas_item
        if normalized is None:
            if canvas is not None and item is not None:
                canvas.itemconfigure(item, image="", state="hidden")
            for label in self._background_page_labels.values():
                label.configure(image="")
            return True
        try:
            with Image.open(normalized) as opened:
                opened.load()
                self._background_source_image = opened.convert(
                    "RGBA" if opened.mode in {"RGBA", "LA"} else "RGB"
                )
        except (OSError, ValueError):
            if canvas is not None and item is not None:
                canvas.itemconfigure(item, image="", state="hidden")
            for label in self._background_page_labels.values():
                label.configure(image="")
            return False
        self._background_loaded_path = normalized
        if canvas is not None:
            self._schedule_background_resize(
                max(1, int(canvas.winfo_width())),
                max(1, int(canvas.winfo_height())),
            )
        return True

    def _background_status_text(self, message: str = "") -> str:
        if self.background_image_path is None:
            status = "目前背景：未設定，沿用介面配色。"
        elif self._background_source_image is None:
            status = "目前背景：受管背景副本無法顯示。"
        else:
            status = (
                "目前背景：已套用受管背景副本"
                f"（{self.background_image_path.name}）。"
            )
        return f"{message}\n{status}" if message else status

    def _refresh_background_status(self, message: str = "") -> None:
        if self._background_status_label is not None:
            self._background_status_label.configure(
                text=self._background_status_text(message)
            )

    def _on_page_mousewheel(self, event) -> str | None:
        canvas = self._page_canvas
        if canvas is None:
            return None
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return None
        units = -1 if delta > 0 else 1
        canvas.yview_scroll(units, "units")
        return "break"

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
            text="目前組別角色",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X, pady=(20, 8))
        role_card = self._card(page, padx=10, pady=10)
        role_card.pack(fill=X)
        self._home_role_rows_frame = Frame(role_card, bg=SURFACE)
        self._home_role_rows_frame.pack(fill=X)
        self.refresh_group_role_statuses()

        self._reconnect_failure_card = self._card(page, pady=10)
        self._reconnect_failure_label = Label(
            self._reconnect_failure_card,
            text="",
            justify=LEFT,
            font=("Microsoft JhengHei UI", 10, "bold"),
            bg=SURFACE,
            fg=WARNING,
            anchor="w",
        )
        self._reconnect_failure_label.pack(fill=X)

        self._home_activity_heading = Label(
            page,
            text="今日已登記活動",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        )
        self._home_activity_heading.pack(fill=X, pady=(20, 8))
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

    def _build_records_page(self, parent) -> Frame:
        page = Frame(parent, bg=BACKGROUND)
        self._page_heading(
            page,
            "紀錄",
            "程式內顯示最近一個月；每日文字檔永久保留",
        )
        current_card = self._card(page)
        current_card.pack(fill=X)
        Label(
            current_card,
            text="最近一個月",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        self._operation_records_label = Label(
            current_card,
            text="目前沒有紀錄。",
            justify=LEFT,
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        )
        self._operation_records_label.pack(fill=X, pady=(10, 0))

        files_card = self._card(page)
        files_card.pack(fill=X, pady=(14, 0))
        Label(
            files_card,
            text="每日文字記錄檔",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        self._operation_record_files_frame = Frame(
            files_card,
            bg=SURFACE,
        )
        self._operation_record_files_frame.pack(fill=X, pady=(10, 0))

        search_card = self._card(page)
        search_card.pack(fill=X, pady=(14, 0))
        Label(
            search_card,
            text="依日期與角色搜尋",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        search_fields = Frame(search_card, bg=SURFACE)
        search_fields.pack(fill=X, pady=(10, 0))
        self._operation_record_date_entry = Entry(
            search_fields,
            font=("Microsoft JhengHei UI", 10),
            relief="flat",
            bg=BACKGROUND,
            fg=TEXT,
        )
        self._operation_record_date_entry.insert(0, "")
        self._operation_record_date_entry.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(0, 6),
        )
        self._operation_record_role_entry = Entry(
            search_fields,
            font=("Microsoft JhengHei UI", 10),
            relief="flat",
            bg=BACKGROUND,
            fg=TEXT,
        )
        self._operation_record_role_entry.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=6,
        )
        self._button(
            search_fields,
            "搜尋",
            self._search_operation_records,
            primary=True,
        ).pack(side=RIGHT, padx=(6, 0))
        Label(
            search_card,
            text="日期格式：YYYY-MM-DD；角色可輸入完整或部分名稱",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(6, 0))
        self._operation_record_search_frame = Frame(
            search_card,
            bg=SURFACE,
        )
        self._operation_record_search_frame.pack(fill=X, pady=(10, 0))
        self.refresh_operation_records()
        return page

    def _search_operation_records(self) -> None:
        frame = self._operation_record_search_frame
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        date_text = (
            self._operation_record_date_entry.get().strip()
            if self._operation_record_date_entry is not None
            else ""
        )
        role_name = (
            self._operation_record_role_entry.get().strip()
            if self._operation_record_role_entry is not None
            else ""
        )
        try:
            results = (
                self.operation_record_search(date_text, role_name)
                if self.operation_record_search is not None
                else ()
            )
            if not isinstance(results, tuple) or any(
                not isinstance(item, OperationRecordSearchResult)
                for item in results
            ):
                raise TypeError("operation record search is invalid.")
        except Exception as error:
            self._report_refresh_error(error)
            return
        if not results:
            Label(
                frame,
                text="沒有符合的紀錄。",
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=MUTED,
                anchor="w",
            ).pack(fill=X)
            return
        for item in results[:20]:
            self._button(
                frame,
                item.preview,
                lambda value=item.daily_file: (
                    self._open_operation_record_file(value)
                ),
            ).pack(fill=X, pady=2)

    def _open_operation_record_file(self, path: Path) -> None:
        if self.on_open_operation_record_file is None:
            return
        try:
            self.on_open_operation_record_file(path)
        except Exception as error:
            self._report_refresh_error(error)

    def refresh_operation_records(self) -> tuple[str, ...]:
        try:
            lines = (
                self.operation_record_lines_provider()
                if self.operation_record_lines_provider is not None
                else ()
            )
            files = (
                self.operation_record_files_provider()
                if self.operation_record_files_provider is not None
                else ()
            )
            if (
                not isinstance(lines, tuple)
                or any(not isinstance(line, str) for line in lines)
                or not isinstance(files, tuple)
                or any(not isinstance(path, Path) for path in files)
            ):
                raise TypeError("operation record provider is invalid.")
        except Exception as error:
            self._report_refresh_error(error)
            return ()
        if self._operation_records_label is not None:
            visible = lines[:20]
            self._operation_records_label.configure(
                text="\n".join(visible) if visible else "目前沒有紀錄。"
            )
        frame = self._operation_record_files_frame
        if frame is not None:
            for child in frame.winfo_children():
                child.destroy()
            if not files:
                Label(
                    frame,
                    text="尚未建立每日文字記錄檔。",
                    font=("Microsoft JhengHei UI", 9),
                    bg=SURFACE,
                    fg=MUTED,
                    anchor="w",
                ).pack(fill=X)
            for path in files[:14]:
                self._button(
                    frame,
                    path.stem,
                    lambda value=path: self._open_operation_record_file(
                        value
                    ),
                ).pack(fill=X, pady=2)
        return lines

    @staticmethod
    def _safe_group_role_statuses(
        values: object,
    ) -> tuple[GroupRoleStatus, ...]:
        if not isinstance(values, tuple) or any(
            not isinstance(value, GroupRoleStatus) for value in values
        ):
            raise TypeError(
                "group role status provider must return a status tuple."
            )
        return values

    def _run_group_role_action(self, action_id: str) -> None:
        if self.on_group_role_action is None:
            return
        try:
            self.on_group_role_action(action_id)
        except Exception as error:
            self._report_refresh_error(error)

    def refresh_group_role_statuses(
        self,
    ) -> tuple[GroupRoleStatus, ...]:
        frame = self._home_role_rows_frame
        try:
            rows = (
                self._safe_group_role_statuses(
                    self.group_role_status_provider()
                )
                if self.group_role_status_provider is not None
                else ()
            )
        except Exception as error:
            self._report_refresh_error(error)
            return ()
        if frame is None:
            return rows
        if rows == self._last_group_role_statuses:
            return rows
        self._last_group_role_statuses = rows
        for child in frame.winfo_children():
            child.destroy()
        if not rows:
            Label(
                frame,
                text="目前組別尚未建立可辨識的角色清單。",
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=MUTED,
                anchor="w",
            ).pack(fill=X, pady=4)
            return ()
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        for index, item in enumerate(rows):
            row = Frame(
                frame,
                bg=BACKGROUND,
                padx=10,
                pady=7,
                cursor="hand2",
            )
            row.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=3,
                pady=2,
            )
            name_label = Label(
                row,
                text=f"{item.order}. {item.display_name}",
                font=("Microsoft JhengHei UI", 10, "bold"),
                bg=BACKGROUND,
                fg=TEXT,
                anchor="w",
                cursor="hand2",
            )
            name_label.pack(side=LEFT, fill=X, expand=True)
            color = (
                SUCCESS
                if item.status == "已開啟"
                else WARNING
                if item.status in {"斷線", "重連失敗"}
                else PRIMARY
                if item.status == "重連中"
                else MUTED
            )
            status_label = Label(
                row,
                text=item.status,
                font=("Microsoft JhengHei UI", 9, "bold"),
                bg=BACKGROUND,
                fg=color,
                cursor="hand2",
            )
            status_label.pack(side=RIGHT)
            activate = lambda _event, value=item.action_id: (
                self._run_group_role_action(value)
            )
            row.bind("<Button-1>", activate)
            name_label.bind("<Button-1>", activate)
            status_label.bind("<Button-1>", activate)
        return rows

    @staticmethod
    def _safe_reconnect_failure_messages(
        values: object,
    ) -> tuple[str, ...]:
        if not isinstance(values, tuple):
            raise TypeError(
                "reconnect failure provider must return a tuple."
            )
        messages: list[str] = []
        for value in values:
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > 140
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("reconnect failure message is invalid.")
            messages.append(value.strip())
        return tuple(messages)

    def refresh_reconnect_failures(self) -> tuple[str, ...]:
        try:
            messages = (
                self._safe_reconnect_failure_messages(
                    self.reconnect_failure_messages_provider()
                )
                if self.reconnect_failure_messages_provider is not None
                else ()
            )
        except Exception as error:
            self._report_refresh_error(error)
            return ()
        card = self._reconnect_failure_card
        label = self._reconnect_failure_label
        if card is None or label is None:
            return messages
        if messages:
            label.configure(
                text="\n".join(f"● {message}" for message in messages)
            )
            if not card.winfo_manager():
                options: dict[str, object] = {
                    "fill": X,
                    "pady": (12, 0),
                }
                if self._home_activity_heading is not None:
                    options["before"] = self._home_activity_heading
                card.pack(**options)
        else:
            label.configure(text="")
            if card.winfo_manager():
                card.pack_forget()
        return messages

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
        launch_row = Frame(selector, bg=SURFACE)
        launch_row.pack(fill=X, pady=(12, 0))
        self._group_launch_button = self._button(
            launch_row,
            "一鍵啟動並還原位置",
            self._launch_current_group,
            primary=True,
        )
        self._group_launch_button.pack(side=LEFT)
        if not names or self.on_launch_group is None:
            self._group_launch_button.configure(state=DISABLED)
        self._group_restore_button = self._button(
            launch_row,
            "只還原位置",
            self._restore_current_group,
        )
        self._group_restore_button.pack(side=LEFT, padx=(8, 0))
        if not names or self.on_restore_group is None:
            self._group_restore_button.configure(state=DISABLED)
        self._group_record_button = self._button(
            launch_row,
            "記錄目前位置",
            self._record_current_group_positions,
        )
        self._group_record_button.pack(side=LEFT, padx=(8, 0))
        if not names or self.on_record_group_positions is None:
            self._group_record_button.configure(state=DISABLED)
        self._group_launch_status_label = Label(
            launch_row,
            text="",
            font=("Microsoft JhengHei UI", 9, "bold"),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        )
        self._group_launch_status_label.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(12, 0),
        )
        group_name_row = Frame(selector, bg=SURFACE)
        group_name_row.pack(fill=X, pady=(12, 0))
        self._group_name_entry = Entry(
            group_name_row,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._group_name_entry.insert(
            0,
            self.current_group_name or "",
        )
        self._group_name_entry.pack(
            side=LEFT,
            fill=X,
            expand=True,
            ipady=8,
        )
        self._button(
            group_name_row,
            "新增組",
            self._create_group,
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            group_name_row,
            "改名",
            self._rename_current_group,
        ).pack(side=LEFT, padx=(8, 0))
        group_order_row = Frame(selector, bg=SURFACE)
        group_order_row.pack(fill=X, pady=(8, 0))
        self._button(
            group_order_row,
            "上移",
            lambda: self._move_current_group(-1),
        ).pack(side=LEFT)
        self._button(
            group_order_row,
            "下移",
            lambda: self._move_current_group(1),
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            group_order_row,
            "刪除組",
            self._delete_current_group,
        ).pack(side=LEFT, padx=(8, 0))

        entry_card = self._card(page, padx=12, pady=12)
        entry_card.pack(fill=X, pady=(14, 0))
        entry_header = Frame(entry_card, bg=SURFACE)
        entry_header.pack(fill=X)
        Label(
            entry_header,
            text="組別角色",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(side=LEFT, fill=X, expand=True)
        self._button(
            entry_header,
            "加入角色到組別",
            self._add_shortcuts_to_current_group,
            primary=True,
        ).pack(side=RIGHT)
        self._button(
            entry_header,
            "清空角色",
            self._clear_current_group,
        ).pack(side=RIGHT, padx=(0, 8))
        self._group_entries_frame = Frame(entry_card, bg=SURFACE)
        self._group_entries_frame.pack(fill=X, pady=(10, 0))
        self._group_setting_message_label = Label(
            entry_card,
            text="",
            font=("Microsoft JhengHei UI", 9, "bold"),
            bg=SURFACE,
            fg=WARNING,
            anchor="w",
        )
        self.refresh_group_entries()

        sync_card = self._card(page, padx=12, pady=12)
        sync_card.pack(fill=X, pady=(14, 0))
        Label(
            sync_card,
            text="延伸同步範圍",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            sync_card,
            text="從目前組別主控遞迴加入其他角色或組別主控",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(3, 8))
        sync_actions = Frame(sync_card, bg=SURFACE)
        sync_actions.pack(fill=X)
        self._group_sync_choice_variable = StringVar(
            master=self.parent,
            value="目前沒有可加入角色",
        )
        self._group_sync_choice_menu = OptionMenu(
            sync_actions,
            self._group_sync_choice_variable,
            "目前沒有可加入角色",
        )
        self._group_sync_choice_menu.configure(
            font=("Microsoft JhengHei UI", 9),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self._group_sync_choice_menu.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(0, 8),
        )
        self._button(
            sync_actions,
            "加入延伸同步",
            self._add_group_sync_relation,
            primary=True,
        ).pack(side=RIGHT)
        self._group_sync_relations_frame = Frame(
            sync_card,
            bg=SURFACE,
        )
        self._group_sync_relations_frame.pack(fill=X, pady=(10, 0))
        self.refresh_group_sync_relations()

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

        auto_click_card = self._card(page)
        auto_click_card.pack(fill=X, pady=(14, 0))
        Label(
            auto_click_card,
            text="連續點擊",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        settings_row = Frame(auto_click_card, bg=SURFACE)
        settings_row.pack(fill=X, pady=(10, 0))
        Label(
            settings_row,
            text="間隔毫秒",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)
        self._auto_click_interval_entry = Entry(
            settings_row,
            width=7,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._auto_click_interval_entry.insert(0, "20")
        self._auto_click_interval_entry.pack(side=LEFT, padx=(6, 14), ipady=5)

        self._auto_click_button_variable = StringVar(
            master=self.parent,
            value="左鍵",
        )
        button_menu = OptionMenu(
            settings_row,
            self._auto_click_button_variable,
            "左鍵",
            "右鍵",
        )
        button_menu.configure(
            font=("Microsoft JhengHei UI", 9),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        button_menu.pack(side=LEFT)

        self._auto_click_forever_variable = IntVar(
            master=self.parent,
            value=1,
        )
        Checkbutton(
            settings_row,
            text="無限",
            variable=self._auto_click_forever_variable,
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
            activebackground=SURFACE,
            selectcolor=BACKGROUND,
        ).pack(side=LEFT, padx=(14, 6))
        Label(
            settings_row,
            text="次數",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)
        self._auto_click_count_entry = Entry(
            settings_row,
            width=7,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._auto_click_count_entry.insert(0, "1")
        self._auto_click_count_entry.pack(side=LEFT, padx=(6, 14), ipady=5)
        Label(
            settings_row,
            text="快捷鍵 F1",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)

        auto_click_actions = Frame(auto_click_card, bg=SURFACE)
        auto_click_actions.pack(fill=X, pady=(10, 0))
        self._auto_click_toggle_button = self._button(
            auto_click_actions,
            "",
            self._toggle_auto_click,
            primary=True,
        )
        self._auto_click_toggle_button.pack(side=LEFT)
        self._auto_click_status_label = Label(
            auto_click_actions,
            text="",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        )
        self._auto_click_status_label.pack(side=LEFT, padx=12)
        self._refresh_auto_click_controls()
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
                    "停止同步視窗"
                    if self.keyboard_sync_enabled
                    else "開始同步視窗"
                ),
                bg=(
                    WARNING if self.keyboard_sync_enabled else PRIMARY
                ),
            )
        if self._keyboard_sync_label is not None:
            self._keyboard_sync_label.configure(
                text=(
                    "● 已啟用｜同步左鍵、拖曳與已確認快捷鍵"
                    if self.keyboard_sync_enabled
                    else "● 尚未啟用"
                ),
                fg=SUCCESS if self.keyboard_sync_enabled else MUTED,
            )

    def set_keyboard_sync_enabled(self, enabled: bool) -> None:
        self.keyboard_sync_enabled = bool(enabled)
        self._refresh_keyboard_sync_controls()

    def set_smart_reconnect_enabled(self, enabled: bool) -> None:
        self.smart_reconnect_enabled = bool(enabled)
        self._refresh_smart_reconnect_controls()

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
                    "● 自動監看中｜依目前狀態安全重試"
                    if self.smart_reconnect_enabled
                    else "● 安全停止｜不會點擊遊戲視窗"
                ),
                fg=SUCCESS if self.smart_reconnect_enabled else MUTED,
            )

    def _auto_click_settings(self) -> tuple[int, str, bool, int]:
        if (
            self._auto_click_interval_entry is None
            or self._auto_click_button_variable is None
            or self._auto_click_forever_variable is None
            or self._auto_click_count_entry is None
        ):
            raise RuntimeError("continuous click controls are unavailable.")
        interval_ms = int(self._auto_click_interval_entry.get().strip())
        repeat_count = int(self._auto_click_count_entry.get().strip())
        if not 1 <= interval_ms <= 600_000:
            raise ValueError("間隔毫秒必須介於 1 到 600000。")
        if not 1 <= repeat_count <= 999_999:
            raise ValueError("次數必須介於 1 到 999999。")
        button = (
            "right"
            if self._auto_click_button_variable.get() == "右鍵"
            else "left"
        )
        return (
            interval_ms,
            button,
            bool(self._auto_click_forever_variable.get()),
            repeat_count,
        )

    def _toggle_auto_click(self) -> None:
        if self.on_auto_click_change is None:
            return
        desired = not self.auto_click_running
        try:
            settings = self._auto_click_settings()
            accepted = self.on_auto_click_change(desired, *settings)
        except Exception as error:
            self._report_refresh_error(error)
            return
        if accepted is False:
            return
        self.auto_click_running = desired
        self._refresh_auto_click_controls()

    def toggle_auto_click_from_hotkey(self) -> None:
        self._toggle_auto_click()

    def set_auto_click_running(
        self,
        running: bool,
        sent_count: int = 0,
    ) -> None:
        self.auto_click_running = bool(running)
        self._refresh_auto_click_controls(sent_count=sent_count)

    def _refresh_auto_click_controls(self, *, sent_count: int = 0) -> None:
        if self._auto_click_toggle_button is not None:
            self._auto_click_toggle_button.configure(
                text=(
                    "停止連續點擊"
                    if self.auto_click_running
                    else "開始連續點擊"
                ),
                bg=WARNING if self.auto_click_running else PRIMARY,
            )
        if self._auto_click_status_label is not None:
            self._auto_click_status_label.configure(
                text=(
                    f"● 點擊中｜已送出 {max(0, int(sent_count))} 次"
                    if self.auto_click_running
                    else "● 尚未啟用"
                ),
                fg=SUCCESS if self.auto_click_running else MUTED,
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
        theme_card = self._card(page)
        theme_card.pack(fill=X)
        Label(
            theme_card,
            text="介面風格",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            theme_card,
            text="可隨時切換整個主畫面的配色與閱讀風格。",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(6, 12))
        theme_row = Frame(theme_card, bg=SURFACE)
        theme_row.pack(fill=X)
        self._theme_variable = StringVar(
            master=self.parent,
            value=UI_THEME_LABELS[self.theme_name],
        )
        theme_menu = OptionMenu(
            theme_row,
            self._theme_variable,
            *UI_THEME_LABELS.values(),
        )
        theme_menu.configure(
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        theme_menu["menu"].configure(
            font=("Microsoft JhengHei UI", 10)
        )
        theme_menu.pack(side=LEFT, fill=X, expand=True)
        self._button(
            theme_row,
            "套用風格",
            self._apply_selected_theme,
            primary=True,
        ).pack(side=RIGHT, padx=(8, 0))

        background_card = self._card(page)
        background_card.pack(fill=X, pady=(14, 0))
        Label(
            background_card,
            text="背景圖片",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            background_card,
            text=(
                "可選擇一般圖片或相機 RAW；程式只顯示轉換後的"
                "受管副本，原始圖片不會變更。"
            ),
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            justify=LEFT,
            wraplength=680,
        ).pack(fill=X, pady=(6, 10))
        self._background_status_label = Label(
            background_card,
            text=self._background_status_text(),
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
            justify=LEFT,
            wraplength=680,
        )
        self._background_status_label.pack(fill=X, pady=(0, 12))
        background_row = Frame(background_card, bg=SURFACE)
        background_row.pack(fill=X)
        choose_button = self._button(
            background_row,
            "選擇背景圖片",
            self._choose_background_image,
            primary=True,
        )
        choose_button.pack(side=LEFT)
        if self.on_select_background_image is None:
            choose_button.configure(state=DISABLED)
        clear_button = self._button(
            background_row,
            "清除背景",
            self._clear_background_image,
        )
        clear_button.pack(side=LEFT, padx=(8, 0))
        if self.on_clear_background_image is None:
            clear_button.configure(state=DISABLED)

        card = self._card(page)
        card.pack(fill=X, pady=(14, 0))
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

    def _apply_selected_theme(self) -> None:
        if self._theme_variable is None:
            return
        selected_label = self._theme_variable.get()
        selected_name = next(
            (
                name
                for name, label in UI_THEME_LABELS.items()
                if label == selected_label
            ),
            None,
        )
        if selected_name is None or selected_name == self.theme_name:
            return
        try:
            if self.on_theme_change is not None:
                accepted = self.on_theme_change(selected_name)
                if accepted is False:
                    return
        except Exception as error:
            self._report_refresh_error(error)
            return
        self.theme_name = selected_name
        self._active_page = "settings"
        self.build()

    def _choose_background_image(self) -> None:
        if self.on_select_background_image is None:
            return
        try:
            result = self.on_select_background_image()
        except Exception:
            self._refresh_background_status(
                "背景圖片處理失敗，原本背景已保留。"
            )
            return
        if result is None:
            return
        if not isinstance(result, BackgroundImageResult):
            self._refresh_background_status(
                "背景圖片處理失敗，原本背景已保留。"
            )
            return
        loaded = self._set_background_path(result.managed_path)
        message = result.message
        if result.succeeded and result.managed_path is not None and not loaded:
            message = "受管背景副本無法顯示，請重新選擇圖片。"
        self._refresh_background_status(message)

    def _clear_background_image(self) -> None:
        if self.on_clear_background_image is None:
            return
        try:
            result = self.on_clear_background_image()
        except Exception:
            self._refresh_background_status(
                "背景圖片無法清除，原本背景已保留。"
            )
            return
        if not isinstance(result, BackgroundImageResult):
            self._refresh_background_status(
                "背景圖片無法清除，原本背景已保留。"
            )
            return
        self._set_background_path(result.managed_path)
        self._refresh_background_status(result.message)

    def dispose(self) -> None:
        """Release background image resources before the Tk window closes."""
        self._cancel_background_resize()
        if self._background_source_image is not None:
            self._background_source_image.close()
        self._background_source_image = None
        self._background_loaded_path = None
        self._background_photo = None

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
        if name == "records":
            self.refresh_operation_records()
        self._active_page = name
        if self._page_canvas is not None:
            self._page_canvas.yview_moveto(0.0)
            self.parent.after_idle(self._sync_page_scroll_region)

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
        if self._group_name_entry is not None:
            self._group_name_entry.delete(0, "end")
            self._group_name_entry.insert(0, name)
        self.refresh_workspace()
        self.refresh_group_entries()
        self.refresh_group_sync_relations()
        self.refresh_group_role_statuses()
        self.refresh_operation_records()

    def _launch_current_group(self) -> None:
        self._run_group_window_action(
            self.on_launch_group,
            "正在啟動並還原位置…",
        )

    def _restore_current_group(self) -> None:
        self._run_group_window_action(
            self.on_restore_group,
            "正在還原目前組別位置…",
        )

    def _record_current_group_positions(self) -> None:
        self._run_group_window_action(
            self.on_record_group_positions,
            "正在記錄目前組別位置…",
        )

    def _run_group_window_action(
        self,
        callback: Callable[[str], object] | None,
        progress_message: str,
    ) -> None:
        if self.current_group_name is None or callback is None:
            return
        self.set_group_launch_state(True, progress_message)
        try:
            accepted = callback(self.current_group_name)
        except Exception as error:
            self.set_group_launch_state(False, "組別視窗操作未完成。")
            self._report_refresh_error(error)
            return
        if accepted is False:
            self.set_group_launch_state(
                False,
                "整組啟動正在進行，請稍候。",
            )
        elif isinstance(accepted, str) and accepted.strip():
            self.set_group_launch_state(False, accepted.strip())

    def set_group_launch_state(
        self,
        running: bool,
        message: str,
    ) -> None:
        for button in (
            self._group_launch_button,
            self._group_restore_button,
            self._group_record_button,
        ):
            if button is not None:
                button.configure(
                    state=DISABLED if running else NORMAL
                )
        if self._group_launch_status_label is not None:
            self._group_launch_status_label.configure(
                text=message.strip(),
                fg=PRIMARY if running else MUTED,
            )

    def _apply_group_management_result(
        self,
        result: object,
    ) -> None:
        if not isinstance(result, GroupManagementViewResult):
            raise TypeError("group management result is invalid.")
        if not result.success:
            self._show_group_setting_message(
                result.message or "組別設定沒有變更。"
            )
            return
        choices = (
            self.group_choices_provider()
            if self.group_choices_provider is not None
            else self.group_choices
        )
        if not isinstance(choices, tuple) or any(
            not isinstance(choice, PlayerGroupChoice)
            for choice in choices
        ):
            raise TypeError("group choices provider returned invalid values.")
        self.group_choices = choices
        valid_names = {choice.name for choice in choices}
        self.current_group_name = (
            result.current_group_name
            if result.current_group_name in valid_names
            else (choices[0].name if choices else None)
        )
        self._active_page = "groups"
        self.build()

    def _create_group(self) -> None:
        if self._group_name_entry is None or self.on_create_group is None:
            return
        name = self._group_name_entry.get().strip()
        if not name:
            self._show_group_setting_message("請先輸入組別名稱。")
            return
        try:
            self._apply_group_management_result(
                self.on_create_group(name)
            )
        except Exception as error:
            self._report_refresh_error(error)

    def _rename_current_group(self) -> None:
        if (
            self.current_group_name is None
            or self._group_name_entry is None
            or self.on_rename_group is None
        ):
            return
        new_name = self._group_name_entry.get().strip()
        if not new_name:
            self._show_group_setting_message("請先輸入新的組別名稱。")
            return
        try:
            self._apply_group_management_result(
                self.on_rename_group(
                    self.current_group_name,
                    new_name,
                )
            )
        except Exception as error:
            self._report_refresh_error(error)

    def _delete_current_group(self) -> None:
        if (
            self.current_group_name is None
            or self.on_delete_group is None
        ):
            return
        confirmed = messagebox.askyesno(
            "輔｜刪除組別",
            f"確定刪除「{self.current_group_name}」嗎？\n"
            "舊版設定不會被修改。",
            parent=self.parent,
        )
        if not confirmed:
            return
        try:
            self._apply_group_management_result(
                self.on_delete_group(self.current_group_name)
            )
        except Exception as error:
            self._report_refresh_error(error)

    def _move_current_group(self, direction: int) -> None:
        if (
            self.current_group_name is None
            or self.on_move_group is None
        ):
            return
        try:
            self._apply_group_management_result(
                self.on_move_group(
                    self.current_group_name,
                    direction,
                )
            )
        except Exception as error:
            self._report_refresh_error(error)

    def _add_shortcuts_to_current_group(self) -> None:
        if (
            self.current_group_name is None
            or self.on_add_group_shortcuts is None
        ):
            return
        try:
            result = self.on_add_group_shortcuts(self.current_group_name)
        except Exception as error:
            self._report_refresh_error(error)
            return
        label = self._group_setting_message_label
        if label is None:
            return
        if isinstance(result, str) and result.strip():
            label.configure(text=result.strip())
            if not label.winfo_manager():
                label.pack(fill=X, pady=(8, 0))
        else:
            label.configure(text="")
            if label.winfo_manager():
                label.pack_forget()

    def _remove_group_shortcut(self, entry_id: str) -> None:
        if (
            self.current_group_name is None
            or self.on_remove_group_shortcut is None
        ):
            return
        try:
            self.on_remove_group_shortcut(
                self.current_group_name,
                entry_id,
            )
        except Exception as error:
            self._report_refresh_error(error)

    def refresh_group_entries(
        self,
    ) -> tuple[GroupConfigurationEntry, ...]:
        frame = self._group_entries_frame
        if frame is None:
            return ()
        for child in frame.winfo_children():
            child.destroy()
        if (
            self.current_group_name is None
            or self.group_entries_provider is None
        ):
            entries: tuple[GroupConfigurationEntry, ...] = ()
        else:
            entries = self.group_entries_provider(self.current_group_name)
            if any(
                not isinstance(entry, GroupConfigurationEntry)
                for entry in entries
            ):
                raise TypeError(
                    "group entries provider returned invalid values."
                )
        if not entries:
            Label(
                frame,
                text="目前組別尚未加入角色捷徑。",
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=MUTED,
                anchor="w",
            ).pack(fill=X, pady=4)
            return ()
        for entry in entries:
            row = Frame(frame, bg=BACKGROUND, padx=10, pady=7)
            row.pack(fill=X, pady=2)
            Label(
                row,
                text=f"{entry.order}. {entry.display_name}",
                font=("Microsoft JhengHei UI", 10, "bold"),
                bg=BACKGROUND,
                fg=TEXT,
                anchor="w",
            ).pack(side=LEFT, fill=X, expand=True)
            Label(
                row,
                text=entry.role,
                font=("Microsoft JhengHei UI", 9),
                bg=BACKGROUND,
                fg=MUTED,
            ).pack(side=LEFT, padx=10)
            self._button(
                row,
                "移除",
                lambda value=entry.entry_id: self._remove_group_shortcut(
                    value
                ),
            ).pack(side=RIGHT)
            if entry.role != "主窗口":
                self._button(
                    row,
                    "設為主窗口",
                    lambda value=entry.entry_id: self._set_group_main(
                        value
                    ),
                ).pack(side=RIGHT, padx=(0, 6))
        return entries

    def _set_group_main(self, entry_id: str) -> None:
        if (
            self.current_group_name is None
            or self.on_set_group_main is None
        ):
            return
        try:
            result = self.on_set_group_main(
                self.current_group_name,
                entry_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()
        self.refresh_group_sync_relations()

    def _clear_current_group(self) -> None:
        if (
            self.current_group_name is None
            or self.on_clear_group is None
        ):
            return
        confirmed = messagebox.askyesno(
            "輔｜清空組別",
            f"確定清空「{self.current_group_name}」的角色嗎？\n"
            "組別會保留，舊版設定不會被修改。",
            parent=self.parent,
        )
        if not confirmed:
            return
        try:
            result = self.on_clear_group(self.current_group_name)
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._apply_group_management_result(result)

    def _show_group_setting_message(self, value: object) -> None:
        label = self._group_setting_message_label
        if label is None:
            return
        message = value.strip() if isinstance(value, str) else ""
        label.configure(text=message)
        if message and not label.winfo_manager():
            label.pack(fill=X, pady=(8, 0))
        elif not message and label.winfo_manager():
            label.pack_forget()

    def _add_group_sync_relation(self) -> None:
        if (
            self.current_group_name is None
            or self.on_add_group_sync_relation is None
            or self._group_sync_choice_variable is None
        ):
            return
        member_id = self._group_sync_choice_ids.get(
            self._group_sync_choice_variable.get()
        )
        if member_id is None:
            return
        try:
            result = self.on_add_group_sync_relation(
                self.current_group_name,
                member_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_sync_relations()

    def _remove_group_sync_relation(self, member_id: str) -> None:
        if (
            self.current_group_name is None
            or self.on_remove_group_sync_relation is None
        ):
            return
        try:
            self.on_remove_group_sync_relation(
                self.current_group_name,
                member_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message("")
        self.refresh_group_sync_relations()

    def refresh_group_sync_relations(
        self,
    ) -> tuple[GroupSyncMemberChoice, ...]:
        group_name = self.current_group_name
        choices = (
            self.group_sync_choices_provider(group_name)
            if group_name is not None
            and self.group_sync_choices_provider is not None
            else ()
        )
        relations = (
            self.group_sync_relations_provider(group_name)
            if group_name is not None
            and self.group_sync_relations_provider is not None
            else ()
        )
        if any(
            not isinstance(item, GroupSyncMemberChoice)
            for item in (*choices, *relations)
        ):
            raise TypeError("group sync provider returned invalid values.")
        self._group_sync_choice_ids = {
            item.label: item.entry_id for item in choices
        }
        variable = self._group_sync_choice_variable
        menu = getattr(self, "_group_sync_choice_menu", None)
        labels = tuple(self._group_sync_choice_ids) or (
            "目前沒有可加入角色",
        )
        if variable is not None:
            variable.set(labels[0])
        if menu is not None:
            menu["menu"].delete(0, "end")
            for label in labels:
                menu["menu"].add_command(
                    label=label,
                    command=lambda value=label: variable.set(value)
                    if variable is not None
                    else None,
                )
            menu.configure(state=NORMAL if choices else DISABLED)
        frame = self._group_sync_relations_frame
        if frame is not None:
            for child in frame.winfo_children():
                child.destroy()
            if not relations:
                Label(
                    frame,
                    text="目前沒有額外延伸同步。",
                    font=("Microsoft JhengHei UI", 9),
                    bg=SURFACE,
                    fg=MUTED,
                    anchor="w",
                ).pack(fill=X)
            for item in relations:
                row = Frame(frame, bg=BACKGROUND, padx=10, pady=6)
                row.pack(fill=X, pady=2)
                Label(
                    row,
                    text=item.label,
                    font=("Microsoft JhengHei UI", 9),
                    bg=BACKGROUND,
                    fg=TEXT,
                    anchor="w",
                ).pack(side=LEFT, fill=X, expand=True)
                self._button(
                    row,
                    "移除",
                    lambda value=item.entry_id: (
                        self._remove_group_sync_relation(value)
                    ),
                ).pack(side=RIGHT)
        return relations

    def _refresh_from_player_action(self) -> None:
        if self.on_start is not None:
            self.on_start()
        self.refresh_workspace()
        self.refresh_activity_schedule()
        self.refresh_cards()
        self.refresh_target_window()
        self.refresh_group_role_statuses()

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
