from pathlib import Path

import pytest
from PIL import Image

from adapters.windows_background_capture import CaptureSample
from adapters.windows_role_id_ocr import (
    WindowsRoleIdOcrReader,
    role_id_ocr_image,
)
from services.role_id_template_service import (
    ROLE_ID_REGION,
    ROLE_ID_REFERENCE_SIZE,
    RoleIdTemplateService,
    clean_role_id_text,
    role_id_region_sample,
)


class CaptureBackend:
    def __init__(self, sample):
        self.sample = sample
        self.calls = []

    def capture(self, handle):
        self.calls.append(handle)
        return self.sample


class OcrReader:
    def __init__(self, value: str):
        self.value = value
        self.samples = []

    def read(self, sample):
        self.samples.append(sample)
        return self.value


def sample_with_name_pixels():
    width, height = ROLE_ID_REFERENCE_SIZE
    pixels = bytearray(b"\x00\x00\x00\xff" * (width * height))
    left, top, right, bottom = ROLE_ID_REGION
    for y in range(top, bottom):
        for x in range(left, left + 120):
            offset = (y * width + x) * 4
            pixels[offset : offset + 4] = b"\xff\xff\xff\xff"
        # Bright but coloured pixels model the envelope icon which must not
        # extend the OCR glyph area after the wider name crop.
        for x in range(left + 130, right):
            offset = (y * width + x) * 4
            pixels[offset : offset + 4] = b"\x00\xcc\xff\xff"
    return CaptureSample(width, height, bytes(pixels), True)


def test_role_id_text_cleanup_keeps_supported_game_name_characters():
    assert clean_role_id_text(" 120 嗚の百二古武！ ") == "120嗚の百二古武"
    assert clean_role_id_text("嘻の百级古武") == "嘻の百級古武"
    assert clean_role_id_text("Mae嶽") == "Mae嶽"
    assert clean_role_id_text("這是?測試") == "這是 測試"


def test_role_id_text_cleanup_rejects_shortcut_and_ellipsized_text():
    assert clean_role_id_text("70獵.lnk") == ""
    assert clean_role_id_text("C:\\遊戲\\70獵.lnk") == ""
    assert clean_role_id_text("這是測…") == ""


def test_role_id_ocr_image_uses_only_the_game_name_area():
    sample = role_id_region_sample(sample_with_name_pixels())
    assert sample is not None
    image = role_id_ocr_image(sample)

    assert image is not None
    # The complete 120-pixel name is kept while the coloured icon is absent.
    assert image.width >= 130 * 10
    assert image.width < (ROLE_ID_REGION[2] - ROLE_ID_REGION[0]) * 10
    assert image.height == ((ROLE_ID_REGION[3] - ROLE_ID_REGION[1]) + 10) * 10
    assert image.getpixel((0, 0)) == (0, 0, 0)


class LocalReader:
    def __init__(self, result):
        self.result = result
        self.images = []

    def __call__(self, image):
        self.images.append(image)
        return self.result, 0.0


def test_local_reader_keeps_only_text_returned_from_game_pixels():
    engine = LocalReader([([0, 0, 1, 1], "Mae", 0.99)])
    reader = WindowsRoleIdOcrReader(engine=engine)

    assert reader.read(role_id_region_sample(sample_with_name_pixels())) == "Mae"
    assert len(engine.images) == 1


def test_saved_real_game_window_reads_the_complete_role_name():
    reference = (
        Path("assets")
        / "game_data_reference"
        / "role_id"
        / "full_window_1347x933.png"
    )
    with Image.open(reference) as image:
        rgba = image.convert("RGBA")
        sample = CaptureSample(
            rgba.width,
            rgba.height,
            rgba.tobytes("raw", "BGRA"),
            True,
        )
    reader = WindowsRoleIdOcrReader()
    if reader._reader() is None:
        pytest.skip("本機未安裝封裝時會包含的角色名稱辨識元件。")
    backend = CaptureBackend(sample)
    result = RoleIdTemplateService(
        capture_provider=backend,
        ocr_reader=reader,
    ).read(123)

    assert result.success is True
    assert result.role_id == "嘻の百級古武"
    assert backend.calls == [123, 123]


def test_read_uses_current_game_text_not_a_shortcut_or_saved_template():
    sample = sample_with_name_pixels()
    backend = CaptureBackend(sample)
    reader = OcrReader("嗚の百二古武")
    service = RoleIdTemplateService(
        capture_provider=backend,
        ocr_reader=reader,
    )

    result = service.read(123, entry_id="70獵")

    assert result.success is True
    assert result.role_id == "嗚の百二古武"
    assert backend.calls == [123, 123]
    assert len(reader.samples) == 2


def test_calibration_reads_current_game_text_and_ignores_entry_identity():
    reader = OcrReader("遊戲角色")
    service = RoleIdTemplateService(
        capture_provider=CaptureBackend(sample_with_name_pixels()),
        ocr_reader=reader,
    )

    result = service.calibrate(123, entry_id="捷徑檔名")

    assert result.success is True
    assert result.role_id == "遊戲角色"


def test_failed_read_never_returns_or_overwrites_a_fallback_name():
    service = RoleIdTemplateService(
        capture_provider=CaptureBackend(sample_with_name_pixels()),
        ocr_reader=OcrReader(""),
    )

    result = service.read(123, entry_id="70獵")

    assert result.success is False
    assert result.role_id == ""
    assert "保持不變" in result.message


def test_partially_read_game_text_is_saved_without_guessing_missing_characters():
    service = RoleIdTemplateService(
        capture_provider=CaptureBackend(sample_with_name_pixels()),
        ocr_reader=OcrReader("這是測試"),
    )

    result = service.read(123, entry_id="70獵")

    assert result.success is True
    assert result.role_id == "這是測試"


def test_automatic_read_never_overwrites_an_existing_role_name():
    backend = CaptureBackend(sample_with_name_pixels())
    reader = OcrReader("Mae嶽")
    service = RoleIdTemplateService(
        capture_provider=backend,
        ocr_reader=reader,
    )

    result = service.read_if_missing(
        123,
        existing_role_id="既有完整名稱",
    )

    assert result.success is False
    assert backend.calls == []
    assert reader.samples == []


def test_automatic_read_rejects_an_ellipsized_game_name():
    service = RoleIdTemplateService(
        capture_provider=CaptureBackend(sample_with_name_pixels()),
        ocr_reader=OcrReader("這是測…"),
    )

    result = service.read_if_missing(123, existing_role_id="")

    assert result.success is False
    assert result.role_id == ""
