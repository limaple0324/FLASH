from datetime import datetime, timezone

from cards.history_store import CardHistoryStore
from cards.models import GroupCard
from cards.priority import CardPriorityReason
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.character import Character
from domain.group import CharacterGroup
from main import CARD_HISTORY_FILENAME, build_services
from services.app_context import AppContext
from services.card_history_service import CardHistoryService


def _card(reason: CardPriorityReason) -> GroupCard:
    character = Character(character_id="120-old", display_name="120古", level=120)
    return GroupCard(
        card_id=f"guard-{reason.name.lower()}",
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
        current_progress="120古｜守紀中斷",
        affected_character_ids=(character.character_id,),
        next_step="返回競技場繼續守紀",
        priority_reason=reason,
    )


def test_service_persists_and_reloads_retained_history(tmp_path):
    path = tmp_path / "card_history.json"
    service = CardHistoryService(CardHistoryStore(path))
    recorded_at = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)

    record = service.record(_card(CardPriorityReason.DISCONNECTION), recorded_at)
    reloaded = CardHistoryService(CardHistoryStore(path))

    assert record is not None
    assert reloaded.all() == (record,)


def test_service_does_not_write_general_reminders(tmp_path):
    path = tmp_path / "card_history.json"
    service = CardHistoryService(CardHistoryStore(path))

    record = service.record(
        _card(CardPriorityReason.GENERAL),
        datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc),
    )

    assert record is None
    assert service.all() == ()
    assert not path.exists()


def test_build_services_registers_history_inside_managed_data(tmp_path):
    paths, _logger = build_services(root=tmp_path)

    store = AppContext.get(CardHistoryStore)
    service = AppContext.get(CardHistoryService)

    assert store.path == paths.data_dir() / CARD_HISTORY_FILENAME
    assert service.store is store
