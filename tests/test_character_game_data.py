from domain.character_game_data import (
    CharacterGameData,
    LifeSoul,
    ObsidianPagesSnapshot,
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
                "已讀取 1／10 頁\n"
                "最高已讀到第 4 頁\n"
                "已讀頁尚餘 7 個未點亮節點\n"
                "第 4 頁｜未亮 7 格｜最後更新 2026-07-27 12:30"
            ),
            life_soul=(
                "已讀取 1／2 隻培養寵物\n"
                "刷刷刷刷｜第 1 頁｜勇氣｜等級 3｜攻擊 +5｜"
                "最後更新 2026-07-27 12:35"
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


def _verified_obsidian(
    page: int,
    *,
    opened: int,
    unlit: int,
    updated_at: str,
) -> ObsidianSnapshot:
    return ObsidianSnapshot(
        opened_page=page,
        opened_nodes=opened,
        unlit_nodes=unlit,
        stage="階段一／完成",
        page_shape_signature=f"shape-{page}",
        updated_at=updated_at,
    )


def test_legacy_single_obsidian_is_normalized_to_one_page_collection() -> None:
    legacy = _verified_obsidian(
        4,
        opened=12,
        unlit=3,
        updated_at="2026-08-03T12:00:00+08:00",
    )
    record = CharacterGameData(character_id="char-a", obsidian=legacy)

    assert isinstance(record.obsidian, ObsidianPagesSnapshot)
    assert record.obsidian.pages == (legacy,)
    assert record.to_dict()["obsidian"] == {"pages": [legacy.to_dict()]}

    restored = CharacterGameData.from_dict(
        {
            "character_id": "char-a",
            "obsidian": legacy.to_dict(),
        }
    )
    assert restored.obsidian == ObsidianPagesSnapshot((legacy,))


def test_obsidian_page_collection_round_trip_orders_pages_and_rejects_duplicates(tmp_path) -> None:
    first = _verified_obsidian(
        1,
        opened=2,
        unlit=0,
        updated_at="2026-08-03T12:00:00+08:00",
    )
    fourth = _verified_obsidian(
        4,
        opened=12,
        unlit=3,
        updated_at="2026-08-03T12:05:00+08:00",
    )
    pages = ObsidianPagesSnapshot((fourth, first))
    record = CharacterGameData(character_id="char-a", obsidian=pages)
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    store.save((record,))

    assert pages.pages == (first, fourth)
    assert store.load() == (record,)

    try:
        ObsidianPagesSnapshot((first, first))
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate obsidian pages must be rejected")


def test_obsidian_summary_counts_only_pages_that_were_read(tmp_path) -> None:
    first = _verified_obsidian(
        1,
        opened=2,
        unlit=0,
        updated_at="2026-08-03T12:00:00+08:00",
    )
    fourth = _verified_obsidian(
        4,
        opened=12,
        unlit=3,
        updated_at="2026-08-03T12:05:00+08:00",
    )
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    store.save(
        (
            CharacterGameData(
                character_id="char-a",
                obsidian=ObsidianPagesSnapshot((first, fourth)),
            ),
        )
    )

    summary = CharacterGameDataViewService(store).get("char-a").obsidian

    assert "已讀取 2／10 頁" in summary
    assert "最高已讀到第 4 頁" in summary
    assert "已讀頁尚餘 3 個未點亮節點" in summary
    assert "第 1 頁｜階段一／完成｜已亮 2 格｜未亮 0 格" in summary
    assert "第 4 頁｜階段一／完成｜已亮 12 格｜未亮 3 格" in summary
