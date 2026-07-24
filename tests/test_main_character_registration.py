from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore
from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore
from main import build_services
from services.app_context import AppContext
from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)
from services.character_view_service import CharacterViewService, PlayerCharacterView


def test_build_services_loads_character_profiles_into_read_only_view(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character(
        "same-character",
        "目前名稱",
        group="14支",
        role="古",
        note="守紀優先",
    )
    WindowRegistryStore(
        tmp_path / "data" / "window_registry.json"
    ).save(registry)
    CharacterStore(tmp_path / "data" / "characters.json").save(
        (
            Character(
                "same-character",
                "原資料名稱",
                120,
                CharacterImportance.PRIMARY,
            ),
        )
    )
    SoulStoneStore(tmp_path / "data" / "soul_stones.json").save(
        (SoulStoneRecord("same-character", "本週先保留稀有靈魂石"),)
    )

    build_services(root=tmp_path)

    assert AppContext.get(CharacterStore) is not None
    assert AppContext.get(CharacterViewService).all() == (
        PlayerCharacterView(
            display_name="目前名稱",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
        ),
    )
    assert AppContext.get(CharacterDetailViewService).all() == (
        PlayerCharacterDetail(
            display_name="目前名稱",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
            soul_stone="本週先保留稀有靈魂石",
        ),
    )


def test_build_services_keeps_missing_character_profiles_empty(tmp_path) -> None:
    build_services(root=tmp_path)

    store = AppContext.get(CharacterStore)
    view_service = AppContext.get(CharacterViewService)
    assert store is not None
    assert view_service is not None
    assert view_service.all() == ()
    assert (tmp_path / "data" / "characters.json").exists() is False


def test_build_services_isolates_corrupt_character_profiles(tmp_path) -> None:
    path = tmp_path / "data" / "characters.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    build_services(root=tmp_path)

    store = AppContext.get(CharacterStore)
    assert store.recovered_from_corruption is True
    assert store.recovered_from_backup is False
    assert AppContext.get(CharacterViewService).all() == ()
    assert list(path.parent.glob("characters.json.corrupt*"))
