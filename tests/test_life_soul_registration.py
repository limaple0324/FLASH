from domain.life_soul import LifeSoulRecord
from domain.life_soul_store import LifeSoulStore
from main import LIFE_SOUL_FILENAME, build_services
from services.app_context import AppContext
from services.life_soul_service import LifeSoulService


def test_build_services_loads_life_souls_from_managed_data(tmp_path) -> None:
    path = tmp_path / "data" / LIFE_SOUL_FILENAME
    LifeSoulStore(path).save(
        (
            LifeSoulRecord("char-b", "第二個角色的命魂紀錄"),
            LifeSoulRecord("char-a", "第一個角色的命魂紀錄"),
        )
    )

    paths, _logger = build_services(root=tmp_path)

    store = AppContext.get(LifeSoulStore)
    service = AppContext.get(LifeSoulService)
    assert store.path == paths.data_dir() / LIFE_SOUL_FILENAME
    assert service.store is store
    assert service.all() == (
        LifeSoulRecord("char-a", "第一個角色的命魂紀錄"),
        LifeSoulRecord("char-b", "第二個角色的命魂紀錄"),
    )
    assert service.for_character(" char-b ") == LifeSoulRecord(
        "char-b",
        "第二個角色的命魂紀錄",
    )


def test_build_services_keeps_missing_life_souls_empty_without_defaults(
    tmp_path,
) -> None:
    build_services(root=tmp_path)

    service = AppContext.get(LifeSoulService)
    assert service.all() == ()
    assert service.for_character("char-a") is None
    assert (tmp_path / "data" / LIFE_SOUL_FILENAME).exists() is False


def test_build_services_isolates_corrupt_life_souls(tmp_path) -> None:
    path = tmp_path / "data" / LIFE_SOUL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    build_services(root=tmp_path)

    store = AppContext.get(LifeSoulStore)
    service = AppContext.get(LifeSoulService)
    assert store.recovered_from_corruption is True
    assert service.all() == ()
    assert list(path.parent.glob("life_souls.json.corrupt*"))


def test_life_soul_service_rejects_invalid_character_lookup(tmp_path) -> None:
    service = LifeSoulService(
        LifeSoulStore(tmp_path / LIFE_SOUL_FILENAME)
    )

    try:
        service.for_character(" ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:
        raise AssertionError("Empty character identity must be rejected.")
