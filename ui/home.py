"""SP3 player home and confirmed feature pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
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
    Scale,
    Scrollbar,
    HORIZONTAL,
    IntVar,
    StringVar,
)
from tkinter import colorchooser, messagebox
from tkinter.ttk import Progressbar

from PIL import Image, ImageStat, ImageTk

from cards.view_state import CardViewState
from core.target_window_observation import TargetWindowObservation
from domain.game_shortcuts import (
    CONFIRMED_GAME_SHORTCUTS,
)
from habit.preference_service import PlayerHabitSettingsView
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
from services.feature_hotkey_monitor import (
    FEATURE_HOTKEYS,
    normalize_feature_hotkey,
)
from services.background_image_service import (
    BackgroundImageResult,
    BackgroundMetadata,
    BackgroundSettings,
    DEFAULT_BACKGROUND_FILL_COLOR,
    DEFAULT_BACKGROUND_OPACITY,
)
from services.card_preview_selection_service import (
    CardPreviewChoice,
    CardPreviewSelectionState,
)
from services.sync_operation_record_store import (
    OperationRecordSearchResult,
)
from services.activity_schedule_view_service import PlayerActivitySchedule
from services.window_size_adjustment_service import (
    DEFAULT_FLASH_CLIENT_HEIGHT,
    DEFAULT_FLASH_CLIENT_WIDTH,
    MAX_FLASH_CLIENT_SIZE,
    MIN_FLASH_CLIENT_SIZE,
    WindowSizeAdjustmentResult,
)
from services.game_time_timed_click_service import (
    GameTimeTimedClickResult,
    GameTimeTimedClickSnapshot,
    MAX_TIME_OFFSET_MS,
    MIN_TIME_OFFSET_MS,
    clamp_time_offset_ms,
)
from workspace.models import WorkspaceState


INPUT_POLICY_LABELS = {
    "foreground_only": "僅允許前台",
    "foreground_background": "允許前台與背景",
    "all": "全部允許（含最小化）",
}

BACKGROUND_PAGE_LABELS = {
    "home": "首頁",
    "groups": "目前組別",
    "sync": "同步與重連",
    "characters": "角色資料",
    "records": "紀錄",
    "settings": "設定",
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


def _blend_hex_color(base: str, backdrop: str, opacity: int) -> str:
    """Blend a legacy UI color over the visible background."""
    normalized_opacity = max(0, min(100, int(opacity))) / 100

    def rgb(value: str) -> tuple[int, int, int]:
        return tuple(
            int(value[index : index + 2], 16)
            for index in (1, 3, 5)
        )

    base_rgb = rgb(base)
    backdrop_rgb = rgb(backdrop)
    blended = tuple(
        round(
            backdrop_channel * (1 - normalized_opacity)
            + base_channel * normalized_opacity
        )
        for base_channel, backdrop_channel in zip(
            base_rgb,
            backdrop_rgb,
        )
    )
    return "#" + "".join(f"{channel:02X}" for channel in blended)


def _average_image_color(image: Image.Image) -> str:
    mean = ImageStat.Stat(image.convert("RGB")).mean
    return "#" + "".join(
        f"{max(0, min(255, round(value))):02X}" for value in mean[:3]
    )


def _contrast_ratio(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        channels = [
            int(value[index : index + 2], 16) / 255
            for index in (1, 3, 5)
        ]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return (
            0.2126 * linear[0]
            + 0.7152 * linear[1]
            + 0.0722 * linear[2]
        )

    light, dark = sorted(
        (luminance(first), luminance(second)),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


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


def _contain_geometry(
    source_size: tuple[int, int],
    viewport_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Return no-upscale contain dimensions and centered placement."""
    source_width, source_height = source_size
    viewport_width, viewport_height = viewport_size
    if min(source_width, source_height, viewport_width, viewport_height) < 1:
        raise ValueError("image and viewport dimensions must be positive")
    scale = min(
        1.0,
        viewport_width / source_width,
        viewport_height / source_height,
    )
    resized_width = max(1, round(source_width * scale))
    resized_height = max(1, round(source_height * scale))
    offset_x = max(0, (viewport_width - resized_width) // 2)
    offset_y = max(0, (viewport_height - resized_height) // 2)
    return resized_width, resized_height, offset_x, offset_y


def _reordered_entry_ids(
    entry_ids: tuple[str, ...],
    source_id: str,
    target_id: str,
) -> tuple[str, ...]:
    if (
        len(entry_ids) != len(set(entry_ids))
        or source_id not in entry_ids
        or target_id not in entry_ids
        or source_id == target_id
    ):
        return entry_ids
    reordered = list(entry_ids)
    source_index = reordered.index(source_id)
    target_index = reordered.index(target_id)
    reordered.pop(source_index)
    if source_index < target_index:
        target_index -= 1
    reordered.insert(target_index, source_id)
    return tuple(reordered)


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


def _selected_sync_key_summary(keys: Iterable[str]) -> str:
    selected = {
        key for key in keys if isinstance(key, str)
    }
    ordered = tuple(
        shortcut.key
        for shortcut in CONFIRMED_GAME_SHORTCUTS
        if shortcut.key in selected
    )
    return "未勾選" if not ordered else "、".join(ordered)


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
        selected_sync_keys: Iterable[str] = ("ESC",),
        on_selected_sync_keys_change: (
            Callable[[tuple[str, ...]], object] | None
        ) = None,
        sync_keys_collapsed: bool = True,
        on_sync_keys_collapsed_change: (
            Callable[[bool], object] | None
        ) = None,
        feature_hotkeys: Mapping[str, object] | None = None,
        on_feature_hotkey_change: (
            Callable[[str, str], object] | None
        ) = None,
        group_choices: Iterable[PlayerGroupChoice] = (),
        group_choices_provider: (
            Callable[[], tuple[PlayerGroupChoice, ...]] | None
        ) = None,
        current_group_name: str | None = None,
        on_group_change: Callable[[str], object] | None = None,
        on_launch_group: Callable[[str], object] | None = None,
        on_restore_group: Callable[[str], object] | None = None,
        on_stop_all_managed_games: (
            Callable[[str], object] | None
        ) = None,
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
        on_export_group_configuration: Callable[[], object] | None = None,
        on_import_group_configuration: Callable[[], object] | None = None,
        group_launch_hotkey_provider: (
            Callable[[str], str] | None
        ) = None,
        on_group_launch_hotkey_change: (
            Callable[[str, str], object] | None
        ) = None,
        group_entries_provider: (
            Callable[[str], tuple[GroupConfigurationEntry, ...]] | None
        ) = None,
        on_reorder_group_entries: (
            Callable[[str, tuple[str, ...]], object] | None
        ) = None,
        group_master_locked_provider: (
            Callable[[str], bool] | None
        ) = None,
        on_group_master_locked_change: (
            Callable[[str, bool], object] | None
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
        on_capture_sync_base_point: (
            Callable[[str], object] | None
        ) = None,
        on_capture_sync_target_point: (
            Callable[[str, str], object] | None
        ) = None,
        on_save_sync_target_settings: (
            Callable[[str, str, bool, int, int, int], object] | None
        ) = None,
        on_clear_sync_target_settings: (
            Callable[[str, str], object] | None
        ) = None,
        on_calibrate_role_id: (
            Callable[[str, str, str], object] | None
        ) = None,
        on_read_role_id: (
            Callable[[str, str], object] | None
        ) = None,
        window_size: tuple[int, int] = (
            DEFAULT_FLASH_CLIENT_WIDTH,
            DEFAULT_FLASH_CLIENT_HEIGHT,
        ),
        window_size_auto_enabled: bool = False,
        on_read_main_window_size: (
            Callable[[str], WindowSizeAdjustmentResult] | None
        ) = None,
        on_apply_group_window_size: (
            Callable[[str, int, int], WindowSizeAdjustmentResult] | None
        ) = None,
        on_apply_all_window_size: (
            Callable[[int, int], WindowSizeAdjustmentResult] | None
        ) = None,
        game_time_offset_ms: int = 0,
        game_time_auto_update: bool = True,
        timed_click_target_time: str = "",
        timed_click_lead_ms: int = 120,
        timed_click_repeat_count: int = 2,
        timed_click_repeat_interval_ms: int = 250,
        game_time_snapshot_provider: (
            Callable[[], GameTimeTimedClickSnapshot] | None
        ) = None,
        on_game_time_settings_change: (
            Callable[[int, bool], GameTimeTimedClickSnapshot] | None
        ) = None,
        on_capture_timed_click_target: (
            Callable[[], GameTimeTimedClickResult] | None
        ) = None,
        on_timed_click_change: (
            Callable[
                [bool, str, int, int, int],
                GameTimeTimedClickResult,
            ]
            | None
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
        smart_reconnect_interval_ms: int = 1000,
        on_smart_reconnect_interval_change: (
            Callable[[int], object] | None
        ) = None,
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
        card_preview_choices_provider: (
            Callable[[], tuple[CardPreviewChoice, ...]] | None
        ) = None,
        on_card_preview_select: (
            Callable[[str], CardPreviewSelectionState] | None
        ) = None,
        on_card_preview_clear: (
            Callable[[], CardPreviewSelectionState] | None
        ) = None,
        habit_settings_provider: (
            Callable[[], PlayerHabitSettingsView] | None
        ) = None,
        on_habit_observation_days_update: (
            Callable[[int], PlayerHabitSettingsView] | None
        ) = None,
        on_modify_habit_preference: (
            Callable[[str, tuple[str, ...]], PlayerHabitSettingsView] | None
        ) = None,
        on_remove_habit_preference: (
            Callable[[str], PlayerHabitSettingsView] | None
        ) = None,
        on_clear_habit_preferences: (
            Callable[[], PlayerHabitSettingsView] | None
        ) = None,
        theme_name: str = "clear_blue",
        on_theme_change: Callable[[str], object] | None = None,
        background_image_path: Path | None = None,
        background_fill_color: str = "#C9A35D",
        background_settings: BackgroundSettings | None = None,
        background_for_page: Callable[[str], Path | None] | None = None,
        background_metadata_provider: (
            Callable[[Path | None], BackgroundMetadata | None] | None
        ) = None,
        background_settings_provider: (
            Callable[[], BackgroundSettings] | None
        ) = None,
        on_select_background_image: (
            Callable[[], BackgroundImageResult | None] | None
        ) = None,
        on_choose_background_source: (
            Callable[[], Path | None] | None
        ) = None,
        on_prepare_background_image: (
            Callable[[Path, Callable[[], bool]], BackgroundImageResult]
            | None
        ) = None,
        on_save_background_image: (
            Callable[[Path, bool, tuple[str, ...]], BackgroundImageResult]
            | None
        ) = None,
        on_discard_background_image: Callable[[Path | None], object] | None = None,
        on_clear_background_image: (
            Callable[[], BackgroundImageResult] | None
        ) = None,
        on_clear_page_background: (
            Callable[[str], BackgroundImageResult] | None
        ) = None,
        on_clear_all_backgrounds: (
            Callable[[], BackgroundImageResult] | None
        ) = None,
        on_export_background_settings: (
            Callable[[], Path | None] | None
        ) = None,
        on_import_background_settings: (
            Callable[[], BackgroundImageResult | None] | None
        ) = None,
        on_background_display_settings_update: (
            Callable[[str, int, int, int], BackgroundSettings] | None
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
        known_shortcut_keys = {
            shortcut.key for shortcut in CONFIRMED_GAME_SHORTCUTS
        }
        self.selected_sync_keys = tuple(
            dict.fromkeys(
                key
                for key in selected_sync_keys
                if isinstance(key, str) and key in known_shortcut_keys
            )
        )
        self.on_selected_sync_keys_change = on_selected_sync_keys_change
        self.sync_keys_collapsed = bool(sync_keys_collapsed)
        self.on_sync_keys_collapsed_change = (
            on_sync_keys_collapsed_change
        )
        self.feature_hotkeys = {
            name: normalize_feature_hotkey(
                (feature_hotkeys or {}).get(name)
            )
            for name in ("sync", "reconnect", "auto_click")
        }
        self.on_feature_hotkey_change = on_feature_hotkey_change
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
        self.on_stop_all_managed_games = on_stop_all_managed_games
        self.on_record_group_positions = on_record_group_positions
        self.on_create_group = on_create_group
        self.on_rename_group = on_rename_group
        self.on_delete_group = on_delete_group
        self.on_move_group = on_move_group
        self.on_export_group_configuration = (
            on_export_group_configuration
        )
        self.on_import_group_configuration = (
            on_import_group_configuration
        )
        self.group_launch_hotkey_provider = (
            group_launch_hotkey_provider
        )
        self.on_group_launch_hotkey_change = (
            on_group_launch_hotkey_change
        )
        self.group_entries_provider = group_entries_provider
        self.on_reorder_group_entries = on_reorder_group_entries
        self.group_master_locked_provider = (
            group_master_locked_provider
        )
        self.on_group_master_locked_change = (
            on_group_master_locked_change
        )
        self.on_add_group_shortcuts = on_add_group_shortcuts
        self.on_remove_group_shortcut = on_remove_group_shortcut
        self.on_set_group_main = on_set_group_main
        self.on_clear_group = on_clear_group
        self.on_capture_sync_base_point = on_capture_sync_base_point
        self.on_capture_sync_target_point = on_capture_sync_target_point
        self.on_save_sync_target_settings = (
            on_save_sync_target_settings
        )
        self.on_clear_sync_target_settings = (
            on_clear_sync_target_settings
        )
        self.on_calibrate_role_id = on_calibrate_role_id
        self.on_read_role_id = on_read_role_id
        width, height = window_size
        self.window_size = (
            width
            if isinstance(width, int)
            and MIN_FLASH_CLIENT_SIZE <= width <= MAX_FLASH_CLIENT_SIZE
            else DEFAULT_FLASH_CLIENT_WIDTH,
            height
            if isinstance(height, int)
            and MIN_FLASH_CLIENT_SIZE <= height <= MAX_FLASH_CLIENT_SIZE
            else DEFAULT_FLASH_CLIENT_HEIGHT,
        )
        self.window_size_auto_enabled = bool(window_size_auto_enabled)
        self.on_read_main_window_size = on_read_main_window_size
        self.on_apply_group_window_size = on_apply_group_window_size
        self.on_apply_all_window_size = on_apply_all_window_size
        self.game_time_offset_ms = clamp_time_offset_ms(game_time_offset_ms)
        self.game_time_auto_update = bool(game_time_auto_update)
        self.timed_click_target_time = (
            timed_click_target_time.strip()
            if isinstance(timed_click_target_time, str)
            else ""
        )
        self.timed_click_lead_ms = (
            timed_click_lead_ms
            if isinstance(timed_click_lead_ms, int)
            and 0 <= timed_click_lead_ms <= 5_000
            else 120
        )
        self.timed_click_repeat_count = (
            timed_click_repeat_count
            if isinstance(timed_click_repeat_count, int)
            and 1 <= timed_click_repeat_count <= 10
            else 2
        )
        self.timed_click_repeat_interval_ms = (
            timed_click_repeat_interval_ms
            if isinstance(timed_click_repeat_interval_ms, int)
            and 50 <= timed_click_repeat_interval_ms <= 3_000
            else 250
        )
        self.game_time_snapshot_provider = game_time_snapshot_provider
        self.on_game_time_settings_change = on_game_time_settings_change
        self.on_capture_timed_click_target = on_capture_timed_click_target
        self.on_timed_click_change = on_timed_click_change
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
        self.smart_reconnect_interval_ms = (
            smart_reconnect_interval_ms
            if isinstance(smart_reconnect_interval_ms, int)
            and not isinstance(smart_reconnect_interval_ms, bool)
            and smart_reconnect_interval_ms > 0
            else 1000
        )
        self.on_smart_reconnect_interval_change = (
            on_smart_reconnect_interval_change
        )
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
        self.card_preview_choices_provider = (
            card_preview_choices_provider
        )
        self.on_card_preview_select = on_card_preview_select
        self.on_card_preview_clear = on_card_preview_clear
        self.habit_settings_provider = habit_settings_provider
        self.on_habit_observation_days_update = (
            on_habit_observation_days_update
        )
        self.on_modify_habit_preference = on_modify_habit_preference
        self.on_remove_habit_preference = on_remove_habit_preference
        self.on_clear_habit_preferences = on_clear_habit_preferences
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
        self.background_fill_color = (
            background_fill_color
            if isinstance(background_fill_color, str)
            and len(background_fill_color) == 7
            and background_fill_color.startswith("#")
            else "#C9A35D"
        )
        self.background_settings = background_settings
        self.background_for_page = background_for_page
        self.background_metadata_provider = background_metadata_provider
        self.background_settings_provider = background_settings_provider
        self.on_select_background_image = on_select_background_image
        self.on_choose_background_source = on_choose_background_source
        self.on_prepare_background_image = on_prepare_background_image
        self.on_save_background_image = on_save_background_image
        self.on_discard_background_image = on_discard_background_image
        self.on_clear_background_image = on_clear_background_image
        self.on_clear_page_background = on_clear_page_background
        self.on_clear_all_backgrounds = on_clear_all_backgrounds
        self.on_export_background_settings = on_export_background_settings
        self.on_import_background_settings = on_import_background_settings
        self.on_background_display_settings_update = (
            on_background_display_settings_update
        )
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
        self._habit_observation_days_entry: Entry | None = None
        self._habit_preferences_frame: Frame | None = None
        self._habit_status_label: Label | None = None
        self._keyboard_sync_label: Label | None = None
        self._keyboard_sync_button: Button | None = None
        self._sync_key_variables: dict[str, IntVar] = {}
        self._sync_key_count_label: Label | None = None
        self._sync_key_summary_label: Label | None = None
        self._sync_key_toggle_button: Button | None = None
        self._sync_key_list_frame: Frame | None = None
        self._sync_key_actions_frame: Frame | None = None
        self._feature_hotkey_variables: dict[str, StringVar] = {}
        self._smart_reconnect_label: Label | None = None
        self._smart_reconnect_button: Button | None = None
        self._smart_reconnect_interval_entry: Entry | None = None
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
        self._group_master_lock_button: Button | None = None
        self._group_add_button: Button | None = None
        self._group_clear_button: Button | None = None
        self._group_launch_button: Button | None = None
        self._group_restore_button: Button | None = None
        self._group_record_button: Button | None = None
        self._group_stop_all_button: Button | None = None
        self._group_reorder_button: Button | None = None
        self._group_reorder_finish_button: Button | None = None
        self._group_reorder_cancel_button: Button | None = None
        self._group_reorder_mode = False
        self._group_reorder_original: tuple[str, ...] = ()
        self._group_reorder_working: list[str] = []
        self._group_drag_entry_id: str | None = None
        self._group_launch_running = False
        self._group_launch_status_label: Label | None = None
        self._group_launch_hotkey_variable: StringVar | None = None
        self._window_size_width_entry: Entry | None = None
        self._window_size_height_entry: Entry | None = None
        self._window_size_auto_variable: IntVar | None = None
        self._window_size_status_label: Label | None = None
        self._window_size_after_id: str | None = None
        self._game_time_offset_entry: Entry | None = None
        self._game_time_auto_variable: IntVar | None = None
        self._game_time_value_label: Label | None = None
        self._game_time_after_id: str | None = None
        self._timed_click_target_entry: Entry | None = None
        self._timed_click_lead_entry: Entry | None = None
        self._timed_click_repeat_entry: Entry | None = None
        self._timed_click_interval_entry: Entry | None = None
        self._timed_click_point_label: Label | None = None
        self._timed_click_status_label: Label | None = None
        self._timed_click_toggle_button: Button | None = None
        self._timed_click_capture_after_id: str | None = None
        self._group_name_entry: Entry | None = None
        self._group_sync_choice_variable: StringVar | None = None
        self._group_sync_choice_ids: dict[str, str] = {}
        self._group_sync_relations_frame: Frame | None = None
        self._operation_records_label: Label | None = None
        self._operation_record_files_frame: Frame | None = None
        self._operation_record_date_entry: Entry | None = None
        self._operation_record_role_entry: Entry | None = None
        self._operation_record_search_frame: Frame | None = None
        self._card_preview_variable: StringVar | None = None
        self._card_preview_status_label: Label | None = None
        self._page_canvas: Canvas | None = None
        self._page_canvas_window: int | None = None
        self._theme_variable: StringVar | None = None
        self._background_status_label: Label | None = None
        self._background_sidebar_label: Label | None = None
        self._background_sidebar_photo = None
        self._background_widget_colors: dict[object, str] = {}
        self._background_widget_highlights: dict[
            object,
            tuple[int, str],
        ] = {}
        self._background_choose_button: Button | None = None
        self._background_cancel_button: Button | None = None
        self._background_progress_bar: Progressbar | None = None
        self._background_prepare_cancel = Event()
        self._background_prepare_results: Queue[
            BackgroundImageResult | Exception
        ] = Queue()
        self._background_prepare_poll_id: str | None = None
        self._background_prepare_running = False
        self._background_canvas_item: int | None = None
        self._background_page_labels: dict[str, Label] = {}
        self._background_source_image: Image.Image | None = None
        self._background_loaded_path: Path | None = None
        self._background_photo = None
        self._background_resize_id: str | None = None
        self._background_pending_render_size: tuple[int, int] | None = None
        self._background_render_size: tuple[int, int] | None = None
        self._pending_background_path: Path | None = None
        self._pending_background_result: BackgroundImageResult | None = None
        self._background_apply_all_variable: IntVar | None = None
        self._background_page_variables: dict[str, IntVar] = {}
        self._background_preview_page_variable: StringVar | None = None
        self._background_preview_active = False
        self._background_fill_entry: Entry | None = None
        self._background_opacity_scales: dict[str, Scale] = {}
        self._saved_background_display_values = {
            "fill_color": (
                background_settings.fill_color
                if background_settings is not None
                else self.background_fill_color
            ),
            "sidebar": (
                background_settings.sidebar_opacity
                if background_settings is not None
                else DEFAULT_BACKGROUND_OPACITY["sidebar"]
            ),
            "panel": (
                background_settings.panel_opacity
                if background_settings is not None
                else DEFAULT_BACKGROUND_OPACITY["panel"]
            ),
            "role_row": (
                background_settings.role_row_opacity
                if background_settings is not None
                else DEFAULT_BACKGROUND_OPACITY["role_row"]
            ),
        }

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
        self._cancel_window_size_poll()
        self._cancel_game_time_tick()
        self._cancel_timed_click_capture()
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
        self._background_widget_colors.clear()
        self._background_widget_highlights.clear()
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
        self._background_sidebar_label = Label(
            sidebar,
            bg=SIDEBAR,
            bd=0,
            highlightthickness=0,
            anchor="nw",
        )
        self._background_sidebar_label.place(
            x=0,
            y=0,
            relwidth=1,
            relheight=1,
        )
        self._background_sidebar_label.lower()
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
        self._background_sidebar_photo = None
        self._background_render_size = None
        self._set_background_path(self.background_image_path)
        content.bind("<Configure>", self._sync_page_scroll_region)
        canvas.bind("<Configure>", self._resize_page_content)
        self._mousewheel_binding_id = self.parent.bind(
            "<MouseWheel>",
            self._on_page_mousewheel,
            add="+",
        )

        page_specs = (
            ("home", "首頁"),
            ("groups", "目前組別"),
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
        if self.window_size_auto_enabled:
            self._schedule_window_size_poll()
        self._schedule_game_time_tick()
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
        self._background_pending_render_size = None

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
        self._background_pending_render_size = requested_size
        if self._background_resize_id is not None:
            return
        self._background_resize_id = self.parent.after(
            33,
            self._render_pending_background,
        )

    def _render_pending_background(self) -> None:
        self._background_resize_id = None
        requested_size = self._background_pending_render_size
        self._background_pending_render_size = None
        if requested_size is not None:
            self._render_background(requested_size)

    def _render_background(self, viewport_size: tuple[int, int]) -> None:
        self._background_resize_id = None
        canvas = self._page_canvas
        item = self._background_canvas_item
        source = self._background_source_image
        if canvas is None or item is None or source is None:
            return
        width, height = viewport_size
        try:
            root_width = max(
                width + 176,
                int(self._root.winfo_width()) if self._root is not None else 0,
            )
            root_height = max(
                height,
                int(self._root.winfo_height()) if self._root is not None else 0,
            )
            resized_width, resized_height, offset_x, offset_y = _contain_geometry(
                source.size,
                (root_width, root_height),
            )
            resized = source.resize(
                (resized_width, resized_height),
                Image.Resampling.LANCZOS,
            )
            try:
                display = Image.new(
                    "RGB",
                    (root_width, root_height),
                    self.background_fill_color,
                )
                try:
                    display.paste(
                        resized,
                        (offset_x, offset_y),
                        resized if resized.mode == "RGBA" else None,
                    )
                    sidebar_width = min(176, root_width)
                    content_left = max(0, root_width - width)
                    sidebar_image = display.crop(
                        (0, 0, sidebar_width, root_height)
                    )
                    content_image = display.crop(
                        (
                            content_left,
                            0,
                            min(root_width, content_left + width),
                            min(root_height, height),
                        )
                    )
                    try:
                        try:
                            values = self._background_display_values()
                        except (TypeError, ValueError):
                            values = self._saved_background_display_values
                        sidebar_tint = Image.new(
                            "RGB",
                            sidebar_image.size,
                            SIDEBAR,
                        )
                        panel_tint = Image.new(
                            "RGB",
                            content_image.size,
                            BACKGROUND,
                        )
                        try:
                            sidebar_display = Image.blend(
                                sidebar_image,
                                sidebar_tint,
                                int(values["sidebar"]) / 100,
                            )
                            panel_display = Image.blend(
                                content_image,
                                panel_tint,
                                int(values["panel"]) / 100,
                            )
                            try:
                                photo = ImageTk.PhotoImage(
                                    panel_display,
                                    master=self.parent,
                                )
                                sidebar_photo = ImageTk.PhotoImage(
                                    sidebar_display,
                                    master=self.parent,
                                )
                                panel_average = _average_image_color(
                                    content_image
                                )
                                sidebar_average = _average_image_color(
                                    sidebar_image
                                )
                            finally:
                                sidebar_display.close()
                                panel_display.close()
                        finally:
                            sidebar_tint.close()
                            panel_tint.close()
                    finally:
                        sidebar_image.close()
                        content_image.close()
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
            if self._background_sidebar_label is not None:
                self._background_sidebar_label.configure(
                    image=sidebar_photo
                )
                self._background_sidebar_label.lower()
            self._background_photo = photo
            self._background_sidebar_photo = sidebar_photo
            self._background_render_size = viewport_size
            self._apply_background_widget_colors(
                sidebar_average,
                panel_average,
                int(values["sidebar"]),
                int(values["panel"]),
                int(values["role_row"]),
            )
        except Exception:
            canvas.itemconfigure(item, image="", state="hidden")
            if self._background_sidebar_label is not None:
                self._background_sidebar_label.configure(image="")
            self._background_photo = None
            self._background_sidebar_photo = None
            self._background_render_size = None
            self._restore_background_widget_colors()
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
        self._background_sidebar_photo = None
        self._background_render_size = None

        canvas = self._page_canvas
        item = self._background_canvas_item
        if normalized is None:
            if canvas is not None and item is not None:
                canvas.itemconfigure(item, image="", state="hidden")
            for label in self._background_page_labels.values():
                label.configure(image="")
            if self._background_sidebar_label is not None:
                self._background_sidebar_label.configure(image="")
            self._restore_background_widget_colors()
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
            if self._background_sidebar_label is not None:
                self._background_sidebar_label.configure(image="")
            self._restore_background_widget_colors()
            return False
        self._background_loaded_path = normalized
        if canvas is not None:
            self._schedule_background_resize(
                max(1, int(canvas.winfo_width())),
                max(1, int(canvas.winfo_height())),
            )
        return True

    def _remember_and_blend_widget_tree(
        self,
        widget,
        backdrop: str,
        opacity: int,
    ) -> None:
        try:
            current = str(widget.cget("background"))
        except Exception:
            current = ""
        if (
            len(current) == 7
            and current.startswith("#")
        ):
            base = self._background_widget_colors.setdefault(
                widget,
                current,
            )
            try:
                blended = _blend_hex_color(
                    base,
                    backdrop,
                    opacity,
                )
                widget.configure(background=blended)
                try:
                    foreground = str(widget.cget("foreground"))
                    widget_class = str(widget.winfo_class())
                except Exception:
                    foreground = ""
                    widget_class = ""
                if (
                    widget_class == "Label"
                    and len(foreground) == 7
                    and foreground.startswith("#")
                ):
                    original_highlight = (
                        int(widget.cget("highlightthickness")),
                        str(widget.cget("highlightbackground")),
                    )
                    self._background_widget_highlights.setdefault(
                        widget,
                        original_highlight,
                    )
                    if _contrast_ratio(foreground, blended) < 3:
                        widget.configure(
                            highlightthickness=1,
                            highlightbackground=(
                                "#FFFFFF"
                                if _contrast_ratio(
                                    foreground,
                                    "#FFFFFF",
                                )
                                > _contrast_ratio(
                                    foreground,
                                    "#000000",
                                )
                                else "#000000"
                            ),
                        )
                    else:
                        widget.configure(
                            highlightthickness=original_highlight[0],
                            highlightbackground=original_highlight[1],
                        )
            except Exception:
                pass
        try:
            children = widget.winfo_children()
        except Exception:
            children = ()
        for child in children:
            self._remember_and_blend_widget_tree(
                child,
                backdrop,
                opacity,
            )

    def _apply_background_widget_colors(
        self,
        sidebar_backdrop: str,
        panel_backdrop: str,
        sidebar_opacity: int,
        panel_opacity: int,
        role_row_opacity: int,
    ) -> None:
        if self._background_sidebar_label is not None:
            sidebar = self._background_sidebar_label.master
            self._remember_and_blend_widget_tree(
                sidebar,
                sidebar_backdrop,
                sidebar_opacity,
            )
        for page in self._pages.values():
            self._remember_and_blend_widget_tree(
                page,
                panel_backdrop,
                panel_opacity,
            )
        if self._home_role_rows_frame is not None:
            self._remember_and_blend_widget_tree(
                self._home_role_rows_frame,
                panel_backdrop,
                role_row_opacity,
            )

    def _restore_background_widget_colors(self) -> None:
        for widget, color in tuple(self._background_widget_colors.items()):
            try:
                widget.configure(background=color)
            except Exception:
                pass
        for widget, (thickness, color) in tuple(
            self._background_widget_highlights.items()
        ):
            try:
                widget.configure(
                    highlightthickness=thickness,
                    highlightbackground=color,
                )
            except Exception:
                pass

    def _background_status_text(self, message: str = "") -> str:
        pending = self._pending_background_result
        if pending is not None and pending.managed_path is not None:
            size = (
                f"{pending.original_size[0]}×{pending.original_size[1]}"
                if pending.original_size is not None
                else "未知"
            )
            status = (
                f"預覽圖片：{pending.original_name or pending.managed_path.name}\n"
                f"原始尺寸：{size}\n"
                f"最後更新：{pending.updated_at or '尚未儲存'}"
            )
        elif self.background_image_path is None:
            status = "目前背景：未設定，沿用介面配色。"
        elif self._background_source_image is None:
            status = "目前背景：受管背景副本無法顯示。"
        else:
            metadata = (
                self.background_metadata_provider(self.background_image_path)
                if self.background_metadata_provider is not None
                else None
            )
            if metadata is None:
                status = "目前背景：已套用受管背景副本。"
            else:
                applied_page_names: list[str] = []
                if self.background_settings is not None:
                    if (
                        self.background_settings.global_path
                        == self.background_image_path
                    ):
                        applied_page_names.append("全部頁面")
                    applied_page_names.extend(
                        BACKGROUND_PAGE_LABELS.get(page, page)
                        for page, path in self.background_settings.page_paths
                        if path == self.background_image_path
                    )
                applied_pages = (
                    "、".join(dict.fromkeys(applied_page_names))
                    or BACKGROUND_PAGE_LABELS.get(
                        self._active_page,
                        self._active_page,
                    )
                )
                status = (
                    f"目前圖片：{metadata.original_name}\n"
                    f"原始尺寸：{metadata.original_size[0]}×"
                    f"{metadata.original_size[1]}\n"
                    f"套用頁面：{applied_pages}\n"
                    f"最後更新：{metadata.updated_at}"
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
            text="目前組別",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X)
        self._target_label = Label(
            target_card,
            text=self._current_group_summary_text(),
            font=("Microsoft JhengHei UI", 11),
            bg=SURFACE,
            fg=TEXT,
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

    def _current_group_summary_text(self) -> str:
        group_name = self.current_group_name
        if not group_name:
            return "尚未選擇組別"
        entries: tuple[GroupConfigurationEntry, ...] = ()
        if self.group_entries_provider is not None:
            try:
                entries = self.group_entries_provider(group_name)
            except Exception:
                entries = ()
        if not entries:
            return f"{group_name}｜尚未加入角色"
        return (
            f"{group_name}｜主控：{entries[0].display_name}｜"
            f"{len(entries)} 個視窗"
        )

    def refresh_current_group_summary(self) -> str:
        text = self._current_group_summary_text()
        if self._target_label is not None:
            self._target_label.configure(text=text, fg=TEXT)
        return text

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
            "目前組別",
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
            "恢復上次位置",
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
        self._group_stop_all_button = self._button(
            launch_row,
            "停止全部受管遊戲",
            self._stop_all_managed_games,
        )
        self._group_stop_all_button.configure(
            bg=WARNING,
            fg="#FFFFFF",
            activebackground=WARNING,
            activeforeground="#FFFFFF",
        )
        self._group_stop_all_button.pack(side=LEFT, padx=(8, 0))
        if self.on_stop_all_managed_games is None:
            self._group_stop_all_button.configure(state=DISABLED)
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
        hotkey_row = Frame(selector, bg=SURFACE)
        hotkey_row.pack(fill=X, pady=(10, 0))
        Label(
            hotkey_row,
            text="整組啟動快捷鍵",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)
        current_group_hotkey = (
            self.group_launch_hotkey_provider(
                self.current_group_name
            )
            if self.current_group_name is not None
            and self.group_launch_hotkey_provider is not None
            else ""
        )
        self._group_launch_hotkey_variable = StringVar(
            master=self.parent,
            value=current_group_hotkey or "未設定",
        )
        group_hotkey_menu = OptionMenu(
            hotkey_row,
            self._group_launch_hotkey_variable,
            "未設定",
            *(value for value in FEATURE_HOTKEYS if value),
            command=self._change_group_launch_hotkey,
        )
        group_hotkey_menu.configure(
            font=("Microsoft JhengHei UI", 9),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        group_hotkey_menu.pack(side=LEFT, padx=(8, 0))
        if (
            self.current_group_name is None
            or self.on_group_launch_hotkey_change is None
        ):
            group_hotkey_menu.configure(state=DISABLED)
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
        self._button(
            group_order_row,
            "匯出組別設定",
            self._export_group_configuration,
        ).pack(side=LEFT, padx=(16, 0))
        self._button(
            group_order_row,
            "匯入組別設定",
            self._import_group_configuration,
        ).pack(side=LEFT, padx=(8, 0))
        Label(
            selector,
            text="匯入時同名組別會直接更新；舊版設定保持不變。",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(8, 0))

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
        self._group_add_button = self._button(
            entry_header,
            "加入角色到組別",
            self._add_shortcuts_to_current_group,
            primary=True,
        )
        self._group_add_button.pack(side=RIGHT)
        self._group_reorder_button = None
        self._group_reorder_finish_button = None
        self._group_reorder_cancel_button = None
        if self._group_reorder_mode:
            self._group_reorder_finish_button = self._button(
                entry_header,
                "完成排序",
                self._finish_group_entry_reorder,
                primary=True,
            )
            self._group_reorder_finish_button.pack(side=RIGHT, padx=(0, 8))
            self._group_reorder_cancel_button = self._button(
                entry_header,
                "取消",
                self._cancel_group_entry_reorder,
            )
            self._group_reorder_cancel_button.pack(side=RIGHT, padx=(0, 8))
        else:
            self._group_reorder_button = self._button(
                entry_header,
                "調整順序",
                self._start_group_entry_reorder,
            )
            self._group_reorder_button.pack(side=RIGHT, padx=(0, 8))
        self._group_clear_button = self._button(
            entry_header,
            "清空角色",
            self._clear_current_group,
        )
        self._group_clear_button.pack(side=RIGHT, padx=(0, 8))
        self._group_master_lock_button = self._button(
            entry_header,
            "主窗：已上鎖",
            self._toggle_group_master_locked,
        )
        self._group_master_lock_button.pack(side=RIGHT, padx=(0, 8))
        sync_base_button = self._button(
            entry_header,
            "設定主基準點（3秒）",
            self._start_sync_base_point_capture,
        )
        sync_base_button.pack(side=RIGHT, padx=(0, 8))
        if self.on_capture_sync_base_point is None:
            sync_base_button.configure(state=DISABLED)
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

        self._build_window_size_card(page)

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

    def _build_window_size_card(self, page) -> None:
        card = self._card(page)
        card.pack(fill=X, pady=(14, 0))
        Label(
            card,
            text="還原／調整遊戲視窗尺寸",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            card,
            text="沿用舊版操作；尺寸是遊戲內容區，不會移動或切換視窗",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(3, 10))

        values = Frame(card, bg=SURFACE)
        values.pack(fill=X)
        Label(
            values,
            text="寬",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
        ).pack(side=LEFT)
        self._window_size_width_entry = Entry(
            values,
            width=7,
            font=("Microsoft JhengHei UI", 10),
            relief="flat",
            bd=0,
        )
        self._window_size_width_entry.pack(side=LEFT, padx=(6, 14))
        self._window_size_width_entry.insert(0, str(self.window_size[0]))
        Label(
            values,
            text="高",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
        ).pack(side=LEFT)
        self._window_size_height_entry = Entry(
            values,
            width=7,
            font=("Microsoft JhengHei UI", 10),
            relief="flat",
            bd=0,
        )
        self._window_size_height_entry.pack(side=LEFT, padx=(6, 14))
        self._window_size_height_entry.insert(0, str(self.window_size[1]))
        self._button(
            values,
            "取主窗尺寸",
            self._load_main_window_size,
        ).pack(side=LEFT)
        self._button(
            values,
            "套用目前組",
            self._apply_current_group_window_size,
            primary=True,
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            values,
            "套用全部遊戲視窗",
            self._apply_all_game_window_size,
        ).pack(side=LEFT, padx=(8, 0))

        auto_row = Frame(card, bg=SURFACE)
        auto_row.pack(fill=X, pady=(10, 0))
        self._window_size_auto_variable = IntVar(
            master=self.parent,
            value=1 if self.window_size_auto_enabled else 0,
        )
        Checkbutton(
            auto_row,
            text="新視窗自動套用",
            variable=self._window_size_auto_variable,
            command=self._toggle_auto_window_size,
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
            activebackground=SURFACE,
            selectcolor=BACKGROUND,
        ).pack(side=LEFT)
        self._window_size_status_label = Label(
            auto_row,
            text=(
                "● 已啟用"
                if self.window_size_auto_enabled
                else "● 尚未啟用"
            ),
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=PRIMARY if self.window_size_auto_enabled else MUTED,
            anchor="w",
        )
        self._window_size_status_label.pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=(12, 0),
        )

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
        shortcut_summary = Frame(
            input_card,
            bg=SURFACE,
        )
        shortcut_summary.pack(fill=X, pady=(12, 0))
        shortcut_heading = Frame(shortcut_summary, bg=SURFACE)
        shortcut_heading.pack(fill=X)
        Label(
            shortcut_heading,
            text="勾選要同步的遊戲按鍵",
            font=("Microsoft JhengHei UI", 10, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(side=LEFT)
        self._sync_key_toggle_button = self._button(
            shortcut_heading,
            "",
            self._toggle_sync_key_settings,
        )
        self._sync_key_toggle_button.pack(side=RIGHT)
        self._sync_key_count_label = Label(
            shortcut_summary,
            text="",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        )
        self._sync_key_count_label.pack(fill=X, pady=(4, 0))
        self._sync_key_summary_label = Label(
            shortcut_summary,
            text="",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            justify=LEFT,
            wraplength=720,
        )
        self._sync_key_summary_label.pack(fill=X, pady=(2, 0))
        shortcut_list = Frame(input_card, bg=BACKGROUND, padx=10, pady=8)
        self._sync_key_list_frame = shortcut_list
        self._sync_key_variables = {}
        for shortcut in CONFIRMED_GAME_SHORTCUTS:
            variable = IntVar(
                master=self.parent,
                value=1 if shortcut.key in self.selected_sync_keys else 0,
            )
            self._sync_key_variables[shortcut.key] = variable
            Checkbutton(
                shortcut_list,
                text=shortcut.player_label,
                variable=variable,
                command=self._sync_key_selection_changed,
                font=("Microsoft JhengHei UI", 9),
                bg=BACKGROUND,
                fg=TEXT,
                activebackground=BACKGROUND,
                selectcolor=SURFACE,
                anchor="w",
                justify=LEFT,
            ).pack(fill=X, anchor="w")

        actions = Frame(input_card, bg=SURFACE)
        self._sync_key_actions_frame = actions
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
        self._build_feature_hotkey_selector(
            input_card,
            "sync",
            "同步啟閉快捷鍵",
        )
        self._apply_sync_key_collapsed_state()
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
        interval_row = Frame(reconnect_card, bg=SURFACE)
        interval_row.pack(fill=X, pady=(10, 0))
        Label(
            interval_row,
            text="監看間隔毫秒",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=TEXT,
        ).pack(side=LEFT)
        self._smart_reconnect_interval_entry = Entry(
            interval_row,
            width=10,
            font=("Microsoft JhengHei UI", 10),
            bg="#F8FAFD",
            fg=TEXT,
            relief="flat",
        )
        self._smart_reconnect_interval_entry.insert(
            0,
            str(self.smart_reconnect_interval_ms),
        )
        self._smart_reconnect_interval_entry.pack(
            side=LEFT,
            padx=(8, 8),
            ipady=5,
        )
        self._button(
            interval_row,
            "保存間隔",
            self._save_smart_reconnect_interval,
        ).pack(side=LEFT)
        self._build_feature_hotkey_selector(
            reconnect_card,
            "reconnect",
            "智慧重連啟閉快捷鍵",
        )
        Label(
            reconnect_card,
            text="啟用後會自動判定與重試；不需逐次按重連按鈕。",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(8, 0))
        self._refresh_smart_reconnect_controls()

        self._build_game_time_card(page)
        self._build_timed_click_card(page)

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
        self._build_feature_hotkey_selector(
            auto_click_card,
            "auto_click",
            "連續點擊啟閉快捷鍵",
        )
        self._refresh_auto_click_controls()
        return page

    def _build_game_time_card(self, page) -> None:
        card = self._card(page)
        card.pack(fill=X, pady=(14, 0))
        Label(
            card,
            text="遊戲時間",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        row = Frame(card, bg=SURFACE)
        row.pack(fill=X, pady=(10, 0))
        Label(
            row,
            text="時間來源：系統時間",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
        ).pack(side=LEFT, padx=(0, 12))
        Label(
            row,
            text="偏移ms",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)
        self._game_time_offset_entry = Entry(
            row,
            width=8,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._game_time_offset_entry.insert(0, str(self.game_time_offset_ms))
        self._game_time_offset_entry.pack(side=LEFT, padx=(6, 12), ipady=5)
        self._game_time_offset_entry.bind(
            "<FocusOut>",
            lambda _event: self._apply_game_time_settings(),
        )
        self._game_time_offset_entry.bind(
            "<Return>",
            lambda _event: self._apply_game_time_settings(),
        )
        self._game_time_auto_variable = IntVar(
            master=self.parent,
            value=1 if self.game_time_auto_update else 0,
        )
        Checkbutton(
            row,
            text="自動更新",
            variable=self._game_time_auto_variable,
            command=self._apply_game_time_settings,
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
            activebackground=SURFACE,
            selectcolor=BACKGROUND,
        ).pack(side=LEFT, padx=(0, 12))
        self._game_time_value_label = Label(
            row,
            text="遊戲時間：讀取中",
            font=("Microsoft JhengHei UI", 10, "bold"),
            bg=SURFACE,
            fg=PRIMARY,
            anchor="w",
        )
        self._game_time_value_label.pack(side=LEFT, fill=X, expand=True)
        Label(
            card,
            text=(
                f"偏移可設定 {MIN_TIME_OFFSET_MS}～{MAX_TIME_OFFSET_MS} 毫秒；"
                "不讀取遊戲畫面或記憶體。"
            ),
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(8, 0))

    def _build_timed_click_card(self, page) -> None:
        card = self._card(page)
        card.pack(fill=X, pady=(14, 0))
        Label(
            card,
            text="定時按下",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        row = Frame(card, bg=SURFACE)
        row.pack(fill=X, pady=(10, 0))
        Label(
            row,
            text="目標時間",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)
        self._timed_click_target_entry = Entry(
            row,
            width=14,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._timed_click_target_entry.insert(0, self.timed_click_target_time)
        self._timed_click_target_entry.pack(
            side=LEFT,
            padx=(6, 12),
            ipady=5,
        )
        for label_text, attribute, initial, width in (
            ("提前ms", "_timed_click_lead_entry", self.timed_click_lead_ms, 7),
            ("連點", "_timed_click_repeat_entry", self.timed_click_repeat_count, 5),
            (
                "間隔ms",
                "_timed_click_interval_entry",
                self.timed_click_repeat_interval_ms,
                7,
            ),
        ):
            Label(
                row,
                text=label_text,
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=MUTED,
            ).pack(side=LEFT)
            entry = Entry(
                row,
                width=width,
                font=("Microsoft JhengHei UI", 10),
                bg=BACKGROUND,
                fg=TEXT,
                relief="flat",
                bd=0,
            )
            entry.insert(0, str(initial))
            entry.pack(side=LEFT, padx=(6, 12), ipady=5)
            setattr(self, attribute, entry)
        actions = Frame(card, bg=SURFACE)
        actions.pack(fill=X, pady=(10, 0))
        self._button(
            actions,
            "設定按鈕位置",
            self._capture_timed_click_target,
        ).pack(side=LEFT)
        self._timed_click_toggle_button = self._button(
            actions,
            "啟用定時",
            self._toggle_timed_click,
            primary=True,
        )
        self._timed_click_toggle_button.pack(side=LEFT, padx=(8, 0))
        self._button(
            actions,
            "取消",
            self._cancel_timed_click,
        ).pack(side=LEFT, padx=(8, 0))
        self._timed_click_point_label = Label(
            actions,
            text="按鈕位置：未設定",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        )
        self._timed_click_point_label.pack(side=LEFT, padx=(12, 0))
        self._timed_click_status_label = Label(
            card,
            text="● 定時按下：未啟用",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        )
        self._timed_click_status_label.pack(fill=X, pady=(8, 0))
        Label(
            card,
            text=(
                "設定位置後才可啟用；只操作該唯一角色視窗，"
                "不切換、不啟用、不移動其他視窗。"
            ),
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        ).pack(fill=X, pady=(4, 0))

    def _build_feature_hotkey_selector(
        self,
        parent,
        feature: str,
        title: str,
    ) -> None:
        row = Frame(parent, bg=SURFACE)
        row.pack(fill=X, pady=(10, 0))
        Label(
            row,
            text=title,
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
        ).pack(side=LEFT)
        display_values = ("未設定",) + tuple(
            value for value in FEATURE_HOTKEYS if value
        )
        current = self.feature_hotkeys.get(feature, "")
        variable = StringVar(
            master=self.parent,
            value=current or "未設定",
        )
        self._feature_hotkey_variables[feature] = variable

        def changed(value: str) -> None:
            normalized = normalize_feature_hotkey(
                "" if value == "未設定" else value
            )
            previous = self.feature_hotkeys.get(feature, "")
            if self.on_feature_hotkey_change is not None:
                accepted = self.on_feature_hotkey_change(
                    feature,
                    normalized,
                )
                if accepted is False:
                    variable.set(previous or "未設定")
                    return
            self.feature_hotkeys[feature] = normalized

        menu = OptionMenu(row, variable, *display_values, command=changed)
        menu.configure(
            font=("Microsoft JhengHei UI", 9),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        menu.pack(side=LEFT, padx=(8, 0))

    def _sync_key_selection_changed(self) -> None:
        selected = tuple(
            shortcut.key
            for shortcut in CONFIRMED_GAME_SHORTCUTS
            if (
                shortcut.key in self._sync_key_variables
                and self._sync_key_variables[shortcut.key].get()
            )
        )
        previous = self.selected_sync_keys
        if self.on_selected_sync_keys_change is not None:
            accepted = self.on_selected_sync_keys_change(selected)
            if accepted is False:
                for key, variable in self._sync_key_variables.items():
                    variable.set(1 if key in previous else 0)
                return
        self.selected_sync_keys = selected
        self._refresh_keyboard_sync_controls()

    def _toggle_sync_key_settings(self) -> None:
        desired = not self.sync_keys_collapsed
        if self.on_sync_keys_collapsed_change is not None:
            accepted = self.on_sync_keys_collapsed_change(desired)
            if accepted is False:
                return
        self.sync_keys_collapsed = desired
        self._apply_sync_key_collapsed_state()

    def _apply_sync_key_collapsed_state(self) -> None:
        if self._sync_key_list_frame is not None:
            if self.sync_keys_collapsed:
                self._sync_key_list_frame.pack_forget()
            elif self._sync_key_actions_frame is not None:
                self._sync_key_list_frame.pack(
                    fill=X,
                    pady=(6, 0),
                    before=self._sync_key_actions_frame,
                )
        if self._sync_key_toggle_button is not None:
            self._sync_key_toggle_button.configure(
                text=(
                    "展開設定"
                    if self.sync_keys_collapsed
                    else "收合設定"
                )
            )

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
        if self._sync_key_count_label is not None:
            self._sync_key_count_label.configure(
                text=(
                    f"已啟用 {len(self.selected_sync_keys)}／"
                    f"{len(CONFIRMED_GAME_SHORTCUTS)} 個按鍵"
                )
            )
        if self._sync_key_summary_label is not None:
            self._sync_key_summary_label.configure(
                text=(
                    "已勾選："
                    f"{_selected_sync_key_summary(self.selected_sync_keys)}"
                )
            )

    def set_keyboard_sync_enabled(self, enabled: bool) -> None:
        self.keyboard_sync_enabled = bool(enabled)
        self._refresh_keyboard_sync_controls()

    def toggle_keyboard_sync_from_hotkey(self) -> None:
        self._toggle_keyboard_sync()

    def set_smart_reconnect_enabled(self, enabled: bool) -> None:
        self.smart_reconnect_enabled = bool(enabled)
        self._refresh_smart_reconnect_controls()

    def toggle_smart_reconnect_from_hotkey(self) -> None:
        self._toggle_smart_reconnect()

    def _save_smart_reconnect_interval(self) -> None:
        entry = self._smart_reconnect_interval_entry
        if entry is None:
            return
        try:
            interval_ms = int(entry.get().strip())
        except ValueError:
            interval_ms = 0
        if interval_ms <= 0:
            messagebox.showerror(
                "輔｜智慧重連",
                "監看間隔必須是大於 0 的毫秒整數。",
                parent=self.parent,
            )
            return
        if self.on_smart_reconnect_interval_change is None:
            return
        if self.on_smart_reconnect_interval_change(interval_ms) is False:
            return
        self.smart_reconnect_interval_ms = interval_ms
        entry.delete(0, "end")
        entry.insert(0, str(interval_ms))

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

    def _cancel_game_time_tick(self) -> None:
        if self._game_time_after_id is None:
            return
        try:
            self.parent.after_cancel(self._game_time_after_id)
        except Exception:
            pass
        self._game_time_after_id = None

    def _schedule_game_time_tick(self) -> None:
        if self._game_time_after_id is not None:
            return
        self._game_time_after_id = self.parent.after(
            50,
            self._poll_game_time,
        )

    def _poll_game_time(self) -> None:
        self._game_time_after_id = None
        snapshot = None
        if self.game_time_snapshot_provider is not None:
            try:
                snapshot = self.game_time_snapshot_provider()
            except Exception as error:
                self._report_refresh_error(error)
        if (
            isinstance(snapshot, GameTimeTimedClickSnapshot)
            and self._game_time_value_label is not None
        ):
            self.game_time_offset_ms = snapshot.offset_ms
            self.game_time_auto_update = snapshot.auto_update
            self._game_time_value_label.configure(
                text=(
                    f"遊戲時間：{snapshot.current_time_text}"
                    if snapshot.auto_update
                    else "遊戲時間：自動更新已關閉"
                ),
                fg=PRIMARY if snapshot.auto_update else MUTED,
            )
        self._schedule_game_time_tick()

    def _apply_game_time_settings(self) -> None:
        value = (
            self._game_time_offset_entry.get().strip()
            if self._game_time_offset_entry is not None
            else str(self.game_time_offset_ms)
        )
        offset = clamp_time_offset_ms(value)
        auto_update = bool(
            self._game_time_auto_variable is not None
            and self._game_time_auto_variable.get()
        )
        self.game_time_offset_ms = offset
        self.game_time_auto_update = auto_update
        if self._game_time_offset_entry is not None:
            self._game_time_offset_entry.delete(0, "end")
            self._game_time_offset_entry.insert(0, str(offset))
        if self.on_game_time_settings_change is None:
            return
        try:
            self.on_game_time_settings_change(offset, auto_update)
        except Exception as error:
            self._report_refresh_error(error)

    def _timed_click_values(self) -> tuple[str, int, int, int]:
        if (
            self._timed_click_target_entry is None
            or self._timed_click_lead_entry is None
            or self._timed_click_repeat_entry is None
            or self._timed_click_interval_entry is None
        ):
            raise RuntimeError("timed click controls are unavailable")
        target = self._timed_click_target_entry.get().strip()
        try:
            lead = int(self._timed_click_lead_entry.get().strip())
            repeats = int(self._timed_click_repeat_entry.get().strip())
            interval = int(self._timed_click_interval_entry.get().strip())
        except ValueError as error:
            raise ValueError("定時按下的數值設定必須是整數。") from error
        return target, lead, repeats, interval

    def set_timed_click_result(
        self,
        result: GameTimeTimedClickResult,
    ) -> None:
        if not isinstance(result, GameTimeTimedClickResult):
            raise TypeError(
                "timed click callback must return GameTimeTimedClickResult"
            )
        snapshot = result.snapshot
        if snapshot is not None:
            self.game_time_offset_ms = snapshot.offset_ms
            self.game_time_auto_update = snapshot.auto_update
            self.timed_click_lead_ms = snapshot.lead_ms
            self.timed_click_repeat_count = snapshot.repeat_count
            self.timed_click_repeat_interval_ms = snapshot.repeat_interval_ms
            if self._game_time_auto_variable is not None:
                self._game_time_auto_variable.set(
                    1 if snapshot.auto_update else 0
                )
            if self._timed_click_toggle_button is not None:
                self._timed_click_toggle_button.configure(
                    text="取消定時" if snapshot.enabled else "啟用定時",
                    bg=WARNING if snapshot.enabled else PRIMARY,
                )
            if self._timed_click_point_label is not None:
                target = snapshot.target
                self._timed_click_point_label.configure(
                    text=(
                        "按鈕位置：未設定"
                        if target is None
                        else "按鈕位置："
                        + (target.display_name or "目前角色")
                        + " 已設定"
                    ),
                    fg=PRIMARY if target is not None else MUTED,
                )
        if self._timed_click_status_label is not None:
            self._timed_click_status_label.configure(
                text="● " + result.message,
                fg=PRIMARY if result.success else WARNING,
            )

    def _cancel_timed_click_capture(self) -> None:
        if self._timed_click_capture_after_id is None:
            return
        try:
            self.parent.after_cancel(self._timed_click_capture_after_id)
        except Exception:
            pass
        self._timed_click_capture_after_id = None

    def _capture_timed_click_target(self) -> None:
        if self.on_capture_timed_click_target is None:
            return
        self._cancel_timed_click_capture()
        if self._timed_click_status_label is not None:
            self._timed_click_status_label.configure(
                text="● 請把滑鼠移到按鈕上，3 秒後抓取。",
                fg=PRIMARY,
            )
        self._timed_click_capture_after_id = self.parent.after(
            3_000,
            self._finish_timed_click_capture,
        )

    def _finish_timed_click_capture(self) -> None:
        self._timed_click_capture_after_id = None
        if self.on_capture_timed_click_target is None:
            return
        try:
            self.set_timed_click_result(
                self.on_capture_timed_click_target()
            )
        except Exception as error:
            self._report_refresh_error(error)

    def _toggle_timed_click(self) -> None:
        if self.on_timed_click_change is None:
            return
        snapshot = (
            self.game_time_snapshot_provider()
            if self.game_time_snapshot_provider is not None
            else None
        )
        if isinstance(snapshot, GameTimeTimedClickSnapshot) and snapshot.enabled:
            self._cancel_timed_click()
            return
        try:
            target, lead, repeats, interval = self._timed_click_values()
            result = self.on_timed_click_change(
                True,
                target,
                lead,
                repeats,
                interval,
            )
            self.set_timed_click_result(result)
        except Exception as error:
            if self._timed_click_status_label is not None:
                self._timed_click_status_label.configure(
                    text=f"● {error}",
                    fg=WARNING,
                )

    def _cancel_timed_click(self) -> None:
        self._cancel_timed_click_capture()
        if self.on_timed_click_change is None:
            return
        try:
            target, lead, repeats, interval = self._timed_click_values()
        except Exception:
            target = ""
            lead = self.timed_click_lead_ms
            repeats = self.timed_click_repeat_count
            interval = self.timed_click_repeat_interval_ms
        try:
            self.set_timed_click_result(
                self.on_timed_click_change(
                    False,
                    target,
                    lead,
                    repeats,
                    interval,
                )
            )
        except Exception as error:
            self._report_refresh_error(error)

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

    def set_character_data(
        self,
        characters: Iterable[PlayerCharacterView],
        character_choices: Iterable[PlayerCharacterDetailChoice],
    ) -> None:
        updated_characters = tuple(characters)
        updated_choices = tuple(character_choices)
        if any(
            not isinstance(character, PlayerCharacterView)
            for character in updated_characters
        ):
            raise TypeError(
                "characters must contain PlayerCharacterView values."
            )
        if any(
            not isinstance(choice, PlayerCharacterDetailChoice)
            for choice in updated_choices
        ):
            raise TypeError(
                "character_choices must contain "
                "PlayerCharacterDetailChoice values."
            )
        self.characters = updated_characters
        self.character_choices = updated_choices
        current = self._pages.get("characters")
        if current is None:
            return
        parent = current.master
        current.destroy()
        page = self._build_characters_page(parent)
        self._pages["characters"] = page
        background_label = Label(
            page,
            bg=BACKGROUND,
            bd=0,
            highlightthickness=0,
            anchor="nw",
        )
        background_label.place(x=0, y=0, relwidth=1, relheight=1)
        background_label.lower()
        self._background_page_labels["characters"] = background_label
        if self._active_page == "characters":
            self.show_page("characters")

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
        self._background_choose_button = choose_button
        choose_button.pack(side=LEFT)
        if (
            self.on_select_background_image is None
            and (
                self.on_choose_background_source is None
                or self.on_prepare_background_image is None
            )
        ):
            choose_button.configure(state=DISABLED)
        clear_button = self._button(
            background_row,
            "清除背景",
            self._clear_background_image,
        )
        clear_button.pack(side=LEFT, padx=(8, 0))
        if self.on_clear_background_image is None:
            clear_button.configure(state=DISABLED)
        clear_pages_button = self._button(
            background_row,
            "移除勾選頁面獨立背景",
            self._clear_selected_page_backgrounds,
        )
        clear_pages_button.pack(side=LEFT, padx=(8, 0))
        if self.on_clear_page_background is None:
            clear_pages_button.configure(state=DISABLED)
        progress_row = Frame(background_card, bg=SURFACE)
        progress_row.pack(fill=X, pady=(8, 0))
        self._background_progress_bar = Progressbar(
            progress_row,
            mode="indeterminate",
        )
        self._background_progress_bar.pack(
            side=LEFT,
            fill=X,
            expand=True,
        )
        self._background_progress_bar.pack_forget()
        self._background_cancel_button = self._button(
            progress_row,
            "取消轉換",
            self._cancel_background_prepare,
        )
        self._background_cancel_button.pack(side=LEFT, padx=(8, 0))
        self._background_cancel_button.configure(state=DISABLED)

        scope_frame = Frame(background_card, bg=SURFACE)
        scope_frame.pack(fill=X, pady=(14, 0))
        Label(
            scope_frame,
            text="套用頁面",
            font=("Microsoft JhengHei UI", 10, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        self._background_apply_all_variable = IntVar(
            master=self.parent,
            value=1,
        )
        Checkbutton(
            scope_frame,
            text="全部頁面（未來新增頁面也自動套用）",
            variable=self._background_apply_all_variable,
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=TEXT,
            activebackground=SURFACE,
            selectcolor=BACKGROUND,
            anchor="w",
        ).pack(fill=X, pady=(4, 2))
        self._background_page_variables = {}
        for page_key, page_label in BACKGROUND_PAGE_LABELS.items():
            variable = IntVar(master=self.parent, value=0)
            self._background_page_variables[page_key] = variable
            Checkbutton(
                scope_frame,
                text=page_label,
                variable=variable,
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=TEXT,
                activebackground=SURFACE,
                selectcolor=BACKGROUND,
                anchor="w",
            ).pack(fill=X)
        scope_actions = Frame(scope_frame, bg=SURFACE)
        scope_actions.pack(fill=X, pady=(6, 0))
        self._button(
            scope_actions,
            "全部選取",
            lambda: self._set_all_background_pages(True),
        ).pack(side=LEFT)
        self._button(
            scope_actions,
            "全部取消",
            lambda: self._set_all_background_pages(False),
        ).pack(side=LEFT, padx=(8, 0))
        preview_row = Frame(scope_frame, bg=SURFACE)
        preview_row.pack(fill=X, pady=(8, 0))
        self._background_preview_page_variable = StringVar(
            master=self.parent,
            value=BACKGROUND_PAGE_LABELS["home"],
        )
        preview_menu = OptionMenu(
            preview_row,
            self._background_preview_page_variable,
            *BACKGROUND_PAGE_LABELS.values(),
        )
        preview_menu.configure(
            font=("Microsoft JhengHei UI", 9),
            bg=BACKGROUND,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        preview_menu.pack(side=LEFT, fill=X, expand=True)
        self._button(
            preview_row,
            "預覽選取頁面",
            self._preview_selected_background_page,
        ).pack(side=LEFT, padx=(8, 0))

        display_frame = Frame(background_card, bg=SURFACE)
        display_frame.pack(fill=X, pady=(14, 0))
        Label(
            display_frame,
            text="填補色與區域透明度",
            font=("Microsoft JhengHei UI", 10, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        fill_row = Frame(display_frame, bg=SURFACE)
        fill_row.pack(fill=X, pady=(6, 4))
        self._background_fill_entry = Entry(
            fill_row,
            width=12,
            font=("Microsoft JhengHei UI", 9),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._background_fill_entry.insert(
            0,
            str(self._saved_background_display_values["fill_color"]),
        )
        self._background_fill_entry.bind(
            "<KeyRelease>",
            lambda _event: self._preview_background_display_changes(),
        )
        self._background_fill_entry.pack(side=LEFT, ipady=6)
        self._button(
            fill_row,
            "選色",
            self._choose_background_fill_color,
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            fill_row,
            "恢復舊版預設色",
            self._restore_default_background_fill,
        ).pack(side=LEFT, padx=(8, 0))
        self._background_opacity_scales = {}
        for key, label in (
            ("sidebar", "左側選單"),
            ("panel", "操作面板"),
            ("role_row", "角色列"),
        ):
            row = Frame(display_frame, bg=SURFACE)
            row.pack(fill=X, pady=2)
            Label(
                row,
                text=label,
                width=10,
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=MUTED,
                anchor="w",
            ).pack(side=LEFT)
            scale = Scale(
                row,
                from_=0,
                to=100,
                orient=HORIZONTAL,
                showvalue=True,
                resolution=1,
                command=lambda _value: self._preview_background_display_changes(),
                bg=SURFACE,
                fg=TEXT,
                highlightthickness=0,
                troughcolor=BACKGROUND,
                length=280,
            )
            scale.set(int(self._saved_background_display_values[key]))
            scale.pack(side=LEFT, fill=X, expand=True)
            self._button(
                row,
                "恢復此區預設",
                lambda selected=key: self._restore_background_opacity(
                    selected
                ),
            ).pack(side=LEFT, padx=(8, 0))
            self._background_opacity_scales[key] = scale
        save_row = Frame(background_card, bg=SURFACE)
        save_row.pack(fill=X, pady=(14, 0))
        self._button(
            save_row,
            "儲存背景設定",
            self._save_background_changes,
            primary=True,
        ).pack(side=LEFT)
        self._button(
            save_row,
            "取消未儲存變更",
            self._discard_background_changes,
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            save_row,
            "全部恢復舊版預設",
            self._restore_all_background_defaults,
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            save_row,
            "匯出背景設定",
            self._export_background_settings,
        ).pack(side=LEFT, padx=(8, 0))
        self._button(
            save_row,
            "匯入背景設定",
            self._import_background_settings,
        ).pack(side=LEFT, padx=(8, 0))

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

        preview_card = self._card(page)
        preview_card.pack(fill=X, pady=(14, 0))
        Label(
            preview_card,
            text="提醒卡樣式",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            preview_card,
            text=(
                "只會套用已確認的提醒卡樣式；選擇會保存，"
                "下次開啟程式自動恢復。"
            ),
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            justify=LEFT,
        ).pack(fill=X, pady=(6, 12))
        preview_choices = self._card_preview_choices()
        selected_choice = next(
            (choice for choice in preview_choices if choice.selected),
            None,
        )
        preview_row = Frame(preview_card, bg=SURFACE)
        preview_row.pack(fill=X)
        choice_labels = tuple(
            choice.display_name for choice in preview_choices
        )
        self._card_preview_variable = StringVar(
            master=self.parent,
            value=(
                selected_choice.display_name
                if selected_choice is not None
                else (choice_labels[0] if choice_labels else "尚無可用樣式")
            ),
        )
        preview_menu = OptionMenu(
            preview_row,
            self._card_preview_variable,
            *(choice_labels or ("尚無可用樣式",)),
        )
        preview_menu.configure(
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        preview_menu["menu"].configure(
            font=("Microsoft JhengHei UI", 10)
        )
        preview_menu.pack(side=LEFT, fill=X, expand=True)
        apply_preview_button = self._button(
            preview_row,
            "套用樣式",
            self._apply_card_preview_choice,
            primary=True,
        )
        apply_preview_button.pack(side=LEFT, padx=(8, 0))
        clear_preview_button = self._button(
            preview_row,
            "停用提醒浮層",
            self._clear_card_preview_choice,
        )
        clear_preview_button.pack(side=LEFT, padx=(8, 0))
        if not preview_choices or self.on_card_preview_select is None:
            apply_preview_button.configure(state=DISABLED)
        if self.on_card_preview_clear is None:
            clear_preview_button.configure(state=DISABLED)
        self._card_preview_status_label = Label(
            preview_card,
            text=(
                f"目前樣式：{selected_choice.display_name}"
                if selected_choice is not None
                else "提醒浮層目前已停用。"
            ),
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        )
        self._card_preview_status_label.pack(fill=X, pady=(10, 0))

        habit_card = self._card(page)
        habit_card.pack(fill=X, pady=(14, 0))
        Label(
            habit_card,
            text="玩家習慣",
            font=("Microsoft JhengHei UI", 13, "bold"),
            bg=SURFACE,
            fg=TEXT,
            anchor="w",
        ).pack(fill=X)
        Label(
            habit_card,
            text=(
                "只觀察活動時間與角色操作順序並提出建議；"
                "不會直接操作遊戲。"
            ),
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
            justify="left",
        ).pack(fill=X, pady=(6, 12))
        try:
            habit_settings = (
                self.habit_settings_provider()
                if self.habit_settings_provider is not None
                else PlayerHabitSettingsView(14, 10, 8, ())
            )
        except Exception as error:
            self._report_refresh_error(error)
            habit_settings = PlayerHabitSettingsView(14, 10, 8, ())
        habit_row = Frame(habit_card, bg=SURFACE)
        habit_row.pack(fill=X)
        Label(
            habit_row,
            text="觀察天數",
            font=("Microsoft JhengHei UI", 10),
            bg=SURFACE,
            fg=TEXT,
        ).pack(side=LEFT)
        self._habit_observation_days_entry = Entry(
            habit_row,
            width=8,
            font=("Microsoft JhengHei UI", 10),
            bg=BACKGROUND,
            fg=TEXT,
            relief="flat",
            bd=0,
        )
        self._habit_observation_days_entry.insert(
            0,
            str(habit_settings.observation_days),
        )
        self._habit_observation_days_entry.pack(
            side=LEFT,
            padx=(8, 0),
            ipady=7,
        )
        Label(
            habit_row,
            text=(
                f"天｜同一習慣至少 {habit_settings.minimum_occurrences} 次，"
                f"分布至少 {habit_settings.minimum_distinct_days} 天"
            ),
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            padx=8,
        ).pack(side=LEFT)
        self._button(
            habit_row,
            "儲存",
            self._save_habit_observation_days,
            primary=True,
        ).pack(side=LEFT, padx=(8, 0))
        self._habit_status_label = Label(
            habit_card,
            text="尚未有玩家確認的偏好。",
            font=("Microsoft JhengHei UI", 9),
            bg=SURFACE,
            fg=MUTED,
            anchor="w",
        )
        self._habit_status_label.pack(fill=X, pady=(12, 6))
        self._habit_preferences_frame = Frame(habit_card, bg=SURFACE)
        self._habit_preferences_frame.pack(fill=X)
        self._render_habit_preferences(habit_settings)
        self._button(
            habit_card,
            "全部清除已保存偏好",
            self._clear_habit_preferences,
        ).pack(anchor="w", pady=(10, 0))

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
        if self._background_prepare_running:
            return
        if (
            self.on_choose_background_source is not None
            and self.on_prepare_background_image is not None
        ):
            try:
                source = self.on_choose_background_source()
            except Exception:
                self._refresh_background_status(
                    "背景圖片選取失敗，原本背景已保留。"
                )
                return
            if source is not None:
                self._start_background_prepare(source)
            return
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
        if not result.succeeded or result.managed_path is None:
            self._refresh_background_status(result.message)
            return
        if (
            self._pending_background_path is not None
            and self.on_discard_background_image is not None
        ):
            self.on_discard_background_image(self._pending_background_path)
        self._pending_background_path = result.managed_path
        self._pending_background_result = result
        loaded = self._set_background_path(result.managed_path)
        message = result.message
        if result.succeeded and result.managed_path is not None and not loaded:
            message = "受管背景副本無法顯示，請重新選擇圖片。"
        self._refresh_background_status(message)

    def _start_background_prepare(self, source: Path) -> None:
        callback = self.on_prepare_background_image
        if callback is None:
            return
        self._background_prepare_cancel = Event()
        self._background_prepare_results = Queue()
        self._background_prepare_running = True
        if self._background_choose_button is not None:
            self._background_choose_button.configure(state=DISABLED)
        if self._background_cancel_button is not None:
            self._background_cancel_button.configure(state=NORMAL)
        if self._background_progress_bar is not None:
            self._background_progress_bar.pack(
                side=LEFT,
                fill=X,
                expand=True,
            )
            self._background_progress_bar.start(12)
        self._refresh_background_status("正在轉換背景圖片，請稍候。")

        def worker() -> None:
            try:
                result = callback(
                    Path(source),
                    self._background_prepare_cancel.is_set,
                )
            except Exception as error:
                self._background_prepare_results.put(error)
            else:
                self._background_prepare_results.put(result)

        Thread(
            target=worker,
            name="fu-background-prepare",
            daemon=True,
        ).start()
        self._poll_background_prepare()

    def _poll_background_prepare(self) -> None:
        self._background_prepare_poll_id = None
        if not self._background_prepare_running:
            return
        try:
            outcome = self._background_prepare_results.get_nowait()
        except Empty:
            self._background_prepare_poll_id = self.parent.after(
                100,
                self._poll_background_prepare,
            )
            return
        self._background_prepare_running = False
        if self._background_progress_bar is not None:
            self._background_progress_bar.stop()
            self._background_progress_bar.pack_forget()
        if self._background_choose_button is not None:
            self._background_choose_button.configure(state=NORMAL)
        if self._background_cancel_button is not None:
            self._background_cancel_button.configure(state=DISABLED)
        if isinstance(outcome, Exception):
            self._refresh_background_status(
                "背景圖片處理失敗，原本背景已保留。"
            )
            return
        if self._background_prepare_cancel.is_set():
            if (
                outcome.managed_path is not None
                and self.on_discard_background_image is not None
            ):
                self.on_discard_background_image(outcome.managed_path)
            self._refresh_background_status(
                "背景圖片轉換已取消，原本背景已保留。"
            )
            return
        if not outcome.succeeded or outcome.managed_path is None:
            self._refresh_background_status(outcome.message)
            return
        if (
            self._pending_background_path is not None
            and self.on_discard_background_image is not None
        ):
            self.on_discard_background_image(self._pending_background_path)
        self._pending_background_path = outcome.managed_path
        self._pending_background_result = outcome
        loaded = self._set_background_path(outcome.managed_path)
        self._refresh_background_status(
            outcome.message
            if loaded
            else "受管背景副本無法顯示，請重新選擇圖片。"
        )

    def _cancel_background_prepare(self) -> None:
        if not self._background_prepare_running:
            return
        self._background_prepare_cancel.set()
        if self._background_cancel_button is not None:
            self._background_cancel_button.configure(state=DISABLED)
        self._refresh_background_status(
            "正在取消背景圖片轉換；原本背景會保持不變。"
        )

    def _set_all_background_pages(self, selected: bool) -> None:
        for variable in self._background_page_variables.values():
            variable.set(1 if selected else 0)
        if selected and self._background_apply_all_variable is not None:
            self._background_apply_all_variable.set(0)

    def _preview_selected_background_page(self) -> None:
        variable = self._background_preview_page_variable
        if variable is None:
            return
        selected_label = variable.get()
        page = next(
            (
                key
                for key, label in BACKGROUND_PAGE_LABELS.items()
                if label == selected_label
            ),
            None,
        )
        if page is None:
            return
        self.show_page(page, _background_preview=True)

    def _choose_background_fill_color(self) -> None:
        if self._background_fill_entry is None:
            return
        selected = colorchooser.askcolor(
            color=self._background_fill_entry.get().strip(),
            parent=self.parent,
            title="選擇背景填補色",
        )
        color = selected[1]
        if not color:
            return
        self._background_fill_entry.delete(0, "end")
        self._background_fill_entry.insert(0, str(color).upper())
        self._preview_background_display_changes()

    def _restore_default_background_fill(self) -> None:
        if self._background_fill_entry is None:
            return
        self._background_fill_entry.delete(0, "end")
        self._background_fill_entry.insert(0, DEFAULT_BACKGROUND_FILL_COLOR)
        self._preview_background_display_changes()

    def _restore_background_opacity(self, region: str) -> None:
        scale = self._background_opacity_scales.get(region)
        if scale is None or region not in DEFAULT_BACKGROUND_OPACITY:
            return
        scale.set(DEFAULT_BACKGROUND_OPACITY[region])
        self._preview_background_display_changes()

    def _background_display_values(self) -> dict[str, object]:
        if self._background_fill_entry is None:
            return dict(self._saved_background_display_values)
        fill_color = self._background_fill_entry.get().strip().upper()
        if (
            len(fill_color) != 7
            or not fill_color.startswith("#")
        ):
            raise ValueError("背景填補色必須使用 #RRGGBB 色碼。")
        int(fill_color[1:], 16)
        return {
            "fill_color": fill_color,
            **{
                key: int(scale.get())
                for key, scale in self._background_opacity_scales.items()
            },
        }

    def _preview_background_display_changes(self) -> None:
        try:
            values = self._background_display_values()
        except (TypeError, ValueError):
            return
        self.background_fill_color = str(values["fill_color"])
        self._background_render_size = None
        canvas = self._page_canvas
        if canvas is not None:
            self._schedule_background_resize(
                max(1, int(canvas.winfo_width())),
                max(1, int(canvas.winfo_height())),
            )

    def _save_background_changes(self) -> bool:
        if self._background_prepare_running:
            self._refresh_background_status(
                "背景圖片仍在轉換，請先等待完成或取消轉換。"
            )
            return False
        try:
            values = self._background_display_values()
        except (TypeError, ValueError):
            self._refresh_background_status(
                "背景填補色或透明度無效，原本設定已保留。"
            )
            return False
        if self.on_background_display_settings_update is not None:
            try:
                settings = self.on_background_display_settings_update(
                    str(values["fill_color"]),
                    int(values["sidebar"]),
                    int(values["panel"]),
                    int(values["role_row"]),
                )
            except Exception:
                self._refresh_background_status(
                    "背景顯示設定無法保存，原本設定已保留。"
                )
                return False
            if not isinstance(settings, BackgroundSettings):
                return False
            self.background_settings = settings
        if self._pending_background_path is not None:
            if self.on_save_background_image is None:
                return False
            apply_all = bool(
                self._background_apply_all_variable
                and self._background_apply_all_variable.get()
            )
            pages = tuple(
                key
                for key, variable in self._background_page_variables.items()
                if variable.get()
            )
            try:
                result = self.on_save_background_image(
                    self._pending_background_path,
                    apply_all,
                    pages,
                )
            except Exception:
                self._refresh_background_status(
                    "背景圖片無法保存，原本背景已保留。"
                )
                return False
            if not isinstance(result, BackgroundImageResult) or not result.succeeded:
                self._refresh_background_status(
                    result.message
                    if isinstance(result, BackgroundImageResult)
                    else "背景圖片無法保存，原本背景已保留。"
                )
                return False
            self._pending_background_path = None
            self._pending_background_result = None
            self._refresh_background_status(result.message)
        if self.background_settings_provider is not None:
            try:
                self.background_settings = self.background_settings_provider()
            except Exception:
                pass
        self._saved_background_display_values = dict(values)
        self.background_fill_color = str(values["fill_color"])
        self._background_preview_active = False
        self._apply_saved_background_for_page(self._active_page)
        return True

    def _discard_background_changes(self) -> None:
        if self._background_prepare_running:
            self._background_prepare_cancel.set()
        if (
            self._pending_background_path is not None
            and self.on_discard_background_image is not None
        ):
            self.on_discard_background_image(self._pending_background_path)
        self._pending_background_path = None
        self._pending_background_result = None
        self._background_preview_active = False
        values = self._saved_background_display_values
        if self._background_fill_entry is not None:
            self._background_fill_entry.delete(0, "end")
            self._background_fill_entry.insert(0, str(values["fill_color"]))
        for key, scale in self._background_opacity_scales.items():
            scale.set(int(values[key]))
        self.background_fill_color = str(values["fill_color"])
        self._apply_saved_background_for_page(self._active_page)
        self._refresh_background_status("未儲存的背景變更已放棄。")

    def _restore_all_background_defaults(self) -> None:
        if not messagebox.askyesno(
            "輔｜恢復背景預設",
            "確定移除全部背景，並將填補色與三區透明度恢復為舊版預設？",
            parent=self.parent,
        ):
            return
        if (
            self._pending_background_path is not None
            and self.on_discard_background_image is not None
        ):
            self.on_discard_background_image(self._pending_background_path)
        self._pending_background_path = None
        self._pending_background_result = None
        if self.on_clear_all_backgrounds is not None:
            try:
                result = self.on_clear_all_backgrounds()
            except Exception:
                self._refresh_background_status(
                    "所有背景無法恢復預設，原本設定已保留。"
                )
                return
            if not isinstance(result, BackgroundImageResult) or not result.succeeded:
                self._refresh_background_status(
                    result.message
                    if isinstance(result, BackgroundImageResult)
                    else "所有背景無法恢復預設，原本設定已保留。"
                )
                return
        if self._background_fill_entry is not None:
            self._background_fill_entry.delete(0, "end")
            self._background_fill_entry.insert(
                0,
                DEFAULT_BACKGROUND_FILL_COLOR,
            )
        for key, scale in self._background_opacity_scales.items():
            scale.set(DEFAULT_BACKGROUND_OPACITY[key])
        values = self._background_display_values()
        if self.on_background_display_settings_update is not None:
            try:
                self.background_settings = (
                    self.on_background_display_settings_update(
                        str(values["fill_color"]),
                        int(values["sidebar"]),
                        int(values["panel"]),
                        int(values["role_row"]),
                    )
                )
            except Exception:
                self._refresh_background_status(
                    "背景顯示設定無法恢復預設。"
                )
                return
        self._saved_background_display_values = dict(values)
        self.background_fill_color = DEFAULT_BACKGROUND_FILL_COLOR
        self._set_background_path(None)
        self._refresh_background_status("所有背景設定已恢復舊版預設。")

    def _export_background_settings(self) -> None:
        if self.on_export_background_settings is None:
            return
        if not self._confirm_background_departure():
            return
        try:
            destination = self.on_export_background_settings()
        except Exception:
            self._refresh_background_status("背景設定無法匯出。")
            return
        if destination is not None:
            self._refresh_background_status(
                f"背景設定已匯出：{Path(destination).name}"
            )

    def _import_background_settings(self) -> None:
        if self.on_import_background_settings is None:
            return
        if not self._confirm_background_departure():
            return
        if not messagebox.askyesno(
            "輔｜匯入背景設定",
            "匯入後會取代目前背景、套用頁面、填補色與三區透明度。確定繼續？",
            parent=self.parent,
        ):
            return
        try:
            result = self.on_import_background_settings()
        except Exception:
            self._refresh_background_status(
                "背景設定無法匯入，原本設定已保留。"
            )
            return
        if result is None:
            return
        if not isinstance(result, BackgroundImageResult) or not result.succeeded:
            self._refresh_background_status(
                result.message
                if isinstance(result, BackgroundImageResult)
                else "背景設定無法匯入，原本設定已保留。"
            )
            return
        if self.background_settings_provider is not None:
            self.background_settings = self.background_settings_provider()
        if self.background_settings is not None:
            self._saved_background_display_values = {
                "fill_color": self.background_settings.fill_color,
                "sidebar": self.background_settings.sidebar_opacity,
                "panel": self.background_settings.panel_opacity,
                "role_row": self.background_settings.role_row_opacity,
            }
        self.background_fill_color = str(
            self._saved_background_display_values["fill_color"]
        )
        self._apply_saved_background_for_page(self._active_page)
        self._active_page = "settings"
        self.build()
        self._refresh_background_status(result.message)

    def _background_changes_dirty(self) -> bool:
        if self._background_prepare_running:
            return True
        if self._pending_background_path is not None:
            return True
        try:
            return (
                self._background_display_values()
                != self._saved_background_display_values
            )
        except (TypeError, ValueError):
            return True

    def _confirm_background_departure(self) -> bool:
        if not self._background_changes_dirty():
            return True
        choice = messagebox.askyesnocancel(
            "輔｜背景尚未儲存",
            "背景設定尚未儲存。\n"
            "選「是」儲存、選「否」放棄、選「取消」留在目前頁面。",
            parent=self.parent,
        )
        if choice is None:
            return False
        if choice:
            return self._save_background_changes()
        self._discard_background_changes()
        return True

    def prepare_close(self) -> bool:
        return self._confirm_background_departure()

    def _apply_saved_background_for_page(self, page: str) -> None:
        if self._pending_background_path is not None:
            return
        path = (
            self.background_for_page(page)
            if self.background_for_page is not None
            else self.background_image_path
        )
        self._set_background_path(path)

    def _clear_background_image(self) -> None:
        if self.on_clear_background_image is None:
            return
        if not messagebox.askyesno(
            "輔｜移除背景",
            "確定移除全域背景？各頁獨立背景會保留。",
            parent=self.parent,
        ):
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
        self._apply_saved_background_for_page(self._active_page)
        if self.background_settings_provider is not None:
            try:
                self.background_settings = self.background_settings_provider()
            except Exception:
                pass
        self._refresh_background_status(result.message)

    def _clear_selected_page_backgrounds(self) -> None:
        if self.on_clear_page_background is None:
            return
        pages = tuple(
            key
            for key, variable in self._background_page_variables.items()
            if variable.get()
        )
        if not pages:
            self._refresh_background_status("請先勾選要移除獨立背景的頁面。")
            return
        names = "、".join(BACKGROUND_PAGE_LABELS[key] for key in pages)
        if not messagebox.askyesno(
            "輔｜移除頁面背景",
            f"確定移除以下頁面的獨立背景？\n{names}\n"
            "移除後會恢復使用全域背景；沒有全域背景時顯示填補色。",
            parent=self.parent,
        ):
            return
        try:
            for page in pages:
                result = self.on_clear_page_background(page)
                if not isinstance(result, BackgroundImageResult) or not result.succeeded:
                    raise RuntimeError("page background removal failed")
        except Exception:
            self._refresh_background_status(
                "頁面獨立背景無法移除，未完成的部分保持原狀。"
            )
            return
        self._apply_saved_background_for_page(self._active_page)
        if self.background_settings_provider is not None:
            try:
                self.background_settings = self.background_settings_provider()
            except Exception:
                pass
        self._refresh_background_status("已移除勾選頁面的獨立背景。")

    def dispose(self) -> None:
        """Release background image resources before the Tk window closes."""
        self._cancel_game_time_tick()
        self._cancel_timed_click_capture()
        self._background_prepare_cancel.set()
        self._background_prepare_running = False
        if self._background_prepare_poll_id is not None:
            try:
                self.parent.after_cancel(self._background_prepare_poll_id)
            except Exception:
                pass
            self._background_prepare_poll_id = None
        if self._background_progress_bar is not None:
            self._background_progress_bar.stop()
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

    def _card_preview_choices(self) -> tuple[CardPreviewChoice, ...]:
        if self.card_preview_choices_provider is None:
            return ()
        choices = self.card_preview_choices_provider()
        if not isinstance(choices, tuple) or any(
            not isinstance(choice, CardPreviewChoice)
            for choice in choices
        ):
            raise TypeError(
                "card preview choices provider returned invalid values."
            )
        return choices

    def _set_card_preview_status(self) -> None:
        choices = self._card_preview_choices()
        selected = next(
            (choice for choice in choices if choice.selected),
            None,
        )
        if self._card_preview_variable is not None and selected is not None:
            self._card_preview_variable.set(selected.display_name)
        if self._card_preview_status_label is not None:
            self._card_preview_status_label.configure(
                text=(
                    f"目前樣式：{selected.display_name}"
                    if selected is not None
                    else "提醒浮層目前已停用。"
                )
            )

    def _apply_card_preview_choice(self) -> None:
        if (
            self._card_preview_variable is None
            or self.on_card_preview_select is None
        ):
            return
        selected_label = self._card_preview_variable.get()
        choice = next(
            (
                item
                for item in self._card_preview_choices()
                if item.display_name == selected_label
            ),
            None,
        )
        if choice is None:
            return
        try:
            self.on_card_preview_select(choice.profile_id)
            self._set_card_preview_status()
        except Exception as error:
            self._report_refresh_error(error)

    def _clear_card_preview_choice(self) -> None:
        if self.on_card_preview_clear is None:
            return
        try:
            self.on_card_preview_clear()
            self._set_card_preview_status()
        except Exception as error:
            self._report_refresh_error(error)

    def _render_habit_preferences(
        self,
        settings: PlayerHabitSettingsView,
    ) -> None:
        frame = self._habit_preferences_frame
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        if self._habit_status_label is not None:
            self._habit_status_label.configure(
                text=(
                    f"已保存 {len(settings.preferences)} 筆偏好。"
                    if settings.preferences
                    else "尚未有玩家確認的偏好。"
                )
            )
        for preference in settings.preferences:
            row = Frame(frame, bg=BACKGROUND, padx=10, pady=7)
            row.pack(fill=X, pady=2)
            Label(
                row,
                text=(
                    f"{preference.kind}｜{preference.subject}"
                    f"｜目前：{preference.decision}"
                ),
                font=("Microsoft JhengHei UI", 9),
                bg=BACKGROUND,
                fg=TEXT,
                anchor="w",
                justify="left",
            ).pack(fill=X)
            edit_row = Frame(row, bg=BACKGROUND)
            edit_row.pack(fill=X, pady=(4, 0))
            value_entry = Entry(
                edit_row,
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=TEXT,
                relief="flat",
                bd=0,
            )
            value_entry.insert(0, " → ".join(preference.values))
            value_entry.pack(side=LEFT, fill=X, expand=True, ipady=6)
            self._button(
                edit_row,
                "儲存修改",
                lambda preference_id=preference.preference_id,
                entry=value_entry: self._modify_habit_preference(
                    preference_id,
                    entry,
                ),
                primary=True,
            ).pack(side=LEFT, padx=(8, 0))
            self._button(
                edit_row,
                "刪除",
                lambda preference_id=preference.preference_id: (
                    self._remove_habit_preference(preference_id)
                ),
            ).pack(side=LEFT, padx=(8, 0))

    def _save_habit_observation_days(self) -> None:
        if (
            self._habit_observation_days_entry is None
            or self.on_habit_observation_days_update is None
        ):
            return
        try:
            days = int(self._habit_observation_days_entry.get().strip())
            settings = self.on_habit_observation_days_update(days)
            if not isinstance(settings, PlayerHabitSettingsView):
                raise TypeError("habit callback must return settings view.")
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._habit_observation_days_entry.delete(0, "end")
        self._habit_observation_days_entry.insert(
            0,
            str(settings.observation_days),
        )
        self._render_habit_preferences(settings)

    def _remove_habit_preference(self, preference_id: str) -> None:
        if self.on_remove_habit_preference is None:
            return
        try:
            settings = self.on_remove_habit_preference(preference_id)
            if not isinstance(settings, PlayerHabitSettingsView):
                raise TypeError("habit callback must return settings view.")
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._render_habit_preferences(settings)

    def _modify_habit_preference(
        self,
        preference_id: str,
        entry: Entry,
    ) -> None:
        if self.on_modify_habit_preference is None:
            return
        values = tuple(
            value.strip()
            for value in entry.get().split("→")
            if value.strip()
        )
        try:
            settings = self.on_modify_habit_preference(
                preference_id,
                values,
            )
            if not isinstance(settings, PlayerHabitSettingsView):
                raise TypeError("habit callback must return settings view.")
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._render_habit_preferences(settings)

    def _clear_habit_preferences(self) -> None:
        if self.on_clear_habit_preferences is None:
            return
        if not messagebox.askyesno(
            "輔｜清除玩家習慣",
            "確定清除全部已保存偏好？\n觀察紀錄會保留。",
            parent=self.parent,
        ):
            return
        try:
            settings = self.on_clear_habit_preferences()
            if not isinstance(settings, PlayerHabitSettingsView):
                raise TypeError("habit callback must return settings view.")
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._render_habit_preferences(settings)

    def refresh_habit_settings(self) -> None:
        if self.habit_settings_provider is None:
            return
        try:
            settings = self.habit_settings_provider()
            if not isinstance(settings, PlayerHabitSettingsView):
                raise TypeError("habit provider must return settings view.")
        except Exception as error:
            self._report_refresh_error(error)
            return
        if self._habit_observation_days_entry is not None:
            self._habit_observation_days_entry.delete(0, "end")
            self._habit_observation_days_entry.insert(
                0,
                str(settings.observation_days),
            )
        self._render_habit_preferences(settings)

    def show_page(
        self,
        name: str,
        *,
        _background_preview: bool = False,
    ) -> None:
        if name not in self._pages:
            raise KeyError(f"Unknown home page: {name}")
        if (
            self._background_preview_active
            and not _background_preview
            and name != "settings"
            and not self._confirm_background_departure()
        ):
            return
        if (
            self._active_page == "settings"
            and name != "settings"
            and not _background_preview
            and not self._confirm_background_departure()
        ):
            return
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
        self._background_preview_active = bool(_background_preview)
        if _background_preview and self._pending_background_path is not None:
            self._set_background_path(self._pending_background_path)
        else:
            self._apply_saved_background_for_page(name)
        if self._page_canvas is not None:
            self._page_canvas.yview_moveto(0.0)
            self.parent.after_idle(self._sync_page_scroll_region)

    def _select_group(self, name: str) -> None:
        if getattr(self, "_group_reorder_mode", False):
            self._show_group_setting_message(
                "請先完成或取消目前的角色排序。"
            )
            if self._group_variable is not None:
                self._group_variable.set(self.current_group_name or "")
            return
        if name not in {choice.name for choice in self.group_choices}:
            return
        previous_name = self.current_group_name
        result = None
        if self.on_group_change is not None:
            result = self.on_group_change(name)
        if isinstance(result, GroupManagementViewResult):
            if not result.success:
                if self._group_variable is not None:
                    self._group_variable.set(previous_name or "")
                self._show_group_setting_message(
                    result.message or "組別沒有切換。"
                )
                return
            selected_name = result.current_group_name
        elif result is False:
            if self._group_variable is not None:
                self._group_variable.set(previous_name or "")
            self._show_group_setting_message("組別沒有切換。")
            return
        else:
            selected_name = name
        if selected_name not in {
            choice.name for choice in self.group_choices
        }:
            if self._group_variable is not None:
                self._group_variable.set(previous_name or "")
            self._show_group_setting_message("組別沒有切換。")
            return
        self.current_group_name = selected_name
        if self._group_variable is not None:
            self._group_variable.set(selected_name)
        if self._group_value_label is not None:
            self._group_value_label.configure(text=selected_name)
        if self._group_name_entry is not None:
            self._group_name_entry.delete(0, "end")
            self._group_name_entry.insert(0, selected_name)
        self.refresh_workspace()
        self.refresh_current_group_summary()
        self.refresh_group_entries()
        self.refresh_group_sync_relations()
        self.refresh_group_role_statuses()
        self.refresh_operation_records()

    @staticmethod
    def _normalized_window_size_value(
        entry: Entry | None,
        fallback: int,
    ) -> int:
        try:
            value = int(entry.get().strip()) if entry is not None else fallback
        except (TypeError, ValueError):
            value = fallback
        return max(
            MIN_FLASH_CLIENT_SIZE,
            min(MAX_FLASH_CLIENT_SIZE, value),
        )

    def _window_size_values(self) -> tuple[int, int]:
        width = self._normalized_window_size_value(
            self._window_size_width_entry,
            self.window_size[0],
        )
        height = self._normalized_window_size_value(
            self._window_size_height_entry,
            self.window_size[1],
        )
        self.window_size = (width, height)
        for entry, value in (
            (self._window_size_width_entry, width),
            (self._window_size_height_entry, height),
        ):
            if entry is not None:
                entry.delete(0, "end")
                entry.insert(0, str(value))
        return self.window_size

    def set_window_size_status(
        self,
        message: str,
        *,
        success: bool = True,
    ) -> None:
        if self._window_size_status_label is None:
            return
        prefix = "● "
        self._window_size_status_label.configure(
            text=prefix + message.strip(),
            fg=PRIMARY if success else WARNING,
        )

    def _show_window_size_result(
        self,
        result: WindowSizeAdjustmentResult,
    ) -> None:
        if not isinstance(result, WindowSizeAdjustmentResult):
            raise TypeError(
                "window size callback must return WindowSizeAdjustmentResult."
            )
        if result.success and result.action == "read_main":
            self.window_size = (result.width, result.height)
            for entry, value in (
                (self._window_size_width_entry, result.width),
                (self._window_size_height_entry, result.height),
            ):
                if entry is not None:
                    entry.delete(0, "end")
                    entry.insert(0, str(value))
        self.set_window_size_status(
            result.player_message,
            success=result.success,
        )

    def _load_main_window_size(self) -> None:
        if (
            self.current_group_name is None
            or self.on_read_main_window_size is None
        ):
            self.set_window_size_status(
                "目前組別的主窗口尚未設定。",
                success=False,
            )
            return
        try:
            result = self.on_read_main_window_size(
                self.current_group_name
            )
            self._show_window_size_result(result)
        except Exception as error:
            self.set_window_size_status("讀取主窗口尺寸失敗。", success=False)
            self._report_refresh_error(error)

    def _apply_current_group_window_size(self) -> None:
        if (
            self.current_group_name is None
            or self.on_apply_group_window_size is None
        ):
            self.set_window_size_status(
                "目前沒有可套用的組別。",
                success=False,
            )
            return
        width, height = self._window_size_values()
        try:
            result = self.on_apply_group_window_size(
                self.current_group_name,
                width,
                height,
            )
            self._show_window_size_result(result)
        except Exception as error:
            self.set_window_size_status("套用目前組別失敗。", success=False)
            self._report_refresh_error(error)

    def _apply_all_game_window_size(self, *, quiet: bool = False) -> None:
        if self.on_apply_all_window_size is None:
            if not quiet:
                self.set_window_size_status(
                    "遊戲視窗尺寸功能目前不可用。",
                    success=False,
                )
            return
        width, height = self._window_size_values()
        try:
            result = self.on_apply_all_window_size(width, height)
            if (
                not quiet
                or not result.success
                or result.changed_count > 0
            ):
                self._show_window_size_result(result)
        except Exception as error:
            self.set_window_size_status("套用遊戲視窗失敗。", success=False)
            self._report_refresh_error(error)

    def _cancel_window_size_poll(self) -> None:
        if self._window_size_after_id is None:
            return
        try:
            self.parent.after_cancel(self._window_size_after_id)
        except Exception:
            pass
        self._window_size_after_id = None

    def _schedule_window_size_poll(self) -> None:
        if (
            not self.window_size_auto_enabled
            or self._window_size_after_id is not None
        ):
            return
        self._window_size_after_id = self.parent.after(
            1000,
            self._poll_auto_window_size,
        )

    def _poll_auto_window_size(self) -> None:
        self._window_size_after_id = None
        if not self.window_size_auto_enabled:
            return
        self._apply_all_game_window_size(quiet=True)
        self._schedule_window_size_poll()

    def _toggle_auto_window_size(self) -> None:
        enabled = bool(
            self._window_size_auto_variable is not None
            and self._window_size_auto_variable.get()
        )
        self.window_size_auto_enabled = enabled
        self._cancel_window_size_poll()
        if enabled:
            width, height = self._window_size_values()
            self.set_window_size_status(
                f"新視窗自動套用已啟用：{width}×{height}。"
            )
            self._apply_all_game_window_size(quiet=True)
            self._schedule_window_size_poll()
        else:
            self.set_window_size_status("新視窗自動套用已關閉。")

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

    def _stop_all_managed_games(self) -> None:
        self._run_group_window_action(
            self.on_stop_all_managed_games,
            "正在安全停止全部受管遊戲…",
            require_group=False,
        )

    def _run_group_window_action(
        self,
        callback: Callable[[str], object] | None,
        progress_message: str,
        *,
        require_group: bool = True,
    ) -> None:
        if callback is None or (
            require_group and self.current_group_name is None
        ):
            return
        self.set_group_launch_state(True, progress_message)
        try:
            accepted = callback(self.current_group_name or "")
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
        self._group_launch_running = bool(running)
        for button in (
            self._group_launch_button,
            self._group_restore_button,
            self._group_record_button,
            self._group_stop_all_button,
        ):
            if button is not None:
                button.configure(
                    state=DISABLED if running else NORMAL
                )
        self._refresh_group_edit_controls()
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
        self._show_group_setting_message(result.message)

    def _group_reorder_blocks_action(self) -> bool:
        if not getattr(self, "_group_reorder_mode", False):
            return False
        self._show_group_setting_message(
            "請先完成或取消目前的角色排序。"
        )
        return True

    def _create_group(self) -> None:
        if self._group_reorder_blocks_action():
            return
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
        if self._group_reorder_blocks_action():
            return
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
        if self._group_reorder_blocks_action():
            return
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
        if self._group_reorder_blocks_action():
            return
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

    def _export_group_configuration(self) -> None:
        if self._group_reorder_blocks_action():
            return
        if self.on_export_group_configuration is None:
            return
        try:
            result = self.on_export_group_configuration()
        except Exception as error:
            self._show_group_setting_message("組別設定無法匯出。")
            self._report_refresh_error(error)
            return
        if result is None or result is False:
            return
        if isinstance(result, Path):
            self._show_group_setting_message(
                f"組別設定已匯出：{result.name}"
            )
            return
        self._show_group_setting_message(result)

    def _import_group_configuration(self) -> None:
        if self._group_reorder_blocks_action():
            return
        if self.on_import_group_configuration is None:
            return
        try:
            result = self.on_import_group_configuration()
        except Exception as error:
            self._show_group_setting_message(
                "組別設定無法匯入，原本設定已保留。"
            )
            self._report_refresh_error(error)
            return
        if result is None or result is False:
            return
        if isinstance(result, GroupManagementViewResult):
            self._apply_group_management_result(result)
            return
        self._show_group_setting_message(result)

    def _change_group_launch_hotkey(self, value: str) -> None:
        if self._group_reorder_blocks_action():
            return
        if (
            self.current_group_name is None
            or self.on_group_launch_hotkey_change is None
            or self._group_launch_hotkey_variable is None
        ):
            return
        previous = (
            self.group_launch_hotkey_provider(
                self.current_group_name
            )
            if self.group_launch_hotkey_provider is not None
            else ""
        )
        normalized = normalize_feature_hotkey(
            "" if value == "未設定" else value
        )
        try:
            result = self.on_group_launch_hotkey_change(
                self.current_group_name,
                normalized,
            )
        except Exception as error:
            self._group_launch_hotkey_variable.set(
                previous or "未設定"
            )
            self._report_refresh_error(error)
            return
        if result is False or (
            isinstance(result, str) and result.strip()
        ):
            self._group_launch_hotkey_variable.set(
                previous or "未設定"
            )
            self._show_group_setting_message(result)
            return
        self._group_launch_hotkey_variable.set(
            normalized or "未設定"
        )
        self._show_group_setting_message(
            (
                f"整組啟動快捷鍵已設定：{normalized}"
                if normalized
                else "整組啟動快捷鍵已清除。"
            )
        )

    def _add_shortcuts_to_current_group(self) -> None:
        if self._group_reorder_blocks_action():
            return
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

    def _start_group_entry_reorder(self) -> None:
        if (
            self.current_group_name is None
            or self.group_entries_provider is None
            or self.on_reorder_group_entries is None
        ):
            return
        if self._current_group_master_locked():
            self._show_group_setting_message(
                "主窗上鎖中，請先解鎖後再調整順序。"
            )
            return
        entries = self.group_entries_provider(self.current_group_name)
        if len(entries) < 2:
            self._show_group_setting_message(
                "目前至少需要兩個角色才能調整順序。"
            )
            return
        self._group_reorder_original = tuple(
            entry.entry_id for entry in entries
        )
        self._group_reorder_working = list(
            self._group_reorder_original
        )
        self._group_reorder_mode = True
        self._group_drag_entry_id = None
        self.build()
        self._show_group_setting_message(
            "請拖曳角色調整順序；完成前不會保存。"
        )

    def _cancel_group_entry_reorder(self) -> None:
        self._group_reorder_mode = False
        self._group_reorder_original = ()
        self._group_reorder_working = []
        self._group_drag_entry_id = None
        self.build()
        self._show_group_setting_message("已取消，原順序保持不變。")

    def _finish_group_entry_reorder(self) -> None:
        if (
            not self._group_reorder_mode
            or self.current_group_name is None
            or self.on_reorder_group_entries is None
        ):
            return
        proposed = tuple(self._group_reorder_working)
        if proposed == self._group_reorder_original:
            self._cancel_group_entry_reorder()
            return
        try:
            result = self.on_reorder_group_entries(
                self.current_group_name,
                proposed,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        if result is False or (
            isinstance(result, str) and result.strip()
        ):
            self._show_group_setting_message(
                result if isinstance(result, str) else "角色順序沒有保存。"
            )
            return
        self._group_reorder_mode = False
        self._group_reorder_original = ()
        self._group_reorder_working = []
        self._group_drag_entry_id = None
        self.build()
        self._show_group_setting_message("角色啟動順序已保存。")

    def _start_group_entry_drag(self, entry_id: str) -> None:
        if self._group_reorder_mode:
            self._group_drag_entry_id = entry_id

    def _bind_group_entry_drag_tree(
        self,
        widget,
        entry_id: str,
    ) -> None:
        widget._group_entry_id = entry_id
        try:
            widget.configure(cursor="fleur")
        except Exception:
            pass
        widget.bind(
            "<ButtonPress-1>",
            lambda _event, value=entry_id: self._start_group_entry_drag(
                value
            ),
        )
        widget.bind(
            "<ButtonRelease-1>",
            self._finish_group_entry_drag,
        )
        for child in widget.winfo_children():
            self._bind_group_entry_drag_tree(child, entry_id)

    def _finish_group_entry_drag(self, event) -> None:
        source_id = self._group_drag_entry_id
        self._group_drag_entry_id = None
        if not self._group_reorder_mode or source_id is None:
            return
        try:
            widget = self.parent.winfo_containing(
                event.x_root,
                event.y_root,
            )
        except Exception:
            return
        target_id = None
        while widget is not None:
            target_id = getattr(widget, "_group_entry_id", None)
            if target_id is not None:
                break
            widget = getattr(widget, "master", None)
        if (
            target_id is None
            or target_id == source_id
            or source_id not in self._group_reorder_working
            or target_id not in self._group_reorder_working
        ):
            return
        self._group_reorder_working = list(
            _reordered_entry_ids(
                tuple(self._group_reorder_working),
                source_id,
                target_id,
            )
        )
        self.refresh_group_entries()
        self._show_group_setting_message(
            "順序已調整，按「完成排序」才會保存。"
        )

    def _current_group_master_locked(self) -> bool:
        if (
            self.current_group_name is None
            or self.group_master_locked_provider is None
        ):
            return True
        value = self.group_master_locked_provider(
            self.current_group_name
        )
        if not isinstance(value, bool):
            raise TypeError(
                "group master locked provider returned an invalid value."
            )
        return value

    def _refresh_group_edit_controls(self) -> bool:
        locked = self._current_group_master_locked()
        has_group = self.current_group_name is not None
        editable = (
            has_group
            and not locked
            and not self._group_reorder_mode
            and not self._group_launch_running
        )
        state = NORMAL if editable else DISABLED
        for button in (
            self._group_add_button,
            self._group_clear_button,
            self._group_record_button,
        ):
            if button is not None:
                button.configure(state=state)
        if self._group_reorder_button is not None:
            self._group_reorder_button.configure(
                state=(
                    NORMAL
                    if (
                        has_group
                        and not locked
                        and not self._group_launch_running
                        and self.on_reorder_group_entries is not None
                    )
                    else DISABLED
                )
            )
        for button in (
            self._group_reorder_finish_button,
            self._group_reorder_cancel_button,
        ):
            if button is not None:
                button.configure(
                    state=(
                        NORMAL
                        if (
                            self._group_reorder_mode
                            and not self._group_launch_running
                        )
                        else DISABLED
                    )
                )
        if self._group_reorder_mode:
            for button in (
                self._group_launch_button,
                self._group_restore_button,
                self._group_record_button,
                self._group_stop_all_button,
            ):
                if button is not None:
                    button.configure(state=DISABLED)
        if self._group_master_lock_button is not None:
            self._group_master_lock_button.configure(
                text=(
                    "主窗：已上鎖"
                    if locked
                    else "主窗：未上鎖"
                ),
                state=(
                    NORMAL
                    if (
                        has_group
                        and self.on_group_master_locked_change is not None
                        and not self._group_reorder_mode
                        and not self._group_launch_running
                    )
                    else DISABLED
                ),
            )
        return locked

    def _toggle_group_master_locked(self) -> None:
        if (
            self.current_group_name is None
            or self.on_group_master_locked_change is None
        ):
            return
        locked = self._current_group_master_locked()
        try:
            result = self.on_group_master_locked_change(
                self.current_group_name,
                not locked,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()

    def _remove_group_shortcut(self, entry_id: str) -> None:
        if (
            self.current_group_name is None
            or self.on_remove_group_shortcut is None
        ):
            return
        try:
            result = self.on_remove_group_shortcut(
                self.current_group_name,
                entry_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()
        self.refresh_group_sync_relations()

    def refresh_group_entries(
        self,
    ) -> tuple[GroupConfigurationEntry, ...]:
        self.refresh_current_group_summary()
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
        if self._group_reorder_mode:
            entry_by_id = {
                entry.entry_id: entry for entry in entries
            }
            if set(self._group_reorder_working) == set(entry_by_id):
                entries = tuple(
                    entry_by_id[entry_id]
                    for entry_id in self._group_reorder_working
                )
        locked = self._refresh_group_edit_controls()
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
        for display_order, entry in enumerate(entries, start=1):
            row = Frame(frame, bg=BACKGROUND, padx=10, pady=7)
            row._group_entry_id = entry.entry_id
            row.pack(fill=X, pady=3)
            top_row = Frame(row, bg=BACKGROUND)
            top_row._group_entry_id = entry.entry_id
            top_row.pack(fill=X)
            Label(
                top_row,
                text=f"{display_order}. {entry.display_name}",
                font=("Microsoft JhengHei UI", 10, "bold"),
                bg=BACKGROUND,
                fg=TEXT,
                anchor="w",
            ).pack(side=LEFT, fill=X, expand=True)
            Label(
                top_row,
                text=entry.role,
                font=("Microsoft JhengHei UI", 9),
                bg=BACKGROUND,
                fg=MUTED,
            ).pack(side=LEFT, padx=10)
            remove_button = self._button(
                top_row,
                "移除",
                lambda value=entry.entry_id: self._remove_group_shortcut(
                    value
                ),
            )
            remove_button.pack(side=RIGHT)
            if locked or self._group_reorder_mode:
                remove_button.configure(state=DISABLED)
            if entry.role != "主窗口":
                main_button = self._button(
                    top_row,
                    "設為主窗口",
                    lambda value=entry.entry_id: self._set_group_main(
                        value
                    ),
                )
                main_button.pack(side=RIGHT, padx=(0, 6))
                if locked or self._group_reorder_mode:
                    main_button.configure(state=DISABLED)
            if self._group_reorder_mode:
                Label(
                    row,
                    text="拖曳此列到新的位置",
                    font=("Microsoft JhengHei UI", 9),
                    bg=BACKGROUND,
                    fg=MUTED,
                    anchor="w",
                ).pack(fill=X, pady=(6, 0))
                self._bind_group_entry_drag_tree(
                    row,
                    entry.entry_id,
                )
                continue
            settings_row = Frame(row, bg=BACKGROUND)
            settings_row.pack(fill=X, pady=(7, 0))
            enabled_variable = IntVar(
                master=self.parent,
                value=int(entry.sync_settings.offset_enabled),
            )
            Checkbutton(
                settings_row,
                text="啟用角色偏移",
                variable=enabled_variable,
                bg=BACKGROUND,
                fg=TEXT,
                activebackground=BACKGROUND,
                selectcolor=SURFACE,
                font=("Microsoft JhengHei UI", 9),
            ).pack(side=LEFT)

            def numeric_entry(value: int, width: int = 6) -> Entry:
                widget = Entry(
                    settings_row,
                    width=width,
                    font=("Microsoft JhengHei UI", 9),
                    bg=SURFACE,
                    fg=TEXT,
                    relief="flat",
                    bd=0,
                )
                widget.insert(0, str(value))
                widget.pack(side=LEFT, padx=(4, 8), ipady=4)
                return widget

            Label(
                settings_row,
                text="X",
                bg=BACKGROUND,
                fg=MUTED,
                font=("Microsoft JhengHei UI", 9),
            ).pack(side=LEFT)
            offset_x_entry = numeric_entry(entry.sync_settings.offset_x)
            Label(
                settings_row,
                text="Y",
                bg=BACKGROUND,
                fg=MUTED,
                font=("Microsoft JhengHei UI", 9),
            ).pack(side=LEFT)
            offset_y_entry = numeric_entry(entry.sync_settings.offset_y)
            Label(
                settings_row,
                text="延遲ms",
                bg=BACKGROUND,
                fg=MUTED,
                font=("Microsoft JhengHei UI", 9),
            ).pack(side=LEFT)
            delay_entry = numeric_entry(entry.sync_settings.delay_ms)
            self._button(
                settings_row,
                "套用",
                lambda value=entry.entry_id,
                enabled=enabled_variable,
                x_field=offset_x_entry,
                y_field=offset_y_entry,
                delay_field=delay_entry: self._save_sync_target_settings(
                    value,
                    enabled,
                    x_field,
                    y_field,
                    delay_field,
                ),
                primary=True,
            ).pack(side=LEFT, padx=(2, 0))
            self._button(
                settings_row,
                "清除",
                lambda value=entry.entry_id: self._clear_sync_target_settings(
                    value
                ),
            ).pack(side=LEFT, padx=(6, 0))
            if entry.role != "主窗口":
                self._button(
                    settings_row,
                    "取目標點（3秒）",
                    lambda value=entry.entry_id: (
                        self._start_sync_target_point_capture(value)
                    ),
                ).pack(side=LEFT, padx=(6, 0))
            role_row = Frame(row, bg=BACKGROUND)
            role_row.pack(fill=X, pady=(7, 0))
            Label(
                role_row,
                text="角色ID",
                bg=BACKGROUND,
                fg=MUTED,
                font=("Microsoft JhengHei UI", 9),
            ).pack(side=LEFT)
            role_id_entry = Entry(
                role_row,
                width=20,
                font=("Microsoft JhengHei UI", 9),
                bg=SURFACE,
                fg=TEXT,
                relief="flat",
                bd=0,
            )
            role_id_entry.insert(0, entry.role_id)
            role_id_entry.pack(side=LEFT, padx=(6, 8), ipady=4)
            self._button(
                role_row,
                "校正角色ID",
                lambda value=entry.entry_id,
                field=role_id_entry: self._calibrate_group_role_id(
                    value,
                    field,
                ),
            ).pack(side=LEFT)
            self._button(
                role_row,
                "讀取角色ID",
                lambda value=entry.entry_id: self._read_group_role_id(
                    value
                ),
            ).pack(side=LEFT, padx=(6, 0))
        return entries

    def _start_sync_base_point_capture(self) -> None:
        group_name = self.current_group_name
        if group_name is None or self.on_capture_sync_base_point is None:
            return
        self._show_group_setting_message(
            "請在 3 秒內把滑鼠移到主窗口的基準點。"
        )
        self.parent.after(
            3000,
            lambda selected_group=group_name: (
                self._complete_sync_base_point_capture(selected_group)
            ),
        )

    def _complete_sync_base_point_capture(self, group_name: str) -> None:
        if self.on_capture_sync_base_point is None:
            return
        try:
            result = self.on_capture_sync_base_point(group_name)
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)

    def _start_sync_target_point_capture(self, entry_id: str) -> None:
        group_name = self.current_group_name
        if group_name is None or self.on_capture_sync_target_point is None:
            return
        self._show_group_setting_message(
            "請在 3 秒內把滑鼠移到該角色窗口的對應位置。"
        )
        self.parent.after(
            3000,
            lambda selected_group=group_name,
            selected_entry=entry_id: (
                self._complete_sync_target_point_capture(
                    selected_group,
                    selected_entry,
                )
            ),
        )

    def _complete_sync_target_point_capture(
        self,
        group_name: str,
        entry_id: str,
    ) -> None:
        if self.on_capture_sync_target_point is None:
            return
        try:
            result = self.on_capture_sync_target_point(
                group_name,
                entry_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()

    def _save_sync_target_settings(
        self,
        entry_id: str,
        enabled: IntVar,
        offset_x: Entry,
        offset_y: Entry,
        delay: Entry,
    ) -> None:
        if (
            self.current_group_name is None
            or self.on_save_sync_target_settings is None
        ):
            return
        try:
            x_value = int(offset_x.get().strip())
            y_value = int(offset_y.get().strip())
            delay_value = int(delay.get().strip())
        except ValueError:
            self._show_group_setting_message(
                "偏移與延遲必須填入整數。"
            )
            return
        try:
            result = self.on_save_sync_target_settings(
                self.current_group_name,
                entry_id,
                bool(enabled.get()),
                x_value,
                y_value,
                delay_value,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()

    def _clear_sync_target_settings(self, entry_id: str) -> None:
        if (
            self.current_group_name is None
            or self.on_clear_sync_target_settings is None
        ):
            return
        try:
            result = self.on_clear_sync_target_settings(
                self.current_group_name,
                entry_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()

    def _calibrate_group_role_id(
        self,
        entry_id: str,
        field: Entry,
    ) -> None:
        if (
            self.current_group_name is None
            or self.on_calibrate_role_id is None
        ):
            return
        try:
            result = self.on_calibrate_role_id(
                self.current_group_name,
                entry_id,
                field.get(),
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()

    def _read_group_role_id(self, entry_id: str) -> None:
        if (
            self.current_group_name is None
            or self.on_read_role_id is None
        ):
            return
        try:
            result = self.on_read_role_id(
                self.current_group_name,
                entry_id,
            )
        except Exception as error:
            self._report_refresh_error(error)
            return
        self._show_group_setting_message(result)
        self.refresh_group_entries()

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
        self.refresh_current_group_summary()
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
        return text

    def _report_refresh_error(self, error: Exception) -> None:
        if self.on_refresh_error is None:
            raise error
        self.on_refresh_error(error)
