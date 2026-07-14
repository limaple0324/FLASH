from datetime import datetime, timedelta, timezone

import pytest

from cards.models import GroupCard
from cards.service import CardCapacityError, CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup


def _card(card_id: str, progress: str | None = None) -> GroupCard:
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup(group_id="14-windows", name="14支"),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress=progress or card_id,
    )


def test_card_changes_notify_home_refresh_listener():
    service = CardService()
    changes: list[tuple[str, ...]] = []
    service.subscribe(lambda: changes.append(tuple(card.card_id for card in service.cards)))

    service.upsert(_card("guard"))
    service.upsert(_card("guard", "守紀進度更新"))
    service.complete("guard")

    assert changes == [("guard",), ("guard",), ()]


def test_missing_remove_and_rejected_fourth_card_do_not_trigger_refresh():
    service = CardService()
    notifications = 0

    def record_change() -> None:
        nonlocal notifications
        notifications += 1

    service.subscribe(record_change)
    for card_id in ("first", "second", "third"):
        service.upsert(_card(card_id))
    baseline = notifications

    assert service.remove("missing") is None
    with pytest.raises(CardCapacityError):
        service.upsert(_card("fourth"))

    assert notifications == baseline


def test_expiry_notifies_once_only_when_cards_are_removed():
    service = CardService()
    shown_at = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    notifications = []
    service.upsert(_card("guard"), shown_at=shown_at)
    service.subscribe(lambda: notifications.append(service.cards))

    service.remove_expired(shown_at + timedelta(seconds=29))
    service.remove_expired(shown_at + timedelta(seconds=30))

    assert notifications == [()]


def test_unsubscribe_stops_future_refresh_notifications():
    service = CardService()
    notifications = []

    def record_change() -> None:
        notifications.append(True)

    service.subscribe(record_change)
    service.upsert(_card("first"))
    service.unsubscribe(record_change)
    service.remove("first")

    assert notifications == [True]
