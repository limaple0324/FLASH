from datetime import datetime, timedelta, timezone

import pytest

from cards.lifecycle import CardLifecycle, DEFAULT_CARD_LIFETIME
from cards.models import GroupCard
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup


def _card(card_id: str = "farm-ready") -> GroupCard:
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup(group_id="dimension", name="魔心次元組"),
        activity=ActivityDefinition(
            activity_id="farm",
            name="農場",
            activity_type=ActivityType.LOOP,
            reset_rule=ResetRule.COOLDOWN,
        ),
        current_progress="作物已成熟",
    )


def test_lifecycle_expires_at_exactly_thirty_seconds():
    shown_at = datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)
    lifecycle = CardLifecycle(_card(), shown_at)

    assert DEFAULT_CARD_LIFETIME == timedelta(seconds=30)
    assert lifecycle.expires_at == shown_at + timedelta(seconds=30)
    assert lifecycle.is_expired(shown_at + timedelta(seconds=29)) is False
    assert lifecycle.is_expired(shown_at + timedelta(seconds=30)) is True


def test_service_removes_only_expired_cards():
    start = datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)
    service = CardService()
    expired = _card("expired")
    active = _card("active")
    service.upsert(expired, shown_at=start)
    service.upsert(active, shown_at=start + timedelta(seconds=15))

    removed = service.remove_expired(start + timedelta(seconds=30))

    assert removed == (expired,)
    assert service.cards == (active,)


def test_updating_the_same_card_keeps_its_original_display_time():
    start = datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)
    service = CardService()
    service.upsert(_card("same"), shown_at=start)

    updated = _card("same")
    service.upsert(updated, shown_at=start + timedelta(seconds=20))

    assert service.entries[0].card is updated
    assert service.entries[0].shown_at == start
    assert service.remove_expired(start + timedelta(seconds=30)) == (updated,)


def test_completed_card_is_removed_immediately():
    service = CardService()
    card = _card()
    service.upsert(card)

    completed = service.complete(card.card_id)

    assert completed is card
    assert service.cards == ()


@pytest.mark.parametrize("value", [datetime(2026, 7, 13, 7, 0), "now"])
def test_lifecycle_rejects_time_without_a_valid_timezone(value):
    if isinstance(value, datetime):
        with pytest.raises(ValueError):
            CardLifecycle(_card(), value)
    else:
        with pytest.raises(TypeError):
            CardLifecycle(_card(), value)
