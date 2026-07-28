from pathlib import Path

from core.target_window_observation import TargetWindowObservation
from services.character_view_service import PlayerCharacterView
from services.group_selection_service import PlayerGroupChoice
from services.group_role_status_service import GroupRoleStatus
from ui.home import (
    GroupManagementViewResult,
    UI_THEME_LABELS,
    HomeView,
    _blend_hex_color,
    _contrast_ratio,
    _contain_geometry,
    _reordered_entry_ids,
    _safe_character_lines,
    _workspace_state_text,
    theme_palette,
)
from workspace.models import WorkspaceState


class _ValueStub:
    def __init__(self, value: str):
        self.value = value

    def set(self, value: str) -> None:
        self.value = value


def test_home_has_real_product_pages_and_group_selection() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    for label in ("首頁", "目前組別", "同步與重連", "角色資料", "設定"):
        assert label in source
    assert '"groups": "組別與視窗"' not in source
    assert '("groups", "組別與視窗")' not in source
    assert '"組別與遊戲視窗"' not in source
    assert "on_group_change" in source
    assert "character_choices" in source
    assert "靈魂石" not in source
    assert "_build_group_summary(sidebar)" not in source
    assert "_build_header(root)" not in source
    assert 'text="+"' not in source
    assert 'text="遊戲時間"' in source
    assert 'text="定時按下"' in source
    assert "設定按鈕位置" in source
    assert "啟用定時" in source
    assert "時間來源：系統時間" in source


def test_current_group_page_replaces_sidebar_duplicate() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert 'text="目前組別"' in source
    assert '("groups", "目前組別")' in source
    assert "主控：" in source
    assert "個視窗" in source
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
    view._show_group_setting_message = messages.append
    view.on_group_change = lambda _name: GroupManagementViewResult(
        False,
        "甲組",
        "自動操作尚未完全停止，未切換組別。",
    )

    view._select_group("乙組")

    assert view.current_group_name == "甲組"
    assert view._group_variable.value == "甲組"
    assert messages == ["自動操作尚未完全停止，未切換組別。"]


def test_all_pages_share_vertical_scroll_and_group_launch_action() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "Canvas(" in source
    assert "Scrollbar(" in source
    assert "yscrollcommand=scrollbar.set" in source
    assert "self._on_page_mousewheel" in source
    assert "一鍵啟動並還原位置" in source
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
    assert "校正角色ID" in source
    assert "讀取角色ID" in source


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
    assert "messagebox.show" not in source[source.index(
        "def _choose_background_image"
    ):source.index("def dispose")]


def test_player_habit_settings_use_confirmed_thresholds_and_clear_confirmation() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert 'text="玩家習慣"' in source
    assert "只觀察活動時間與角色操作順序" in source
    assert "同一習慣至少" in source
    assert "全部清除已保存偏好" in source
    assert "確定清除全部已保存偏好" in source
    assert "on_habit_observation_days_update" in source
    assert "on_modify_habit_preference" in source
    assert "儲存修改" in source


def test_background_contain_geometry_never_crops_or_upscales() -> None:
    assert _contain_geometry((200, 100), (100, 100)) == (100, 50, 0, 25)


def test_background_region_opacity_blends_legacy_color_over_image() -> None:
    assert _blend_hex_color("#C9A35D", "#000000", 0) == "#000000"
    assert _blend_hex_color("#C9A35D", "#000000", 100) == "#C9A35D"
    assert _contrast_ratio("#000000", "#FFFFFF") == 21
    assert _contain_geometry((100, 200), (100, 100)) == (50, 100, 25, 0)
    assert _contain_geometry((80, 60), (320, 240)) == (80, 60, 120, 90)


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
