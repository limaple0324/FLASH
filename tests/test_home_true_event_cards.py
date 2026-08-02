from datetime import datetime, timezone

from cards.models import CardAction, GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.card_view_state_service import CardViewStateService
from ui.home import HomeView, _card_text


def _card(card_id, name, action=None):
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup("group-14", "14支"),
        activity=ActivityDefinition(
            activity_id=card_id,
            name=name,
            activity_type=ActivityType.PERMANENT,
            reset_rule=ResetRule.NONE,
        ),
        current_progress=f"{name}進度",
        priority_reason=CardPriorityReason.ACTIVITY,
        actions=(action,) if action is not None else (),
    )


def test_home_uses_the_same_three_visible_cards_as_overlay_state():
    cards = CardService()
    shown_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    for index in range(3):
        cards.upsert(
            _card(f"card-{index}", f"提醒{index + 1}"),
            shown_at=shown_at,
        )
    state = CardViewStateService(cards).snapshot()

    text = _card_text({}, state)

    assert tuple(item.activity_name for item in state.cards) == (
        "提醒1",
        "提醒2",
        "提醒3",
    )
    assert all(name in text for name in ("提醒1", "提醒2", "提醒3"))


def test_home_card_action_dispatches_the_exact_card_and_action():
    cards = CardService()
    cards.upsert(
        _card(
            "farm",
            "農場成熟",
            CardAction("複製代碼", "複製代碼"),
        ),
        shown_at=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
    )
    state_service = CardViewStateService(cards)
    actions = []
    view = HomeView(
        None,
        {},
        card_view_state=state_service.snapshot(),
        card_view_state_provider=state_service.snapshot,
        on_card_action=lambda card_id, action_id: actions.append(
            (card_id, action_id)
        ),
    )

    view._run_card_action("farm", "複製代碼")

    assert actions == [("farm", "複製代碼")]
