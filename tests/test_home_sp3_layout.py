import ast
import inspect
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from tkinter import DISABLED, Button, Checkbutton, Entry, Label, TclError, Tk

import pytest

from PIL import Image

from core.target_window_observation import TargetWindowObservation
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from habit.preference_service import (
    PlayerHabitObservationView,
    PlayerHabitPreferenceView,
    PlayerHabitSettingsView,
)
from services.character_view_service import PlayerCharacterView
from services.character_detail_view_service import PlayerCharacterDetail
from services.group_selection_service import PlayerGroupChoice
from services.group_role_status_service import GroupRoleStatus
from services.feature_card_layout_service import FeatureCardPreference
from services.game_time_timed_click_service import (
    GameTimeTimedClickSnapshot,
)
from services.smart_reconnect_monitor import (
    DEFAULT_SMART_RECONNECT_INTERVAL_MS,
)
from ui.home import (
    FeatureCardSettingsSaveResult,
    GroupManagementViewResult,
    SyncToggleViewResult,
    UI_THEME_LABELS,
    HomeView,
    _background_crop_boxes,
    _background_region,
    _blend_hex_color,
    _collapsed_card_title_pady,
    _contrast_ratio,
    _status_text_color,
    _contain_geometry,
    _feature_card_content_pady,
    _feature_card_control_offsets,
    _reordered_entry_ids,
    _safe_character_lines,
    _selected_sync_key_summary,
    _should_reset_feature_card_title,
    _workspace_state_text,
    theme_palette,
)
from workspace.models import WorkspaceState


