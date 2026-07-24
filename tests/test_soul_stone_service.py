import pytest

from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore
from services.soul_stone_service import SoulStoneService


def test_set_for_character_creates_and_persists_normalized_record(
    tmp_path,
) -> None:
    path = tmp_path / "soul_stones.json"
    service = SoulStoneService(SoulStoneStore(path))

    record = service.set_for_character(
        " char-a ",
        " 本週先保留稀有靈魂石 ",
    )

    assert record == SoulStoneRecord(
        "char-a",
        "本週先保留稀有靈魂石",
    )
    assert service.for_character("char-a") == record
    assert SoulStoneStore(path).load() == (record,)


def test_set_for_character_replaces_only_target_and_keeps_stable_order(
    tmp_path,
) -> None:
    path = tmp_path / "soul_stones.json"
    store = SoulStoneStore(path)
    store.save(
        (
            SoulStoneRecord("char-b", "第二個角色"),
            SoulStoneRecord("char-a", "原紀錄"),
        )
    )
    service = SoulStoneService(store)

    updated = service.set_for_character("char-a", "更新後紀錄")

    assert service.all() == (
        updated,
        SoulStoneRecord("char-b", "第二個角色"),
    )
    assert SoulStoneStore(path).load() == service.all()


def test_clear_for_character_persists_removal_and_missing_is_noop(
    tmp_path,
) -> None:
    path = tmp_path / "soul_stones.json"
    store = SoulStoneStore(path)
    store.save((SoulStoneRecord("char-a", "紀錄"),))
    service = SoulStoneService(store)

    assert service.clear_for_character(" char-a ") is True
    assert service.all() == ()
    assert SoulStoneStore(path).load() == ()

    modified_at = path.stat().st_mtime_ns
    assert service.clear_for_character("missing") is False
    assert path.stat().st_mtime_ns == modified_at


@pytest.mark.parametrize(
    ("character_id", "note", "error"),
    [
        (" ", "紀錄", ValueError),
        ("char-a", " ", ValueError),
        (1, "紀錄", TypeError),
        ("char-a", 1, TypeError),
    ],
)
def test_set_for_character_rejects_invalid_input_without_writing(
    tmp_path,
    character_id,
    note,
    error,
) -> None:
    path = tmp_path / "soul_stones.json"
    service = SoulStoneService(SoulStoneStore(path))

    with pytest.raises(error):
        service.set_for_character(character_id, note)

    assert service.all() == ()
    assert path.exists() is False


def test_failed_save_keeps_previous_in_memory_record(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "soul_stones.json"
    store = SoulStoneStore(path)
    original = SoulStoneRecord("char-a", "原紀錄")
    store.save((original,))
    service = SoulStoneService(store)

    def fail_save(_records) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        service.set_for_character("char-a", "不可套用")
    assert service.for_character("char-a") == original

    with pytest.raises(OSError, match="disk unavailable"):
        service.clear_for_character("char-a")
    assert service.for_character("char-a") == original
