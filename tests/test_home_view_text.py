from pathlib import Path

from ui.home import _card_text


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

    assert _card_text(status) == "提醒卡\n系統正常"


def test_home_text_reports_self_check_problem():
    status = {"self_check_passed": False, "window_registry": {"characters": []}}

    assert _card_text(status) == "提醒卡\n自我檢查發現問題"