class _ValueStub:
    def __init__(self, value: str):
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _EntryStub:
    def __init__(self, value: str):
        self.value = value

    def delete(self, _start, _end) -> None:
        self.value = ""

    def insert(self, _index, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _MenuStub:
    def __init__(self):
        self.commands: list[tuple[str, object]] = []

    def delete(self, _start, _end) -> None:
        self.commands.clear()

    def add_command(self, *, label: str, command) -> None:
        self.commands.append((label, command))


class _SelectorStub:
    def __init__(self):
        self.menu = _MenuStub()
        self.values: dict[str, object] = {}

    def __getitem__(self, key: str):
        if key != "menu":
            raise KeyError(key)
        return self.menu

    def configure(self, **values) -> None:
        self.values.update(values)


class _IntStub:
    def __init__(self, value: int):
        self.value = value

    def get(self) -> int:
        return self.value

    def set(self, value: int) -> None:
        self.value = value


class _ConfigureStub:
    def __init__(self):
        self.values: dict[str, object] = {}

    def configure(self, **values) -> None:
        self.values.update(values)


def test_home_uses_the_same_smart_reconnect_interval_default() -> None:
    parameter = inspect.signature(HomeView.__init__).parameters[
        "smart_reconnect_interval_ms"
    ]

    assert parameter.default == DEFAULT_SMART_RECONNECT_INTERVAL_MS


def test_hints_are_opt_in_and_have_a_persistent_toggle() -> None:
    parameter = inspect.signature(HomeView.__init__).parameters["show_hints"]
    assert parameter.default is False
    source = Path("ui/home.py").read_text(encoding="utf-8")
    assert "def _hint_label(" in source
    assert "self._apply_hints_visibility()" in source
    assert 'text="顯示提示說明"' in source
    assert "on_show_hints_change" in source


def test_show_hints_toggle_preserves_functional_state() -> None:
    view = object.__new__(HomeView)
    view._show_hints_variable = _IntStub(1)
    view.show_hints = False
    view.on_show_hints_change = lambda value: value
    applied: list[bool] = []
    view._apply_hints_visibility = lambda: applied.append(True)
    view._sync_page_scroll_region = lambda: applied.append(True)

    view._toggle_show_hints()

    assert view.show_hints is True
    assert view._show_hints_variable.get() == 1
    assert applied == [True, True]


def test_home_has_real_product_pages_and_group_selection() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    for label in ("首頁", "目前組別", "同步與重連", "角色資料", "設定"):
        assert label in source
    assert '"groups": "組別與視窗"' not in source
    assert '("groups", "組別與視窗")' not in source
    assert '"組別與遊戲視窗"' not in source
    assert "on_group_change" in source


def test_home_removes_redundant_heading_and_reserves_card_controls() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert 'self._page_heading(page, "今天要做什麼"' not in source
    assert "control_row = Frame(frame" in source
    assert "widgets.control_row.grid(" in source
    assert "widgets.control_row.pack(" in source
    assert "widgets.toggle_button.place" not in source
    assert "widgets.settings_button.place" not in source
    assert 'widgets.content_manager = "grid" if grid_children else "pack"' in source
    assert "main_action_row = Frame(row, bg=BACKGROUND)" in source
    assert 'widget.bind("<B1-Motion>", self._move_group_entry_drag)' in source
    assert 'text=f"正在移動：{display_name}"' in source
    assert "順序尚未調整；請拖曳到另一個角色位置。" in source
    assert "if isinstance(widget, (Button, Checkbutton, Entry)):" in source
    assert "character_choices" in source
    assert "_build_group_summary(sidebar)" not in source
    assert "_build_header(root)" not in source
    assert 'text="+"' not in source
    assert 'text="遊戲時間"' in source
    assert 'text="定時按下"' in source
    assert "設定按鈕位置" in source
    assert "啟用定時" in source
    assert "來源：系統時間" in source


def test_sync_key_section_has_saved_collapsed_summary_and_full_catalog() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert 'text="勾選要同步的遊戲按鍵"' in source
    assert '"展開設定"' in source
    assert '"收合設定"' in source
    assert "self.sync_keys_collapsed" in source
    assert "on_sync_keys_collapsed_change" in source
    assert "for shortcut in CONFIRMED_GAME_SHORTCUTS:" in source
    assert _selected_sync_key_summary(("SHIFT", "ESC", "未知", "ESC")) == (
        "ESC、SHIFT"
    )
    assert _selected_sync_key_summary(()) == "未勾選"


def test_home_overview_merges_group_primary_window_count_activity_and_next_step() -> None:
    view = object.__new__(HomeView)
    view.workspace_state = WorkspaceState(
        current_group=CharacterGroup("group-a", "甲組"),
        current_activity=ActivityDefinition(
            "daily-a",
            "每日任務",
            ActivityType.DAILY,
            ResetRule.DAILY_MIDNIGHT,
        ),
        next_step="查看提醒",
    )
    view.current_group_name = "甲組"
    view.group_entries_provider = lambda _name: (
        SimpleNamespace(display_name="主角色"),
        SimpleNamespace(display_name="副角色"),
    )

    assert view._home_overview_text() == (
        "目前組別：甲組\n"
        "主控：主角色\n"
        "視窗數：2\n"
        "目前活動：每日任務\n"
        "下一步：查看提醒"
    )


def test_sync_feedback_checks_first_and_failure_never_enables() -> None:
    view = object.__new__(HomeView)
    view.keyboard_sync_enabled = False
    view._keyboard_sync_status_message = ""
    view._keyboard_sync_status_color = ""
    view.parent = SimpleNamespace(update_idletasks=lambda: None)
    view.on_keyboard_sync_change = lambda _enabled: SyncToggleViewResult(
        False,
        False,
        "目前組別無法完整對應到唯一遊戲視窗；維持安全停止。",
    )
    snapshots: list[tuple[bool, str]] = []
    view._refresh_keyboard_sync_controls = lambda: snapshots.append(
        (view.keyboard_sync_enabled, view._keyboard_sync_status_message)
    )
    view._report_refresh_error = lambda _error: None

    view._toggle_keyboard_sync()

    assert snapshots[0] == (False, "正在檢查同步條件…")
    assert snapshots[-1] == (
        False,
        "目前組別無法完整對應到唯一遊戲視窗；維持安全停止。",
    )
    assert view.keyboard_sync_enabled is False


def test_ungrouped_join_failure_preserves_row_and_success_refreshes_once() -> None:
    view = object.__new__(HomeView)
    view.group_choices = (PlayerGroupChoice("group-a", "甲組", 1),)
    view.group_choices_provider = None
    messages: list[tuple[str, bool]] = []
    view._show_ungrouped_status = (
        lambda message, success=False: messages.append((message, success))
    )
    view._report_refresh_error = lambda _error: None
    refreshes: list[str] = []
    view.refresh_group_entries = lambda: refreshes.append("roles")
    view.refresh_group_sync_relations = lambda: refreshes.append("sync")
    view.refresh_ungrouped_windows = lambda: refreshes.append("ungrouped")
    view.on_add_ungrouped_window_to_group = lambda *_args: (
        GroupManagementViewResult(False, "甲組", "主窗上鎖中，未加入。")
    )

    view._add_ungrouped_window("fingerprint", "甲組")

    assert messages == [("主窗上鎖中，未加入。", False)]
    assert refreshes == []

    view.on_add_ungrouped_window_to_group = lambda *_args: (
        GroupManagementViewResult(True, "甲組", "角色甲已加入甲組。")
    )
    view._add_ungrouped_window("fingerprint", "甲組")

    assert messages[-1] == ("角色甲已加入甲組。", True)
    assert refreshes == ["roles", "sync", "ungrouped"]


def test_game_time_and_expand_controls_stay_in_their_required_layout() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "navigation_frame = Frame(sidebar" in source
    assert "self._build_game_time_sidebar(sidebar)" in source
    assert "card.pack(side=BOTTOM, fill=X" in source
    assert "self._build_game_time_card(page)" not in source
    assert "self._build_game_time_settings_card(page)" in source
    assert 'card_id="settings.game_time"' in source
    assert "self._build_timed_click_card(page)" in source
    assert 'self._feature_card_header_button(\n            "sync.input"' in source
    assert 'self._feature_card_header_button(\n            "home.schedule"' in source


def test_game_time_sidebar_has_only_title_and_current_value_at_900x620() -> None:
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.geometry("900x620+20+20")
    changes: list[tuple[int, bool]] = []

    def snapshot(
        offset_ms: int = 0,
        auto_update: bool = True,
    ) -> GameTimeTimedClickSnapshot:
        return GameTimeTimedClickSnapshot(
            offset_ms,
            auto_update,
            86_399_999,
            "23:59:59.999",
            None,
            False,
            None,
            120,
            2,
            250,
            0,
            "尚未啟用",
        )

    def save_settings(
        offset_ms: int,
        auto_update: bool,
    ) -> GameTimeTimedClickSnapshot:
        changes.append((offset_ms, auto_update))
        return snapshot(offset_ms, auto_update)

    try:
        view = HomeView(
            root,
            {"self_check_passed": True},
            game_time_snapshot_provider=snapshot,
            on_game_time_settings_change=save_settings,
        )
        view.build()
        root.deiconify()
        view._cancel_game_time_tick()
        view._poll_game_time()
        root.update()

        card = view._game_time_sidebar_card
        assert card is not None
        visible_children = tuple(
            child
            for child in card.winfo_children()
            if child.winfo_manager()
        )
        assert visible_children == (
            view._game_time_title_label,
            view._game_time_value_label,
        )
        assert all(isinstance(child, Label) for child in visible_children)
        assert tuple(child.cget("text") for child in visible_children) == (
            "遊戲時間",
            "23:59:59.999",
        )
        recursive_visible = []
        pending_sidebar = list(card.winfo_children())
        while pending_sidebar:
            child = pending_sidebar.pop(0)
            if child.winfo_manager():
                recursive_visible.append(child)
            pending_sidebar.extend(child.winfo_children())
        assert tuple(recursive_visible) == visible_children
        assert not any(
            isinstance(child, (Button, Checkbutton, Entry))
            for child in card.winfo_children()
        )

        settings_card = view._feature_cards["settings.game_time"].frame
        pending = list(settings_card.winfo_children())
        descendants = []
        while pending:
            child = pending.pop(0)
            descendants.append(child)
            pending.extend(child.winfo_children())
        assert view._game_time_offset_entry in descendants
        assert any(
            isinstance(child, Checkbutton)
            and child.cget("text") == "自動更新"
            for child in descendants
        )
        assert any(
            isinstance(child, Label)
            and child.cget("text") == "來源：系統時間"
            for child in descendants
        )
        view._game_time_offset_entry.delete(0, "end")
        view._game_time_offset_entry.insert(0, "250")
        view._game_time_auto_variable.set(0)
        view._apply_game_time_settings()
        assert changes == [(250, False)]

        assert (
            view._navigation_frame.winfo_rooty()
            + view._navigation_frame.winfo_height()
            <= card.winfo_rooty()
        )
        assert (
            card.winfo_rooty() + card.winfo_height()
            <= view._sidebar.winfo_rooty() + view._sidebar.winfo_height()
        )
    finally:
        root.destroy()


def test_character_detail_selection_replaces_and_can_be_closed() -> None:
    first = PlayerCharacterDetail("角色甲", "甲組", 120, "主號", "古", None)
    second = PlayerCharacterDetail("角色乙", "甲組", 110, "副號", "補", None)
    view = object.__new__(HomeView)
    view._selected_character_detail = None
    view._on_save_selected_character_note = None
    view._on_clear_selected_character_note = None
    view._on_selected_character_detail_error = None
    refreshes: list[str] = []
    view._refresh_characters_page = lambda: refreshes.append("refresh")
    view.show_page = lambda name: refreshes.append(name)

    view.show_character_detail(
        first,
        on_save_note=lambda _note: first,
        on_clear_note=lambda: first,
    )
    view.show_character_detail(
        second,
        on_save_note=lambda _note: second,
        on_clear_note=lambda: second,
    )
    assert view._selected_character_detail == second

    view.hide_character_detail()

    assert view._selected_character_detail is None
    assert view._on_save_selected_character_note is None
    assert refreshes == ["refresh", "characters", "refresh", "characters", "refresh"]
    source = Path("ui/home.py").read_text(encoding="utf-8")
    assert '"收起" if selected else "查看"' in source
    assert "if selected:\n                self._build_selected_character_detail(card)" in source


def test_current_group_page_replaces_sidebar_duplicate() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert 'title="目前總覽"' in source
    assert '("groups", "目前組別")' in source
    assert "主控：" in source
    assert "視窗數：" in source
    assert "_current_group_summary_text" in source


def test_failed_group_change_keeps_previous_group_selected() -> None:
    view = object.__new__(HomeView)
    view.group_choices = (
        PlayerGroupChoice("group-a", "甲組", 0),
        PlayerGroupChoice("group-b", "乙組", 0),
    )
    view.current_group_name = "甲組"
    view._group_variable = _ValueStub("乙組")
    messages: list[str] = []
    view._show_group_selection_message = (
        lambda message, **_kwargs: messages.append(message)
    )
    view.on_group_change = lambda _name: GroupManagementViewResult(
        False,
        "甲組",
        "自動操作尚未完全停止，未切換組別。",
    )

    view._select_group("乙組")

    assert view.current_group_name == "甲組"
    assert view._group_variable.value == "甲組"
    assert messages == ["自動操作尚未完全停止，未切換組別。"]


def test_successful_group_change_shows_warning_message() -> None:
    view = object.__new__(HomeView)
    view.group_choices = (
        PlayerGroupChoice("group-a", "甲組", 0),
        PlayerGroupChoice("group-b", "乙組", 0),
    )
    view.current_group_name = "甲組"
    view._group_variable = _ValueStub("乙組")
    view._group_value_label = _ConfigureStub()
    view._group_name_entry = _EntryStub("甲組")
    messages: list[str] = []
    view._show_group_selection_message = (
        lambda message, **_kwargs: messages.append(message)
    )
    view._refresh_group_selection_controls = lambda: None
    view.on_group_change = lambda _name: GroupManagementViewResult(
        True,
        "乙組",
        "已切換目前組別；此組視窗身分尚未完整，"
        "同步與智慧重連已保持停用。",
    )
    view.refresh_workspace = lambda: None
    view.refresh_current_group_summary = lambda: None
    view.refresh_group_entries = lambda: None
    view.refresh_ungrouped_windows = lambda: None
    view.refresh_group_sync_relations = lambda: None
    view.refresh_group_role_statuses = lambda: None
    view.refresh_operation_records = lambda: None

    view._select_group("乙組")

    assert view.current_group_name == "乙組"
    assert view._group_variable.value == "乙組"
    assert view._group_name_entry.value == "乙組"
    assert messages == [
        "已切換目前組別；此組視窗身分尚未完整，"
        "同步與智慧重連已保持停用。"
    ]


def test_smart_reconnect_stop_timeout_never_displays_safe_stop() -> None:
    view = object.__new__(HomeView)
    view.smart_reconnect_enabled = True
    view.on_smart_reconnect_change = lambda _enabled: False
    refreshes: list[bool] = []
    view._refresh_smart_reconnect_controls = lambda: refreshes.append(True)

    view._toggle_smart_reconnect()

    assert view.smart_reconnect_enabled is True
    assert refreshes == []


def test_smart_reconnect_enable_and_disable_update_the_header_immediately() -> None:
    view = object.__new__(HomeView)
    view.smart_reconnect_enabled = False
    view.smart_reconnect_runtime_status = None
    view._last_smart_reconnect_runtime_status = None
    view.on_smart_reconnect_change = lambda _enabled: True
    refreshed: list[tuple[bool, object]] = []
    view._refresh_smart_reconnect_controls = lambda: refreshed.append(
        (view.smart_reconnect_enabled, view.smart_reconnect_runtime_status)
    )

    view._toggle_smart_reconnect()
    assert refreshed[-1] == (True, "已開啟")

    view._toggle_smart_reconnect()
    assert refreshed[-1] == (False, None)


def test_invalid_runtime_status_preserves_legal_state_or_fails_closed() -> None:
    view = object.__new__(HomeView)
    view.smart_reconnect_enabled = True
    view.smart_reconnect_runtime_status = "重連中"
    view._last_smart_reconnect_runtime_status = "重連中"
    view._refresh_smart_reconnect_controls = lambda: None

    view.set_smart_reconnect_runtime_status(object())
    assert view.smart_reconnect_runtime_status == "重連中"

    view._last_smart_reconnect_runtime_status = None
    view.set_smart_reconnect_runtime_status(object())
    assert view.smart_reconnect_runtime_status == "重連失敗"


def test_bad_saved_status_colors_use_defaults_and_rejected_edit_is_restored() -> None:
    loaded = HomeView(
        None,
        {},
        smart_reconnect_status_colors={
            "已開啟": "錯誤",
            "重連中": "#ffffff",
            "重連失敗": 123,
        },
    )
    assert loaded.smart_reconnect_status_colors["已開啟"] != "錯誤"
    assert loaded.smart_reconnect_status_colors["重連中"] == "#FFFFFF"
    assert loaded.smart_reconnect_status_colors["重連失敗"] == "#D64545"

    view = object.__new__(HomeView)
    view.smart_reconnect_status_colors = {
        "已開啟": "#112233",
        "重連中": "#445566",
        "重連失敗": "#778899",
    }
    view._smart_reconnect_color_entries = {
        name: _EntryStub("不是色碼")
        for name in view.smart_reconnect_status_colors
    }
    view._smart_reconnect_label = _ConfigureStub()
    view.on_smart_reconnect_status_colors_change = lambda _colors: False

    view._save_smart_reconnect_status_colors()

    assert {
        name: entry.get()
        for name, entry in view._smart_reconnect_color_entries.items()
    } == view.smart_reconnect_status_colors
    assert "已恢復原設定" in view._smart_reconnect_label.values["text"]


def test_smart_reconnect_capture_modes_are_checkbox_settings_with_clear_status():
    source = Path("ui/home.py").read_text(encoding="utf-8")

    for text in (
        "勾選要啟用的斷線檢查方式",
        "前景／完整可見",
        "被其他視窗遮擋",
        "已最小化",
        "已開啟",
        "已關閉",
    ):
        assert text in source
    assert "on_smart_reconnect_capture_modes_change" in source


def test_capture_mode_change_saves_all_three_choices_as_one_setting():
    view = object.__new__(HomeView)
    view.smart_reconnect_capture_modes = {
        "visible": True,
        "obscured": True,
        "minimized": True,
    }
    view._smart_reconnect_capture_mode_variables = {
        "visible": _IntStub(1),
        "obscured": _IntStub(0),
        "minimized": _IntStub(1),
    }
    view._smart_reconnect_capture_mode_status_label = _ConfigureStub()
    saved: list[dict[str, bool]] = []
    view.on_smart_reconnect_capture_modes_change = (
        lambda modes: saved.append(dict(modes)) or True
    )

    view._save_smart_reconnect_capture_modes()

    assert saved == [
        {
            "visible": True,
            "obscured": False,
            "minimized": True,
        }
    ]
    assert view.smart_reconnect_capture_modes == saved[0]
    assert (
        "被其他視窗遮擋－已關閉"
        in view._smart_reconnect_capture_mode_status_label.values["text"]
    )


def test_rejected_capture_mode_change_restores_previous_checkboxes():
    view = object.__new__(HomeView)
    view.smart_reconnect_capture_modes = {
        "visible": True,
        "obscured": True,
        "minimized": True,
    }
    view._smart_reconnect_capture_mode_variables = {
        "visible": _IntStub(0),
        "obscured": _IntStub(0),
        "minimized": _IntStub(0),
    }
    view._smart_reconnect_capture_mode_status_label = _ConfigureStub()
    view.on_smart_reconnect_capture_modes_change = lambda _modes: False

    view._save_smart_reconnect_capture_modes()

    assert all(
        variable.get() == 1
        for variable in view._smart_reconnect_capture_mode_variables.values()
    )
    assert all(view.smart_reconnect_capture_modes.values())


def test_all_pages_share_vertical_scroll_and_group_launch_action() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "Canvas(" in source
    assert "Scrollbar(" in source
    assert "yscrollcommand=scrollbar.set" in source
    assert "self._on_page_mousewheel" in source
    assert "啟動本組" in source
    assert "恢復上次位置" in source
    assert "記錄目前位置" in source
    assert "還原／調整遊戲視窗尺寸" in source
    assert "取主窗尺寸" in source
    assert "套用目前組" in source
    assert "套用全部遊戲視窗" in source
    assert "新視窗自動套用" in source
    for label in ("新增組", "改名", "上移", "下移", "刪除組"):
        assert label in source
    assert "匯出組別設定" in source
    assert "匯入組別設定" in source
    assert "匯入時同名組別會直接更新；舊版設定保持不變。" in source
    assert "on_export_group_configuration" in source
    assert "on_import_group_configuration" in source
    assert "整組啟動快捷鍵" in source
    assert "恢復上次位置" in source
    assert "停止全部受管遊戲" in source
    assert '"停止同步"' in source
    assert "self._stop_sync_from_group_page" in source
    assert "on_stop_all_managed_games" in source
    assert "group_launch_hotkey_provider" in source
    assert "on_group_launch_hotkey_change" in source
    assert "調整順序" in source
    assert "完成排序" in source
    assert "on_reorder_group_entries" in source
    assert "主窗：已上鎖" in source
    assert "主窗：未上鎖" in source
    assert "group_master_locked_provider" in source
    assert "on_group_master_locked_change" in source
    assert "設為主窗口" in source
    assert "清空角色" in source
    assert "設定主基準點（3秒）" in source
    assert "啟用角色偏移" in source
    assert "取目標點（3秒）" in source
    assert "校正角色 ID" in source
    assert "讀取角色 ID" in source


def test_group_drag_order_is_preview_only_until_saved() -> None:
    original = ("甲", "乙", "丙", "丁")

    assert _reordered_entry_ids(original, "丁", "乙") == (
        "甲",
        "丁",
        "乙",
        "丙",
    )
    assert _reordered_entry_ids(original, "甲", "丁") == (
        "乙",
        "丙",
        "甲",
        "丁",
    )
    assert _reordered_entry_ids(original, "甲", "未知") == original
    assert _reordered_entry_ids(
        original,
        "甲",
        "乙",
        after=True,
    ) == ("乙", "甲", "丙", "丁")
    assert _reordered_entry_ids(
        original,
        "乙",
        "甲",
        after=False,
    ) == ("乙", "甲", "丙", "丁")


def test_role_rows_are_compact_by_default_and_keep_role_id_actions_visible() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "group_role_details_expanded_provider" in source
    assert "on_group_role_details_expanded_change" in source
    assert '"收起設定" if expanded else "展開設定"' in source
    assert "if expanded:" in source
    assert 'text="角色 ID"' in source
    assert '"校正角色 ID"' in source
    assert '"讀取角色 ID"' in source
    assert "text=entry.entry_id" not in source
    assert 'text=f"{entry.entry_id}"' not in source


def test_role_row_expand_change_is_saved_before_refresh() -> None:
    view = object.__new__(HomeView)
    calls: list[tuple[str, bool] | str] = []
    view.on_group_role_details_expanded_change = (
        lambda entry_id, expanded: calls.append((entry_id, expanded)) or True
    )
    view.refresh_group_entries = lambda: calls.append("refresh")

    view._toggle_group_role_details("role-a", True)

    assert calls == [("role-a", True), "refresh"]


def test_role_id_actions_report_next_to_the_pressed_role_before_refresh() -> None:
    view = object.__new__(HomeView)
    view.current_group_name = "group-a"
    view._role_id_messages = {}
    view.on_calibrate_role_id = (
        lambda group_name, entry_id: f"已校正遊戲內角色：{group_name}／{entry_id}"
    )
    refreshes: list[bool] = []
    view.refresh_group_entries = lambda: refreshes.append(True)

    view._calibrate_group_role_id("role-a")

    assert view._role_id_messages == {
        "role-a": "已校正遊戲內角色：group-a／role-a"
    }
    assert refreshes == [True]


def test_background_controls_and_cached_canvas_rendering_are_wired() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "選擇背景圖片" in source
    assert "清除背景" in source
    assert "目前背景：" in source
    assert "ImageTk.PhotoImage" in source
    assert "Image.Resampling.LANCZOS" in source
    assert "self._background_source_image" in source
    assert "self._background_resize_id" in source
    assert "canvas.tag_lower(item)" in source
    assert "def _position_background_layers" in source
    assert "document_top" in source
    assert "_render_background_widget_tree" in source
    assert "_background_widget_render_keys" in source
    assert "_background_widget_rendering" in source
    assert "messagebox.show" not in source[source.index(
        "def _choose_background_image"
    ):source.index("def dispose")]


def test_feature_cards_share_persistent_collapse_drag_and_customization() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "feature_card_preference_provider" in source
    assert "feature_card_order_provider" in source
    assert "on_feature_card_collapsed_change" in source
    assert "on_feature_card_order_change" in source
    assert "_start_feature_card_drag" in source
    assert "_finish_feature_card_drag" in source
    assert "儲存卡片設定" in source
    assert "選擇卡片背景" in source
    assert "移除卡片背景" in source
    assert "卡片背景已預覽" in source
    assert "_close_feature_card_settings" in source
    settings_source = source[source.index("def _open_feature_card_settings"):]
    assert "dialog = Frame(widgets.frame" in settings_source
    assert "Toplevel(" not in settings_source
    assert "grab_set" not in settings_source
    assert "control_row = Frame(frame" in source
    assert "settings_button.pack(side=RIGHT)" in source
    assert "card_id=card_id" in source
    assert 'card_id == "groups.current"' in source
    assert "_should_reset_feature_card_title" in source
    assert "_feature_card_control_offsets" in source
    assert "on_save_feature_card_settings" in source
    assert "clear_background=bool(clear_background)" in source
    for card_id in (
        "home.workspace",
        "groups.roles",
        "sync.input",
        "characters.list",
        "records.search",
        "settings.background",
    ):
        assert f'card_id="{card_id}"' in source


def test_home_feature_cards_use_draggable_sections_in_one_stack() -> None:
    tree = ast.parse(Path("ui/home.py").read_text(encoding="utf-8"))
    home_sections: dict[str, tuple[str, str]] = {}
    other_parents: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_card"
        ):
            card_id = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "card_id"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            if card_id is None:
                continue
            order_frame = next(
                (
                    ast.unparse(keyword.value)
                    for keyword in node.keywords
                    if keyword.arg == "order_frame"
                ),
                "",
            )
            if card_id.startswith("home."):
                home_sections[card_id] = (
                    ast.unparse(node.args[0]),
                    order_frame,
                )
            else:
                other_parents.append(ast.unparse(node.args[0]))

    assert home_sections == {
        "home.workspace": ("workspace_section", "workspace_section"),
        "home.roles": ("role_section", "role_section"),
        "home.schedule": ("schedule_section", "schedule_section"),
        "home.reminders": ("reminder_section", "reminder_section"),
    }
    assert set(other_parents) == {"page", "self._group_management_details_frame"}


