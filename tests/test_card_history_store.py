import json
from datetime import datetime, timezone

import pytest

from cards.history import CardHistoryRecord
from cards.history_store import CardHistoryStore
from cards.priority import CardPriorityReason


def _record(reason=CardPriorityReason.DISCONNECTION) -> CardHistoryRecord:
    return CardHistoryRecord(
        recorded_at=datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc),
        card_id="guard-disconnection",
        priority_reason=reason,
        group_id="14-windows",
        group_name="14支",
        activity_id="guard",
        activity_name="守紀",
        current_progress="120古｜守紀中斷",
        affected_character_ids=("120-old",),
        next_step="返回競技場繼續守紀",
    )


def test_history_survives_store_recreation_and_preserves_utf8(tmp_path):
    path = tmp_path / "managed-data" / "card-history.json"
    CardHistoryStore(path).save((_record(),))

    loaded = CardHistoryStore(path).load()

    assert loaded == (_record(),)
    assert "守紀中斷" in path.read_text(encoding="utf-8")
    assert not path.with_suffix(".json.tmp").exists()


def test_store_accepts_empty_history(tmp_path):
    path = tmp_path / "card-history.json"
    store = CardHistoryStore(path)

    store.save(())

    assert store.load() == ()


def test_missing_history_file_loads_as_empty_without_recovery(tmp_path):
    store = CardHistoryStore(tmp_path / "card-history.json")

    assert store.load() == ()
    assert store.recovered_from_corruption is False
    assert store.corrupt_backup is None


def test_corrupt_history_is_preserved_and_recovers_empty(tmp_path):
    path = tmp_path / "card-history.json"
    path.write_text("{broken", encoding="utf-8")
    store = CardHistoryStore(path)

    assert store.load() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup == path.with_suffix(".json.corrupt")
    assert store.corrupt_backup.read_text(encoding="utf-8") == "{broken"
    assert not path.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(schema_version=2),
        lambda payload: payload.update(records={}),
        lambda payload: payload["records"][0].update(priority_reason="一般資訊"),
        lambda payload: payload["records"][0].update(recorded_at="2026-07-13T13:00:00"),
        lambda payload: payload["records"][0].update(affected_character_ids="120-old"),
    ],
)
def test_invalid_history_payload_is_quarantined(tmp_path, mutation):
    path = tmp_path / "card-history.json"
    payload = {"schema_version": 1, "records": [_record().to_dict()]}
    mutation(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    store = CardHistoryStore(path)

    assert store.load() == ()
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None


def test_store_rejects_non_history_values_before_writing(tmp_path):
    path = tmp_path / "card-history.json"

    with pytest.raises(TypeError):
        CardHistoryStore(path).save((object(),))

    assert not path.exists()
