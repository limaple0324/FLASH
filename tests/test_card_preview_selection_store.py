import json

import pytest

from services.card_preview_selection_service import CardPreviewSelectionService
from services.card_preview_selection_store import CardPreviewSelectionStore
from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _profile(profile_id: str) -> CardPreviewProfile:
    return CardPreviewProfile(
        profile_id=profile_id,
        display_name=f"{profile_id} 預覽",
        card_size=CardSize(360, 120),
        right_margin=16,
        bottom_margin=16,
        gap=12,
        text=TkCardTextSettings(
            background="#102030",
            foreground="#ffffff",
            font_family="Microsoft JhengHei UI",
            font_size=12,
            horizontal_padding=12,
            vertical_padding=8,
            line_spacing=4,
        ),
    )


def _catalog() -> CardPreviewCatalog:
    return CardPreviewCatalog((_profile("compact"), _profile("roomy")))


def test_missing_store_keeps_overlay_disabled_without_creating_a_file(tmp_path) -> None:
    path = tmp_path / "card-preview-selection.json"

    service = CardPreviewSelectionService(_catalog(), CardPreviewSelectionStore(path))

    assert service.snapshot().overlay_enabled is False
    assert not path.exists()


def test_explicit_selection_is_saved_and_restored_after_restart(tmp_path) -> None:
    path = tmp_path / "card-preview-selection.json"
    service = CardPreviewSelectionService(_catalog(), CardPreviewSelectionStore(path))

    service.select("roomy")
    restarted = CardPreviewSelectionService(_catalog(), CardPreviewSelectionStore(path))

    assert restarted.snapshot().selected_profile_id == "roomy"
    assert restarted.snapshot().overlay_enabled is True
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "selected_profile_id": "roomy",
    }
    assert not path.with_suffix(".json.tmp").exists()


def test_clear_removes_persisted_selection_instead_of_creating_a_default(tmp_path) -> None:
    path = tmp_path / "card-preview-selection.json"
    service = CardPreviewSelectionService(_catalog(), CardPreviewSelectionStore(path))
    service.select("compact")

    service.clear()

    assert service.snapshot().overlay_enabled is False
    assert not path.exists()


def test_unknown_stored_profile_stays_disabled_and_is_reported(tmp_path) -> None:
    path = tmp_path / "card-preview-selection.json"
    store = CardPreviewSelectionStore(path)
    store.save("retired-layout")

    service = CardPreviewSelectionService(_catalog(), store)

    assert service.snapshot().overlay_enabled is False
    assert service.unavailable_stored_profile_id == "retired-layout"
    assert path.exists()


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        "[]",
        '{"schema_version": 99, "selected_profile_id": "compact"}',
        '{"schema_version": 1, "selected_profile_id": ""}',
    ),
)
def test_corrupt_selection_is_isolated_and_recovers_disabled(tmp_path, payload) -> None:
    path = tmp_path / "card-preview-selection.json"
    path.write_text(payload, encoding="utf-8")
    store = CardPreviewSelectionStore(path)

    service = CardPreviewSelectionService(_catalog(), store)

    assert service.snapshot().overlay_enabled is False
    assert store.recovered_from_corruption is True
    assert store.corrupt_backup is not None
    assert store.corrupt_backup.read_text(encoding="utf-8") == payload
    assert not path.exists()


def test_failed_disk_save_does_not_change_in_memory_selection(tmp_path, monkeypatch) -> None:
    path = tmp_path / "card-preview-selection.json"
    store = CardPreviewSelectionStore(path)
    service = CardPreviewSelectionService(_catalog(), store)
    service.select("compact")

    def fail_save(_profile_id):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        service.select("roomy")

    assert service.snapshot().selected_profile_id == "compact"