def test_drag_moves_the_whole_feature_section_and_keeps_its_heading() -> None:
    class Parent:
        def __init__(self) -> None:
            self.children = []

    class Section:
        def __init__(self, parent, name: str, heading: str) -> None:
            self.master = parent
            self.name = name
            self.heading = heading
            parent.children.append(self)

        def winfo_manager(self) -> str:
            return "pack"

        def winfo_rootx(self) -> int:
            return 0

        def winfo_rooty(self) -> int:
            return self.master.children.index(self) * 100

        def winfo_width(self) -> int:
            return 500

        def winfo_height(self) -> int:
            return 80

        def pack_configure(self, *, before=None, after=None) -> None:
            self.master.children.remove(self)
            target = before if before is not None else after
            index = self.master.children.index(target)
            if after is not None:
                index += 1
            self.master.children.insert(index, self)

    parent = Parent()
    first_section = Section(parent, "first", "第一段標題")
    second_section = Section(parent, "second", "第二段標題")
    view = object.__new__(HomeView)
    view._feature_card_drag_id = "home.first"
    view._feature_cards_by_page = {
        "home": ["home.first", "home.second"],
    }
    view._feature_cards = {
        "home.first": SimpleNamespace(
            card_id="home.first",
            page="home",
            frame=SimpleNamespace(master=object()),
            order_frame=first_section,
        ),
        "home.second": SimpleNamespace(
            card_id="home.second",
            page="home",
            frame=SimpleNamespace(master=object()),
            order_frame=second_section,
        ),
    }
    saved_orders: list[tuple[str, ...]] = []
    view.on_feature_card_order_change = (
        lambda _page, order, _available: saved_orders.append(order)
    )
    view._sync_page_scroll_region = lambda: None
    view._report_refresh_error = lambda _error: None

    view._finish_feature_card_drag(
        "home.first",
        SimpleNamespace(x_root=10, y_root=190),
    )

    assert parent.children == [second_section, first_section]
    assert saved_orders == [("home.second", "home.first")]
    assert first_section.heading == "第一段標題"
    assert second_section.heading == "第二段標題"


