from datetime import datetime, timedelta

import pytest

from cards.history_store import CardHistoryStore
from cards.service import CardService
from domain.character import Character
from domain.confirmed_activity_rules import (
    CONFIRMED_ACTIVITY_RULE_CHANGED_EVENT,
    ConfirmedActivityEvent,
    ConfirmedActivityEventType,
    ConfirmedActivityKind,
)
from domain.group import CharacterGroup
from domain.progress import TAIPEI_TIMEZONE
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.confirmed_activity_rule_service import (
    DEITY_CULTIVATION_DURATION,
    DIMENSION_DURATION,
    ESTATE_FIRST_ROUND_DURATION,
    FANTASY_LATER_DURATION,
    ConfirmedActivityRuleService,
)
from services.event_bus import EventBus
from services.group_role_status_service import (
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_OPEN,
)


def _group() -> CharacterGroup:
    return CharacterGroup(
        "group-14",
        "14支",
        (
            Character("role-120", "120古", 120),
            Character("role-160", "160嶽", 160),
            Character("role-100", "100射", 100),
        ),
    )


def _context(tmp_path):
    cards = CardService()
    event_bus = EventBus()
    service = ConfirmedActivityRuleService(
        CardCoordinator(
            cards,
            CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
        ),
        state_path=tmp_path / "confirmed.json",
        event_bus=event_bus,
    )
    group = _group()
    assert service.register_group(group)
    return service, cards, event_bus, group


def _event(
    activity,
    event_type,
    group,
    at,
    subject_id=None,
    floor=None,
):
    return ConfirmedActivityEvent(
        activity,
        event_type,
        group,
        at,
        subject_id,
        floor,
    )


def test_magic_soldiers_and_dimension_are_daily_role_scoped_without_cards(
    tmp_path,
):
    service, cards, _bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 9, tzinfo=TAIPEI_TIMEZONE)

    assert service.handle(
        _event(
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityEventType.CONFIRMED_COMPLETE,
            group,
            at,
            "role-100",
        )
    ) is False
    assert service.handle(
        _event(
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityEventType.CONFIRMED_COMPLETE,
            group,
            at,
            "role-120",
        )
    ) is True
    assert service.poll(at) == ()
    assert cards.cards == ()
    assert service.handle(
        _event(
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityEventType.CONFIRMED_COMPLETE,
            group,
            at,
            "role-120",
        )
    ) is False

    floor_one = _event(
        ConfirmedActivityKind.DIMENSION_SPACE,
        ConfirmedActivityEventType.DIMENSION_ENTERED,
        group,
        at,
        "role-120",
        floor=1,
    )
    assert service.handle(floor_one) is True
    record_id = "dimension-space:role-120:2026-08-03"
    assert service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            group,
            at + timedelta(minutes=5),
            "role-120",
            floor=2,
        )
    ) is True
    assert service.record(record_id).started_at == floor_one.observed_at
    assert service.poll(at + DIMENSION_DURATION) == ()
    assert service.record(record_id).stage == "已完成"
    assert service.handle(floor_one) is False
    assert service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            group,
            at + timedelta(days=1),
            "role-120",
            floor=1,
        )
    ) is True
    assert cards.cards == ()

    overnight_service, _overnight_cards, _overnight_bus, overnight_group = _context(
        tmp_path / "dimension-overnight"
    )
    overnight_start = datetime(
        2026,
        8,
        3,
        23,
        30,
        tzinfo=TAIPEI_TIMEZONE,
    )
    assert overnight_service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            overnight_group,
            overnight_start,
            "role-120",
            floor=1,
        )
    )
    overnight_record_id = "dimension-space:role-120:2026-08-03"
    assert overnight_service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            overnight_group,
            overnight_start + timedelta(minutes=35),
            "role-120",
            floor=2,
        )
    )
    assert overnight_service.record(overnight_record_id).stage == "第二層"
    assert overnight_service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            overnight_group,
            overnight_start + timedelta(minutes=35),
            "role-120",
            floor=1,
        )
    ) is False
    assert overnight_service.poll(overnight_start + DIMENSION_DURATION) == ()
    assert overnight_service.record(overnight_record_id).stage == "已完成"
    assert overnight_service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            overnight_group,
            overnight_start + timedelta(days=2),
            "role-120",
            floor=1,
        )
    ) is True


