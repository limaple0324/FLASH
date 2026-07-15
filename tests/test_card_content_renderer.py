from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from cards.view_state import CardViewItem
from ui.card_content_renderer import CardContent, CardContentRenderer


def _card(*, next_step: str | None = "返回競技場繼續守紀") -> CardViewItem:
    shown_at = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
    return CardViewItem(
        card_id="guard",
        group_id="14-windows",
        group_name="14支",
        activity_id="guard",
        activity_name="守紀",
        current_progress="120古正在進行第二次所羅門",
        affected_character_ids=("120-old",),
        daily_summary="今日守紀尚未完成",
        requires_player_action=True,
        next_step=next_step,
        priority_reason="斷線",
        priority_level=1,
        shown_at=shown_at,
        expires_at=shown_at + timedelta(seconds=30),
    )


class RecordingPresenter:
    def __init__(self) -> None:
        self.calls = []

    def render(self, window, content):
        self.calls.append((window, content))


def test_content_contains_only_confirmed_card_fields():
    content = CardContent.from_card(_card())

    assert content == CardContent(
        group_name="14支",
        activity_name="守紀",
        current_progress="120古正在進行第二次所羅門",
        next_step="返回競技場繼續守紀",
    )


def test_missing_next_step_stays_missing_instead_of_inventing_text():
    content = CardContent.from_card(_card(next_step=None))

    assert content.next_step is None


def test_content_snapshot_is_immutable():
    content = CardContent.from_card(_card())

    with pytest.raises(FrozenInstanceError):
        content.current_progress = "已完成"


def test_renderer_passes_window_and_stable_content_to_presenter():
    presenter = RecordingPresenter()
    renderer = CardContentRenderer(presenter)
    window = object()

    renderer(window, _card())

    assert presenter.calls == [
        (
            window,
            CardContent(
                group_name="14支",
                activity_name="守紀",
                current_progress="120古正在進行第二次所羅門",
                next_step="返回競技場繼續守紀",
            ),
        )
    ]


def test_renderer_uses_latest_content_on_every_update():
    presenter = RecordingPresenter()
    renderer = CardContentRenderer(presenter)
    window = object()
    original = _card()

    renderer(window, original)
    renderer(
        window,
        replace(
            original,
            current_progress="已恢復登入",
            next_step="返回守紀畫面",
        ),
    )

    assert [content.current_progress for _, content in presenter.calls] == [
        "120古正在進行第二次所羅門",
        "已恢復登入",
    ]
    assert presenter.calls[-1][1].next_step == "返回守紀畫面"


def test_invalid_presenter_and_card_are_rejected_before_rendering():
    with pytest.raises(TypeError, match="presenter"):
        CardContentRenderer(object())

    presenter = RecordingPresenter()
    renderer = CardContentRenderer(presenter)
    with pytest.raises(TypeError, match="CardViewItem"):
        renderer(object(), object())

    assert presenter.calls == []
