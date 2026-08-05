from pathlib import Path

from PIL import Image

from adapters.obsidian_page_recognizer import ObsidianPageRecognizer
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
from services.character_game_data_view_service import CharacterGameDataViewService


_FINGERPRINT = "a" * 64
_OBSIDIAN_REFERENCE_DIR = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "game_data_reference"
    / "obsidian"
)


def _sample() -> CaptureSample:
    return CaptureSample(2, 2, bytes([0, 0, 0, 255] * 4), True)


def _obsidian_sample(page: int = 1) -> CaptureSample:
    with Image.open(_OBSIDIAN_REFERENCE_DIR / f"page_{page:02d}.png") as image:
        rgba = image.convert("RGBA")
        return CaptureSample(
            rgba.width,
            rgba.height,
            rgba.tobytes("raw", "BGRA"),
            True,
        )


def _full_window_obsidian_sample() -> CaptureSample:
    with Image.open(_OBSIDIAN_REFERENCE_DIR / "full_window_page_10.png") as image:
        rgba = image.convert("RGBA")
        return CaptureSample(
            rgba.width,
            rgba.height,
            rgba.tobytes("raw", "BGRA"),
            True,
        )


def _target(
    window_handle: int = 123,
    character_id: str = "char-a",
    process_id: int = 456,
    rect: tuple[int, int, int, int] = (1, 2, 500, 600),
    thread_id: int = 789,
    window_class: str = "ShockwaveFlash",
    process_lifecycle_token: int = 987654321,
) -> RegisteredGameDataWindow:
    return RegisteredGameDataWindow(
        window_handle=window_handle,
        character_id=character_id,
        launch_fingerprint=_FINGERPRINT,
        process_id=process_id,
        rect=rect,
        thread_id=thread_id,
        window_class=window_class,
        process_lifecycle_token=process_lifecycle_token,
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


def test_capture_rejects_late_identity_change_before_any_write(tmp_path) -> None:
    provider = FakeCaptureProvider(_sample())
    calls = []

    def resolver(window_handle):
        calls.append(window_handle)
        return _target(
            window_handle,
            "char-a" if len(calls) == 1 else "char-b",
        )

    service = _service(
        tmp_path,
        provider,
        FakeRecognizer(_page()),
        resolver=resolver,
    )

    result = service.read(123)

    assert result.status is GameDataReadStatus.TARGET_NOT_ELIGIBLE
    assert len(calls) == 2
    assert service._update_service.store.load() == ()


def test_capture_rejects_any_late_window_identity_change_before_any_write(tmp_path) -> None:
    for changed in (
        lambda: _target(process_id=457),
        lambda: _target(rect=(2, 2, 500, 600)),
        lambda: _target(thread_id=790),
        lambda: _target(window_class="OtherFlashWindow"),
        lambda: _target(process_lifecycle_token=987654322),
    ):
        calls = []
        service = _service(
            tmp_path,
            FakeCaptureProvider(_sample()),
            FakeRecognizer(_page()),
            resolver=lambda window_handle, change=changed: (
                calls.append(window_handle) or (_target() if len(calls) == 1 else change())
            ),
        )
        result = service.read(123)
        assert result.status is GameDataReadStatus.TARGET_NOT_ELIGIBLE
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


def test_real_full_window_obsidian_updates_store_and_read_only_view(tmp_path) -> None:
    provider = FakeCaptureProvider(_full_window_obsidian_sample())
    store = CharacterGameDataStore(tmp_path / "character_game_data.json")
    service = CharacterGameDataCaptureService(
        provider,
        ObsidianPageRecognizer(reference_dir=_OBSIDIAN_REFERENCE_DIR),
        CharacterGameDataUpdateService(store),
        lambda window_handle: _target(window_handle),
    )

    first = service.read(123)
    second = service.read(123)
    summary = CharacterGameDataViewService(store).get("char-a").obsidian

    assert first.status is GameDataReadStatus.UPDATED
    assert second.status is GameDataReadStatus.UNCHANGED
    assert "尚未安全讀取" not in summary
    assert "第 10 頁" in summary


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
