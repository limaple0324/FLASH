import json

import pytest

from domain.life_soul import LifeSoulRecord
from domain.life_soul_store import LifeSoulStore


def test_record_is_immutable_and_normalizes_confirmed_text_fields() -> None:
    record = LifeSoulRecord(" char-a ", " 稀有命魂待確認用途 ")

    assert record.character_id == "char-a"
    assert record.note == "稀有命魂待確認用途"
    assert record.to_dict() == {
        "character_id": "char-a",
        "note": "稀有命魂待確認用途",
    }
    with pytest.raises(AttributeError):
        record.note = "不可修改"


@pytest.mark.parametrize(
    ("character_id", "note", "error"),
    [
        ("", "紀錄", ValueError),
        ("char-a", " ", ValueError),
        (1, "紀錄", TypeError),
        ("char-a", 1, TypeError),
    ],
)
def test_record_rejects_missing_or_unconfirmed_field_types(
    character_id, note, error
) -> None:
    with pytest.raises(error):
        LifeSoulRecord(character_id, note)


def test_missing_store_returns_empty_without_creating_defaults(tmp_path) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)

    assert store.load() == ()
    assert path.exists() is False
    assert store.recovered_from_corruption is False


def test_store_round_trips_independent_character_notes_atomically(tmp_path) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)
    records = (
        LifeSoulRecord("char-a", "第一個角色的命魂紀錄"),
        LifeSoulRecord("char-b", "第二個角色的命魂紀錄"),
    )

    store.save(records)

    assert store.load() == records
    assert path.with_suffix(".json.tmp").exists() is False
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "records": [
            {
                "character_id": "char-a",
                "note": "第一個角色的命魂紀錄",
            },
            {
                "character_id": "char-b",
                "note": "第二個角色的命魂紀錄",
            },
        ],
    }


def test_store_rejects_duplicate_character_identity_before_writing(tmp_path) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)

    with pytest.raises(ValueError, match="Duplicate life soul character"):
        store.save(
            (
                LifeSoulRecord("same", "第一筆"),
                LifeSoulRecord("same", "第二筆"),
            )
        )

    assert path.exists() is False


def test_store_rejects_non_record_values_before_writing(tmp_path) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)

    with pytest.raises(TypeError, match="LifeSoulRecord"):
        store.save(({"character_id": "char-a", "note": "紀錄"},))

    assert path.exists() is False


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "records": []},
        {
            "schema_version": 1,
            "records": [
                {
                    "character_id": "char-a",
                    "note": "紀錄",
                    "level": 10,
                }
            ],
        },
        {
            "schema_version": 1,
            "records": [
                {"character_id": "same", "note": "第一筆"},
                {"character_id": "same", "note": "第二筆"},
            ],
        },
    ],
)
def test_unknown_or_invalid_data_is_isolated_without_guessing(tmp_path, payload) -> None:
    path = tmp_path / "life_souls.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    store = LifeSoulStore(path)

    assert store.load() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup == path.with_suffix(".json.corrupt")
    assert store.corrupt_backup.exists()
    assert path.exists() is False
