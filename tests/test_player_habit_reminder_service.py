from datetime import datetime, timedelta, timezone

from cards.history_store import CardHistoryStore
from cards.service import CardService
from habit.preference_models import HabitDecision
from habit.preference_service import PlayerHabitPreferenceService
from habit.preference_store import PlayerHabitStore
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.player_habit_reminder_monitor import (
    HABIT_REMINDER_CHECK_MS,
    PlayerHabitReminderMonitor,
)
from services.player_habit_reminder_service import (
    HABIT_ACTIONS,
    PlayerHabitReminderService,
)


TAIPEI = timezone(timedelta(hours=8))


def _ready_habits(tmp_path) -> tuple[PlayerHabitPreferenceService, datetime]:
    service = PlayerHabitPreferenceService(
        PlayerHabitStore(tmp_path / "player_habits.json")
    )
    service.set_observation_days(8)
    start = datetime(2026, 7, 1, 19, 50, tzinfo=TAIPEI)
    for day in range(8):
        service.record_activity_time("神秘考官", start + timedelta(days=day))
    service.record_activity_time(
        "神秘考官",
        start + timedelta(days=6),
    )
    service.record_activity_time(
        "神秘考官",
        start + timedelta(days=7),
    )
    return service, start + timedelta(days=8)


def _reminders(tmp_path):
    habits, now = _ready_habits(tmp_path)
    cards = CardService()
    coordinator = CardCoordinator(
        cards,
        CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
    )
    records = []
    service = PlayerHabitReminderService(
        habits,
        coordinator,
        cards,
        lambda: "14支",
        record_callback=lambda *values: records.append(values),
    )
    return habits, cards, service, records, now


def test_ready_candidate_becomes_one_preference_card_with_four_actions(
    tmp_path,
) -> None:
    _habits, cards, service, _records, now = _reminders(tmp_path)

    shown = service.refresh(now)

    assert len(shown) == 1
    assert shown[0].group.name == "14支"
    assert shown[0].current_progress == "活動時間｜神秘考官｜19:50"
    assert shown[0].actions == HABIT_ACTIONS
    assert cards.cards == shown


def test_player_action_saves_choice_removes_card_and_records_result(
    tmp_path,
) -> None:
    habits, cards, service, records, now = _reminders(tmp_path)
    card = service.refresh(now)[0]

    result = service.handle_action(card.card_id, "later", now)

    assert result.decision is HabitDecision.SNOOZED
    assert result.remind_after == now + timedelta(minutes=10)
    assert cards.cards == ()
    assert records == [
        (
            "玩家習慣",
            "神秘考官",
            "活動時間｜神秘考官｜19:50－稍後再問",
        )
    ]
    assert service.refresh(now + timedelta(minutes=9)) == ()
    assert len(service.refresh(now + timedelta(minutes=10))) == 1


def test_monitor_checks_immediately_then_once_per_minute(tmp_path) -> None:
    _habits, _cards, service, _records, now = _reminders(tmp_path)
    scheduled = []
    cancelled = []
    monitor = PlayerHabitReminderMonitor(
        service,
        lambda delay, callback: scheduled.append((delay, callback)) or "after-1",
        cancelled.append,
        now_provider=lambda: now,
    )

    monitor.start()

    assert monitor.running is True
    assert scheduled[0][0] == HABIT_REMINDER_CHECK_MS
    monitor.stop()
    assert cancelled == ["after-1"]