def test_card_selector_rebuilds_real_menu_and_keeps_duplicate_titles_unique():
    view = object.__new__(HomeView)
    view._feature_cards = {
        "sync.first": SimpleNamespace(
            default_title="第一張",
            page="sync",
        ),
        "sync.second": SimpleNamespace(
            default_title="第二張",
            page="sync",
        ),
    }
    view.feature_card_preference_provider = (
        lambda card_id, _default: FeatureCardPreference(
            card_id,
            "相同名稱",
            False,
        )
    )
    view._feature_card_choice_ids = {}
    view._feature_card_variable = _ValueStub("")
    view._feature_card_selector = _SelectorStub()
    view._refresh_feature_card_settings = lambda: None

    view._rebuild_feature_card_selector("sync.second")

    labels = tuple(view._feature_card_choice_ids)
    assert labels == (
        "同步與重連｜相同名稱",
        "同步與重連｜相同名稱（2）",
    )
    assert tuple(
        label
        for label, _command in view._feature_card_selector.menu.commands
    ) == labels
    assert view._feature_card_variable.get() == labels[1]
    assert view._feature_card_choice_ids[labels[0]] == "sync.first"
    assert view._feature_card_choice_ids[labels[1]] == "sync.second"
    view._feature_card_selector.menu.commands[0][1]()
    assert view._feature_card_variable.get() == labels[0]


