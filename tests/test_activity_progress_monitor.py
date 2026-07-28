from datetime import datetime

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.progress import TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from domain.status import ActivityStatus
from services.activity_progress_monitor import (
    ACTIVITY_PROGRESS_CHECK_MS,
    ActivityProgressMonitor,
)
from services.activity_progress_service import (
    ACTIVITY_PROGRESS_CHANGED_EVENT,
    ActivityProgressChange,
    ActivityProgressService,
)
from services.event_bus import EventBus


def _definition() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="long-running",
        name="跨日執行活動",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
    )


def _service(tmp_path, bus=None) -> ActivityProgressService:
    service = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json"),
        bus,
    )
    service.register_definition(_definition())
    return service


class Scheduler:
    def __init__(self) -> None:
        self.items: list[tuple[int, object, object]] = []
        self.cancelled: list[object] = []

    def schedule(self, delay_ms, callback):
        after_id = f"after-{len(self.items) + 1}"
        self.items.append((delay_ms, callback, after_id))
        return after_id

    def cancel(self, after_id):
        self.cancelled.append(after_id)


def test_2359_to_0000_carries_running_state_and_emits_once(tmp_path):
    bus = EventBus()
    changes: list[ActivityProgressChange] = []
    bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, changes.append)
    service = _service(tmp_path, bus)
    before = datetime(2026, 7, 11, 23, 59, tzinfo=TAIPEI_TIMEZONE)
    midnight = datetime(2026, 7, 12, 0, 0, tzinfo=TAIPEI_TIMEZONE)
    service.start("long-running", "character-a", before)
    changes.clear()

    first = service.reset_due(midnight)
    second = service.reset_due(midnight)

    assert first == second
    assert first[0].status is ActivityStatus.RUNNING
    assert first[0].period_started_on == midnight.date()
    assert first[0].started_at == before
    assert [change.reason for change in changes] == ["running_carried_over"]


def test_sleep_wake_poll_handles_elapsed_midnight(tmp_path):
    service = _service(tmp_path)
    before = datetime(2026, 7, 11, 23, 58, tzinfo=TAIPEI_TIMEZONE)
    after_wake = datetime(2026, 7, 12, 8, 30, tzinfo=TAIPEI_TIMEZONE)
    service.start("long-running", "character-a", before)
    current = [before]
    scheduler = Scheduler()
    monitor = ActivityProgressMonitor(
        service,
        scheduler.schedule,
        scheduler.cancel,
        now=lambda: current[0],
    )

    assert monitor.start() is True
    assert scheduler.items[0][0] == ACTIVITY_PROGRESS_CHECK_MS
    current[0] = after_wake
    scheduler.items[0][1]()

    assert service.all()[0].status is ActivityStatus.RUNNING
    assert service.all()[0].period_started_on == after_wake.date()
    assert len(scheduler.items) == 2
    assert monitor.stop() is True
    assert scheduler.cancelled == [scheduler.items[-1][2]]


def test_program_restart_after_midnight_recovers_before_scheduling(tmp_path):
    before = datetime(2026, 7, 11, 23, 59, tzinfo=TAIPEI_TIMEZONE)
    after = datetime(2026, 7, 12, 0, 5, tzinfo=TAIPEI_TIMEZONE)
    original = _service(tmp_path)
    original.start("long-running", "character-a", before)
    restarted = _service(tmp_path)
    scheduler = Scheduler()
    monitor = ActivityProgressMonitor(
        restarted,
        scheduler.schedule,
        scheduler.cancel,
        now=lambda: after,
    )

    monitor.start()

    assert restarted.all()[0].status is ActivityStatus.RUNNING
    assert restarted.all()[0].period_started_on == after.date()
    assert scheduler.items[0][0] == ACTIVITY_PROGRESS_CHECK_MS


def test_clock_rollback_does_not_reset_backwards_or_repeat_event(tmp_path):
    bus = EventBus()
    changes: list[ActivityProgressChange] = []
    bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, changes.append)
    service = _service(tmp_path, bus)
    current = datetime(2026, 7, 12, 8, 0, tzinfo=TAIPEI_TIMEZONE)
    rolled_back = datetime(2026, 7, 11, 22, 0, tzinfo=TAIPEI_TIMEZONE)
    service.start("long-running", "character-a", current)
    changes.clear()

    before = service.all()
    after = service.reset_due(rolled_back)

    assert after == before
    assert changes == []


def test_main_window_starts_stops_and_detaches_progress_monitor():
    source = open("main.py", encoding="utf-8").read()

    assert "ActivityProgressMonitor(" in source
    assert 'stop_named("activity_progress", activity_progress_monitor)' in source
    assert "ACTIVITY_PROGRESS_CHANGED_EVENT" in source
    assert "event_bus.unsubscribe(" in source
