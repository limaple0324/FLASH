from threading import Event, Lock, Thread

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


class BlockingFirstSaveStore(CharacterGameDataStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.first_save_entered = Event()
        self.release_first_save = Event()
        self.second_save_entered = Event()
        self._save_lock = Lock()
        self._save_count = 0

    def save(self, records) -> None:
        with self._save_lock:
            self._save_count += 1
            save_index = self._save_count
        if save_index == 1:
            self.first_save_entered.set()
            if not self.release_first_save.wait(timeout=1):
                raise AssertionError("first save was not released")
        else:
            self.second_save_entered.set()
        super().save(records)


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


def test_unprovided_sections_are_preserved(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
    initial_talent = _pet_page(1, "第一頁")
    service.update(
        "char-a",
        pet_talent=initial_talent,
        artifact=ArtifactSnapshot(
            page_name="皇冠",
            level=41,
            rune_text=(),
            summary_lines=("目前屬性",),
            updated_at="2026-08-03 12:00",
        ),
    )

    service.update(
        "char-a",
        artifact=ArtifactSnapshot(
            page_name="皇冠",
            level=42,
            rune_text=(),
            summary_lines=("下一級屬性",),
            updated_at="2026-08-03 12:05",
        ),
    )

    record = store.load()[0]
    assert record.pet_talent.pages == (initial_talent,)
    assert record.artifact.level == 42


def test_update_serialization_prevents_concurrent_lost_updates(tmp_path) -> None:
    store = BlockingFirstSaveStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
    errors = []

    def update_talent() -> None:
        try:
            service.update("char-a", pet_talent=_pet_page(1, "第一頁"))
        except Exception as error:
            errors.append(error)

    def update_artifact() -> None:
        try:
            service.update(
                "char-a",
                artifact=ArtifactSnapshot(
                    page_name="皇冠",
                    level=41,
                    rune_text=(),
                    summary_lines=("目前屬性",),
                    updated_at="2026-08-03 12:05",
                ),
            )
        except Exception as error:
            errors.append(error)

    first = Thread(target=update_talent)
    second = Thread(target=update_artifact)
    first.start()
    assert store.first_save_entered.wait(timeout=1)
    second.start()
    assert not store.second_save_entered.wait(timeout=0.1)
    store.release_first_save.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    record = store.load()[0]
    assert record.pet_talent.pages[0].observed_text == "第一頁"
    assert record.artifact.page_name == "皇冠"


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


def test_obsidian_pages_preserve_other_pages_and_replace_only_current_page(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
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
    refreshed_first = _verified_obsidian(
        1,
        opened=1,
        unlit=1,
        updated_at="2026-08-03T12:10:00+08:00",
    )

    service.update("char-a", obsidian=first)
    service.update("char-a", obsidian=fourth)
    result = service.update("char-a", obsidian=refreshed_first)

    assert result.changed_sections == ("黑曜石",)
    pages = store.load()[0].obsidian.pages
    assert pages == (refreshed_first, fourth)


def test_unverified_obsidian_page_does_not_replace_existing_collection(tmp_path) -> None:
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataUpdateService(store)
    first = _verified_obsidian(
        1,
        opened=2,
        unlit=0,
        updated_at="2026-08-03T12:00:00+08:00",
    )
    service.update("char-a", obsidian=first)
    before = store.path.read_bytes()

    try:
        service.update(
            "char-a",
            obsidian=ObsidianSnapshot(
                opened_page=2,
                unlit_nodes=3,
                updated_at="2026-08-03T12:05:00+08:00",
            ),
        )
    except ValueError as error:
        assert "page shape" in str(error)
    else:
        raise AssertionError("unverified obsidian page must be rejected")

    assert store.path.read_bytes() == before
    assert store.load()[0].obsidian.pages == (first,)