def test_player_habit_settings_use_confirmed_thresholds_and_clear_confirmation() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "def _build_habit_settings_card" in source
    assert "玩家可能想記錄的習慣" in source
    assert "_toggle_habit_management" in source
    assert "self._build_habit_settings_card(page)" in source
    assert "前七個有效日只觀察活動時間與角色操作順序" in source
    assert "第八天才提出建議" in source
    assert "同一習慣至少" in source
    assert "全部清除玩家習慣" in source
    assert "確定清除全部玩家習慣" in source
    assert "最近可信觀察" in source
    assert "活動完成事件" in source
    assert "on_habit_observation_days_update" in source
    assert "on_modify_habit_preference" in source
    assert "on_remove_habit_observation" in source
    assert "儲存修改" in source
    assert "刪除紀錄" in source


def test_habit_settings_card_is_first_and_keeps_existing_controls_inline() -> None:
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    settings = PlayerHabitSettingsView(
        7,
        7,
        7,
        (
            PlayerHabitPreferenceView(
                "first", "活動時間", "第一位角色", ("早上",), "", "保存"
            ),
            PlayerHabitPreferenceView(
                "second", "操作順序", "第二位角色", ("第二",), "", "保存"
            ),
        ),
        (
            PlayerHabitObservationView(
                "observation",
                datetime(2026, 8, 4, 9, 0),
                "活動時間",
                "第一位角色",
                ("早上",),
                False,
                (),
            ),
        ),
    )
    try:
        view = HomeView(
            root,
            {"self_check_passed": True},
            habit_settings_provider=lambda: settings,
        )
        view.build()
        root.update_idletasks()
        habit = view._feature_cards["settings.habits"]
        theme = view._feature_cards["settings.theme"]
        assert habit.title_label.cget("text") == "玩家可能想記錄的習慣"
        assert view._pages["settings"].winfo_children().index(habit.frame) < (
            view._pages["settings"].winfo_children().index(theme.frame)
        )
        assert view._habit_status_label.cget("text") == "已記錄 2 項"
        assert view._habit_management_frame.winfo_manager() == ""
        assert view._habit_management_button.cget("text") == "管理習慣"
        view._toggle_habit_management()
        root.update_idletasks()
        assert view._habit_management_frame.winfo_manager() == "pack"
        descendants = [view._habit_management_frame]
        for container in descendants:
            descendants.extend(container.winfo_children())
        texts = {
            child.cget("text")
            for child in descendants
            if isinstance(child, Button)
        }
        assert {"儲存", "儲存修改", "刪除", "刪除紀錄", "全部清除玩家習慣"} <= texts
        assert view._habit_observation_days_entry.winfo_manager() == "pack"
        assert view._habit_preferences_frame.winfo_manager() == "pack"
        view._toggle_habit_management()
        assert view._habit_management_frame.winfo_manager() == ""
        assert view._habit_status_label.cget("text") == "已記錄 2 項"
        assert tuple(view._feature_cards).count("settings.habits") == 1
        view._render_habit_preferences(PlayerHabitSettingsView(7, 7, 7, ()))
        assert view._habit_status_label.cget("text") == "尚無已記錄習慣"
    finally:
        root.destroy()


def test_habit_changes_refresh_the_same_summary_renderer(monkeypatch) -> None:
    view = object.__new__(HomeView)
    updated = PlayerHabitSettingsView(9, 7, 7, ())
    rendered: list[PlayerHabitSettingsView] = []
    view._habit_observation_days_entry = _EntryStub("9")
    view._render_habit_preferences = rendered.append
    view._report_refresh_error = lambda _error: pytest.fail("不應回報錯誤")
    view.on_habit_observation_days_update = lambda _days: updated
    view.on_remove_habit_preference = lambda _preference_id: updated
    view.on_remove_habit_observation = lambda _observation_id: updated
    view.on_clear_habit_preferences = lambda: updated
    view.parent = None

    view._save_habit_observation_days()
    view._remove_habit_preference("preference")
    view._remove_habit_observation("observation")
    monkeypatch.setattr(
        "ui.home.messagebox.askyesno",
        lambda *_args, **_kwargs: True,
    )
    view._clear_habit_preferences()

    assert rendered == [updated, updated, updated, updated]


def test_background_contain_geometry_never_crops_or_upscales() -> None:
    assert _contain_geometry((200, 100), (100, 100)) == (100, 50, 0, 25)


