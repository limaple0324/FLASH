from datetime import datetime, timezone

import pytest

from cards.history_store import CardHistoryStore
from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardCapacityError, CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from main import build_services
from services.app_context import AppContext
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService


def _card(
    card_id: str,
    reason: CardPriorityReason,
    progress: str = "守紀中斷",
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
        current_progress=progress,
        priority_reason=reason,
    )


def _coordinator(tmp_path):
    cards = CardService()
    history = CardHistoryService(CardHistoryStore(tmp_path / "card_history.json"))
    return CardCoordinator(cards, history)


def test_disconnection_card_is_visible_and_recorded_with_same_time(tmp_path):
    coordinator = _coordinator(tmp_path)
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
    card = _card("guard-disconnected", CardPriorityReason.DISCONNECTION)

    coordinator.show(card, shown_at=shown_at)

    assert coordinator.cards.cards == (card,)
    assert coordinator.history.all()[0].recorded_at == shown_at


def test_general_card_is_visible_without_history(tmp_path):
    coordinator = _coordinator(tmp_path)
    card = _card("guard-info", CardPriorityReason.GENERAL)

    coordinator.show(card, shown_at=datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc))

    assert coordinator.cards.cards == (card,)
    assert coordinator.history.all() == ()


def test_visible_card_update_does_not_duplicate_history(tmp_path):
    coordinator = _coordinator(tmp_path)
    first = _card("guard-disconnected", CardPriorityReason.DISCONNECTION)
    updated = _card(
        "guard-disconnected",
        CardPriorityReason.DISCONNECTION,
        progress="守紀仍在中斷",
    )
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)

    coordinator.show(first, shown_at=shown_at)
    coordinator.show(
        updated,
        shown_at=datetime(2026, 7, 13, 22, 0, 10, tzinfo=timezone.utc),
    )

    assert coordinator.cards.cards == (updated,)
    assert coordinator.cards.entries[0].shown_at == shown_at
    assert len(coordinator.history.all()) == 1


def test_visible_card_transition_to_recovery_adds_recovery_history(tmp_path):
    coordinator = _coordinator(tmp_path)
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
    recovered_at = datetime(2026, 7, 13, 22, 0, 10, tzinfo=timezone.utc)
    coordinator.show(
        _card("guard-status", CardPriorityReason.DISCONNECTION),
        shown_at=shown_at,
    )

    coordinator.show(
        _card("guard-status", CardPriorityReason.RECOVERY, progress="已恢復登入"),
        shown_at=recovered_at,
    )

    assert tuple(item.priority_reason for item in coordinator.history.all()) == (
        CardPriorityReason.DISCONNECTION,
        CardPriorityReason.RECOVERY,
    )
    assert coordinator.history.all()[1].recorded_at == recovered_at


def test_rejected_fourth_card_does_not_leave_history(tmp_path):
    coordinator = _coordinator(tmp_path)
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
    for card_id in ("first", "second", "third"):
        coordinator.show(_card(card_id, CardPriorityReason.GENERAL), shown_at=shown_at)

    with pytest.raises(CardCapacityError):
        coordinator.show(
            _card("fourth", CardPriorityReason.DISCONNECTION),
            shown_at=shown_at,
        )

    assert len(coordinator.cards.cards) == 3
    assert coordinator.history.all() == ()


def test_build_services_registers_coordinator_with_shared_services(tmp_path):
    build_services(root=tmp_path)

    coordinator = AppContext.get(CardCoordinator)

    assert coordinator.cards is AppContext.get(CardService)
    assert coordinator.history is AppContext.get(CardHistoryService)
