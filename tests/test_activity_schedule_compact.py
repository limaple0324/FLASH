from datetime import date
from tkinter import TclError, Tk

import pytest

from services.activity_schedule_view_service import (
    PlayerActivitySchedule,
    PlayerScheduledActivity,
)
from ui.home import HomeView, _activity_schedule_compact_text, _activity_schedule_visible_items


def _schedule(*statuses: str) -> PlayerActivitySchedule:
    return PlayerActivitySchedule(
        date(2026, 8, 4),
        tuple(
            PlayerScheduledActivity(str(index), f"活動{index}", "10:00", status_text=status)
            for index, status in enumerate(statuses)
        ),
    )


def test_compact_schedule_prefers_current_and_next_and_expands_all():
    state = _schedule("未開始", "進行中", "未開始", "未開始")
    visible, hidden = _activity_schedule_visible_items(state)
    assert tuple(item.activity_id for item in visible) == ("1", "2")
    assert hidden == 2
    expanded, hidden = _activity_schedule_visible_items(state, True)
    assert len(expanded) == 4
    assert hidden == 0


def test_compact_schedule_handles_zero_one_and_no_current():
    assert _activity_schedule_visible_items(_schedule())[0] == ()
    visible, hidden = _activity_schedule_visible_items(_schedule("\u5c1a\u672a\u958b\u59cb"))
    assert len(visible) == 1 and hidden == 0
    visible, hidden = _activity_schedule_visible_items(_schedule("\u5c1a\u672a\u958b\u59cb", "\u5c1a\u672a\u958b\u59cb", "\u7d50\u675f"))
    assert tuple(item.activity_id for item in visible) == ("0", "1")
    assert hidden == 1


def test_compact_schedule_skips_ended_items_and_keeps_description_when_expanded():
    state = _schedule("\u7d50\u675f", "\u5b8c\u6210", "\u5c1a\u672a\u958b\u59cb", "\u5c1a\u672a\u958b\u59cb")
    visible, _hidden = _activity_schedule_visible_items(state)
    assert tuple(item.activity_id for item in visible) == ("2", "3")
    ended, _hidden = _activity_schedule_visible_items(_schedule("\u7d50\u675f", "\u5b8c\u6210"))
    assert tuple(item.activity_id for item in ended) == ("1",)
    detailed = PlayerActivitySchedule(
        date(2026, 8, 4),
        (PlayerScheduledActivity("x", "活動", "10:00", "適用角色", "尚未開始", "前往", "既有說明"),),
    )
    text = _activity_schedule_compact_text(detailed, True)
    assert "下一項：" in text and "適用角色" in text and "既有說明" in text


def test_home_activity_toggle_and_refresh_do_not_keep_old_items():
    try:
        root = Tk()
    except TclError:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    current = _schedule("未開始", "進行中", "未開始", "未開始")
    replacement = _schedule("未開始")
    values = iter((replacement,))
    try:
        view = HomeView(
            root,
            {"self_check_passed": True},
            activity_schedule=current,
            activity_schedule_provider=lambda: next(values),
        )
        view.build()
        assert view._activity_schedule_details_frame.winfo_manager() == ""
        assert "活動1" in view._activity_schedule_label.cget("text")
        assert "活動3" not in view._activity_schedule_label.cget("text")
        assert "其餘 2 項" in view._activity_schedule_toggle_button.cget("text")
        view._toggle_activity_schedule_details()
        assert "活動3" in view._activity_schedule_label.cget("text")
        assert view._activity_schedule_details_frame.winfo_manager() == "pack"
        view._toggle_activity_schedule_details()
        assert "活動3" not in view._activity_schedule_label.cget("text")
        assert view._activity_schedule_details_frame.winfo_manager() == ""
        view.refresh_activity_schedule()
        assert "活動0" in view._activity_schedule_label.cget("text")
        assert "活動1" not in view._activity_schedule_label.cget("text")
        assert view._activity_schedule_toggle_button.winfo_manager() == "pack"
        assert "活動敘述設定" in view._activity_schedule_toggle_button.cget("text")
        view._toggle_activity_schedule_details()
        assert view._activity_schedule_details_frame.winfo_manager() == "pack"
    finally:
        root.destroy()
