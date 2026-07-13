from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardService
from cards.view_state import CardViewState
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.character import Character
from domain.group import CharacterGroup
from main import build_services
from services.app_context import AppContext
from services.card_view_state_service import CardViewStateService


def _card(card_id: str, progress: str = "守紀中斷") -> GroupCard:
    character = Character(character_id="120-old", display_name="120古", level=120)
    return GroupCard(
        card_id=card_id,
        group=CharacterGroup(
            group_id="14-windows",
            name="14支",
            characters=(character,),
        ),
        activity=ActivityDefinition(
            activity_id="guard",
            name="守紀",
            activity_type=ActivityType.DAILY,
            reset_rule=ResetRule.DAILY_MIDNIGHT,
        ),
        current_progress=progress,
        affected_character_ids=(character.character_id,),
        daily_summary="今日守紀尚未完成",
        requires_player_action=True,
        next_step="返回競技場繼續守紀",
        priority_reason=CardPriorityReason.DISCONNECTION,
    )


def test_empty_card_service_produces_empty_read_only_state():
    state = CardViewStateService(CardService()).snapshot()

    assert state == CardViewState()
    assert state.is_empty is True
    assert state.to_dict() == {"cards": []}


def test_snapshot_flattens_confirmed_card_information_for_ui():
    shown_at = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    cards = CardService()
    cards.upsert(_card("guard-disconnected"), shown_at=shown_at)

    state = CardViewStateService(cards).snapshot()
    item = state.cards[0]

    assert item.group_name == "14支"
    assert item.activity_name == "守紀"
    assert item.current_progress == "守紀中斷"
    assert item.affected_character_ids == ("120-old",)
    assert item.next_step == "返回競技場繼續守紀"
    assert item.priority_reason == "斷線"
    assert item.shown_at == shown_at
    assert item.expires_at == shown_at + timedelta(seconds=30)
    assert state.to_dict()["cards"][0]["shown_at"] == shown_at.isoformat()


def test_snapshot_is_detached_from_later_card_updates():
    cards = CardService()
    shown_at = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    cards.upsert(_card("guard", "第一次狀態"), shown_at=shown_at)
    first_snapshot = CardViewStateService(cards).snapshot()

    cards.upsert(_card("guard", "第二次狀態"), shown_at=shown_at)
    second_snapshot = CardViewStateService(cards).snapshot()

    assert first_snapshot.cards[0].current_progress == "第一次狀態"
    assert second_snapshot.cards[0].current_progress == "第二次狀態"
    with pytest.raises(FrozenInstanceError):
        first_snapshot.cards[0].current_progress = "不可修改"


def test_snapshot_preserves_visible_order_and_three_card_limit():
    cards = CardService()
    shown_at = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    for card_id in ("first", "second", "third"):
        cards.upsert(_card(card_id), shown_at=shown_at)

    state = CardViewStateService(cards).snapshot()

    assert tuple(item.card_id for item in state.cards) == ("first", "second", "third")


def test_build_services_registers_view_state_for_shared_card_service(tmp_path):
    build_services(root=tmp_path)
    card_service = AppContext.get(CardService)
    view_state_service = AppContext.get(CardViewStateService)
    card_service.upsert(
        _card("guard"),
        shown_at=datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc),
    )

    assert view_state_service.snapshot().cards[0].card_id == "guard"
