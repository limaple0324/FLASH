from datetime import datetime, timedelta, timezone

from domain.progress import ActivityProgress
from habit.preference_models import HabitKind
from habit.preference_service import PlayerHabitPreferenceService
from habit.preference_store import PlayerHabitStore
from services.activity_progress_service import ActivityProgressChange
from services.player_habit_activity_observer import PlayerHabitActivityObserver


TAIPEI = timezone(timedelta(hours=8))


def _observer(tmp_path):
    habits = PlayerHabitPreferenceService(
        PlayerHabitStore(tmp_path / "player_habits.json")
    )
    return habits, PlayerHabitActivityObserver(habits)


def _change(
    *,
    reason: str = "completion_recorded",
    activity_id: str = "神秘考官",
    subject_id: str = "主號",
    count: int = 1,
    at: datetime,
) -> ActivityProgressChange:
    previous = ActivityProgress(
        activity_id=activity_id,
        subject_id=subject_id,
        current_count=max(0, count - 1),
    )
    current = ActivityProgress(
        activity_id=activity_id,
        subject_id=subject_id,
        current_count=count,
        completed_at=at,
    )
    return ActivityProgressChange(reason, at, previous, current)


def test_only_typed_completion_event_creates_traceable_observations(
    tmp_path,
) -> None:
    habits = PlayerHabitPreferenceService(
        PlayerHabitStore(tmp_path / "player_habits.json")
    )
    observer = PlayerHabitActivityObserver(
        habits,
        activity_name_provider=lambda _activity_id: "神秘考官",
    )
    at = datetime(2026, 7, 1, 19, 50, tzinfo=TAIPEI)

    recorded = observer.handle(_change(at=at))

    assert tuple(item.kind for item in recorded) == (
        HabitKind.ACTIVITY_TIME,
        HabitKind.CHARACTER_ORDER,
    )
    assert all(item.source_event_ids for item in recorded)
    assert recorded[0].subject == "神秘考官"
    assert recorded[1].values == ("神秘考官",)
    assert habits.snapshot().observations == recorded


def test_non_completion_signals_are_not_learned(tmp_path) -> None:
    habits, observer = _observer(tmp_path)
    at = datetime(2026, 7, 1, 19, 50, tzinfo=TAIPEI)

    assert observer.handle(_change(reason="started", at=at)) == ()
    assert observer.handle(_change(reason="daily_reset", at=at)) == ()
    assert habits.snapshot().observations == ()


def test_same_true_event_is_idempotent_after_restart(tmp_path) -> None:
    path = tmp_path / "player_habits.json"
    at = datetime(2026, 7, 1, 19, 50, tzinfo=TAIPEI)
    change = _change(at=at)
    first = PlayerHabitPreferenceService(PlayerHabitStore(path))
    PlayerHabitActivityObserver(first).handle(change)
    restarted = PlayerHabitPreferenceService(PlayerHabitStore(path))

    assert PlayerHabitActivityObserver(restarted).handle(change) == ()
    assert len(restarted.snapshot().observations) == 2


def test_daily_character_order_is_updated_by_true_completion_sequence(
    tmp_path,
) -> None:
    habits, observer = _observer(tmp_path)
    start = datetime(2026, 7, 1, 12, 55, tzinfo=TAIPEI)

    observer.handle(
        _change(
            activity_id="諸魔殿",
            count=1,
            at=start,
        )
    )
    observer.handle(
        _change(
            activity_id="魔兵",
            count=1,
            at=start + timedelta(minutes=10),
        )
    )

    orders = tuple(
        item
        for item in habits.snapshot().observations
        if item.kind is HabitKind.CHARACTER_ORDER
    )
    assert len(orders) == 1
    assert orders[0].values == ("諸魔殿", "魔兵")
    assert len(orders[0].source_event_ids) == 2
