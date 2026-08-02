from adapters.windows_background_capture import CaptureSample
from domain.character_game_data import (
    ArtifactSnapshot,
    ObsidianSnapshot,
    PetTalentPageSnapshot,
)
from domain.character_game_data_store import CharacterGameDataStore
from services.character_game_data_capture_service import (
    CharacterGameDataCaptureService,
    GameDataPageKind,
    GameDataReadStatus,
    VerifiedGameDataPage,
)
from services.character_game_data_update_service import (
    CharacterGameDataUpdateService,
)


def _sample() -> CaptureSample:
    return CaptureSample(2, 2, bytes([0, 0, 0, 255] * 4), True)


class FakeCaptureProvider:
    def __init__(self, sample):
        self.sample = sample
        self.handles = []

    def capture(self, window_handle):
        self.handles.append(window_handle)
        return self.sample


class FakeRecognizer:
    def __init__(self, page):
        self.page = page
        self.samples = []

    def read(self, sample):
        self.samples.append(sample)
        return self.page


def test_capture_updates_verified_page_without_focus_or_input(tmp_path) -> None:
    page = VerifiedGameDataPage(
        character_id="char-a",
        page_kind=GameDataPageKind.PET_TALENT,
        content_signature="pet-1",
        data=PetTalentPageSnapshot(1, "第一頁", "2026-08-03 12:00"),
    )
    provider = FakeCaptureProvider(_sample())
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        provider,
        FakeRecognizer(page),
        CharacterGameDataUpdateService(store),
        lambda handle, character_id: True,
    )

    result = service.read(123, "char-a")

    assert result.status is GameDataReadStatus.UPDATED
    assert provider.handles == [123]
    assert store.load()[0].pet_talent.pages[0].page_number == 1


def test_same_page_signature_is_not_read_or_written_again(tmp_path) -> None:
    page = VerifiedGameDataPage(
        character_id="char-a",
        page_kind=GameDataPageKind.OBSIDIAN,
        content_signature="obsidian-1",
        data=ObsidianSnapshot(
            1,
            3,
            "2026-08-03 12:00",
            stage="50-80",
            opened_nodes=7,
            page_shape_signature="shape-1",
        ),
    )
    recognizer = FakeRecognizer(page)
    provider = FakeCaptureProvider(_sample())
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        provider,
        recognizer,
        CharacterGameDataUpdateService(store),
        lambda handle, character_id: True,
    )

    assert service.read(123, "char-a").status is GameDataReadStatus.UPDATED
    before = store.path.read_bytes()
    assert service.read(123, "char-a").status is GameDataReadStatus.UNCHANGED
    assert store.path.read_bytes() == before
    assert len(recognizer.samples) == 2


def test_unrecognized_page_does_not_create_data(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        provider,
        FakeRecognizer(None),
        CharacterGameDataUpdateService(store),
        lambda handle, character_id: True,
    )

    result = service.read(123, "char-a")

    assert result.status is GameDataReadStatus.PAGE_UNRECOGNIZED
    assert store.load() == ()


def test_identity_mismatch_does_not_write_other_character(tmp_path) -> None:
    page = VerifiedGameDataPage(
        character_id="other",
        page_kind=GameDataPageKind.ARTIFACT,
        content_signature="artifact-1",
        data=ArtifactSnapshot("皇冠", 41, (), (), "2026-08-03 12:00"),
    )
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        FakeCaptureProvider(_sample()),
        FakeRecognizer(page),
        CharacterGameDataUpdateService(store),
        lambda handle, character_id: True,
    )

    assert service.read(123, "char-a").status is GameDataReadStatus.IDENTITY_MISMATCH
    assert store.load() == ()


def test_target_guard_blocks_unregistered_window_before_capture(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        provider,
        FakeRecognizer(None),
        CharacterGameDataUpdateService(store),
        lambda handle, character_id: False,
    )

    result = service.read(123, "char-a")

    assert result.status is GameDataReadStatus.TARGET_NOT_ELIGIBLE
    assert result.error
    assert provider.handles == []
    assert store.load() == ()


def test_recognizer_error_is_returned_without_writing(tmp_path) -> None:
    class FailingRecognizer:
        def read(self, sample):
            raise RuntimeError("測試辨識錯誤")

    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        FakeCaptureProvider(_sample()),
        FailingRecognizer(),
        CharacterGameDataUpdateService(store),
        lambda handle, character_id: True,
    )

    result = service.read(123, "char-a")

    assert result.status is GameDataReadStatus.PAGE_UNRECOGNIZED
    assert result.error == "測試辨識錯誤"
    assert store.load() == ()
