from datetime import datetime, timedelta, timezone

import pytest

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


def test_home_card_refresh_failure_keeps_last_good_state_and_reports_error():
    state = CardViewState(cards=(_item("guard"),))
    errors: list[Exception] = []
    calls = 0

    def provider() -> CardViewState:
        nonlocal calls
        calls += 1
        if calls == 1:
            return state
        raise OSError(r"C:\private\cards.json")

    view = HomeView(
        None,
        {},
        card_view_state_provider=provider,
        on_card_refresh_error=errors.append,
    )
    label = _FakeLabel()
    view._card_label = label

    previous = view.refresh_cards()
    failed = view.refresh_cards()

    assert failed == previous
    assert label.text == previous
    assert view.card_view_state is state
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)


@pytest.mark.parametrize("invalid_state", (None, object()))
def test_home_card_refresh_rejects_invalid_provider_without_replacing_state(
    invalid_state,
):
    previous = CardViewState(cards=(_item("guard"),))
    errors: list[Exception] = []
    view = HomeView(
        None,
        {},
        card_view_state=previous,
        card_view_state_provider=lambda: invalid_state,
        on_card_refresh_error=errors.append,
    )

    text = view.refresh_cards()

    assert view.card_view_state is previous
    assert text.startswith("提醒卡（1）\n14支｜守紀")
    assert len(errors) == 1
    assert isinstance(errors[0], TypeError)


def test_home_card_refresh_raises_without_an_error_boundary():
    view = HomeView(
        None,
        {},
        card_view_state_provider=lambda: object(),
    )

    with pytest.raises(TypeError, match="CardViewState"):
        view.refresh_cards()


def test_home_card_label_failure_keeps_previous_state_and_reports_error():
    previous = CardViewState(cards=(_item("guard"),))
    replacement = CardViewState(
        cards=(
            _item("guard"),
            _item("second", next_step="稍後再處理"),
        )
    )
    errors: list[Exception] = []

    class _FailingLabel:
        def configure(self, *, text: str) -> None:
            raise RuntimeError(f"label unavailable: {text}")

    view = HomeView(
        None,
        {},
        card_view_state=previous,
        card_view_state_provider=lambda: replacement,
        on_card_refresh_error=errors.append,
    )
    view._card_label = _FailingLabel()

    text = view.refresh_cards()

    assert view.card_view_state is previous
    assert text.startswith("提醒卡（1）\n14支｜守紀")
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