def test_fantasy_training_pauses_only_for_game_close_and_uses_collection_day_count(
    tmp_path,
):
    service, cards, _bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 9, tzinfo=TAIPEI_TIMEZONE)
    start = _event(
        ConfirmedActivityKind.FANTASY_TRAINING,
        ConfirmedActivityEventType.TRAINING_STARTED,
        group,
        at,
        "role-120",
    )

    assert service.handle(start)
    assert service.handle_role_status(
        "role-120",
        ROLE_STATUS_CLOSED,
        at + timedelta(minutes=5),
    ) == ()
    assert service.poll(at + timedelta(minutes=20)) == ()
    resumed = service.handle_role_status(
        "role-120",
        ROLE_STATUS_OPEN,
        at + timedelta(minutes=25),
    )
    assert all(
        card.activity.activity_id
        == ConfirmedActivityKind.ARTIFACT_DAILY.value
        for card in resumed
    )
    first = service.poll(at + timedelta(minutes=30))
    assert len(first) == 1
    assert "今天第1次" in first[0].current_progress
    assert service.poll(at + timedelta(minutes=34, seconds=59)) == ()
    assert len(service.poll(at + timedelta(minutes=35))) == 1
    assert service.handle(
        _event(
            ConfirmedActivityKind.FANTASY_TRAINING,
            ConfirmedActivityEventType.TRAINING_COLLECTED,
            group,
            at + timedelta(minutes=36),
            "role-120",
        )
    )
    assert all(
        card.activity.activity_id
        != ConfirmedActivityKind.FANTASY_TRAINING.value
        for card in cards.cards
    )
    assert service.fantasy_collection_count("role-120", at.date()) == 1

    for index in (2, 3):
        started_at = at + timedelta(minutes=40 + index * 20)
        assert service.handle(
            _event(
                ConfirmedActivityKind.FANTASY_TRAINING,
                ConfirmedActivityEventType.TRAINING_STARTED,
                group,
                started_at,
                "role-120",
            )
        )
        assert service.handle(
            _event(
                ConfirmedActivityKind.FANTASY_TRAINING,
                ConfirmedActivityEventType.TRAINING_COLLECTED,
                group,
                started_at + timedelta(minutes=10),
                "role-120",
            )
        )
    fourth_at = at + timedelta(hours=2)
    assert service.handle(
        _event(
            ConfirmedActivityKind.FANTASY_TRAINING,
            ConfirmedActivityEventType.TRAINING_STARTED,
            group,
            fourth_at,
            "role-120",
        )
    )
    assert service.record("fantasy-training:role-120").duration_seconds == int(
        FANTASY_LATER_DURATION.total_seconds()
    )

    overnight_service, _overnight_cards, _bus, overnight_group = _context(
        tmp_path / "overnight"
    )
    overnight_start = datetime(2026, 8, 3, 23, 55, tzinfo=TAIPEI_TIMEZONE)
    assert overnight_service.handle(
        _event(
            ConfirmedActivityKind.FANTASY_TRAINING,
            ConfirmedActivityEventType.TRAINING_STARTED,
            overnight_group,
            overnight_start,
            "role-120",
        )
    )
    overnight = overnight_service.poll(
        datetime(2026, 8, 4, 0, 5, tzinfo=TAIPEI_TIMEZONE)
    )
    assert len(overnight) == 1
    assert "今天第1次" in overnight[0].current_progress


