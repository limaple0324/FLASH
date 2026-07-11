from pathlib import Path

from ui.home import _card_text, _group_text, _status_text, _workspace_text


def test_home_action_button_is_status_oriented():
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "查看目前狀態" in source
    assert "啟動輔助" not in source


def test_home_text_uses_empty_player_state():
    status = {
        "self_check_passed": True,
        "window_registry": {"characters": []},
        "target_window": {"configured": False, "safe": False},
    }

    assert _group_text(status) == "目前組別\n尚未設定"
    assert _status_text(status) == "目前狀態\n● 已準備完成"
    assert _workspace_text(status) == "工作區\n等待設定組別"
    assert _card_text(status) == "提醒卡\n尚未設定遊戲主視窗"


def test_home_text_summarizes_registered_group():
    status = {
        "self_check_passed": True,
        "window_registry": {
            "characters": [
                {"display_name": "160古", "group": "160"},
                {"display_name": "120古", "group": "120"},
            ]
        },
        "target_window": {"configured": True, "safe": True},
    }

    assert _group_text(status) == "目前組別\n120、160\n160古、120古"
    assert _status_text(status) == "目前狀態\n● 已找到遊戲視窗"
    assert _workspace_text(status) == "工作區\n已載入 2 個角色"
    assert _card_text(status) == "提醒卡\n系統正常"


def test_home_text_reports_self_check_problem():
    status = {"self_check_passed": False, "window_registry": {"characters": []}}

    assert _status_text(status) == "目前狀態\n● 需要檢查"
    assert _card_text(status) == "提醒卡\n自我檢查發現問題"
