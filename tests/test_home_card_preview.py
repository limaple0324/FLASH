from datetime import datetime, timedelta, timezone

from cards.view_state import CardViewItem, CardViewState
from ui.home import HomeView, _card_text


def _item(card_id: str, *, next_step: str | None = "返回競技場繼續守紀") -> CardViewItem:
    shown_at = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
    return CardViewItem(
        card_id=card_id,
        group_id="14-windows",
        group_name="14支",
        activity_id="guard",
        activity_name="守紀",
        current_progress="守紀中斷",
        affected_character_ids=("120-old",),
        daily_summary="今日守紀尚未完成",
        requires_player_action=True,
        next_step=next_step,
        priority_reason="斷線",
        priority_level=1,
        shown_at=shown_at,
        expires_at=shown_at + timedelta(seconds=30),
    )


def test_home_card_preview_reports_empty_read_only_state():
    text = _card_text({}, CardViewState())

    assert text == "提醒卡（0）\n目前沒有提醒"


def test_home_card_preview_uses_first_cards_real_content():
    text = _card_text({}, CardViewState(cards=(_item("guard"),)))

    assert text == (
        "提醒卡（1）\n"
        "14支｜守紀\n"
        "進度：守紀中斷\n"
        "下一步：返回競技場繼續守紀"
    )


def test_home_card_preview_counts_all_cards_but_only_renders_first():
    second = _item("second", next_step="不應顯示的第二張卡")

    text = _card_text({}, CardViewState(cards=(_item("first"), second)))

    assert text.startswith("提醒卡（2）\n")
    assert "返回競技場繼續守紀" in text
    assert "不應顯示的第二張卡" not in text


def test_home_card_preview_marks_missing_next_step_without_guessing():
    text = _card_text({}, CardViewState(cards=(_item("guard", next_step=None),)))

    assert "下一步：尚未提供" in text


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


def test_home_card_refresh_reads_a_new_snapshot_and_updates_existing_label():
    states = iter((CardViewState(), CardViewState(cards=(_item("guard"),))))
    view = HomeView(
        None,
        {},
        card_view_state_provider=lambda: next(states),
    )
    label = _FakeLabel()
    view._card_label = label

    assert view.refresh_cards() == "提醒卡（0）\n目前沒有提醒"
    assert view.refresh_cards().startswith("提醒卡（1）\n14支｜守紀")
    assert label.text.startswith("提醒卡（1）\n14支｜守紀")


def test_home_card_refresh_keeps_static_state_when_provider_is_unavailable():
    state = CardViewState(cards=(_item("guard"),))
    view = HomeView(None, {}, card_view_state=state)

    assert view.refresh_cards().startswith("提醒卡（1）\n14支｜守紀")
