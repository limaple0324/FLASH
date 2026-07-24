from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore
from main import SOUL_STONE_FILENAME, build_services
from services.app_context import AppContext
from services.soul_stone_service import SoulStoneService


def test_build_services_loads_soul_stones_from_managed_data(tmp_path) -> None:
    path = tmp_path / "data" / SOUL_STONE_FILENAME
    SoulStoneStore(path).save(
        (
            SoulStoneRecord("char-b", "第二個角色的紀錄"),
            SoulStoneRecord("char-a", "第一個角色的紀錄"),
        )
    )

    paths, _logger = build_services(root=tmp_path)

    store = AppContext.get(SoulStoneStore)
    service = AppContext.get(SoulStoneService)
    assert store.path == paths.data_dir() / SOUL_STONE_FILENAME
    assert service.store is store
    assert service.all() == (
        SoulStoneRecord("char-a", "第一個角色的紀錄"),
        SoulStoneRecord("char-b", "第二個角色的紀錄"),
    )
    assert service.for_character(" char-b ") == SoulStoneRecord(
        "char-b",
        "第二個角色的紀錄",
    )


def test_build_services_keeps_missing_soul_stones_empty_without_defaults(
    tmp_path,
) -> None:
    build_services(root=tmp_path)

    service = AppContext.get(SoulStoneService)
    assert service.all() == ()
    assert service.for_character("char-a") is None
    assert (tmp_path / "data" / SOUL_STONE_FILENAME).exists() is False


def test_build_services_isolates_corrupt_soul_stones(tmp_path) -> None:
    path = tmp_path / "data" / SOUL_STONE_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    build_services(root=tmp_path)

    store = AppContext.get(SoulStoneStore)
    service = AppContext.get(SoulStoneService)
    assert store.recovered_from_corruption is True
    assert service.all() == ()
    assert list(path.parent.glob("soul_stones.json.corrupt*"))


def test_service_rejects_invalid_character_lookup(tmp_path) -> None:
    service = SoulStoneService(
        SoulStoneStore(tmp_path / SOUL_STONE_FILENAME)
    )

    try:
        service.for_character(" ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:
        raise AssertionError("Empty character identity must be rejected.")
