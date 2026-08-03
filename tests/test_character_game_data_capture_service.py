from adapters.windows_background_capture import CaptureSample
from domain.character_game_data import PetTalentPageSnapshot
from domain.character_game_data_store import CharacterGameDataStore
from services.character_game_data_capture_service import (
    CharacterGameDataCaptureService,
    GameDataPageKind,
    GameDataReadStatus,
    RegisteredGameDataWindow,
    VerifiedGameDataPage,
)
from services.character_game_data_update_service import (
    CharacterGameDataUpdateService,
)


_FINGERPRINT = "a" * 64


def _sample() -> CaptureSample:
    return CaptureSample(2, 2, bytes([0, 0, 0, 255] * 4), True)


def _target(
    window_handle: int = 123,
    character_id: str = "char-a",
) -> RegisteredGameDataWindow:
    return RegisteredGameDataWindow(
        window_handle=window_handle,
        character_id=character_id,
        launch_fingerprint=_FINGERPRINT,
    )


def _page(
    logical_page_id: str = "verified-page-a",
    content_signature: str = "content-a",
    observed_text: str = "已確認內容甲",
    page_number: int = 1,
) -> VerifiedGameDataPage:
    return VerifiedGameDataPage(
        page_kind=GameDataPageKind.PET_TALENT,
        logical_page_id=logical_page_id,
        content_signature=content_signature,
        data=PetTalentPageSnapshot(
            page_number,
            observed_text,
            "2026-08-03 12:00",
        ),
    )


class FakeCaptureProvider:
    def __init__(self, sample):
        self.sample = sample
        self.handles = []
        self.actions = []

    def capture(self, window_handle):
        self.handles.append(window_handle)
        return self.sample

    def activate(self, *args, **kwargs):
        self.actions.append(("activate", args, kwargs))
        raise AssertionError("capture service must not activate a window")

    def click(self, *args, **kwargs):
        self.actions.append(("click", args, kwargs))
        raise AssertionError("capture service must not click a game window")

    def send_keys(self, *args, **kwargs):
        self.actions.append(("send_keys", args, kwargs))
        raise AssertionError("capture service must not send game input")


class FakeRecognizer:
    def __init__(self, page):
        self.page = page
        self.samples = []

    def read(self, sample):
        self.samples.append(sample)
        return self.page


class SequenceRecognizer:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.samples = []

    def read(self, sample):
        self.samples.append(sample)
        return next(self.pages)


def _service(tmp_path, provider, recognizer, resolver=None):
    return CharacterGameDataCaptureService(
        provider,
        recognizer,
        CharacterGameDataUpdateService(
            CharacterGameDataStore(tmp_path / "character_game_data.json")
        ),
        resolver or (lambda window_handle: _target(window_handle)),
    )


def test_recognizer_page_does_not_contain_character_identity(tmp_path) -> None:
    page = _page()
    provider = FakeCaptureProvider(_sample())
    service = _service(tmp_path, provider, FakeRecognizer(page))

    result = service.read(123)

    assert not hasattr(page, "character_id")
    assert result.status is GameDataReadStatus.UPDATED
    assert provider.handles == [123]
    assert service._update_service.store.load()[0].character_id == "char-a"


def test_capture_rejects_missing_verified_window_binding(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    service = _service(
        tmp_path,
        provider,
        FakeRecognizer(_page()),
        resolver=lambda _window_handle: None,
    )

    result = service.read(123)

    assert result.status is GameDataReadStatus.TARGET_NOT_ELIGIBLE
    assert result.error
    assert provider.handles == []
    assert service._update_service.store.load() == ()


def test_signature_cache_isolated_by_reliable_logical_page_identity(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    first = _page("logical-a", "same-signature", "內容甲")
    second = _page("logical-b", "same-signature", "內容乙", 2)
    service = _service(
        tmp_path,
        provider,
        SequenceRecognizer((first, second, first)),
    )

    assert service.read(123).status is GameDataReadStatus.UPDATED
    assert service.read(123).status is GameDataReadStatus.UPDATED
    assert service.read(123).status is GameDataReadStatus.UNCHANGED


def test_capture_reads_only_capture_provider(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    service = _service(tmp_path, provider, FakeRecognizer(_page()))

    assert service.read(123).status is GameDataReadStatus.UPDATED
    assert provider.handles == [123]
    assert provider.actions == []


def test_unrecognized_page_does_not_create_data(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    service = _service(tmp_path, provider, FakeRecognizer(None))

    result = service.read(123)

    assert result.status is GameDataReadStatus.PAGE_UNRECOGNIZED
    assert service._update_service.store.load() == ()


def test_invalid_recognizer_result_is_not_saved_or_raised(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    service = _service(tmp_path, provider, FakeRecognizer(object()))

    result = service.read(123)

    assert result.status is GameDataReadStatus.PAGE_UNRECOGNIZED
    assert provider.handles == [123]
    assert service._update_service.store.load() == ()


def test_recognizer_error_is_returned_without_writing(tmp_path) -> None:
    class FailingRecognizer:
        def read(self, sample):
            raise RuntimeError("測試辨識錯誤")

    service = _service(
        tmp_path,
        FakeCaptureProvider(_sample()),
        FailingRecognizer(),
    )

    result = service.read(123)

    assert result.status is GameDataReadStatus.PAGE_UNRECOGNIZED
    assert result.error == "測試辨識錯誤"
    assert service._update_service.store.load() == ()
