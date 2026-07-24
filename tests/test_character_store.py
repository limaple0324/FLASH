import json

import pytest

from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore


def _character(
    character_id: str,
    display_name: str,
    level: int,
    importance: CharacterImportance,
) -> Character:
    return Character(character_id, display_name, level, importance)


def test_missing_store_returns_empty_without_creating_defaults(tmp_path) -> None:
    path = tmp_path / "characters.json"
    store = CharacterStore(path)

    assert store.load() == ()
    assert path.exists() is False
    assert store.recovered_from_corruption is False
    assert store.recovered_from_backup is False


def test_store_round_trips_player_character_profiles_atomically(tmp_path) -> None:
    path = tmp_path / "characters.json"
    store = CharacterStore(path)
    characters = (
        _character("char-100", "嘻の百級", 100, CharacterImportance.SECONDARY),
        _character("char-160", "次元主號", 160, CharacterImportance.PRIMARY),
    )

    store.save(characters)

    assert store.load() == characters
    assert path.with_suffix(".json.tmp").exists() is False
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "characters": [
            {
                "character_id": "char-100",
                "display_name": "嘻の百級",
                "level": 100,
                "importance": "次要",
            },
            {
                "character_id": "char-160",
                "display_name": "次元主號",
                "level": 160,
                "importance": "主號",
            },
        ],
    }


def test_store_rejects_duplicate_character_identity_before_writing(tmp_path) -> None:
    path = tmp_path / "characters.json"
    store = CharacterStore(path)
    characters = (
        _character("same", "角色甲", 100, CharacterImportance.PRIMARY),
        _character("same", "角色乙", 120, CharacterImportance.RESERVE),
    )

    with pytest.raises(ValueError, match="Duplicate stable character identity"):
        store.save(characters)

    assert path.exists() is False


def test_corrupt_store_is_preserved_and_does_not_create_guessed_data(tmp_path) -> None:
    path = tmp_path / "characters.json"
    path.write_text("{broken", encoding="utf-8")
    store = CharacterStore(path)

    assert store.load() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup == path.with_suffix(".json.corrupt")
    assert store.corrupt_backup.read_text(encoding="utf-8") == "{broken"
    assert path.exists() is False


def test_corrupt_current_store_recovers_last_valid_backup(tmp_path) -> None:
    path = tmp_path / "characters.json"
    store = CharacterStore(path)
    first = (
        _character("char-a", "角色甲", 100, CharacterImportance.PRIMARY),
    )
    second = (
        _character("char-b", "角色乙", 120, CharacterImportance.SECONDARY),
    )
    store.save(first)
    store.save(second)
    path.write_text("{broken", encoding="utf-8")

    recovered = CharacterStore(path)

    assert recovered.load() == first
    assert recovered.recovered_from_corruption is True
    assert recovered.recovered_from_backup is True


def test_unknown_importance_is_preserved_as_corrupt_input(tmp_path) -> None:
    path = tmp_path / "characters.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "characters": [
                    {
                        "character_id": "char-a",
                        "display_name": "角色甲",
                        "level": 100,
                        "importance": "自行猜測",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = CharacterStore(path)

    assert store.load() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None
