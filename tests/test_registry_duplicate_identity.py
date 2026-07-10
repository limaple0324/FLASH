import json

import pytest

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore


def duplicate_payload():
    return {
        "schema_version": 2,
        "characters": [
            {"character_id": "same-id", "display_name": "第一角"},
            {"character_id": "same-id", "display_name": "第二角"},
        ],
    }


def test_registry_rejects_duplicate_character_ids():
    with pytest.raises(ValueError, match="Duplicate character ID"):
        WindowRegistry.from_dict(duplicate_payload())


def test_store_treats_duplicate_character_ids_as_corruption(tmp_path):
    path = tmp_path / "window_registry.json"
    path.write_text(json.dumps(duplicate_payload(), ensure_ascii=False), encoding="utf-8")

    store = WindowRegistryStore(path)
    registry = store.load()

    assert registry.all() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None
    assert not path.exists()
