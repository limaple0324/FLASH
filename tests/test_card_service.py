from datetime import datetime, timedelta, timezone

import pytest

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.lifecycle import CardPresentationOrder
from cards.service import CardService, MAX_VISIBLE_CARDS
from decision.models import DecisionCategory
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.character import CharacterImportance
from domain.group import CharacterGroup


def _card(
    card_id: str,
    progress: str | None = None,
    priority: CardPriorityReason = CardPriorityReason.ACTIVITY,
) -> GroupCard:
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
        priority_reason=priority,
    )


def _order(
    category: DecisionCategory,
    card_id: str,
    *,
    remaining_time: timedelta | None = None,
    importance: CharacterImportance = CharacterImportance.SECONDARY,
) -> CardPresentationOrder:
    return CardPresentationOrder(
        category=category,
        remaining_time=remaining_time,
        character_importance=importance,
        event_id=card_id,
    )


def test_service_keeps_at_most_three_visible_cards():
    service = CardService()
    first = _card("first")
    second = _card("second")
    third = _card("third")

    service.upsert(first)
    service.upsert(second)
    service.upsert(third)

    assert MAX_VISIBLE_CARDS == 3
    assert service.cards == (first, second, third)

    fourth = _card("fourth")
    service.upsert(fourth)

    assert service.cards == (first, fourth, second)
    assert tuple(
        entry.card.card_id for entry in service.pending_entries
    ) == ("third",)


def test_same_card_identity_is_replaced_without_using_another_slot():
    service = CardService()
    original = _card("guard", "進行第1次")
    updated = _card("guard", "進行第2次")

    shown_at = datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    service.upsert(
        original,
        shown_at=shown_at,
        presentation_order=_order(DecisionCategory.SUGGESTION, "guard"),
    )
    result = service.upsert(
        updated,
        presentation_order=_order(DecisionCategory.LOSS_RISK, "guard"),
    )

    assert result is updated
    assert service.cards == (updated,)
    assert service.entries[0].shown_at == shown_at
    assert service.entries[0].presentation_order.category is DecisionCategory.LOSS_RISK


def test_replacement_stays_available_when_all_three_slots_are_used():
    service = CardService()
    for card_id in ("first", "second", "third"):
        service.upsert(_card(card_id))

    replacement = _card("second", "已更新")
    service.upsert(replacement)

    assert service.cards[1] is replacement
    assert len(service.cards) == 3


def test_remove_returns_the_card_and_opens_a_slot_for_the_next_card():
    service = CardService()
    cards = tuple(_card(card_id) for card_id in ("first", "second", "third"))
    for card in cards:
        service.upsert(card)

    removed = service.remove(" second ")
    fourth = _card("fourth")
    service.upsert(fourth)

    assert removed is cards[1]
    assert service.cards == (cards[0], fourth, cards[2])
    assert service.remove("missing") is None


def test_decision_priority_moves_lower_priority_card_to_queue():
    service = CardService()
    service.upsert(
        _card("activity-1"),
        presentation_order=_order(
            DecisionCategory.IMPORTANT_TODAY,
            "activity-1",
        ),
    )
    service.upsert(
        _card("activity-2"),
        presentation_order=_order(
            DecisionCategory.IMPORTANT_TODAY,
            "activity-2",
        ),
    )
    service.upsert(
        _card("preference", priority=CardPriorityReason.PREFERENCE),
        presentation_order=_order(
            DecisionCategory.SUGGESTION,
            "preference",
        ),
    )
    service.upsert(
        _card("disconnected", priority=CardPriorityReason.DISCONNECTION),
        presentation_order=_order(
            DecisionCategory.SAFETY_AND_DISCONNECTION,
            "disconnected",
        ),
    )

    assert tuple(card.card_id for card in service.cards) == (
        "disconnected",
        "activity-1",
        "activity-2",
    )
    assert service.pending_entries[0].card.card_id == "preference"


def test_decision_order_controls_all_visible_layers_and_pending_queue():
    service = CardService()
    visible_categories = tuple(DecisionCategory)[:-1]
    for category in reversed(visible_categories):
        card_id = f"layer-{int(category)}"
        service.upsert(
            _card(card_id),
            presentation_order=_order(category, card_id),
        )

    assert tuple(
        entry.presentation_order.category for entry in service.entries
    ) == (
        DecisionCategory.SAFETY_AND_DISCONNECTION,
        DecisionCategory.LOSS_RISK,
        DecisionCategory.TIME_LIMIT,
    )
    assert tuple(
        entry.presentation_order.category
        for entry in service.pending_entries
    ) == visible_categories[3:]


def test_same_category_uses_time_then_importance_then_stable_event_identity():
    service = CardService()
    shown_at = datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    items = (
        ("later-primary", timedelta(minutes=15), CharacterImportance.PRIMARY),
        ("tie-b", timedelta(minutes=10), CharacterImportance.SECONDARY),
        ("early-secondary", timedelta(minutes=5), CharacterImportance.SECONDARY),
        ("tie-a", timedelta(minutes=10), CharacterImportance.SECONDARY),
        ("no-deadline", None, CharacterImportance.PRIMARY),
        ("early-primary", timedelta(minutes=5), CharacterImportance.PRIMARY),
    )
    for card_id, remaining_time, importance in items:
        service.upsert(
            _card(card_id),
            shown_at=shown_at,
            presentation_order=_order(
                DecisionCategory.TIME_LIMIT,
                card_id,
                remaining_time=remaining_time,
                importance=importance,
            ),
        )

    assert tuple(card.card_id for card in service.cards) == (
        "early-primary",
        "early-secondary",
        "tie-a",
    )
    assert tuple(entry.card.card_id for entry in service.pending_entries) == (
        "tie-b",
        "later-primary",
        "no-deadline",
    )


def test_presentation_order_rejects_bare_integer_category():
    with pytest.raises(TypeError):
        CardPresentationOrder(
            category=1,
            remaining_time=None,
            character_importance=CharacterImportance.SECONDARY,
            event_id="bad",
        )

    valid = _order(DecisionCategory.GENERAL_INFORMATION, "general")
    assert valid.category is DecisionCategory.GENERAL_INFORMATION

    with pytest.raises(ValueError):
        _order(DecisionCategory.QUIET, "quiet")


def test_pending_card_that_expires_is_removed_without_becoming_visible():
    service = CardService()
    shown_at = datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    for card_id in ("first", "second", "third"):
        service.upsert(
            _card(card_id),
            shown_at=shown_at,
            lifetime=timedelta(minutes=10),
        )
    expired_pending = _card(
        "expired-pending",
        priority=CardPriorityReason.PREFERENCE,
    )
    service.upsert(
        expired_pending,
        shown_at=shown_at,
        lifetime=timedelta(seconds=5),
    )

    removed = service.remove_expired(shown_at + timedelta(seconds=5))

    assert removed == (expired_pending,)
    assert tuple(card.card_id for card in service.cards) == (
        "first",
        "second",
        "third",
    )
    assert service.pending_entries == ()


def test_service_rejects_values_outside_the_card_boundary():
    service = CardService()

    with pytest.raises(TypeError):
        service.upsert(object())
    with pytest.raises(TypeError):
        service.remove(1)
    with pytest.raises(ValueError):
        service.remove("   ")
