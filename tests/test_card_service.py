import pytest

from cards.models import GroupCard
from cards.service import CardCapacityError, CardService, MAX_VISIBLE_CARDS
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

    with pytest.raises(CardCapacityError):
        service.upsert(_card("fourth"))

    assert service.cards == (first, second, third)


def test_same_card_identity_is_replaced_without_using_another_slot():
    service = CardService()
    original = _card("guard", "進行第1次")
    updated = _card("guard", "進行第2次")

    service.upsert(original)
    result = service.upsert(updated)

    assert result is updated
    assert service.cards == (updated,)


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
    assert service.cards == (cards[0], cards[2], fourth)
    assert service.remove("missing") is None


def test_service_rejects_values_outside_the_card_boundary():
    service = CardService()

    with pytest.raises(TypeError):
        service.upsert(object())
    with pytest.raises(TypeError):
        service.remove(1)
    with pytest.raises(ValueError):
        service.remove("   ")