def test_background_region_opacity_blends_legacy_color_over_image() -> None:
    assert _blend_hex_color("#C9A35D", "#000000", 0) == "#000000"
    assert _blend_hex_color("#C9A35D", "#000000", 100) == "#C9A35D"
    assert _contrast_ratio("#000000", "#FFFFFF") == 21
    assert _contain_geometry((100, 200), (100, 100)) == (50, 100, 25, 0)
    assert _contain_geometry((80, 60), (320, 240)) == (80, 60, 120, 90)


def test_smart_reconnect_status_uses_readable_extreme_color_text() -> None:
    assert _status_text_color("#000000") == "#FFFFFF"
    assert _status_text_color("#FFFFFF") == "#000000"


def test_background_region_preserves_real_image_details_and_alignment() -> None:
    source = Image.new("RGB", (4, 2))
    source.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (10, 20, 30),
            (40, 50, 60),
            (70, 80, 90),
            (100, 110, 120),
        ]
    )

    region = _background_region(
        source,
        (1, 0, 3, 2),
        fill="#000000",
    )

    assert list(region.get_flattened_data()) == [
        (0, 255, 0),
        (0, 0, 255),
        (40, 50, 60),
        (70, 80, 90),
    ]


def test_background_crop_uses_actual_content_and_sidebar_coordinates() -> None:
    content, sidebar = _background_crop_boxes(
        (1040, 749),
        content_box=(198, 20, 803, 709),
        sidebar_box=(0, 0, 176, 749),
    )

    assert content == (198, 20, 1001, 729)
    assert sidebar == (0, 0, 176, 749)


def test_role_row_region_follows_widget_ancestor_direction() -> None:
    view = object.__new__(HomeView)
    role_frame = SimpleNamespace(master=None)
    role_label = SimpleNamespace(master=role_frame)
    nested_label = SimpleNamespace(master=role_label)
    outsider = SimpleNamespace(master=None)
    view._home_role_rows_frame = role_frame

    assert view._background_widget_region_name(nested_label) == "role_row"
    assert view._background_widget_region_name(outsider) == "panel"


def test_background_render_schedule_keeps_only_one_pending_job() -> None:
    class ParentStub:
        def __init__(self) -> None:
            self.next_id = 0
            self.pending: dict[str, object] = {}

        def after(self, _delay: int, callback) -> str:
            self.next_id += 1
            job_id = f"job-{self.next_id}"
            self.pending[job_id] = callback
            return job_id

        def after_cancel(self, job_id: str) -> None:
            self.pending.pop(job_id, None)

    parent = ParentStub()
    view = object.__new__(HomeView)
    view.parent = parent
    view._background_panel_display_image = object()
    view._background_widget_render_id = None
    view._render_background_widget_images = lambda: None

    for _ in range(10):
        view._schedule_background_widget_images(delay_ms=60)

    assert len(parent.pending) == 1
    assert view._background_widget_render_id in parent.pending


def test_independent_card_children_use_their_own_background_source() -> None:
    class WidgetStub:
        @staticmethod
        def winfo_ismapped() -> bool:
            return True

        @staticmethod
        def winfo_rootx() -> int:
            return 20

        @staticmethod
        def winfo_rooty() -> int:
            return 30

    class CanvasStub:
        @staticmethod
        def winfo_rootx() -> int:
            return 10

        @staticmethod
        def winfo_rooty() -> int:
            return 10

    panel = Image.new("RGB", (100, 100), "#101010")
    card_source = Image.new("RGB", (60, 40), "#E0E0E0")
    view = object.__new__(HomeView)
    view._background_panel_display_image = panel
    view._background_panel_source_image = panel
    view._background_sidebar_source_image = None
    view._page_canvas = CanvasStub()
    view._active_page = "home"
    frame = WidgetStub()
    card = SimpleNamespace(
        frame=frame,
        background_label=None,
        background_source=card_source,
        background_render_source=card_source,
        background_generation=7,
    )
    view._feature_cards = {"home.card": card}
    view._feature_cards_by_page = {"home": ["home.card"]}
    page = WidgetStub()
    view._pages = {"home": page}
    view._background_sidebar_label = None
    view._background_widget_render_keys = {}
    calls: list[tuple[object, dict[str, object]]] = []
    view._render_background_widget_tree = (
        lambda widget, **kwargs: calls.append((widget, kwargs))
    )

    try:
        view._render_background_widget_images_now()
    finally:
        panel.close()
        card_source.close()

    card_call = next(kwargs for widget, kwargs in calls if widget is frame)
    assert card_call["panel_source"] is card_source
    assert card_call["source_name"] == "card:home.card:7"
    assert card_call["allow_independent_root"] is True
    page_call = next(kwargs for widget, kwargs in calls if widget is page)
    assert page_call["source_name"] == "panel"


def test_card_title_reset_only_survives_when_default_text_is_unchanged() -> None:
    assert _should_reset_feature_card_title(True, "原名稱", "原名稱")
    assert not _should_reset_feature_card_title(True, "新名稱", "原名稱")
    assert not _should_reset_feature_card_title(False, "原名稱", "原名稱")


def test_feature_card_buttons_and_collapsed_title_keep_safe_spacing() -> None:
    toggle_offset, settings_offset = _feature_card_control_offsets(62)

    assert toggle_offset == -8
    assert settings_offset == -76
    assert abs(settings_offset) - abs(toggle_offset) - 62 == 6
    assert _feature_card_content_pady(12, 37) >= 6 + 37 + 8
    assert _feature_card_content_pady(80, 37) == 80


def test_full_home_build_keeps_grid_and_pack_cards_safe_when_settings_toggle():
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    try:
        view = HomeView(root, {"self_check_passed": True})
        view.build()
        root.update_idletasks()
        grid_card = view._feature_cards["groups.list"]
        pack_card = view._feature_cards["sync.input"]
        assert grid_card.content_manager == "grid"
        assert pack_card.content_manager == "pack"
        baseline_rows = {
            child: int(child.grid_info().get("row", 0))
            for child in grid_card.frame.grid_slaves()
            if child is not grid_card.control_row
        }
        view._open_feature_card_settings("groups.list")
        root.update_idletasks()
        assert view._feature_card_settings_dialog is not None
        view._open_feature_card_settings("groups.list")
        assert view._feature_card_settings_dialog is None
        assert {
            child: int(child.grid_info().get("row", 0))
            for child in grid_card.frame.grid_slaves()
            if child is not grid_card.control_row
        } == baseline_rows
        view._open_feature_card_settings("groups.list")
        view._open_feature_card_settings("sync.input")
        root.update_idletasks()
        assert view._feature_card_settings_dialog is not None
        assert {
            child: int(child.grid_info().get("row", 0))
            for child in grid_card.frame.grid_slaves()
            if child is not grid_card.control_row
        } == baseline_rows
    finally:
        root.destroy()


def test_clean_build_collapses_cards_but_keeps_required_statuses_visible():
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    try:
        view = HomeView(
            root,
            {"self_check_passed": True},
            group_choices=(PlayerGroupChoice("group-a", "甲組", 1),),
            current_group_name="甲組",
        )
        view.build()
        root.update_idletasks()

        assert all(card.collapsed for card in view._feature_cards.values())
        assert view._keyboard_sync_label.winfo_manager() == "pack"
        assert view._ungrouped_status_label.winfo_manager() == "pack"
        assert view._group_selection_status_label.winfo_manager() == "grid"
        assert view._sync_key_count_label in view._hint_labels
        assert view._sync_key_summary_label in view._hint_labels
        assert view._keyboard_sync_label not in view._hint_labels
        assert (
            view._sync_key_toggle_button.master
            is view._feature_cards["sync.input"].control_row
        )
        assert (
            view._activity_schedule_toggle_button.master
            is view._feature_cards["home.schedule"].control_row
        )
        current_button = view._group_selection_buttons["甲組"]
        assert current_button.cget("text") == "目前使用"
        assert current_button.cget("state") == DISABLED
        view._set_feature_card_collapsed(
            view._feature_cards["groups.list"],
            False,
            persist=False,
        )
        assert int(current_button.master.grid_info()["row"]) >= 2
    finally:
        root.destroy()


