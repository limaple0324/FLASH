from dataclasses import replace
from datetime import datetime, timedelta, timezone

from habit.preference_models import (
    HabitDecision,
    HabitKind,
    PlayerHabitCandidate,
    PlayerHabitObservation,
)
from habit.preference_service import PlayerHabitPreferenceService
from habit.preference_store import PlayerHabitStore
from main import PLAYER_HABIT_FILENAME, build_services
from services.app_context import AppContext


TAIPEI = timezone(timedelta(hours=8))


def _service(tmp_path) -> PlayerHabitPreferenceService:
    return PlayerHabitPreferenceService(
        PlayerHabitStore(tmp_path / "player_habits.json")
    )


def _with_observations(service, *observations):
    memory = service.snapshot()
    service.store.save(
        replace(memory, observations=memory.observations + tuple(observations))
    )
    return PlayerHabitPreferenceService(service.store)


def _activity_time(observed_at, *, is_exception=False):
    return PlayerHabitObservation(
        observed_at,
        HabitKind.ACTIVITY_TIME,
        "神秘考官",
        (observed_at.strftime("%H:%M"),),
        is_exception,
    )


def _character_order(observed_at, values):
    return PlayerHabitObservation(
        observed_at,
        HabitKind.CHARACTER_ORDER,
        "每日活動",
        values,
    )


def _record_confirmed_activity_pattern(
    service: PlayerHabitPreferenceService,
    start: datetime,
) -> PlayerHabitPreferenceService:
    return _with_observations(
        service,
        *(_activity_time(start + timedelta(days=day)) for day in range(7)),
    )


