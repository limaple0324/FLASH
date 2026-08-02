from domain.character_game_data import (
    ArtifactSnapshot,
    CharacterGameData,
    LifeSoul,
    ObsidianSnapshot,
    PetLifeSoulSnapshot,
    PetTalentPageSnapshot,
    PetTalentSnapshot,
)
from domain.character_game_data_store import CharacterGameDataStore
from services.character_game_data_update_service import (
    CharacterGameDataUpdateService,
)


def _pet_page(number: int, text: str) -> PetTalentPageSnapshot:
    return PetTalentPageSnapshot(
        page_number=number,
        observed_text=text,
        updated_at=f"2026-08-03 10:0{number}",
        content_signature=f"pet-{number}-{text}",
    )


def _soul_page(name: str, page: int) -> PetLifeSoulSnapshot:
    return PetLifeSoulSnapshot(
        pet_name=name,
        pet_identity=f"identity-{name}",
        page_number=page,
        souls=(LifeSoul("命魂名稱", page, "效果文字"),),
        updated_at=f"2026-08-03 11:0{page}",
    )


def test_four_sections_round_trip_and_view_summary(tmp_path) -> None:
    record = CharacterGameData(
        character_id="char-a",
        cultivated_pet_count=1,
        pet_talent=PetTalentSnapshot(
            (_pet_page(1, "第一頁"), _pet_page(2, "第二頁"), _pet_page(3, "第三頁"), _pet_page(4, "第四頁"))
        ),
        obsidian=ObsidianSnapshot(
            opened_page=6,
            unlit_nodes=4,
            updated_at="2026-08-03 12:00",
            stage="50-80",
            opened_nodes=8,
            page_shape_signature="shape-6",
        ),
        life_souls=(_soul_page("寵物甲", 1),),
        artifact=ArtifactSnapshot(
            page_name="皇冠",
            level=41,
            rune_text=("生命+1500", "物攻+187"),
            summary_lines=("目前屬性", "下一級屬性"),
            updated_at="2026-08-03 12:30",
        ),
    )
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    store.save((record,))

    assert store.load() == (record,)


def test_partial_pages_do_not_clear_unread_sections(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
    service.update(
        "char-a",
        cultivated_pet_count=1,
        pet_talent=PetTalentSnapshot((_pet_page(1, "第一頁"),)),
        life_souls=_soul_page("寵物甲", 1),
    )
    first = service.update("char-a", pet_talent=_pet_page(2, "第二頁"))

    assert first.changed is True
    record = store.load()[0]
    assert tuple(page.page_number for page in record.pet_talent.pages) == (1, 2)
    assert len(record.life_souls) == 1


def test_life_soul_second_page_updates_only_that_pet_page(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
    service.update(
        "char-a",
        cultivated_pet_count=1,
        life_souls=(
            _soul_page("寵物甲", 1),
            _soul_page("寵物甲", 2),
        ),
    )
    updated = PetLifeSoulSnapshot(
        pet_name="寵物甲",
        pet_identity="identity-寵物甲",
        page_number=2,
        souls=(LifeSoul("命魂更新", 9, "新效果"),),
        updated_at="2026-08-03 13:00",
    )

    result = service.update("char-a", life_souls=updated)

    assert result.changed_sections == ("命魂",)
    record = store.load()[0]
    assert record.life_souls[0].souls[0].name == "命魂名稱"
    assert record.life_souls[1].souls[0].name == "命魂更新"


def test_repeated_identical_update_does_not_rewrite(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
    page = _pet_page(1, "第一頁")

    service.update("char-a", pet_talent=page)
    before = store.path.read_bytes()
    result = service.update("char-a", pet_talent=page)

    assert result.changed is False
    assert store.path.read_bytes() == before


def test_life_soul_requires_cultivated_count_before_first_read(tmp_path) -> None:
    service = CharacterGameDataUpdateService(
        CharacterGameDataStore(tmp_path / "character_game_data.json")
    )

    try:
        service.update("char-a", life_souls=_soul_page("寵物甲", 1))
    except ValueError as error:
        assert "cultivated_pet_count" in str(error)
    else:
        raise AssertionError("life soul read must require cultivated count")


def test_obsidian_update_requires_stage_and_shape_evidence(tmp_path) -> None:
    service = CharacterGameDataUpdateService(
        CharacterGameDataStore(tmp_path / "character_game_data.json")
    )

    try:
        service.update(
            "char-a",
            obsidian=ObsidianSnapshot(1, 3, "2026-08-03 12:00"),
        )
    except ValueError as error:
        assert "page shape" in str(error)
    else:
        raise AssertionError("unverified obsidian data must not be saved")
