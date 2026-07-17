from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from main import CARD_PREVIEW_SELECTION_FILENAME, build_services
from services.app_context import AppContext
from services.card_preview_selection_service import CardPreviewSelectionService
from services.card_preview_selection_store import CardPreviewSelectionStore
from ui.card_overlay import CardSize
from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile
from ui.tk_card_presenter import TkCardTextSettings


def _catalog() -> CardPreviewCatalog:
    return CardPreviewCatalog(
        (
            CardPreviewProfile(
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
            ),
        )
    )


def _selection_check(paths):
    report = SelfCheck(context=AppContext, paths=paths).run_all()
    return next(
        item for item in report["checks"] if item["name"] == "card_preview_selection"
    )


def test_self_check_reports_overlay_not_configured_without_catalog(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()

    check = _selection_check(paths)

    assert check["passed"] is True
    assert "not configured" in check["message"]


def test_self_check_reports_configured_but_unselected_overlay(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path, card_preview_catalog=_catalog())
    Bootstrap(context=AppContext).start()

    check = _selection_check(paths)

    assert check["passed"] is True
    assert "has not selected" in check["message"]


def test_self_check_reports_selected_overlay_profile(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path, card_preview_catalog=_catalog())
    AppContext.get(CardPreviewSelectionService).select("player-selected")
    Bootstrap(context=AppContext).start()

    check = _selection_check(paths)

    assert check["passed"] is True
    assert "ready" in check["message"]
    assert "player-selected" in check["message"]


def test_self_check_reports_unavailable_saved_profile_as_disabled(tmp_path) -> None:
    path = tmp_path / "data" / CARD_PREVIEW_SELECTION_FILENAME
    CardPreviewSelectionStore(path).save("retired-profile")
    paths, _logger = build_services(root=tmp_path, card_preview_catalog=_catalog())
    Bootstrap(context=AppContext).start()

    check = _selection_check(paths)

    assert check["passed"] is True
    assert "disabled" in check["message"]
    assert "retired-profile" in check["message"]


def test_self_check_rejects_selection_service_without_registered_store(tmp_path) -> None:
    paths, _logger = build_services(root=tmp_path, card_preview_catalog=_catalog())
    Bootstrap(context=AppContext).start()
    AppContext._services.pop(CardPreviewSelectionStore)

    check = _selection_check(paths)

    assert check["passed"] is False
    assert check["message"] == "CardPreviewSelectionStore is not registered."
