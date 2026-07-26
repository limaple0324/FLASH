from datetime import date, timedelta

from habit.models import HabitReviewState
from habit.service import ActivityOrderHabitService
from habit.store import ActivityOrderHabitStore
from main import ACTIVITY_ORDER_HABIT_FILENAME, build_services
from services.app_context import AppContext


def _service(tmp_path) -> ActivityOrderHabitService:
    return ActivityOrderHabitService(
        ActivityOrderHabitStore(tmp_path / "activity_order_habit.json")
    )


def _record_week(service: ActivityOrderHabitService, start: date) -> None:
    for offset in range(7):
        service.record_daily_order(
            start + timedelta(days=offset),
            ("hall-of-demons", "magic-soldiers"),
        )


def test_first_seven_days_only_observe_and_eighth_day_is_ready_for_review(tmp_path):
    service = _service(tmp_path)
    start = date(2026, 7, 1)
    _record_week(service, start)

    day_seven = service.review(date(2026, 7, 7))
    day_eight = service.review(date(2026, 7, 8))

    assert day_seven.state is HabitReviewState.OBSERVING
    assert day_eight.state is HabitReviewState.REVIEW_READY
    assert day_eight.valid_observed_days == 7
    assert day_eight.order_counts == (
        (("hall-of-demons", "magic-soldiers"), 7),
    )


def test_exception_day_is_saved_but_does_not_count_toward_review(tmp_path):
    service = _service(tmp_path)
    start = date(2026, 7, 1)
    _record_week(service, start)
    service.record_daily_order(
        date(2026, 7, 7),
        ("magic-soldiers", "hall-of-demons"),
        is_exception=True,
    )

    review = service.review(date(2026, 7, 8))

    assert review.total_observed_days == 7
    assert review.valid_observed_days == 6
    assert review.state is HabitReviewState.OBSERVING


def test_same_day_observation_can_be_corrected_without_duplicate_day(tmp_path):
    service = _service(tmp_path)
    observed_on = date(2026, 7, 1)

    service.record_daily_order(observed_on, ("first", "second"))
    corrected = service.record_daily_order(observed_on, ("second", "first"))

    assert corrected.activity_ids == ("second", "first")
    assert len(service.snapshot().observations) == 1


def test_accept_modify_pause_resume_and_clear_are_persistent(tmp_path):
    path = tmp_path / "activity_order_habit.json"
    service = ActivityOrderHabitService(ActivityOrderHabitStore(path))
    service.record_daily_order(date(2026, 7, 1), ("first", "second"))

    service.accept(("first", "second"))
    assert service.review(date(2026, 7, 8)).state is HabitReviewState.ACCEPTED
    service.modify(("second", "first"))
    service.set_paused(True)
    assert service.review(date(2026, 7, 8)).state is HabitReviewState.PAUSED
    service.set_paused(False)

    reloaded = ActivityOrderHabitService(ActivityOrderHabitStore(path))
    assert reloaded.snapshot().accepted_order == ("second", "first")
    assert reloaded.review(date(2026, 7, 8)).state is HabitReviewState.ACCEPTED

    reloaded.clear_all()
    assert reloaded.snapshot().observations == ()
    assert ActivityOrderHabitService(
        ActivityOrderHabitStore(path)
    ).snapshot().accepted_order is None


def test_dismiss_review_does_not_delete_observations_or_auto_accept(tmp_path):
    service = _service(tmp_path)
    start = date(2026, 7, 1)
    _record_week(service, start)

    service.dismiss_review()
    review = service.review(date(2026, 7, 8))

    assert review.state is HabitReviewState.DISMISSED
    assert review.valid_observed_days == 7
    assert review.accepted_order is None


def test_remove_observation_supports_player_correction(tmp_path):
    service = _service(tmp_path)
    observed_on = date(2026, 7, 1)
    service.record_daily_order(observed_on, ("first",))

    assert service.remove_observation(observed_on) is True
    assert service.remove_observation(observed_on) is False
    assert service.snapshot().observations == ()


def test_build_services_registers_managed_habit_store_and_service(tmp_path):
    paths, _logger = build_services(root=tmp_path)

    store = AppContext.get(ActivityOrderHabitStore)
    service = AppContext.get(ActivityOrderHabitService)

    assert store.path == paths.data_dir() / ACTIVITY_ORDER_HABIT_FILENAME
    assert service.store is store