def test_estate_disconnect_and_game_reopen_follow_opposite_confirmed_paths(
    tmp_path,
):
    service, cards, _bus, group = _context(tmp_path / "disconnect")
    at = datetime(2026, 8, 3, 8, tzinfo=TAIPEI_TIMEZONE)
    first = _event(
        ConfirmedActivityKind.ESTATE_FIRST_ROUND,
        ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
        group,
        at,
    )
    assert service.handle(first)
    assert service.handle_role_status(
        "role-120",
        ROLE_STATUS_DISCONNECTED,
        at + timedelta(hours=1),
    ) == ()
    assert service.poll(at + timedelta(hours=9)) == ()
    estate_record = next(
        record
        for record in service.records()
        if record.activity is ConfirmedActivityKind.ESTATE_FIRST_ROUND
    )
    assert estate_record.handled_by_disconnect is True
    assert cards.cards == ()
    assert service.handle(
        _event(
            ConfirmedActivityKind.ESTATE_FIRST_ROUND,
            ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
            group,
            at + timedelta(hours=2),
        )
    ) is False
    assert service.handle(
        _event(
            ConfirmedActivityKind.ESTATE_FIRST_ROUND,
            ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
            group,
            at + timedelta(days=1),
        )
    ) is True

    reopened_service, reopened_cards, _bus, reopened_group = _context(
        tmp_path / "reopened"
    )
    assert reopened_service.handle(
        _event(
            ConfirmedActivityKind.ESTATE_FIRST_ROUND,
            ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
            reopened_group,
            at,
        )
    )
    assert reopened_service.handle_role_status(
        "role-120",
        ROLE_STATUS_CLOSED,
        at + timedelta(hours=7),
    ) == ()
    reopened = reopened_service.handle_role_status(
        "role-120",
        ROLE_STATUS_OPEN,
        at + ESTATE_FIRST_ROUND_DURATION,
    )
    estate_cards = tuple(
        card
        for card in reopened
        if card.activity.activity_id
        == ConfirmedActivityKind.ESTATE_FIRST_ROUND.value
    )
    assert len(estate_cards) == 1
    assert estate_cards[0] in reopened_cards.cards
    assert reopened_service.handle(
        _event(
            ConfirmedActivityKind.ESTATE_FIRST_ROUND,
            ConfirmedActivityEventType.ESTATE_SECOND_OPENED,
            reopened_group,
            at + ESTATE_FIRST_ROUND_DURATION + timedelta(minutes=1),
        )
    )
    assert all(
        card.activity.activity_id
        != ConfirmedActivityKind.ESTATE_FIRST_ROUND.value
        for card in reopened_cards.cards
    )
    assert reopened_service.handle(
        _event(
            ConfirmedActivityKind.ESTATE_FIRST_ROUND,
            ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
            reopened_group,
            at + ESTATE_FIRST_ROUND_DURATION + timedelta(minutes=2),
        )
    ) is False


def test_artifact_prompt_excludes_opened_role_recovers_after_disconnect_and_cleans_cross_day_card(
    tmp_path,
):
    service, cards, _bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 23, 55, tzinfo=TAIPEI_TIMEZONE)
    initial = service.handle_role_status("role-120", ROLE_STATUS_OPEN, at)
    assert initial[0].affected_character_ids == ("role-120", "role-160")
    assert service.handle_role_status(
        "role-100",
        ROLE_STATUS_DISCONNECTED,
        at + timedelta(seconds=30),
    ) == ()
    assert cards.cards == initial
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            group,
            at + timedelta(minutes=1),
            "role-120",
        )
    )
    assert cards.cards[0].affected_character_ids == ("role-160",)
    assert service.handle_role_status(
        "role-120",
        ROLE_STATUS_DISCONNECTED,
        at + timedelta(minutes=2),
    ) == ()
    assert cards.cards == ()
    reconnected = service.handle_role_status(
        "role-120",
        ROLE_STATUS_OPEN,
        at + timedelta(minutes=3),
    )
    assert reconnected[0].affected_character_ids == ("role-120", "role-160")
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            group,
            at + timedelta(minutes=4),
            "role-120",
        )
    )
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            group,
            datetime(2026, 8, 4, 0, tzinfo=TAIPEI_TIMEZONE),
            "role-120",
        )
    ) is False
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_CLOSED,
            group,
            at + timedelta(minutes=6),
            "role-120",
        )
    )
    assert cards.cards == ()
    assert service.record("artifact-daily:role-120:2026-08-04").stage == "已完成"


