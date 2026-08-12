from datetime import datetime

import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.progress import ActivityInterruptionReason, TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from domain.status import ActivityStatus
from main import ACTIVITY_PROGRESS_FILENAME, build_services
from services.activity_progress_service import ActivityProgressService
from services.activity_progress_service import (
    ACTIVITY_PROGRESS_CHANGED_EVENT,
    ActivityProgressChange,
)
from services.app_context import AppContext
from services.event_bus import EventBus


def _definition() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="farm",
        name="農場",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=2,
    )


def _second_definition() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="raid",
        name="group-raid",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=2,
    )


def _completed_definition() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="completed",
        name="completed-activity",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=1,
    )


def _progress(service, activity_id, subject_id):
    return next(
        item
        for item in service.all()
        if item.activity_id == activity_id and item.subject_id == subject_id
    )


def test_service_persists_start_and_completion(tmp_path):
    path = tmp_path / "activity_progress.json"
    service = ActivityProgressService(ActivityProgressStore(path))
    service.register_definition(_definition())
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)

    service.start("farm", "character-a", now)
    completed = service.record_completion("farm", "character-a", now)
    reloaded = ActivityProgressService(ActivityProgressStore(path))

    assert completed.current_count == 1
    assert completed.status is ActivityStatus.STANDBY
    assert _progress(reloaded, "farm", "character-a") == completed


def test_service_resets_registered_daily_progress_after_midnight(tmp_path):
    service = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json")
    )
    service.register_definition(_definition())
    before = datetime(2026, 7, 11, 23, 59, tzinfo=TAIPEI_TIMEZONE)
    after = datetime(2026, 7, 12, 0, 0, tzinfo=TAIPEI_TIMEZONE)
    service.record_completion("farm", "character-a", before)

    reset = service.reset_due(after)[0]

    assert reset.current_count == 0
    assert reset.status is ActivityStatus.STANDBY
    assert reset.period_started_on.isoformat() == "2026-07-12"


def test_service_requires_a_registered_activity(tmp_path):
    service = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json")
    )
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)

    try:
        service.start("unknown", "character-a", now)
    except KeyError as exc:
        assert "Unknown activity" in str(exc)
    else:
        raise AssertionError("Unknown activity must be rejected.")


def test_build_services_registers_progress_inside_managed_data(tmp_path):
    paths, _logger = build_services(root=tmp_path)

    store = AppContext.get(ActivityProgressStore)
    service = AppContext.get(ActivityProgressService)

    assert store.path == paths.data_dir() / ACTIVITY_PROGRESS_FILENAME
    assert service.store is store


def test_each_real_change_is_saved_before_one_event(tmp_path):
    bus = EventBus()
    changes: list[ActivityProgressChange] = []
    path = tmp_path / "activity_progress.json"
    disk_snapshots = []
    bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, changes.append)
    bus.subscribe(
        ACTIVITY_PROGRESS_CHANGED_EVENT,
        lambda _change: disk_snapshots.append(
            ActivityProgressStore(path).load()
        ),
    )
    service = ActivityProgressService(
        ActivityProgressStore(path),
        bus,
    )
    service.register_definition(_definition())
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)

    service.start("farm", "character-a", now)
    service.record_completion("farm", "character-a", now)

    assert [change.reason for change in changes] == [
        "started",
        "completion_recorded",
    ]
    assert all(isinstance(change, ActivityProgressChange) for change in changes)
    assert _progress(
        ActivityProgressService(ActivityProgressStore(path)),
        "farm",
        "character-a",
    ) == changes[-1].current
    assert disk_snapshots[-1][0] == changes[-1].current


def test_unchanged_completion_does_not_save_or_publish_again(tmp_path):
    bus = EventBus()
    changes: list[ActivityProgressChange] = []
    bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, changes.append)
    service = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json"),
        bus,
    )
    definition = ActivityDefinition(
        activity_id="once",
        name="單次活動",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=1,
    )
    service.register_definition(definition)
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)

    service.record_completion("once", "character-a", now)
    service.record_completion("once", "character-a", now)

    assert len(changes) == 1


def test_failed_atomic_save_keeps_old_memory_and_publishes_nothing(tmp_path):
    class FailingStore(ActivityProgressStore):
        def save(self, progress):
            raise OSError("simulated save failure")

    bus = EventBus()
    changes: list[ActivityProgressChange] = []
    bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, changes.append)
    service = ActivityProgressService(
        FailingStore(tmp_path / "activity_progress.json"),
        bus,
    )
    service.register_definition(_definition())
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)

    try:
        service.start("farm", "character-a", now)
    except OSError as error:
        assert "simulated save failure" in str(error)
    else:
        raise AssertionError("save failure must be reported")

    assert service.all() == ()
    assert changes == []


