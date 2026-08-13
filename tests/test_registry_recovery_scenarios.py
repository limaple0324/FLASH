import json

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore


def test_missing_primary_recovers_from_valid_backup(tmp_path):
    path = tmp_path / "window_registry.json"
    backup_path = path.with_suffix(path.suffix + ".bak")
    registry = WindowRegistry()
    registry.register_character("160", "160古")
    backup_payload = json.dumps(registry.to_dict(), ensure_ascii=False)
    backup_path.write_text(backup_payload, encoding="utf-8")

    store = WindowRegistryStore(path)
    restored = store.load()

    assert [record.character_id for record in restored.all()] == ["160"]
    assert backup_path.read_text(encoding="utf-8") == backup_payload
    assert store.recovered_from_corruption is False
    assert store.corrupt_backup is None


def test_repeated_backup_recovery_is_idempotent(tmp_path):
    path = tmp_path / "window_registry.json"
    backup_path = path.with_suffix(path.suffix + ".bak")
    registry = WindowRegistry()
    registry.register_character("100", "100古")
    registry.register_character("120", "120古")
    backup_payload = json.dumps(registry.to_dict(), ensure_ascii=False)
    backup_path.write_text(backup_payload, encoding="utf-8")

    store = WindowRegistryStore(path)
    first = store.load().to_dict()
    second = store.load().to_dict()
    third = store.load().to_dict()

    assert first == second == third
    assert [item["character_id"] for item in first["characters"]] == ["100", "120"]
    assert backup_path.read_text(encoding="utf-8") == backup_payload
    assert store.recovered_from_corruption is False


def test_corrupt_primary_and_corrupt_backup_rebuilds_empty_safely(tmp_path):
    path = tmp_path / "window_registry.json"
    backup_path = path.with_suffix(path.suffix + ".bak")
    corrupt_payload = '{"characters": ['
    path.write_text(corrupt_payload, encoding="utf-8")
    backup_path.write_text("not-json", encoding="utf-8")

    store = WindowRegistryStore(path)
    restored = store.load()

    assert restored.all() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None
    assert store.corrupt_backup.exists()
    assert store.corrupt_backup.read_text(encoding="utf-8") == corrupt_payload
    assert backup_path.read_text(encoding="utf-8") == "not-json"


def test_recovery_from_corrupt_primary_remains_stable_on_next_load(tmp_path):
    path = tmp_path / "window_registry.json"
    store = WindowRegistryStore(path)
    registry = WindowRegistry()
    registry.register_character("100", "100古")
    store.save(registry)
    registry.register_character("120", "120古")
    store.save(registry)
    corrupt_payload = '{"characters": ['
    path.write_text(corrupt_payload, encoding="utf-8")

    first = store.load().to_dict()
    corrupt_backup = store.corrupt_backup
    assert store.recovered_from_corruption is True
    assert corrupt_backup is not None
    assert corrupt_backup.read_text(encoding="utf-8") == corrupt_payload
    second = store.load().to_dict()

    assert first == second
    assert [item["character_id"] for item in second["characters"]] == ["100"]
    assert store.recovered_from_corruption is False
