from datetime import datetime

from cards.history_store import CardHistoryStore
from cards.service import CardService
from domain.character import Character
from domain.group import CharacterGroup
from domain.progress import TAIPEI_TIMEZONE
from main import refresh_confirmed_activity_group_scope
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.confirmed_activity_rule_service import ConfirmedActivityRuleService
from services.group_role_status_service import ROLE_STATUS_OPEN
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


def _group(group_id: str, role_id: str) -> CharacterGroup:
    return CharacterGroup(
        group_id,
        f"{group_id}組",
        (Character(role_id, f"{role_id}角色", 120),),
    )


def _service(tmp_path, cards: CardService) -> ConfirmedActivityRuleService:
    return ConfirmedActivityRuleService(
        CardCoordinator(
            cards,
            CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
        ),
        state_path=tmp_path / "confirmed.json",
    )


def test_group_switch_registers_only_current_workspace_group_for_reminders(
    tmp_path,
):
    group_a = _group("group-a", "role-a")
    group_b = _group("group-b", "role-b")
    workspace = WorkspaceService(WorkspaceState(current_group=group_a))
    cards = CardService()
    service = _service(tmp_path, cards)
    opened_at = datetime(2026, 8, 3, 11, 59, tzinfo=TAIPEI_TIMEZONE)

    assert refresh_confirmed_activity_group_scope(
        workspace_service=workspace,
        confirmed_activity_rule_service=service,
        logger=None,
    )
    first = service.handle_role_status("role-a", ROLE_STATUS_OPEN, opened_at)
    assert len(first) == 1
    assert first[0].group.group_id == "group-a"

    workspace.set_current_group(group_b)
    assert refresh_confirmed_activity_group_scope(
        workspace_service=workspace,
        confirmed_activity_rule_service=service,
        logger=None,
    )
    assert cards.cards == ()
    assert service.handle_role_status("role-a", ROLE_STATUS_OPEN, opened_at) == ()

    second = service.handle_role_status(
        "role-b",
        ROLE_STATUS_OPEN,
        opened_at,
    )
    assert len(second) == 1
    assert second[0].group.group_id == "group-b"
    service.poll(datetime(2026, 8, 3, 12, 0, tzinfo=TAIPEI_TIMEZONE))
    assert all(card.group.group_id == "group-b" for card in cards.cards)
