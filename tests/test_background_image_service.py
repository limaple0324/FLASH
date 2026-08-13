from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pillow_heif import from_pillow

from config.config_manager import ConfigManager
from services.background_image_service import (
    BACKGROUND_CARD_CONFIG_KEY,
    BACKGROUND_FILL_CONFIG_KEY,
    BACKGROUND_GLOBAL_CONFIG_KEY,
    BACKGROUND_IMAGE_CONFIG_KEY,
    BACKGROUND_PAGE_CONFIG_KEY,
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
    heif_loader=None,
    error_logger=None,
) -> tuple[BackgroundImageService, ConfigManager]:
    config = ConfigManager(tmp_path / "config" / "settings.json")
    service = BackgroundImageService(
        config,
        tmp_path / "data",
        rawpy_loader=rawpy_loader,
        heif_loader=heif_loader,
        error_logger=error_logger,
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


def test_heif_image_is_decoded_and_original_is_unchanged(tmp_path) -> None:
    source = tmp_path / "手機原圖.heic"
    original_image = Image.new("RGB", (9, 7), "#507090")
    try:
        from_pillow(original_image).save(source, quality=90)
    finally:
        original_image.close()
    original_bytes = source.read_bytes()
    service, _config = _service(tmp_path)

    result = service.select(source)

    assert result.succeeded is True
    assert source.read_bytes() == original_bytes
    assert result.managed_path is not None
    with Image.open(result.managed_path) as converted:
        converted.load()
        assert converted.size == (9, 7)


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
    technical_errors: list[str] = []

    def missing():
        raise ModuleNotFoundError("rawpy")

    service, _config = _service(
        tmp_path,
        rawpy_loader=missing,
        error_logger=technical_errors.append,
    )

    result = service.select(source)

    assert result.succeeded is False
    assert result.message == (
        "相機 RAW 圖片解碼元件目前無法使用，原本背景已保留。"
    )
    assert result.managed_path is None
    assert len(technical_errors) == 1
    assert "階段：載入相機原始圖片解碼元件" in technical_errors[0]
    assert f"來源：{source.resolve()}" in technical_errors[0]
    assert "副檔名：.cr2" in technical_errors[0]
    assert "例外類型：ModuleNotFoundError" in technical_errors[0]
    assert "ModuleNotFoundError: rawpy" in technical_errors[0]


def test_missing_heif_decoder_reports_specific_player_safe_reason(
    tmp_path,
) -> None:
    source = tmp_path / "camera.heic"
    source.write_bytes(b"heif")
    technical_errors: list[str] = []

    def missing():
        raise ModuleNotFoundError("pillow_heif")

    service, _config = _service(
        tmp_path,
        heif_loader=missing,
        error_logger=technical_errors.append,
    )

    result = service.select(source)

    assert result.succeeded is False
    assert result.message == (
        "HEIC／HEIF 圖片解碼元件目前無法使用，原本背景已保留。"
    )
    assert result.managed_path is None
    assert len(technical_errors) == 1
    assert "階段：載入 HEIC／HEIF 圖片解碼元件" in technical_errors[0]
    assert "例外類型：ModuleNotFoundError" in technical_errors[0]
    assert "ModuleNotFoundError: pillow_heif" in technical_errors[0]


def test_raw_decode_failure_keeps_player_message_short_and_logs_details(
    tmp_path,
) -> None:
    source = tmp_path / "損壞月球.CR2"
    source.write_bytes(b"damaged raw")
    technical_errors: list[str] = []

    def fail_decode(_path: str):
        raise ValueError("invalid camera header")

    service, _config = _service(
        tmp_path,
        rawpy_loader=lambda: SimpleNamespace(imread=fail_decode),
        error_logger=technical_errors.append,
    )

    result = service.select(source)

    assert result.succeeded is False
    assert result.message == "選取的檔案不是可解碼的圖片，原本背景已保留。"
    assert result.managed_path is None
    assert len(technical_errors) == 1
    assert "階段：解碼相機原始圖片" in technical_errors[0]
    assert f"來源：{source.resolve()}" in technical_errors[0]
    assert "例外類型：ValueError" in technical_errors[0]
    assert "ValueError: invalid camera header" in technical_errors[0]


def test_publish_failure_keeps_old_background_and_logs_full_details(
    tmp_path,
    monkeypatch,
) -> None:
    existing_source = tmp_path / "existing.png"
    next_source = tmp_path / "next.png"
    Image.new("RGB", (4, 4), "navy").save(existing_source)
    Image.new("RGB", (6, 5), "gold").save(next_source)
    technical_errors: list[str] = []
    service, _config = _service(
        tmp_path,
        error_logger=technical_errors.append,
    )
    existing = service.select(existing_source).managed_path

    def fail_publish(_image):
        raise OSError("storage unavailable")

    monkeypatch.setattr(service, "_publish", fail_publish)

    result = service.prepare(next_source)

    assert result.succeeded is False
    assert result.message == "背景圖片無法準備預覽，原本背景已保留。"
    assert result.managed_path == existing
    assert existing is not None and existing.is_file()
    assert len(technical_errors) == 1
    assert "階段：建立可顯示背景副本" in technical_errors[0]
    assert f"來源：{next_source.resolve()}" in technical_errors[0]
    assert "例外類型：OSError" in technical_errors[0]
    assert "OSError: storage unavailable" in technical_errors[0]


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


def test_prepare_is_preview_only_until_player_saves_scope(tmp_path) -> None:
    source = tmp_path / "preview.png"
    Image.new("RGB", (30, 20), "purple").save(source)
    service, config = _service(tmp_path)

    preview = service.prepare(source)

    assert preview.succeeded is True
    assert preview.managed_path is not None
    assert service.current_background() is None
    assert config.get(BACKGROUND_GLOBAL_CONFIG_KEY) is None

    saved = service.commit_prepared(
        preview.managed_path,
        apply_all=False,
        pages=("home", "sync"),
    )

    assert saved.succeeded is True
    assert service.current_background() is None
    assert service.current_background("home") == preview.managed_path
    assert service.current_background("sync") == preview.managed_path
    assert config.get(BACKGROUND_PAGE_CONFIG_KEY) == {
        "home": str(preview.managed_path),
        "sync": str(preview.managed_path),
    }


def test_cancelled_prepare_keeps_original_background_and_no_half_file(
    tmp_path,
) -> None:
    service, _config = _service(tmp_path)
    existing_source = tmp_path / "existing.png"
    next_source = tmp_path / "next.png"
    Image.new("RGB", (12, 12), "navy").save(existing_source)
    Image.new("RGB", (18, 18), "gold").save(next_source)
    existing = service.select(existing_source).managed_path

    result = service.prepare(next_source, cancelled=lambda: True)

    assert result.succeeded is False
    assert "已取消" in result.message
    assert service.current_background() == existing
    assert tuple((tmp_path / "data" / "backgrounds").glob("*.tmp")) == ()
    assert tuple((tmp_path / "data" / "backgrounds").glob("background-*.png")) == (
        existing,
    )


def test_global_and_page_backgrounds_follow_confirmed_fallback_rules(
    tmp_path,
) -> None:
    service, _config = _service(tmp_path)
    global_source = tmp_path / "global.png"
    page_source = tmp_path / "page.png"
    Image.new("RGB", (10, 10), "green").save(global_source)
    Image.new("RGB", (11, 11), "orange").save(page_source)
    global_path = service.select(global_source).managed_path
    preview = service.prepare(page_source)
    service.commit_prepared(
        preview.managed_path,
        apply_all=False,
        pages=("characters",),
    )

    assert service.current_background("home") == global_path
    assert service.current_background("characters") == preview.managed_path

    service.clear_page("characters")
    assert service.current_background("characters") == global_path


def test_clear_all_removes_every_assignment_and_managed_copy(tmp_path) -> None:
    service, _config = _service(tmp_path)
    global_source = tmp_path / "global.png"
    page_source = tmp_path / "page.png"
    Image.new("RGB", (20, 20), "#112233").save(global_source)
    Image.new("RGB", (30, 30), "#445566").save(page_source)
    global_result = service.select(global_source)
    page_preview = service.prepare(page_source)
    page_result = service.commit_prepared(
        page_preview.managed_path,
        apply_all=False,
        pages=("characters",),
    )

    result = service.clear_all()

    assert global_result.succeeded is True
    assert page_result.succeeded is True
    assert result.succeeded is True
    assert service.current_background() is None
    assert service.current_background("characters") is None
    assert global_result.managed_path is not None
    assert page_result.managed_path is not None
    assert global_result.managed_path.exists() is False
    assert page_result.managed_path.exists() is False


def test_fill_color_and_three_region_opacities_are_saved_together(
    tmp_path,
) -> None:
    service, config = _service(tmp_path)

    settings = service.update_display_settings(
        fill_color="#123abc",
        sidebar_opacity=10,
        panel_opacity=50,
        role_row_opacity=100,
    )

    assert settings.fill_color == "#123ABC"
    assert settings.sidebar_opacity == 10
    assert settings.panel_opacity == 50
    assert settings.role_row_opacity == 100
    assert config.get(BACKGROUND_FILL_CONFIG_KEY) == "#123ABC"


def test_export_and_import_restores_images_scope_and_display_settings(
    tmp_path,
) -> None:
    source_service, _source_config = _service(tmp_path / "source")
    global_source = tmp_path / "global.png"
    page_source = tmp_path / "page.png"
    card_source = tmp_path / "card.png"
    Image.new("RGB", (22, 14), "#123456").save(global_source)
    Image.new("RGB", (18, 30), "#ABCDEF").save(page_source)
    Image.new("RGB", (9, 7), "#654321").save(card_source)
    source_service.select(global_source)
    page_preview = source_service.prepare(page_source)
    source_service.commit_prepared(
        page_preview.managed_path,
        apply_all=False,
        pages=("characters", "records"),
    )
    card_preview = source_service.prepare(card_source)
    source_service.commit_prepared_to_card(
        card_preview.managed_path,
        "sync.reconnect",
    )
    source_service.update_display_settings(
        fill_color="#102030",
        sidebar_opacity=12,
        panel_opacity=34,
        role_row_opacity=56,
    )
    backup = source_service.export_settings(tmp_path / "backgrounds.zip")
    restored_service, _restored_config = _service(tmp_path / "restored")

    result = restored_service.import_settings(backup)
    settings = restored_service.settings()
    page_paths = dict(settings.page_paths)
    card_paths = dict(settings.card_paths)

    assert result.succeeded is True
    assert settings.global_path is not None
    assert page_paths["characters"] is not None
    assert page_paths["records"] == page_paths["characters"]
    assert card_paths["sync.reconnect"] is not None
    assert settings.fill_color == "#102030"
    assert settings.sidebar_opacity == 12
    assert settings.panel_opacity == 34
    assert settings.role_row_opacity == 56
    with Image.open(settings.global_path) as restored_global:
        assert restored_global.size == (22, 14)
    with Image.open(page_paths["characters"]) as restored_page:
        assert restored_page.size == (18, 30)
    with Image.open(card_paths["sync.reconnect"]) as restored_card:
        assert restored_card.size == (9, 7)


def test_card_background_replacement_and_clear_are_scoped(tmp_path) -> None:
    service, config = _service(tmp_path)
    first_source = tmp_path / "first.png"
    second_source = tmp_path / "second.png"
    Image.new("RGB", (6, 5), "#112233").save(first_source)
    Image.new("RGB", (8, 7), "#445566").save(second_source)
    first = service.prepare(first_source)
    second = service.prepare(second_source)

    first_result = service.commit_prepared_to_card(
        first.managed_path,
        "home.workspace",
    )
    second_result = service.commit_prepared_to_card(
        second.managed_path,
        "home.target",
    )

    assert first_result.succeeded is True
    assert second_result.succeeded is True
    assert service.current_card_background("home.workspace") == first.managed_path
    assert service.current_card_background("home.target") == second.managed_path
    assert config.get(BACKGROUND_CARD_CONFIG_KEY) == {
        "home.workspace": str(first.managed_path),
        "home.target": str(second.managed_path),
    }

    result = service.clear_card("home.workspace")

    assert result.succeeded is True
    assert service.current_card_background("home.workspace") is None
    assert service.current_card_background("home.target") == second.managed_path
    assert first.managed_path is not None
    assert not first.managed_path.exists()


def test_invalid_import_keeps_existing_background(tmp_path) -> None:
    service, _config = _service(tmp_path)
    existing_source = tmp_path / "existing.png"
    Image.new("RGB", (10, 10), "red").save(existing_source)
    existing = service.select(existing_source).managed_path
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")

    result = service.import_settings(invalid)

    assert result.succeeded is False
    assert service.current_background() == existing
    assert existing is not None and existing.exists()


def test_raw_decoder_is_declared_and_collected_for_windows_package() -> None:
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    specification = Path("FLASH.spec").read_text(encoding="utf-8")

    assert "rawpy>=0.25.0" in requirements
    assert "pillow-heif>=1.5.0" in requirements
    assert "collect_dynamic_libs('rawpy')" in specification
    assert "collect_dynamic_libs('pillow_heif')" in specification
    assert "collect_submodules('pillow_heif')" in specification
    assert "'rawpy'" in specification
    assert "'rawpy._rawpy'" in specification
