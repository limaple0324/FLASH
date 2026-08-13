import json
from datetime import date

from habit.models import ActivityOrderHabitMemory, ActivityOrderObservation
from habit.store import ActivityOrderHabitStore
from main import ACTIVITY_ORDER_HABIT_FILENAME, build_services


def _memory() -> ActivityOrderHabitMemory:
    return ActivityOrderHabitMemory(
        observations=(
            ActivityOrderObservation(
                observed_on=date(2026, 7, 1),
                activity_ids=("hall-of-demons", "magic-soldiers"),
            ),
        ),
        accepted_order=("hall-of-demons", "magic-soldiers"),
        paused=True,
        dismissed_through=date(2026, 7, 1),
    )


def test_store_roundtrip_preserves_the_existing_schema_and_model(tmp_path) -> None:
    path = tmp_path / "activity_order_habit.json"
    store = ActivityOrderHabitStore(path)
    memory = _memory()

    store.save(memory)

    assert store.load() == memory
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "activity_order": memory.to_dict(),
    }


def test_store_loads_an_existing_activity_order_file(tmp_path) -> None:
    path = tmp_path / "activity_order_habit.json"
    memory = _memory()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activity_order": memory.to_dict(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert ActivityOrderHabitStore(path).load() == memory


def test_corrupt_main_file_is_isolated_and_valid_backup_is_restored(tmp_path) -> None:
    path = tmp_path / "activity_order_habit.json"
    store = ActivityOrderHabitStore(path)
    memory = _memory()
    store.save(memory)
    store.backup_path.write_bytes(path.read_bytes())
    path.write_text("not-json", encoding="utf-8")

    restored = ActivityOrderHabitStore(path)

    assert restored.load() == memory
    assert restored.recovered_from_corruption is True
    assert restored.recovered_from_backup is True
    assert restored.corrupt_backup is not None
    assert restored.corrupt_backup.read_text(encoding="utf-8") == "not-json"


def test_build_services_loads_managed_activity_order_habits_once(
    tmp_path,
    monkeypatch,
) -> None:
    loaded_paths = []
    original_load = ActivityOrderHabitStore.load

    def record_load(store):
        loaded_paths.append(store.path)
        return original_load(store)

    monkeypatch.setattr(ActivityOrderHabitStore, "load", record_load)

    build_services(root=tmp_path)

    assert loaded_paths == [
        tmp_path / "data" / ACTIVITY_ORDER_HABIT_FILENAME
    ]
