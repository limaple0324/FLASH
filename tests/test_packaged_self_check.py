import json

from main import format_self_check, run


def test_format_self_check_accepts_current_list_shape():
    status = {
        "self_check_passed": True,
        "self_check": [
            {"name": "logger_service", "passed": True, "message": "Logger is writable."},
            {"name": "event_bus", "passed": True, "message": "Event bus delivery succeeded."},
        ],
    }

    headline, details = format_self_check(status)

    assert headline == "自我檢查通過"
    assert "✓ logger_service" in details
    assert "✓ event_bus" in details
    assert "沒有取得檢查結果" not in details


def test_format_self_check_marks_missing_report_as_failure():
    headline, details = format_self_check({"self_check_passed": True})

    assert headline == "自我檢查發現問題"
    assert "沒有取得檢查結果" in details


def test_self_check_only_writes_machine_readable_report(tmp_path):
    exit_code = run(self_check_only=True, root=tmp_path)
    report_path = tmp_path / "data" / "self_check.json"

    assert exit_code == 0
    assert report_path.exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sprint"] == "SP1"
    assert report["self_check_passed"] is True
    assert isinstance(report["self_check"], list)
    assert report["self_check"]
