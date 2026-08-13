import pytest

from datetime import datetime, timezone

from cards.history_store import CardHistoryStore
from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardService
from decision.models import DecisionCandidate
from decision.service import DecisionService
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
    *,
    requires_player_action: bool = False,
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
        requires_player_action=requires_player_action,
        priority_reason=reason,
    )


def _coordinator(tmp_path):
    cards = CardService()
    history = CardHistoryService(CardHistoryStore(tmp_path / "card_history.json"))
    return CardCoordinator(cards, history)


def _candidate(card: GroupCard, **changes) -> DecisionCandidate:
    return CardCoordinator.candidate_for_card(card, **changes)


def test_disconnection_card_is_visible_and_recorded_with_same_time(tmp_path):
    coordinator = _coordinator(tmp_path)
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
    card = _card("guard-disconnected", CardPriorityReason.DISCONNECTION)

    coordinator.submit(_candidate(card), card, shown_at=shown_at)

    assert coordinator.cards.cards == (card,)
    assert coordinator.history.all()[0].recorded_at == shown_at


def test_general_card_is_visible_without_history(tmp_path):
    coordinator = _coordinator(tmp_path)
    card = _card("guard-info", CardPriorityReason.GENERAL)

    coordinator.submit(
        _candidate(card),
        card,
        shown_at=datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc),
    )

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

    coordinator.submit(_candidate(first), first, shown_at=shown_at)
    coordinator.submit(
        _candidate(updated),
        updated,
        shown_at=datetime(2026, 7, 13, 22, 0, 10, tzinfo=timezone.utc),
    )

    assert coordinator.cards.cards == (updated,)
    assert coordinator.cards.entries[0].shown_at == shown_at
    assert len(coordinator.history.all()) == 1


def test_visible_card_transition_to_recovery_does_not_add_history(tmp_path):
    coordinator = _coordinator(tmp_path)
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
    recovered_at = datetime(2026, 7, 13, 22, 0, 10, tzinfo=timezone.utc)
    disconnected = _card("guard-status", CardPriorityReason.DISCONNECTION)
    coordinator.submit(
        _candidate(disconnected),
        disconnected,
        shown_at=shown_at,
    )
    recovered = _card(
        "guard-status",
        CardPriorityReason.RECOVERY,
        progress="已恢復登入",
        requires_player_action=True,
    )
    coordinator.submit(
        _candidate(recovered),
        recovered,
        shown_at=recovered_at,
    )

    assert tuple(item.priority_reason for item in coordinator.history.all()) == (
        CardPriorityReason.DISCONNECTION,
    )


def test_priority_queued_fourth_card_keeps_history_and_three_visible(tmp_path):
    coordinator = _coordinator(tmp_path)
    shown_at = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
    for card_id in ("first", "second", "third"):
        card = _card(card_id, CardPriorityReason.GENERAL)
        coordinator.submit(_candidate(card), card, shown_at=shown_at)

    fourth = _card("fourth", CardPriorityReason.DISCONNECTION)
    coordinator.submit(
        _candidate(fourth),
        fourth,
        shown_at=shown_at,
    )

    assert len(coordinator.cards.cards) == 3
    assert coordinator.cards.cards[0].card_id == "fourth"
    assert coordinator.history.all()[0].card_id == "fourth"


def test_submission_requires_matching_event_identity_and_decides_once(tmp_path):
    class CountingDecisionService(DecisionService):
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, candidates):
            self.calls += 1
            return super().decide(candidates)

    cards = CardService()
    history = CardHistoryService(CardHistoryStore(tmp_path / "history.json"))
    decisions = CountingDecisionService()
    coordinator = CardCoordinator(cards, history, decisions)
    card = _card("event", CardPriorityReason.GENERAL)

    with pytest.raises(ValueError, match="candidate_id"):
        coordinator.submit(
            DecisionCandidate(
                candidate_id="other-event",
                priority_reason=CardPriorityReason.GENERAL,
            ),
            card,
        )

    result = coordinator.submit(_candidate(card), card)

    assert result is card
    assert decisions.calls == 1


def test_same_event_that_becomes_quiet_removes_stale_card(tmp_path):
    coordinator = _coordinator(tmp_path)
    visible = _card(
        "same-event",
        CardPriorityReason.ACTIVITY,
        requires_player_action=True,
    )
    quiet = _card(
        "same-event",
        CardPriorityReason.ACTIVITY,
        requires_player_action=False,
    )

    assert coordinator.submit(
        _candidate(visible, is_current_group_progress=True),
        visible,
    ) is visible
    assert coordinator.cards.cards == (visible,)
    assert coordinator.submit(
        _candidate(quiet, is_current_group_progress=True),
        quiet,
    ) is None
    assert coordinator.cards.cards == ()


def test_runtime_card_sources_use_the_single_submission_entrypoint():
    sources = (
        "services/activity_reminder_service.py",
        "services/farm_timer_service.py",
        "services/player_habit_reminder_service.py",
        "services/true_event_card_service.py",
    )

    for path in sources:
        source = open(path, encoding="utf-8").read()
        assert "._coordinator.show(" not in source
        assert "._coordinator.submit(" in source


def test_build_services_registers_coordinator_with_shared_services(tmp_path):
    build_services(root=tmp_path)

    coordinator = AppContext.get(CardCoordinator)

    assert coordinator.cards is AppContext.get(CardService)
    assert coordinator.history is AppContext.get(CardHistoryService)
    assert isinstance(coordinator.decision_service, DecisionService)