def test_role_interruption_updates_only_running_exact_subject_and_persists(tmp_path):
    path = tmp_path / "activity_progress.json"
    service = ActivityProgressService(ActivityProgressStore(path))
    service.register_definition(_definition())
    service.register_definition(_second_definition())
    service.register_definition(_completed_definition())
    started = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)
    interrupted_at = datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE)
    service.start("farm", "character-a", started)
    service.start("raid", "character-a", started)
    service.start("farm", "character-b", started)
    service.record_completion("completed", "character-a", started)

    changed = service.record_interruption(
        "character-a",
        ActivityInterruptionReason.DISCONNECTED,
        interrupted_at,
    )
    reloaded = ActivityProgressService(ActivityProgressStore(path))

    assert {item.activity_id for item in changed} == {"farm", "raid"}
    assert (
        _progress(service, "farm", "character-a").interruption.reason
        is ActivityInterruptionReason.DISCONNECTED
    )
    assert _progress(service, "raid", "character-a").interruption is not None
    assert _progress(service, "farm", "character-b").interruption is None
    assert _progress(service, "completed", "character-a").interruption is None
    assert (
        _progress(reloaded, "farm", "character-a").interruption.reason
        is ActivityInterruptionReason.DISCONNECTED
    )


def test_open_role_clears_only_exact_subject_interruption(tmp_path):
    service = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json")
    )
    service.register_definition(_definition())
    started = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)
    disconnected = datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE)
    reopened = datetime(2026, 7, 11, 20, 10, tzinfo=TAIPEI_TIMEZONE)
    service.start("farm", "character-a", started)
    service.start("farm", "character-b", started)
    service.record_interruption(
        "character-a",
        ActivityInterruptionReason.DISCONNECTED,
        disconnected,
    )
    service.record_interruption(
        "character-b",
        ActivityInterruptionReason.GAME_CLOSED,
        disconnected,
    )
    before_reopen = _progress(service, "farm", "character-a")

    changed = service.clear_interruption("character-a", reopened)

    assert len(changed) == 1
    restored = _progress(service, "farm", "character-a")
    assert restored.status is ActivityStatus.RUNNING
    assert restored.started_at == before_reopen.started_at == started
    assert restored.current_count == before_reopen.current_count
    assert restored.period_started_on == before_reopen.period_started_on
    assert restored.interruption is None
    assert _progress(service, "farm", "character-b").interruption.reason is (
        ActivityInterruptionReason.GAME_CLOSED
    )


def test_interruption_transition_saves_all_affected_progress_before_typed_changes(
    tmp_path,
):
    class CountingStore(ActivityProgressStore):
        def __init__(self, path):
            super().__init__(path)
            self.save_calls = 0
            self.fail_saves = False

        def save(self, progress):
            self.save_calls += 1
            if self.fail_saves:
                raise OSError("interruption save failed")
            super().save(progress)

    bus = EventBus()
    changes: list[ActivityProgressChange] = []
    snapshots = []
    path = tmp_path / "activity_progress.json"
    bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, changes.append)
    bus.subscribe(
        ACTIVITY_PROGRESS_CHANGED_EVENT,
        lambda _change: snapshots.append(
            ActivityProgressStore(path).load()
        ),
    )
    store = CountingStore(path)
    service = ActivityProgressService(store, bus)
    service.register_definition(_definition())
    service.register_definition(_second_definition())
    started = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)
    closed_at = datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE)
    service.start("farm", "character-a", started)
    service.start("raid", "character-a", started)
    changes.clear()
    snapshots.clear()
    saves_before_interruption = store.save_calls

    changed = service.record_interruption(
        "character-a",
        ActivityInterruptionReason.GAME_CLOSED,
        closed_at,
    )

    assert len(changed) == 2
    assert store.save_calls == saves_before_interruption + 1
    assert [change.reason for change in changes] == ["interrupted", "interrupted"]
    assert all(isinstance(change, ActivityProgressChange) for change in changes)
    assert len(snapshots) == 2
    assert all(
        all(
            item.interruption is not None
            and item.interruption.reason is ActivityInterruptionReason.GAME_CLOSED
            for item in snapshot
        )
        for snapshot in snapshots
    )

    failed_bus = EventBus()
    failed_changes: list[ActivityProgressChange] = []
    failed_bus.subscribe(ACTIVITY_PROGRESS_CHANGED_EVENT, failed_changes.append)
    failed_store = CountingStore(tmp_path / "failed_activity_progress.json")
    failed_service = ActivityProgressService(failed_store, failed_bus)
    failed_service.register_definition(_definition())
    failed_service.start("farm", "character-a", started)
    failed_changes.clear()
    before_failure = failed_service.all()
    failed_store.fail_saves = True

    with pytest.raises(OSError, match="interruption save failed"):
        failed_service.record_interruption(
            "character-a",
            ActivityInterruptionReason.DISCONNECTED,
            closed_at,
        )

    assert failed_service.all() == before_failure
    assert failed_changes == []
