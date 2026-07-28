import json

from cards.settings import CARD_LIFETIME_SECONDS_CONFIG_KEY
from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from main import build_services, format_card_display_settings_status
from services.app_context import AppContext


def _write_settings(tmp_path, value) -> None:
    config_path = tmp_path / "config" / "settings.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({CARD_LIFETIME_SECONDS_CONFIG_KEY: value}),
        encoding="utf-8",
    )


def _duration_check(paths):
    Bootstrap(context=AppContext).start()
    report = SelfCheck(context=AppContext, paths=paths).run_all()
    return next(
        item for item in report["checks"] if item["name"] == "card_display_settings"
    )


def _status(message: str, *, passed: bool = True) -> dict[str, object]:
    return {
        "self_check": [
            {
                "name": "card_display_settings",
                "passed": passed,
                "message": message,
            }
        ]
    }


def test_self_check_reports_default_card_display_time(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path)

    check = _duration_check(paths)

    assert check["passed"] is True
    assert check["message"] == "Card lifetime uses default of 30 seconds."


def test_self_check_reports_player_configured_card_display_time(tmp_path) -> None:
    _write_settings(tmp_path, 75)
    paths, _logger = build_services(root=tmp_path)

    check = _duration_check(paths)

    assert check["passed"] is True
    assert check["message"] == "Card lifetime is configured to 75 seconds."


def test_self_check_reports_invalid_setting_recovery(tmp_path) -> None:
    _write_settings(tmp_path, "seventy-five")
    paths, _logger = build_services(root=tmp_path)

    check = _duration_check(paths)

    assert check["passed"] is True
    assert check["message"] == (
        "Card lifetime setting was invalid; using safe default of 30 seconds."
    )


def test_player_summary_explains_all_card_display_time_states() -> None:
    assert format_card_display_settings_status(
        _status("Card lifetime uses default of 30 seconds.")
    ) == "提醒卡顯示時間：目前使用預設 30 秒。"
    assert format_card_display_settings_status(
        _status("Card lifetime is configured to 75 seconds.")
    ) == "提醒卡顯示時間：目前設定為 75 秒。"
    assert format_card_display_settings_status(
        _status(
            "Card lifetime setting was invalid; "
            "using safe default of 30 seconds."
        )
    ) == "提醒卡顯示時間：原設定無效，已安全改用 30 秒。"


def test_player_summary_handles_missing_failed_and_unknown_states() -> None:
    assert format_card_display_settings_status({}) == (
        "提醒卡顯示時間：未取得設定狀態。"
    )
    assert format_card_display_settings_status(
        _status("failure", passed=False)
    ) == "提醒卡顯示時間：設定檢查未通過，目前使用安全預設值。"
    assert format_card_display_settings_status(
        _status("unrecognized")
    ) == "提醒卡顯示時間：狀態無法判斷，目前使用安全預設值。"
