from datetime import datetime, timedelta, timezone

from cards.history_store import CardHistoryStore
from cards.priority import CardPriorityReason, CardPriorityTier
from cards.service import CardService
from domain.character import Character
from domain.group import CharacterGroup
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.farm_timer_service import (
    COPY_CODE_ACTION_ID,
    FarmCompleted,
    FarmPlantingConfirmed,
    FarmTimerService,
)


def _context(tmp_path):
    characters = (
        Character("role-a", "120古", 120),
        Character("role-b", "120靈", 120),
    )
    group = CharacterGroup("group-14", "14支", characters)
    cards = CardService()
    copied = []
    records = []
    service = FarmTimerService(
        CardCoordinator(
            cards,
            CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
        ),
        state_path=tmp_path / "farm.json",
        clipboard_writer=lambda value: copied.append(value),
        record_callback=lambda *values: records.append(values),
    )
    return service, cards, group, copied, records


def _start(service, group, role, planted_at, code):
    return service.start(
        FarmPlantingConfirmed(
            timer_id=f"timer-{role}",
            group=group,
            character_id=role,
            planted_at=planted_at,
            copy_code=code,
        )
    )


def test_each_character_matures_independently_at_exactly_forty_minutes(tmp_path):
    service, cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    _start(service, group, "role-a", planted_at, "正確代碼甲")
    _start(
        service,
        group,
        "role-b",
        planted_at + timedelta(minutes=1),
        "正確代碼乙",
    )

    assert service.poll(planted_at + timedelta(minutes=39, seconds=59)) == ()
    first = service.poll(planted_at + timedelta(minutes=40))

    assert len(first) == 1
    assert first[0].affected_character_ids == ("role-a",)
    assert first[0].priority_reason is CardPriorityReason.TIME_LIMIT
    assert cards.cards == first

    second = service.poll(planted_at + timedelta(minutes=41))
    assert len(second) == 1
    assert second[0].affected_character_ids == ("role-b",)
    assert len(cards.cards) == 2


def test_overdue_at_forty_five_minutes_updates_same_card_and_priority(tmp_path):
    service, cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    timer = _start(service, group, "role-a", planted_at, "正確代碼")
    mature = service.poll(planted_at + timedelta(minutes=40))[0]
    overdue = service.poll(planted_at + timedelta(minutes=45))[0]

    assert mature.card_id == timer.card_id == overdue.card_id
    assert overdue.priority_reason is CardPriorityReason.LOSS_RISK
    assert overdue.priority_tier is CardPriorityTier.HIGHEST
    assert cards.cards == (overdue,)
    assert service.poll(planted_at + timedelta(minutes=46)) == ()


def test_card_action_copies_exact_event_code_and_completion_removes_card(tmp_path):
    service, cards, group, copied, records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    timer = _start(service, group, "role-a", planted_at, "/farm exact")
    card = service.poll(planted_at + timedelta(minutes=40))[0]

    result = service.handle_action(card.card_id, COPY_CODE_ACTION_ID)
    completed = service.complete(
        FarmCompleted(timer.timer_id, planted_at + timedelta(minutes=41))
    )

    assert result is True
    assert copied == ["/farm exact"]
    assert completed is True
    assert cards.cards == ()
    assert [item[2] for item in records] == [
        "已開始獨立計時",
        "已成熟",
        "已複製正確代碼",
        "已完成並移除提醒",
    ]


def test_timer_state_survives_restart_without_repeating_same_stage(tmp_path):
    service, _cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    _start(service, group, "role-a", planted_at, "正確代碼")
    service.poll(planted_at + timedelta(minutes=40))

    reloaded_cards = CardService()
    copied = []
    reloaded = FarmTimerService(
        CardCoordinator(
            reloaded_cards,
            CardHistoryService(CardHistoryStore(tmp_path / "other-history.json")),
        ),
        state_path=tmp_path / "farm.json",
        clipboard_writer=copied.append,
    )

    assert reloaded.poll(planted_at + timedelta(minutes=44)) == ()
    overdue = reloaded.poll(planted_at + timedelta(minutes=45))
    assert len(overdue) == 1
    assert overdue[0].priority_reason is CardPriorityReason.LOSS_RISK
    assert len(reloaded.timers()) == 1


def test_unconfirmed_or_empty_code_is_rejected_before_timer_exists(tmp_path):
    service, _cards, group, _copied, _records = _context(tmp_path)
    planted_at = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)

    try:
        FarmPlantingConfirmed(
            "timer-a",
            group,
            "role-a",
            planted_at,
            " ",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("empty code must be rejected")

    assert service.timers() == ()
