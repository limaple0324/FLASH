import pytest

from domain.life_soul import LifeSoulRecord
from domain.life_soul_store import LifeSoulStore
from services.life_soul_service import LifeSoulService


def test_set_for_character_creates_and_persists_normalized_record(
    tmp_path,
) -> None:
    path = tmp_path / "life_souls.json"
    service = LifeSoulService(LifeSoulStore(path))

    record = service.set_for_character(
        " char-a ",
        " 本週先保留稀有命魂 ",
    )

    assert record == LifeSoulRecord(
        "char-a",
        "本週先保留稀有命魂",
    )
    assert service.for_character("char-a") == record
    assert LifeSoulStore(path).load() == (record,)


def test_set_for_character_replaces_only_target_and_keeps_stable_order(
    tmp_path,
) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)
    store.save(
        (
            LifeSoulRecord("char-b", "第二個角色"),
            LifeSoulRecord("char-a", "原紀錄"),
        )
    )
    service = LifeSoulService(store)

    updated = service.set_for_character("char-a", "更新後紀錄")

    assert service.all() == (
        updated,
        LifeSoulRecord("char-b", "第二個角色"),
    )
    assert LifeSoulStore(path).load() == service.all()


def test_clear_for_character_persists_removal_and_missing_is_noop(
    tmp_path,
) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)
    store.save((LifeSoulRecord("char-a", "紀錄"),))
    service = LifeSoulService(store)

    assert service.clear_for_character(" char-a ") is True
    assert service.all() == ()
    assert LifeSoulStore(path).load() == ()

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
    path = tmp_path / "life_souls.json"
    service = LifeSoulService(LifeSoulStore(path))

    with pytest.raises(error):
        service.set_for_character(character_id, note)

    assert service.all() == ()
    assert path.exists() is False


def test_failed_save_keeps_previous_in_memory_record(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "life_souls.json"
    store = LifeSoulStore(path)
    original = LifeSoulRecord("char-a", "原紀錄")
    store.save((original,))
    service = LifeSoulService(store)

    def fail_save(_records) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        service.set_for_character("char-a", "不可套用")
    assert service.for_character("char-a") == original

    with pytest.raises(OSError, match="disk unavailable"):
        service.clear_for_character("char-a")
    assert service.for_character("char-a") == original