def test_current_group_management_starts_hidden_and_toggles_inline():
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    try:
        view = HomeView(root, {"self_check_passed": True})
        view.build()
        assert view._group_management_frame.winfo_manager() == ""
        assert view._group_management_button.cget("text") == "管理組別"
        view._toggle_group_management()
        assert view._group_management_frame.winfo_manager() == "pack"
        assert view._group_management_button.cget("text") == "收起管理"
        for button in (view._group_restore_button, view._group_record_button):
            assert button.winfo_manager() == "pack"
        view._toggle_group_management()
        assert view._group_management_frame.winfo_manager() == ""
    finally:
        root.destroy()


def test_current_group_details_toggle_without_hiding_external_group_lists():
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    try:
        view = HomeView(root, {"self_check_passed": True})
        view.build()
        assert view._group_management_details_frame.winfo_manager() == ""
        view._toggle_group_management()
        assert view._group_management_details_frame.winfo_manager() == "pack"
        assert view._feature_cards["groups.roles"].frame.master is view._group_management_details_frame
        assert view._feature_cards["groups.extended_sync"].frame.master is view._group_management_details_frame
        assert view._feature_cards["groups.window_size"].frame.master is view._group_management_details_frame
        assert view._feature_cards["groups.ungrouped_windows"].frame.winfo_manager() == "pack"
        view._toggle_group_management()
        assert view._group_management_details_frame.winfo_manager() == ""
    finally:
        root.destroy()


def test_group_management_action_rows_fit_three_buttons_at_narrow_width():
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.geometry("760x600")
    root.withdraw()
    try:
        view = HomeView(root, {"self_check_passed": True})
        view.build()
        view._toggle_group_management()
        root.update_idletasks()
        rows = (
            view._group_stop_all_button.master,
            view._group_add_button.master,
            view._group_clear_button.master,
        )
        for row in rows:
            assert sum(isinstance(child, Button) for child in row.winfo_children()) <= 3
    finally:
        root.destroy()
    required_pady = _collapsed_card_title_pady(24, 2, 31)
    collapsed_height = 24 - 4 + required_pady * 2
    assert collapsed_height >= 31 + 12


def test_batch_card_settings_commit_once_and_updates_only_after_success() -> None:
    view = object.__new__(HomeView)
    title_updates: list[str] = []
    loads: list[str] = []
    callback_calls: list[dict[str, object]] = []
    widgets = SimpleNamespace(
        default_title="預設名稱",
        collapsed=False,
        title_label=SimpleNamespace(
            configure=lambda **values: title_updates.append(values["text"])
        ),
        page="settings",
    )
    view._feature_cards = {"settings.card": widgets}
    view._feature_card_title_entry = _EntryStub("舊名稱")
    view._feature_card_save_error = ""
    view._pending_card_background_path = Path("preview.png")
    view._pending_card_background_id = "settings.card"
    view._pending_card_background_clear_id = None
    view._feature_card_status_label = None
    view._feature_card_choice_ids = {
        "設定｜舊名稱": "settings.card",
    }
    view._feature_card_variable = _ValueStub("設定｜舊名稱")
    view._feature_card_selector = None
    view.feature_card_preference_provider = (
        lambda card_id, _default: FeatureCardPreference(
            card_id,
            "新名稱",
            False,
        )
    )
    view.feature_hotkeys = {"sync": "F1"}
    view._feature_hotkey_variables = {}
    view._group_launch_hotkey_variable = None
    view._load_feature_card_background = loads.append
    view._report_refresh_error = lambda _error: None

    def save_batch(**values):
        callback_calls.append(values)
        return FeatureCardSettingsSaveResult(
            True,
            "全部儲存。",
            preference=FeatureCardPreference(
                "settings.card",
                "新名稱",
                False,
            ),
            background_path=Path("saved.png"),
            hotkey="F2",
        )

    view.on_save_feature_card_settings = save_batch

    assert view._save_feature_card_settings(
        card_id="settings.card",
        title="新名稱",
        hotkey_feature="sync",
        hotkey="F2",
    )
    assert len(callback_calls) == 1
    assert callback_calls[0]["pending_background_path"] == Path(
        "preview.png"
    )
    assert callback_calls[0]["clear_background"] is False
    assert view._pending_card_background_path is None
    assert view._pending_card_background_id is None
    assert view.feature_hotkeys["sync"] == "F2"
    assert title_updates == ["新名稱"]
    assert loads == ["settings.card"]
    assert view._feature_card_title_entry.get() == "新名稱"
    assert view._feature_card_changes_dirty() is False


def test_failed_batch_card_settings_keeps_valid_pending_and_visible_state(
    tmp_path,
) -> None:
    view = object.__new__(HomeView)
    title_updates: list[str] = []
    loads: list[str] = []
    widgets = SimpleNamespace(
        default_title="預設名稱",
        collapsed=False,
        title_label=SimpleNamespace(
            configure=lambda **values: title_updates.append(values["text"])
        ),
        page="settings",
    )
    view._feature_cards = {"settings.card": widgets}
    view._feature_card_title_entry = None
    view._feature_card_save_error = ""
    preview_path = tmp_path / "preview.png"
    preview_path.write_bytes(b"preview")
    view._pending_card_background_path = preview_path
    view._pending_card_background_id = "settings.card"
    view._pending_card_background_clear_id = None
    view._feature_card_status_label = None
    view._feature_card_choice_ids = {
        "設定｜預設名稱": "settings.card",
    }
    view._feature_card_variable = None
    view.feature_hotkeys = {"sync": "F1"}
    view._feature_hotkey_variables = {}
    view._group_launch_hotkey_variable = None
    view._load_feature_card_background = loads.append
    view._report_refresh_error = lambda _error: None
    calls: list[bool] = []
    view.on_save_feature_card_settings = lambda **_values: (
        calls.append(True)
        or FeatureCardSettingsSaveResult(
            False,
            "背景無法儲存，全部設定均未變更。",
        )
    )

    assert not view._save_feature_card_settings(
        card_id="settings.card",
        title="新名稱",
        hotkey_feature="sync",
        hotkey="F2",
    )
    assert calls == [True]
    assert view._pending_card_background_path == preview_path
    assert view._pending_card_background_id == "settings.card"
    assert view.feature_hotkeys["sync"] == "F1"
    assert title_updates == []
    assert loads == []
    assert "全部設定均未變更" in view._feature_card_save_error


def test_failed_batch_clears_deleted_pending_background_and_reloads_saved(
    tmp_path,
):
    view = object.__new__(HomeView)
    deleted_preview = tmp_path / "deleted-preview.png"
    loads: list[str] = []
    view._pending_card_background_path = deleted_preview
    view._pending_card_background_id = "settings.card"
    view._feature_card_save_error = ""
    view._load_feature_card_background = loads.append
    view.on_save_feature_card_settings = lambda **_values: (
        FeatureCardSettingsSaveResult(
            False,
            "整組快捷鍵儲存失敗；全部設定均未變更。",
        )
    )

    saved = view._save_feature_card_settings_batch(
        selected_card_id="settings.card",
        widgets=SimpleNamespace(default_title="預設名稱"),
        clean_title="新名稱",
        reset_title=False,
        hotkey_feature="group_launch",
        hotkey="F9",
        group_name="14支",
        clear_background=False,
    )

    assert saved is False
    assert view._pending_card_background_path is None
    assert view._pending_card_background_id is None
    assert loads == ["settings.card"]
    assert "背景預覽已失效，已恢復原本背景" in (
        view._feature_card_save_error
    )


