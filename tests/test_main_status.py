from main import format_self_check


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
