from domain.character_game_data import (
    CharacterGameData,
    LifeSoul,
    ObsidianSnapshot,
    PetLifeSoulSnapshot,
)
from domain.character_game_data_store import CharacterGameDataStore
from services.character_game_data_view_service import (
    CharacterGameDataView,
    CharacterGameDataViewService,
)


def _record() -> CharacterGameData:
    return CharacterGameData(
        character_id="char-a",
        cultivated_pet_count=2,
        obsidian=ObsidianSnapshot(
            opened_page=4,
            unlit_nodes=7,
            updated_at="2026-07-27 12:30",
        ),
        life_souls=(
            PetLifeSoulSnapshot(
                pet_name="刷刷刷刷",
                souls=(LifeSoul("勇氣", 3, "攻擊 +5"),),
                updated_at="2026-07-27 12:35",
            ),
        ),
    )


def test_character_game_data_round_trip_and_player_summary(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    store.save((_record(),))

    assert store.load() == (_record(),)
    assert CharacterGameDataViewService(store).get("char-a") == (
        CharacterGameDataView(
            pet_talent="尚未安全讀取",
            obsidian=(
                "已開啟至第 4 頁｜尚餘 7 個未點亮節點｜"
                "最後更新 2026-07-27 12:30"
            ),
            life_soul=(
                "已讀取 1／2 隻培養寵物\n"
                "刷刷刷刷｜勇氣｜等級 3｜攻擊 +5｜最後更新 2026-07-27 12:35"
            ),
            artifact="尚未安全讀取",
        )
    )


def test_unknown_character_does_not_invent_game_data(tmp_path) -> None:
    view = CharacterGameDataViewService(
        CharacterGameDataStore(tmp_path / "character_game_data.json")
    ).get("missing")

    assert view == CharacterGameDataView(
        pet_talent="尚未安全讀取",
        obsidian="尚未安全讀取",
        life_soul="尚未安全讀取",
        artifact="尚未安全讀取",
    )