def test_settings_card_reset_title_waits_for_batch_save() -> None:
    view = object.__new__(HomeView)
    calls: list[str] = []
    view._feature_card_choice_ids = {
        "設定｜自訂名稱": "settings.card",
    }
    view._feature_card_variable = _ValueStub("設定｜自訂名稱")
    view._feature_cards = {
        "settings.card": SimpleNamespace(
            default_title="預設名稱",
            title_label=_ConfigureStub(),
        )
    }
    view._feature_card_title_entry = _EntryStub("自訂名稱")
    view._feature_card_status_label = _ConfigureStub()
    view._pending_card_title_reset_id = None
    view.on_feature_card_title_reset = calls.append

    view._reset_feature_card_title()

    assert calls == []
    assert view._pending_card_title_reset_id == "settings.card"
    assert view._feature_card_title_entry.get() == "預設名稱"
    assert view._feature_cards["settings.card"].title_label.values == {}
    assert "按「儲存卡片設定」" in (
        view._feature_card_status_label.values["text"]
    )


def test_settings_card_clear_background_waits_for_batch_save(
    monkeypatch,
) -> None:
    view = object.__new__(HomeView)
    clear_calls: list[str] = []
    view.parent = None
    view._feature_card_choice_ids = {
        "設定｜卡片": "settings.card",
    }
    view._feature_card_variable = _ValueStub("設定｜卡片")
    view._feature_cards = {
        "settings.card": SimpleNamespace(
            title_label=SimpleNamespace(cget=lambda _name: "卡片"),
        )
    }
    view._pending_card_background_path = None
    view._pending_card_background_id = None
    view._pending_card_background_clear_id = None
    view.on_save_feature_card_settings = lambda **_values: None
    view.on_clear_card_background = clear_calls.append
    view.on_discard_background_image = None
    view._load_feature_card_background = lambda _card_id: None
    view._feature_card_status_label = _ConfigureStub()
    monkeypatch.setattr(
        "ui.home.messagebox.askyesno",
        lambda *_args, **_kwargs: True,
    )

    view._clear_feature_card_background()

    assert clear_calls == []
    assert view._pending_card_background_clear_id == "settings.card"
    assert "按「儲存卡片設定」" in (
        view._feature_card_status_label.values["text"]
    )


def test_direct_card_clear_is_pending_until_batch_save_or_cancel() -> None:
    view = object.__new__(HomeView)
    discarded: list[Path] = []
    loads: list[str] = []
    clear_calls: list[str] = []
    view._pending_card_background_path = Path("preview.png")
    view._pending_card_background_id = "settings.card"
    view._pending_card_background_clear_id = None
    view.on_discard_background_image = discarded.append
    view.on_clear_card_background = clear_calls.append
    view._load_feature_card_background = loads.append

    view._mark_feature_card_background_clear("settings.card")

    assert discarded == [Path("preview.png")]
    assert clear_calls == []
    assert loads == ["settings.card"]
    assert view._pending_card_background_path is None
    assert view._pending_card_background_id is None
    assert view._pending_card_background_clear_id == "settings.card"


def test_direct_card_background_error_message_is_not_hidden() -> None:
    view = object.__new__(HomeView)
    view._card_background_prepare_running = False
    view._pending_card_background_id = "settings.card"
    view._pending_card_background_path = None
    view._pending_card_background_clear_id = None
    view._card_background_prepare_message = "RAW 圖片轉換失敗。"

    text, warning, keep_polling = (
        view._direct_feature_card_background_status("settings.card")
    )

    assert text == "RAW 圖片轉換失敗。"
    assert warning is True
    assert keep_polling is False


def test_four_player_selectable_themes_have_complete_palettes() -> None:
    assert tuple(UI_THEME_LABELS.values()) == (
        "俐落藍",
        "柔和紫",
        "舊版金色",
        "極簡黑白",
    )
    for name in UI_THEME_LABELS:
        palette = theme_palette(name)
        assert {
            "background",
            "surface",
            "sidebar",
            "sidebar_active",
            "sidebar_group",
            "sidebar_muted",
            "primary",
            "primary_hover",
            "text",
            "muted",
            "border",
            "success",
            "warning",
        } == set(palette)
    assert theme_palette(None) == theme_palette("classic_gold")
    assert theme_palette("classic_gold")["background"] == "#C9A35D"
    assert theme_palette("classic_gold")["surface"] == "#EAD3A0"
    assert theme_palette("classic_gold")["border"] == "#80591F"


def test_group_page_stop_sync_never_toggles_sync_on() -> None:
    view = object.__new__(HomeView)
    calls = []
    states = []
    view.keyboard_sync_enabled = True
    view.on_keyboard_sync_change = lambda enabled: calls.append(enabled) or True
    view._refresh_keyboard_sync_controls = lambda: states.append("refreshed")
    view.set_group_launch_state = (
        lambda running, message: states.append((running, message))
    )
    view._report_refresh_error = lambda _error: None

    view._stop_sync_from_group_page()

    assert calls == [False]
    assert view.keyboard_sync_enabled is False
    assert states == ["refreshed", (False, "同步已停止。")]


def test_character_page_uses_confirmed_note_field() -> None:
    lines = _safe_character_lines(
        (
            PlayerCharacterView(
                display_name="120古",
                group="120",
                level=120,
                importance="主號",
                role="古",
                note="守紀優先",
            ),
        )
    )

    assert lines == ("120古｜120｜古｜備註：守紀優先",)


def test_workspace_player_text_does_not_expose_ids() -> None:
    text = _workspace_state_text(WorkspaceState(next_step="選擇組別"))

    assert text == (
        "目前組別：尚未選擇\n"
        "目前活動：等待可信遊戲進度\n"
        "下一步：選擇組別"
    )
    assert "group_id" not in text


def test_activity_table_and_player_description_editor_are_concise() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "適用：" in source
    assert "狀態：" in source
    assert "下一步：" in source
    assert "調整活動敘述" in source
    assert "儲存敘述" in source
    assert "不會改變活動識別或進度" in source


def test_target_window_observation_remains_safe_read_only_value() -> None:
    observation = TargetWindowObservation(
        configured=True,
        safe=True,
        code="window.ready",
    )

    assert not hasattr(observation, "handle")


def test_unchanged_role_status_refresh_does_not_rebuild_home_rows() -> None:
    row = GroupRoleStatus(
        action_id="role-1",
        display_name="100古",
        status="已開啟",
        order=1,
    )

    class FrameThatMustNotRebuild:
        @staticmethod
        def winfo_children():
            raise AssertionError("unchanged role rows must not be rebuilt")

    view = object.__new__(HomeView)
    view._home_role_rows_frame = FrameThatMustNotRebuild()
    view._last_group_role_statuses = (row,)
    view.group_role_status_provider = lambda: (row,)
    view.on_refresh_error = None

    assert view.refresh_group_role_statuses() == (row,)
