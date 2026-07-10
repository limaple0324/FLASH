import json

from core.window_registry import WindowHealth, WindowRegistry
from core.window_registry_store import WindowRegistryStore


def test_registry_round_trip_preserves_identity_but_not_stale_handle(tmp_path):
    registry = WindowRegistry()
    created = registry.register_character("160", "160古")
    registry.rename_character("160", "160戰神")
    registry.confirm_window(
        "160",
        handle=321,
        process_id=654,
        window_class="ShockwaveFlash",
        rect=(10, 20, 810, 620),
        health=WindowHealth.READY,
    )

    store = WindowRegistryStore(tmp_path / "window_registry.json")
    store.save(registry)
    restored = store.load().get("160")

    assert restored.character_uuid == created.character_uuid
    assert restored.display_name == "160戰神"
    assert restored.aliases == ("160古",)
    assert restored.handle is None
    assert restored.process_id == 654
    assert restored.window_class == "ShockwaveFlash"
    assert restored.rect == (10, 20, 810, 620)
    assert restored.health is WindowHealth.UNKNOWN
    assert restored.confirmed is False


def test_store_uses_current_schema_version_and_removes_temp_file(tmp_path):
    path = tmp_path / "window_registry.json"
    store = WindowRegistryStore(path)
    registry = WindowRegistry()
    record = registry.register_character("100", "100古")

    store.save(registry)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == WindowRegistry.SCHEMA_VERSION == 2
    assert payload["characters"][0]["character_uuid"] == record.character_uuid
    assert payload["characters"][0]["display_name"] == "100古"
    assert not path.with_suffix(".json.tmp").exists()


def test_v1_registry_migrates_and_is_saved_as_v2(tmp_path):
    path = tmp_path / "window_registry.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "characters": [
                    {
                        "character_id": "120",
                        "display_name": "120古",
                        "handle": 999999,
                        "process_id": 55,
                        "window_class": "Flash",
                        "rect": [0, 0, 800, 600],
                        "health": "ready",
                        "confirmed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = WindowRegistryStore(path)
    registry = store.load()
    migrated = registry.get("120")

    assert migrated.character_uuid
    assert migrated.display_name == "120古"
    assert migrated.handle is None
    assert migrated.health is WindowHealth.UNKNOWN
    assert migrated.confirmed is False

    store.save(registry)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["characters"][0]["character_uuid"] == migrated.character_uuid


def test_corrupt_registry_is_preserved_and_rebuilt_empty(tmp_path):
    path = tmp_path / "window_registry.json"
    path.write_text('{"characters": [', encoding="utf-8")
    store = WindowRegistryStore(path)

    registry = store.load()

    assert registry.all() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None
    assert store.corrupt_backup.read_text(encoding="utf-8") == '{"characters": ['
    assert not path.exists()


def test_unsupported_schema_is_recovered_conservatively(tmp_path):
    path = tmp_path / "window_registry.json"
    path.write_text('{"schema_version": 999, "characters": []}', encoding="utf-8")
    store = WindowRegistryStore(path)

    registry = store.load()

    assert registry.all() == ()
    assert store.recovered_from_corruption is True


def test_invalid_saved_handle_is_never_restored(tmp_path):
    path = tmp_path / "window_registry.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "characters": [
                    {
                        "character_id": "120",
                        "display_name": "120古",
                        "handle": 999999,
                        "process_id": 55,
                        "window_class": "Flash",
                        "rect": [0, 0, 800, 600],
                        "health": "ready",
                        "confirmed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    record = WindowRegistryStore(path).load().get("120")

    assert record.handle is None
    assert record.health is WindowHealth.UNKNOWN
    assert record.confirmed is False