def test_artifact_game_close_silences_today_and_allows_a_new_day_start(tmp_path):
    service, cards, _bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 20, tzinfo=TAIPEI_TIMEZONE)
    assert service.handle_role_status("role-120", ROLE_STATUS_OPEN, at)
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            group,
            at + timedelta(minutes=1),
            "role-120",
        )
    )
    assert cards.cards[0].affected_character_ids == ("role-160",)
    assert service.handle_role_status(
        "role-120",
        ROLE_STATUS_CLOSED,
        at + timedelta(minutes=2),
    ) == ()
    record = service.record("artifact-daily:role-120:2026-08-03")
    assert record.stage == "遊戲關閉不提醒"
    assert record.completed_at == at + timedelta(minutes=2)
    assert service.handle_role_status(
        "role-120",
        ROLE_STATUS_OPEN,
        at + timedelta(minutes=3),
    ) == ()
    assert cards.cards[0].affected_character_ids == ("role-160",)
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            group,
            at + timedelta(minutes=4),
            "role-120",
        )
    ) is False
    assert service.handle(
        _event(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            group,
            at + timedelta(days=1),
            "role-120",
        )
    ) is True


def test_deity_and_golden_ticket_keep_confirmed_levels_times_and_card_lifetime(
    tmp_path,
):
    service, cards, _bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 11, 55, tzinfo=TAIPEI_TIMEZONE)

    assert service.handle(
        _event(
            ConfirmedActivityKind.DEITY_CULTIVATION,
            ConfirmedActivityEventType.DEITY_TASK_STARTED,
            group,
            at,
            "role-120",
        )
    ) is False
    assert service.handle(
        _event(
            ConfirmedActivityKind.DEITY_CULTIVATION,
            ConfirmedActivityEventType.DEITY_TASK_STARTED,
            group,
            at,
            "role-160",
        )
    )
    assert service.handle(
        _event(
            ConfirmedActivityKind.DEITY_CULTIVATION,
            ConfirmedActivityEventType.DEITY_TASK_STARTED,
            group,
            datetime(2026, 8, 4, 0, tzinfo=TAIPEI_TIMEZONE),
            "role-160",
        )
    ) is False
    assert service.poll(at + DEITY_CULTIVATION_DURATION) == ()
    assert cards.cards == ()
    assert service.handle(
        _event(
            ConfirmedActivityKind.DEITY_CULTIVATION,
            ConfirmedActivityEventType.DEITY_TASK_COMPLETED,
            group,
            datetime(2026, 8, 4, 0, 1, tzinfo=TAIPEI_TIMEZONE),
            "role-160",
        )
    )
    assert service.record("deity-cultivation:group-14:2026-08-04").stage == "已完成"

    noon = datetime(2026, 8, 4, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    ticket = service.poll(noon)
    assert len(ticket) == 1
    assert ticket[0].affected_character_ids == (
        "role-120",
        "role-160",
        "role-100",
    )
    assert cards.entries[0].expires_at == noon + timedelta(minutes=10)
    for role in ("role-120", "role-160", "role-100"):
        assert service.handle(
            _event(
                ConfirmedActivityKind.GOLDEN_TICKET_EXCHANGE,
                ConfirmedActivityEventType.GOLDEN_TICKET_INTERFACE_OPENED,
                group,
                noon + timedelta(minutes=1),
                role,
            )
        )
    assert cards.cards == ()


def test_restart_keeps_timers_daily_results_counts_current_group_and_markers(
    tmp_path,
):
    service, _cards, _bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 11, 30, tzinfo=TAIPEI_TIMEZONE)
    assert service.handle(
        _event(
            ConfirmedActivityKind.DIMENSION_SPACE,
            ConfirmedActivityEventType.DIMENSION_ENTERED,
            group,
            at,
            "role-120",
            floor=1,
        )
    )
    assert service.handle(
        _event(
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityEventType.CONFIRMED_COMPLETE,
            group,
            at,
            "role-160",
        )
    )
    assert service.handle(
        _event(
            ConfirmedActivityKind.FANTASY_TRAINING,
            ConfirmedActivityEventType.TRAINING_STARTED,
            group,
            at - timedelta(minutes=20),
            "role-120",
        )
    )
    assert service.handle(
        _event(
            ConfirmedActivityKind.FANTASY_TRAINING,
            ConfirmedActivityEventType.TRAINING_COLLECTED,
            group,
            at - timedelta(minutes=10),
            "role-120",
        )
    )
    assert len(
        service.handle_role_status(
            "role-120",
            ROLE_STATUS_OPEN,
            at,
        )
    ) == 1
    assert len(service.poll(datetime(2026, 8, 3, 12, tzinfo=TAIPEI_TIMEZONE))) == 1

    reloaded_cards = CardService()
    reloaded = ConfirmedActivityRuleService(
        CardCoordinator(
            reloaded_cards,
            CardHistoryService(CardHistoryStore(tmp_path / "reloaded-history.json")),
        ),
        state_path=tmp_path / "confirmed.json",
    )

    dimension = reloaded.record("dimension-space:role-120:2026-08-03")
    assert dimension is not None
    assert dimension.stage == "進行中"
    assert dimension.elapsed_at(at + timedelta(minutes=45)) == timedelta(minutes=45)
    assert reloaded.record("magic-soldiers:role-160:2026-08-03").stage == "已完成"
    assert reloaded.fantasy_collection_count("role-120", at.date()) == 1
    assert reloaded.handle_role_status("role-120", ROLE_STATUS_OPEN, at) == ()
    assert reloaded.poll(datetime(2026, 8, 3, 12, tzinfo=TAIPEI_TIMEZONE)) == ()
    assert reloaded_cards.cards == ()


