from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from config.config_manager import ConfigManager
from services.background_image_service import (
    BACKGROUND_IMAGE_CONFIG_KEY,
    BackgroundImageService,
)


class _FakeRaw:
    def __init__(self, image: Image.Image) -> None:
        self._image = image

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def postprocess(self, **options):
        assert options == {"use_camera_wb": True, "output_bps": 8}
        return self._image.copy()


def _service(
    tmp_path: Path,
    *,
    rawpy_loader=None,
) -> tuple[BackgroundImageService, ConfigManager]:
    config = ConfigManager(tmp_path / "config" / "settings.json")
    service = BackgroundImageService(
        config,
        tmp_path / "data",
        rawpy_loader=rawpy_loader,
    )
    return service, config


def test_pillow_image_is_decoded_to_managed_png_without_changing_source(
    tmp_path,
) -> None:
    source = tmp_path / "玩家原圖.jpg"
    Image.new("RGB", (12, 8), "#305EA8").save(source, quality=90)
    original_bytes = source.read_bytes()
    service, config = _service(tmp_path)

    result = service.select(source)

    assert result.succeeded is True
    assert result.message == "背景圖片已套用。"
    assert result.managed_path is not None
    assert result.managed_path.parent == tmp_path / "data" / "backgrounds"
    assert result.managed_path.suffix == ".png"
    assert result.managed_path != source
    assert source.read_bytes() == original_bytes
    assert service.current_background() == result.managed_path
    assert config.get(BACKGROUND_IMAGE_CONFIG_KEY) == str(result.managed_path)
    with Image.open(result.managed_path) as converted:
        converted.load()
        assert converted.size == (12, 8)


def test_actual_image_is_accepted_even_when_extension_is_unusual(tmp_path) -> None:
    source = tmp_path / "沒有圖片副檔名.data"
    Image.new("RGBA", (5, 4), (20, 40, 60, 128)).save(
        source,
        format="PNG",
    )
    service, _config = _service(tmp_path)

    result = service.select(source)

    assert result.succeeded is True
    with Image.open(result.managed_path) as converted:
        assert converted.mode == "RGBA"


def test_raw_image_uses_rawpy_and_keeps_original_unchanged(tmp_path) -> None:
    source = tmp_path / "相機原圖.CR2"
    source.write_bytes(b"fake raw bytes kept untouched")
    original_bytes = source.read_bytes()
    calls: list[str] = []

    def imread(path: str):
        calls.append(path)
        return _FakeRaw(Image.new("RGB", (7, 6), "#C8A25A"))

    service, _config = _service(
        tmp_path,
        rawpy_loader=lambda: SimpleNamespace(imread=imread),
    )

    result = service.select(source)

    assert result.succeeded is True
    assert calls == [str(source)]
    assert source.read_bytes() == original_bytes
    with Image.open(result.managed_path) as converted:
        assert converted.size == (7, 6)


def test_non_image_returns_clear_failure_and_retains_existing_background(
    tmp_path,
) -> None:
    service, config = _service(
        tmp_path,
        rawpy_loader=lambda: SimpleNamespace(
            imread=lambda _path: (_ for _ in ()).throw(
                ValueError("not a raw image")
            )
        ),
    )
    existing = tmp_path / "data" / "backgrounds" / "existing.png"
    existing.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "navy").save(existing)
    config.set(BACKGROUND_IMAGE_CONFIG_KEY, str(existing))
    source = tmp_path / "不是圖片.txt"
    source.write_text("plain text", encoding="utf-8")

    result = service.select(source)

    assert result.succeeded is False
    assert result.message == "選取的檔案不是可解碼的圖片，原本背景已保留。"
    assert result.managed_path == existing
    assert service.current_background() == existing
    assert existing.is_file()
    assert config.get(BACKGROUND_IMAGE_CONFIG_KEY) == str(existing)


def test_missing_raw_decoder_reports_specific_player_safe_reason(tmp_path) -> None:
    source = tmp_path / "camera.cr2"
    source.write_bytes(b"raw")

    def missing():
        raise ModuleNotFoundError("rawpy")

    service, _config = _service(tmp_path, rawpy_loader=missing)

    result = service.select(source)

    assert result.succeeded is False
    assert result.message == (
        "相機 RAW 圖片解碼元件目前無法使用，原本背景已保留。"
    )
    assert result.managed_path is None


