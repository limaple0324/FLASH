from datetime import datetime, timezone
import json

from cards.history_store import CardHistoryStore
from cards.priority import CardPriorityReason
from cards.service import CardService
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.character import Character, CharacterImportance
from domain.group import CharacterGroup
from domain.progress import ActivityProgress
from domain.status import ActivityStatus
from services.activity_progress_service import ActivityProgressChange
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.group_role_status_service import (
    GroupRoleStatus,
    GroupRoleStatusChange,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_OPEN,
    ROLE_STATUS_RECONNECTING,
)
from services.true_event_card_service import TrueEventCardService
from workspace.models import WorkspaceState
from main import (
    FARM_TIMER_STATE_FILENAME,
    TRUE_EVENT_CARD_STATE_FILENAME,
    build_services,
)
from services.app_context import AppContext
from services.farm_timer_service import FarmPlantingConfirmed, FarmTimerService


def _context(tmp_path):
    character = Character(
        character_id="role-a",
        display_name="120古",
        level=120,
        importance=CharacterImportance.PRIMARY,
    )
    group = CharacterGroup("group-14", "14支", (character,))
    activity = ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=2,
    )
    cards = CardService()
    history = CardHistoryService(
        CardHistoryStore(tmp_path / "history.json")
    )
    coordinator = CardCoordinator(cards, history)
    workspace = WorkspaceState(
        current_group=group,
        current_activity=activity,
    )
    records = []
    service = TrueEventCardService(
        coordinator,
        lambda: workspace,
        lambda _activity_id: activity,
        state_path=tmp_path / "true-events.json",
        record_callback=lambda *values: records.append(values),
    )
    return service, cards, history, group, activity, records


def _role_change(status, previous):
    return GroupRoleStatusChange(
        group_name="14支",
        previous_status=previous,
        current=GroupRoleStatus(
            action_id="fingerprint-a",
            display_name="120古",
            status=status,
            order=0,
        ),
    )


def test_disconnect_reconnect_and_recovery_clears_one_card_but_keeps_history(tmp_path):
    service, cards, history, _group, _activity, records = _context(tmp_path)
    disconnected_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)

    disconnected = service.handle_role_status(
        _role_change(ROLE_STATUS_DISCONNECTED, ROLE_STATUS_OPEN),
        occurred_at=disconnected_at,
    )
    duplicate = service.handle_role_status(
        _role_change(ROLE_STATUS_DISCONNECTED, ROLE_STATUS_OPEN),
        occurred_at=disconnected_at,
    )
    reconnecting = service.handle_role_status(
        _role_change(ROLE_STATUS_RECONNECTING, ROLE_STATUS_DISCONNECTED),
        occurred_at=disconnected_at,
    )
    recovered = service.handle_role_status(
        _role_change(ROLE_STATUS_OPEN, ROLE_STATUS_RECONNECTING),
        occurred_at=disconnected_at,
    )

    assert disconnected.priority_reason is CardPriorityReason.DISCONNECTION
    assert disconnected.current_progress == "守紀－已中斷"
    assert duplicate is None
    assert reconnecting.card_id == disconnected.card_id
    assert recovered is None
    assert cards.cards == ()
    assert len(history.all()) == 1
    assert history.all()[0].priority_reason is CardPriorityReason.DISCONNECTION
    assert [item[2] for item in records] == ["斷線", "斷線", "已恢復"]


def test_disconnect_state_survives_restart_and_prevents_duplicate_card(tmp_path):
    service, _cards, _history, group, activity, _records = _context(tmp_path)
    now = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    service.handle_role_status(
        _role_change(ROLE_STATUS_DISCONNECTED, ROLE_STATUS_OPEN),
        occurred_at=now,
    )
    cards = CardService()
    reloaded = TrueEventCardService(
        CardCoordinator(
            cards,
            CardHistoryService(CardHistoryStore(tmp_path / "other-history.json")),
        ),
        lambda: WorkspaceState(group, activity),
        lambda _activity_id: activity,
        state_path=tmp_path / "true-events.json",
    )

    assert reloaded.handle_role_status(
        _role_change(ROLE_STATUS_DISCONNECTED, None),
        occurred_at=now,
    ) is None
    assert cards.cards == ()


def test_completion_event_is_deduplicated_across_restart_when_kept_quiet(tmp_path):
    service, cards, _history, _group, activity, _records = _context(tmp_path)
    now = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    previous = ActivityProgress("guard", "role-a")
    current = ActivityProgress(
        "guard",
        "role-a",
        current_count=1,
        status=ActivityStatus.STANDBY,
        period_started_on=now.date(),
        completed_at=now,
    )
    change = ActivityProgressChange(
        "completion_recorded",
        now,
        previous,
        current,
    )

    first = service.handle_activity_progress(change)
    duplicate = service.handle_activity_progress(change)

    assert first is None
    assert duplicate is None
    assert cards.cards == ()

    reloaded_cards = CardService()
    reloaded = TrueEventCardService(
        CardCoordinator(
            reloaded_cards,
            CardHistoryService(CardHistoryStore(tmp_path / "reload-history.json")),
        ),
        lambda: WorkspaceState(),
        lambda _activity_id: activity,
        state_path=tmp_path / "true-events.json",
    )
    assert reloaded.handle_activity_progress(change) is None
    assert reloaded_cards.cards == ()


def test_non_completion_change_never_creates_a_card(tmp_path):
    service, cards, _history, _group, _activity, _records = _context(tmp_path)
    now = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    previous = ActivityProgress("guard", "role-a")
    current = previous.start(now)

    assert service.handle_activity_progress(
        ActivityProgressChange("started", now, previous, current)
    ) is None
    assert cards.cards == ()


def test_build_services_registers_real_event_and_farm_state_in_managed_data(
    tmp_path,
):
    paths, _logger = build_services(root=tmp_path)

    true_events = AppContext.get(TrueEventCardService)
    farm = AppContext.get(FarmTimerService)

    true_events.handle_role_status(
        _role_change(ROLE_STATUS_DISCONNECTED, ROLE_STATUS_OPEN),
        occurred_at=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
    )
    true_event_state = paths.data_dir() / TRUE_EVENT_CARD_STATE_FILENAME
    assert json.loads(true_event_state.read_text(encoding="utf-8"))[
        "schema_version"
    ] == 1
    farm.start(
        FarmPlantingConfirmed(
            "timer-role-a",
            CharacterGroup(
                "group-14",
                "14支",
                (Character("role-a", "120古", 120),),
            ),
            "role-a",
            datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
            "正確代碼",
        )
    )
    assert (paths.data_dir() / FARM_TIMER_STATE_FILENAME).is_file()
