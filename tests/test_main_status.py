from main import format_registry_status, format_self_check


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
