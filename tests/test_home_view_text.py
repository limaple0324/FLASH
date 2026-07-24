from pathlib import Path

from ui.home import (
    _card_text,
    _group_text,
    _status_text,
    _workspace_text,
    format_group_characters,
)


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


def test_group_character_text_lists_every_character_by_group_without_internals():
    status = {
        "window_registry": {
            "characters": [
                {
                    "character_id": "private-id-a",
                    "display_name": "小古",
                    "group": "14支",
                    "handle": 123,
                    "health": "ready",
                },
                {
                    "character_id": "private-id-b",
                    "display_name": "小法",
                    "group": "14支",
                    "handle": 456,
                },
                {
                    "character_id": "private-id-c",
                    "display_name": "次元主號",
                    "group": "魔心次元組",
                },
                {"display_name": "待整理角色", "group": None},
            ]
        }
    }

    text = format_group_characters(status)

    assert text == (
        "【14支】\n"
        "• 小古\n"
        "• 小法\n\n"
        "【魔心次元組】\n"
        "• 次元主號\n\n"
        "【未分組】\n"
        "• 待整理角色"
    )
    assert "private-id" not in text
    assert "123" not in text
    assert "ready" not in text


def test_group_character_text_has_player_facing_empty_state():
    assert (
        format_group_characters({"window_registry": {"characters": []}})
        == "目前沒有可顯示的組別與角色資料。"
    )
