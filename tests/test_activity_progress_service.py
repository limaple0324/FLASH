from datetime import datetime

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.progress import TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from domain.status import ActivityStatus
from main import ACTIVITY_PROGRESS_FILENAME, build_services
from services.app_context import AppContext
from services.activity_progress_service import ActivityProgressService


def _definition() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="farm",
        name="農場",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=2,
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
    assert reloaded.get("farm", "character-a") == completed


def test_service_resets_registered_daily_progress_after_midnight(tmp_path):
    service = ActivityProgressService(ActivityProgressStore(tmp_path / "activity_progress.json"))
    service.register_definition(_definition())
    before = datetime(2026, 7, 11, 23, 59, tzinfo=TAIPEI_TIMEZONE)
    after = datetime(2026, 7, 12, 0, 0, tzinfo=TAIPEI_TIMEZONE)
    service.record_completion("farm", "character-a", before)

    reset = service.reset_due(after)[0]

    assert reset.current_count == 0
    assert reset.status is ActivityStatus.STANDBY
    assert reset.period_started_on.isoformat() == "2026-07-12"


def test_service_requires_a_registered_activity(tmp_path):
    service = ActivityProgressService(ActivityProgressStore(tmp_path / "activity_progress.json"))
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
