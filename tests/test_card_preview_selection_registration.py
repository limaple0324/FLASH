from main import CARD_PREVIEW_SELECTION_FILENAME, build_services
from services.app_context import AppContext
from services.card_preview_selection_service import CardPreviewSelectionService
from services.card_preview_selection_store import CardPreviewSelectionStore
from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _catalog() -> CardPreviewCatalog:
    profile = CardPreviewProfile(
        profile_id="player-selected",
        display_name="玩家選定方案",
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
    return CardPreviewCatalog((profile,))


def test_startup_registers_selection_inside_managed_data_when_catalog_is_explicit(
    tmp_path,
) -> None:
    paths, _logger = build_services(
        root=tmp_path,
        card_preview_catalog=_catalog(),
    )

    store = AppContext.get(CardPreviewSelectionStore)
    service = AppContext.get(CardPreviewSelectionService)

    assert store.path == paths.data_dir() / CARD_PREVIEW_SELECTION_FILENAME
    assert service.snapshot().overlay_enabled is False
    assert not store.path.exists()


def test_startup_reloads_only_an_explicit_saved_selection(tmp_path) -> None:
    path = tmp_path / "data" / CARD_PREVIEW_SELECTION_FILENAME
    CardPreviewSelectionStore(path).save("player-selected")

    build_services(root=tmp_path, card_preview_catalog=_catalog())

    service = AppContext.get(CardPreviewSelectionService)
    assert service.snapshot().selected_profile_id == "player-selected"
    assert service.snapshot().overlay_enabled is True


def test_startup_keeps_unavailable_saved_profile_disabled(tmp_path) -> None:
    path = tmp_path / "data" / CARD_PREVIEW_SELECTION_FILENAME
    CardPreviewSelectionStore(path).save("retired-profile")

    paths, _logger = build_services(root=tmp_path, card_preview_catalog=_catalog())

    service = AppContext.get(CardPreviewSelectionService)
    assert service.snapshot().overlay_enabled is False
    assert service.unavailable_stored_profile_id == "retired-profile"
    assert "unavailable profile" in paths.log_file("flash.log").read_text(
        encoding="utf-8"
    )


def test_startup_without_confirmed_catalog_does_not_register_or_create_selection(
    tmp_path,
) -> None:
    paths, _logger = build_services(root=tmp_path)

    assert AppContext.get(CardPreviewSelectionStore) is None
    assert AppContext.get(CardPreviewSelectionService) is None
    assert not (paths.data_dir() / CARD_PREVIEW_SELECTION_FILENAME).exists()
