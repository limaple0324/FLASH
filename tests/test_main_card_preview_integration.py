from main import (
    BUILTIN_CARD_PREVIEW_PROFILE_ID,
    CARD_PREVIEW_SELECTION_FILENAME,
    build_services,
)
from services.app_context import AppContext
from services.card_preview_selection_service import (
    CardPreviewSelectionService,
)
from services.card_preview_selection_store import (
    CardPreviewSelectionStore,
)


def test_normal_startup_registers_the_confirmed_card_style(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path)

    service = AppContext.get(CardPreviewSelectionService)
    store = AppContext.get(CardPreviewSelectionStore)
    assert service.snapshot().selected_profile_id == (
        BUILTIN_CARD_PREVIEW_PROFILE_ID
    )
    assert store.path == (
        paths.data_dir() / CARD_PREVIEW_SELECTION_FILENAME
    )


def test_player_disabled_card_overlay_survives_restart(tmp_path) -> None:
    build_services(root=tmp_path)
    AppContext.get(CardPreviewSelectionService).clear()

    build_services(root=tmp_path)

    assert (
        AppContext.get(CardPreviewSelectionService)
        .snapshot()
        .overlay_enabled
        is False
    )
