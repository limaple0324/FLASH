from pathlib import Path

import pytest

from services.character_detail_view_service import PlayerCharacterDetail
from services.character_view_service import PlayerCharacterView
from ui.home import (
    _card_text,
    _group_text,
    _status_text,
    _workspace_text,
    format_group_characters,
    format_player_character_detail,
    format_player_characters,
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
                    "role": "主號",
                    "note": "優先追蹤",
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
        "  定位：主號\n"
        "  備註：優先追蹤\n"
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


def test_player_character_text_includes_level_importance_role_and_note():
    text = format_player_characters(
        (
            PlayerCharacterView(
                display_name="小古",
                group="14支",
                level=120,
                importance="主號",
                role="古",
                note="守紀優先",
            ),
            PlayerCharacterView(
                display_name="待補資料",
                group=None,
                level=None,
                importance=None,
                role=None,
                note=None,
            ),
        )
    )

    assert text == (
        "【14支】\n"
        "• 小古\n"
        "  等級：120\n"
        "  分類：主號\n"
        "  定位：古\n"
        "  備註：守紀優先\n\n"
        "【未分組】\n"
        "• 待補資料"
    )


def test_player_character_text_has_player_facing_empty_state():
    assert (
        format_player_characters(())
        == "目前沒有可顯示的組別與角色資料。"
    )


def test_player_character_detail_text_uses_confirmed_chinese_fields():
    text = format_player_character_detail(
        PlayerCharacterDetail(
            display_name="小古",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
        )
    )

    assert text == (
        "【小古】\n"
        "組別：14支\n"
        "等級：120\n"
        "分類：主號\n"
        "定位：古\n"
        "備註：守紀優先"
    )


def test_player_character_detail_text_marks_missing_values_without_guessing():
    text = format_player_character_detail(
        PlayerCharacterDetail(
            display_name="待補資料",
            group=None,
            level=None,
            importance=None,
            role=None,
            note=None,
        )
    )

    assert text == (
        "【待補資料】\n"
        "組別：尚未設定\n"
        "等級：尚未設定\n"
        "分類：尚未設定\n"
        "定位：尚未設定\n"
        "備註：尚未設定"
    )
    assert "靈魂石" not in text
    assert "命魂" not in text
    assert "魂器" not in text
    assert "背包" not in text


def test_player_character_detail_text_rejects_untrusted_values():
    with pytest.raises(TypeError, match="PlayerCharacterDetail"):
        format_player_character_detail(object())
