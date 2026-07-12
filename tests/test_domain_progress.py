from datetime import datetime

import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.progress import ActivityProgress, TAIPEI_TIMEZONE
from domain.status import ActivityStatus


def _guard() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=16,
    )


def test_progress_records_start_and_each_completion():
    started = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)
    completed = datetime(2026, 7, 11, 20, 5, tzinfo=TAIPEI_TIMEZONE)
    progress = ActivityProgress(activity_id="guard", subject_id="character-a")

    running = progress.start(started)
    updated = running.record_completion(_guard(), completed)

    assert running.status is ActivityStatus.RUNNING
    assert updated.current_count == 1
    assert updated.status is ActivityStatus.STANDBY
    assert updated.started_at == started
    assert updated.completed_at == completed


def test_progress_becomes_completed_at_the_daily_maximum():
    definition = _guard()
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)
    progress = ActivityProgress(activity_id="guard", subject_id="character-a")

    for _ in range(16):
        progress = progress.record_completion(definition, now)

    assert progress.current_count == 16
    assert progress.status is ActivityStatus.COMPLETED
    assert progress.record_completion(definition, now) is progress


def test_daily_progress_resets_at_the_first_check_after_midnight():
    definition = _guard()
    before_midnight = datetime(2026, 7, 11, 23, 59, tzinfo=TAIPEI_TIMEZONE)
    after_midnight = datetime(2026, 7, 12, 0, 0, tzinfo=TAIPEI_TIMEZONE)
    progress = ActivityProgress(activity_id="guard", subject_id="character-a")
    progress = progress.start(before_midnight).record_completion(definition, before_midnight)

    reset = progress.reset_if_due(definition, after_midnight)

    assert reset.current_count == 0
    assert reset.status is ActivityStatus.STANDBY
    assert reset.period_started_on.isoformat() == "2026-07-12"
    assert reset.started_at is None
    assert reset.completed_at is None


def test_daily_progress_does_not_reset_twice_on_the_same_day():
    definition = _guard()
    now = datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE)
    progress = ActivityProgress(activity_id="guard", subject_id="character-a")
    progress = progress.record_completion(definition, now)

    assert progress.reset_if_due(definition, now) is progress


def test_progress_requires_timezone_aware_times():
    progress = ActivityProgress(activity_id="guard", subject_id="character-a")

    with pytest.raises(ValueError):
        progress.start(datetime(2026, 7, 11, 20, 0))