def test_default_rule_observes_seven_days_and_reviews_on_day_eight(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    start = datetime(2026, 7, 1, 19, 50, tzinfo=TAIPEI)
    service = _record_confirmed_activity_pattern(service, start)

    assert service.candidates(start + timedelta(days=6)) == ()

    candidates = service.candidates(start + timedelta(days=7))

    assert len(candidates) == 1
    assert candidates[0].kind is HabitKind.ACTIVITY_TIME
    assert candidates[0].subject == "神秘考官"
    assert candidates[0].values == ("19:50",)
    assert candidates[0].occurrence_count == 7
    assert candidates[0].distinct_days == 7


def test_exception_observations_do_not_count_and_observation_days_are_adjustable(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    service.set_observation_days(10)
    start = datetime(2026, 7, 1, 19, 50, tzinfo=TAIPEI)
    service = _with_observations(
        service,
        *(_activity_time(start + timedelta(days=day)) for day in range(6)),
        _activity_time(
            start + timedelta(days=6, minutes=1),
            is_exception=True,
        ),
        _activity_time(
            start + timedelta(days=6, minutes=2),
            is_exception=True,
        ),
    )

    assert service.candidates(start + timedelta(days=10)) == ()

    service = _with_observations(
        service,
        _activity_time(start + timedelta(days=7)),
    )
    assert len(service.candidates(start + timedelta(days=10))) == 1


def test_two_fully_qualified_opposite_patterns_are_not_suggested(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    service.set_observation_days(7)
    start = datetime(2026, 7, 1, 12, 0, tzinfo=TAIPEI)
    service = _with_observations(
        service,
        *(
            observation
            for day in range(7)
            for observation in (
                _character_order(
                    start + timedelta(days=day),
                    ("主號", "分號"),
                ),
                _character_order(
                    start + timedelta(days=day, minutes=1),
                    ("分號", "主號"),
                ),
            )
        ),
    )
    assert service.candidates(start + timedelta(days=7)) == ()


def test_four_confirmed_player_choices_are_persistent_and_filter_reminders(
    tmp_path,
) -> None:
    path = tmp_path / "player_habits.json"
    service = PlayerHabitPreferenceService(PlayerHabitStore(path))
    candidate = PlayerHabitCandidate(
        candidate_id="candidate-a",
        kind=HabitKind.CHARACTER_ORDER,
        subject="每日活動",
        values=("主號", "分號"),
        occurrence_count=10,
        distinct_days=8,
        first_observed_on=datetime(2026, 7, 1).date(),
        last_observed_on=datetime(2026, 7, 8).date(),
    )
    decided_at = datetime(2026, 7, 20, 8, 0, tzinfo=TAIPEI)

    adopted = service.choose(candidate, HabitDecision.ADOPTED, decided_at)
    assert adopted.decision is HabitDecision.ADOPTED
    assert (
        PlayerHabitPreferenceService(PlayerHabitStore(path))
        .snapshot()
        .preferences[0]
        .decision
        is HabitDecision.ADOPTED
    )

    today = service.choose(candidate, HabitDecision.TODAY_ONLY, decided_at)
    assert today.applies_on == decided_at.date()

    never = service.choose(candidate, HabitDecision.NEVER_ASK, decided_at)
    assert never.decision is HabitDecision.NEVER_ASK

    snoozed = service.choose(candidate, HabitDecision.SNOOZED, decided_at)
    assert snoozed.remind_after == decided_at + timedelta(minutes=10)


def test_saved_preferences_can_be_modified_removed_and_cleared(tmp_path) -> None:
    service = _service(tmp_path)
    decided_at = datetime(2026, 7, 20, 8, 0, tzinfo=TAIPEI)
    first = PlayerHabitCandidate(
        "first",
        HabitKind.ACTIVITY_TIME,
        "神秘考官",
        ("19:50",),
        10,
        8,
        decided_at.date(),
        decided_at.date(),
    )
    second = PlayerHabitCandidate(
        "second",
        HabitKind.CHARACTER_ORDER,
        "每日活動",
        ("主號", "分號"),
        10,
        8,
        decided_at.date(),
        decided_at.date(),
    )
    service.choose(first, HabitDecision.ADOPTED, decided_at)
    service.choose(second, HabitDecision.NEVER_ASK, decided_at)

    changed = service.modify_preference(
        "first",
        ("19:55",),
        decided_at + timedelta(minutes=1),
    )
    assert changed.values == ("19:55",)
    assert service.remove_preference("second") is True
    assert service.remove_preference("missing") is False
    service.store.save(replace(service.snapshot(), preferences=()))
    service = PlayerHabitPreferenceService(service.store)
    assert service.snapshot().preferences == ()


def test_settings_view_states_fixed_and_adjustable_rules(tmp_path) -> None:
    service = _service(tmp_path)
    service.set_observation_days(21)

    view = service.settings_view()

    assert view.observation_days == 21
    assert view.minimum_occurrences == 7
    assert view.minimum_distinct_days == 7
    assert view.preferences == ()


def test_true_observations_are_traceable_and_visible_in_settings(tmp_path) -> None:
    service = _service(tmp_path)
    observed_at = datetime(2026, 7, 20, 19, 50, tzinfo=TAIPEI)

    service.record_activity_completion(
        "神秘考官",
        "主號",
        observed_at,
        source_event_id="event-1",
    )

    observations = service.settings_view().observations
    assert len(observations) == 2
    assert all(item.source_event_ids == ("event-1",) for item in observations)
    assert service.remove_observation(observations[0].observation_id) is True
    assert service.remove_observation(observations[0].observation_id) is False
    assert len(service.settings_view().observations) == 1


def test_expired_raw_observations_and_temporary_decisions_are_cleaned(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    old = datetime(2026, 6, 1, 19, 50, tzinfo=TAIPEI)
    now = datetime(2026, 7, 20, 19, 50, tzinfo=TAIPEI)
    service = _with_observations(service, _activity_time(old))
    candidate = PlayerHabitCandidate(
        "old-rejection",
        HabitKind.ACTIVITY_TIME,
        "神秘考官",
        ("19:50",),
        7,
        7,
        old.date(),
        old.date(),
    )
    service.choose(candidate, HabitDecision.NEVER_ASK, old)

    assert service.cleanup_expired(now) == 2
    assert service.snapshot().observations == ()
    assert service.snapshot().preferences == ()


def test_clear_all_removes_observations_and_saved_preferences(tmp_path) -> None:
    service = _service(tmp_path)
    now = datetime(2026, 7, 20, 19, 50, tzinfo=TAIPEI)
    service = _with_observations(service, _activity_time(now))
    candidate = PlayerHabitCandidate(
        "saved",
        HabitKind.ACTIVITY_TIME,
        "神秘考官",
        ("19:50",),
        7,
        7,
        now.date(),
        now.date(),
    )
    service.choose(candidate, HabitDecision.ADOPTED, now)

    assert service.clear_all() == 2
    assert service.snapshot().observations == ()
    assert service.snapshot().preferences == ()


def test_settings_view_exposes_editable_preference_values(tmp_path) -> None:
    service = _service(tmp_path)
    decided_at = datetime(2026, 7, 20, 8, 0, tzinfo=TAIPEI)
    candidate = PlayerHabitCandidate(
        "editable",
        HabitKind.CHARACTER_ORDER,
        "每日活動",
        ("主號", "分號"),
        10,
        8,
        decided_at.date(),
        decided_at.date(),
    )
    service.choose(candidate, HabitDecision.ADOPTED, decided_at)

    preference = service.settings_view().preferences[0]

    assert preference.kind == "角色操作順序"
    assert preference.subject == "每日活動"
    assert preference.values == ("主號", "分號")


def test_build_services_registers_player_habit_store_and_service(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path)

    store = AppContext.get(PlayerHabitStore)
    service = AppContext.get(PlayerHabitPreferenceService)

    assert store.path == paths.data_dir() / PLAYER_HABIT_FILENAME
    assert service.store is store
