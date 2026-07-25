from config.path_manager import PathManager
from main import format_registry_status, format_self_check, format_start_status


def test_format_self_check_reports_success():
    headline, details = format_self_check(
        {
            "self_check_passed": True,
            "self_check": {
                "checks": [
                    {"name": "event_bus", "passed": True, "message": "OK"},
                    {"name": "external_adapter", "passed": True, "message": "Not registered yet"},
                ]
            },
        }
    )

    assert headline == "自我檢查通過"
    assert "✓ event_bus：OK" in details
    assert "✓ external_adapter：Not registered yet" in details


def test_format_self_check_reports_failure():
    headline, details = format_self_check(
        {
            "self_check_passed": False,
            "self_check": {
                "checks": [
                    {"name": "logger_service", "passed": False, "message": "not writable"}
                ]
            },
        }
    )

    assert headline == "自我檢查發現問題"
    assert "✗ logger_service：not writable" in details


def test_format_self_check_rejects_empty_report():
    headline, details = format_self_check({"self_check_passed": True, "self_check": {}})

    assert headline == "自我檢查發現問題"
    assert "沒有取得檢查結果" in details


def test_registry_status_uses_player_facing_words():
    text = format_registry_status({"window_registry": {"loaded": True, "count": 2}})

    assert "角色資料：已載入 2 個角色。" in text
    assert "舊視窗紀錄" in text
    assert "角色註冊表" not in text
    assert "Handle" not in text


def test_registry_status_missing_state_uses_player_facing_words():
    text = format_registry_status({"window_registry": {"loaded": False}})

    assert text == "角色資料：未載入。"


def test_start_status_includes_all_read_only_sections(tmp_path):
    paths = PathManager(root=tmp_path)
    text = format_start_status(
        {
            "self_check_passed": True,
            "self_check": [
                {"name": "paths", "passed": True, "message": "Paths are writable."},
            ],
            "target_window": {
                "safe": False,
                "code": "window.not_configured",
                "message": "No target configured.",
            },
            "background_capabilities": {
                "capabilities": {
                    "background_capture": {"state": "untested"},
                    "background_input": {"state": "untested"},
                    "minimized_input": {"state": "untested"},
                }
            },
            "window_registry": {"loaded": True, "count": 0},
        },
        paths,
    )

    assert "自我檢查通過" in text
    assert "✓ paths：Paths are writable." in text
    assert "代碼：window.not_configured" in text
    assert "被遮擋時讀取畫面：尚未測試" in text
    assert "非前景背景操作：尚未測試" in text
    assert "最小化背景操作：尚未測試" in text
    assert "同步輸入：只在玩家明確執行已批准測試時啟用" in text
    assert "目前權限為「全部允許（含最小化）」" in text
    assert "角色資料：已載入 0 個角色。" in text
    assert "目前只允許玩家明確執行 B／C 同步測試" in text
    assert "智慧重連只依已確認畫面自動監看" in text
    assert f"紀錄位置：{paths.logs_dir()}" in text