def test_rejected_or_failed_persistence_keeps_state_events_and_cards_unchanged(
    tmp_path,
    monkeypatch,
):
    service, cards, event_bus, group = _context(tmp_path)
    at = datetime(2026, 8, 3, 9, tzinfo=TAIPEI_TIMEZONE)
    events = []
    event_bus.subscribe(CONFIRMED_ACTIVITY_RULE_CHANGED_EVENT, events.append)
    before_bytes = service.state_path.read_bytes()
    rejected = _event(
        ConfirmedActivityKind.DIMENSION_SPACE,
        ConfirmedActivityEventType.DIMENSION_ENTERED,
        group,
        at,
        "role-120",
        floor=2,
    )
    assert service.handle(rejected) is False
    assert service.records() == ()
    assert service.state_path.read_bytes() == before_bytes
    assert events == []
    assert cards.cards == ()

    def fail_save(*_args):
        raise OSError("save failed")

    original_persist = service._persist
    monkeypatch.setattr(service, "_persist", fail_save)
    assert service.handle(
        _event(
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityEventType.CONFIRMED_COMPLETE,
            group,
            at,
            "role-120",
        )
    ) is False
    assert service.records() == ()
    assert events == []
    assert cards.cards == ()

    before_prompts = dict(service._artifact_prompts)
    assert service.handle_role_status("role-120", ROLE_STATUS_OPEN, at) == ()
    assert service.records() == ()
    assert service.state_path.read_bytes() == before_bytes
    assert service._artifact_prompts == before_prompts
    assert events == []
    assert cards.cards == ()

    monkeypatch.setattr(service, "_persist", original_persist)
    prompted = service.handle_role_status("role-120", ROLE_STATUS_OPEN, at)
    assert len(prompted) == 1
    assert prompted[0].activity.activity_id == ConfirmedActivityKind.ARTIFACT_DAILY.value
    assert prompted[0].affected_character_ids == ("role-120", "role-160")
    assert cards.cards == prompted