def test_config_save_failure_removes_candidate_and_retains_old_setting(
    tmp_path,
    monkeypatch,
) -> None:
    service, config = _service(tmp_path)
    existing = tmp_path / "data" / "backgrounds" / "existing.png"
    existing.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "navy").save(existing)
    config.set(BACKGROUND_IMAGE_CONFIG_KEY, str(existing))
    source = tmp_path / "new.png"
    Image.new("RGB", (3, 3), "orange").save(source)
    before = config.config_path.read_text(encoding="utf-8")

    def fail_save() -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(config, "save", fail_save)

    result = service.select(source)

    assert result.succeeded is False
    assert result.message == "背景圖片無法保存，原本背景已保留。"
    assert result.managed_path == existing
    assert config.data[BACKGROUND_IMAGE_CONFIG_KEY] == str(existing)
    assert config.config_path.read_text(encoding="utf-8") == before
    managed_files = set(existing.parent.glob("*"))
    assert managed_files == {existing}


def test_external_configured_path_is_not_exposed_as_managed_background(
    tmp_path,
) -> None:
    service, config = _service(tmp_path)
    external = tmp_path / "outside.png"
    Image.new("RGB", (2, 2), "red").save(external)
    config.set(BACKGROUND_IMAGE_CONFIG_KEY, str(external))

    assert service.current_background() is None


def test_successful_setting_is_saved_as_json_string(tmp_path) -> None:
    source = tmp_path / "background.png"
    Image.new("RGB", (4, 4), "green").save(source)
    service, config = _service(tmp_path)

    result = service.select(source)

    payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    assert payload[BACKGROUND_IMAGE_CONFIG_KEY] == str(result.managed_path)


def test_replacing_background_removes_only_previous_managed_copy(
    tmp_path,
) -> None:
    service, _config = _service(tmp_path)
    first_source = tmp_path / "first.png"
    second_source = tmp_path / "second.png"
    Image.new("RGB", (4, 4), "green").save(first_source)
    Image.new("RGB", (5, 5), "orange").save(second_source)
    first_bytes = first_source.read_bytes()
    second_bytes = second_source.read_bytes()
    first = service.select(first_source)

    second = service.select(second_source)

    assert first.managed_path is not None
    assert not first.managed_path.exists()
    assert second.managed_path is not None
    assert second.managed_path.is_file()
    assert first_source.read_bytes() == first_bytes
    assert second_source.read_bytes() == second_bytes


def test_clear_removes_only_managed_copy_and_keeps_original(tmp_path) -> None:
    source = tmp_path / "玩家原圖.png"
    Image.new("RGB", (4, 4), "green").save(source)
    original_bytes = source.read_bytes()
    service, config = _service(tmp_path)
    selected = service.select(source)

    result = service.clear()

    assert result.succeeded is True
    assert result.message == "背景圖片已清除。"
    assert result.managed_path is None
    assert selected.managed_path is not None
    assert not selected.managed_path.exists()
    assert source.read_bytes() == original_bytes
    assert config.get(BACKGROUND_IMAGE_CONFIG_KEY) == ""
    assert service.current_background() is None


def test_clear_never_deletes_external_configured_path(tmp_path) -> None:
    service, config = _service(tmp_path)
    external = tmp_path / "outside.png"
    Image.new("RGB", (2, 2), "red").save(external)
    original_bytes = external.read_bytes()
    config.set(BACKGROUND_IMAGE_CONFIG_KEY, str(external))

    result = service.clear()

    assert result.succeeded is True
    assert external.read_bytes() == original_bytes
    assert config.get(BACKGROUND_IMAGE_CONFIG_KEY) == ""


def test_clear_config_failure_retains_managed_background(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "background.png"
    Image.new("RGB", (4, 4), "green").save(source)
    service, config = _service(tmp_path)
    selected = service.select(source)
    before = config.config_path.read_text(encoding="utf-8")

    def fail_save() -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(config, "save", fail_save)

    result = service.clear()

    assert result.succeeded is False
    assert result.message == "背景圖片無法清除，原本背景已保留。"
    assert result.managed_path == selected.managed_path
    assert selected.managed_path is not None
    assert selected.managed_path.is_file()
    assert config.config_path.read_text(encoding="utf-8") == before
