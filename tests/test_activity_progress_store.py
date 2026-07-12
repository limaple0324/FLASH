from datetime import datetime

from domain.progress import ActivityProgress, TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from domain.status import ActivityStatus


def test_progress_store_round_trips_player_progress(tmp_path):
    path = tmp_path / "activity_progress.json"
    store = ActivityProgressStore(path)
    progress = ActivityProgress(
        activity_id="guard",
        subject_id="character-a",
        current_count=3,
        status=ActivityStatus.RUNNING,
        period_started_on=datetime(2026, 7, 11, tzinfo=TAIPEI_TIMEZONE).date(),
        started_at=datetime(2026, 7, 11, 20, 0, tzinfo=TAIPEI_TIMEZONE),
    )

    store.save((progress,))
    loaded = store.load()

    assert loaded == (progress,)
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_progress_store_preserves_corruption_and_recovers_empty(tmp_path):
    path = tmp_path / "activity_progress.json"
    path.write_text("{not-json", encoding="utf-8")
    store = ActivityProgressStore(path)

    loaded = store.load()

    assert loaded == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None
    assert store.corrupt_backup.exists()
    assert not path.exists()
