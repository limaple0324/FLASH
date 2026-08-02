from main import format_card_overlay_status


def _status(message: str, *, passed: bool = True) -> dict[str, object]:
    return {
        "self_check": [
            {
                "name": "card_preview_selection",
                "passed": passed,
                "message": message,
            }
        ]
    }


def test_summary_explains_missing_candidate_styles() -> None:
    text = format_card_overlay_status(
        _status("Card overlay is not configured; no preview catalog is registered.")
    )

    assert text == "提醒卡浮層：尚未提供候選樣式，因此目前不顯示。"


def test_summary_explains_configured_but_unselected_styles() -> None:
    text = format_card_overlay_status(
        _status(
            "Card overlay is configured; the player has not selected a preview profile."
        )
    )

    assert text == "提醒卡浮層：候選樣式已準備好，尚未選擇。"


def test_summary_explains_selected_style_without_technical_identifier() -> None:
    text = format_card_overlay_status(
        _status("Card overlay is ready with selected preview profile player-selected.")
    )

    assert text == "提醒卡浮層：已選擇樣式，可以顯示。"
    assert "player-selected" not in text


def test_summary_explains_corrupt_selection_recovery() -> None:
    text = format_card_overlay_status(
        _status(
            "Card overlay is disabled because its selection was corrupt; "
            "backup saved as card_preview_selection.json.corrupt."
        )
    )

    assert text == "提醒卡浮層：選擇資料損壞，已安全停用並保留備份。"


def test_summary_explains_unavailable_saved_style() -> None:
    text = format_card_overlay_status(
        _status(
            "Card overlay is disabled because the saved preview profile is "
            "unavailable: retired-profile."
        )
    )

    assert text == "提醒卡浮層：原先選擇的樣式已不可用，目前保持停用。"


def test_summary_keeps_overlay_disabled_when_check_is_missing_or_failed() -> None:
    assert format_card_overlay_status({}) == (
        "提醒卡浮層：未取得狀態，目前保持停用。"
    )
    assert format_card_overlay_status(_status("failure", passed=False)) == (
        "提醒卡浮層：設定檢查未通過，目前保持停用。"
    )
