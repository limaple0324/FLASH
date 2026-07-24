from main import (
    _build_registered_card_overlay_runtime,
    build_services,
    create_main_window,
    run,
)
from services.app_context import AppContext
from services.card_preview_selection_service import CardPreviewSelectionService
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


class FakeRuntime:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class FakeWindow:
    def __init__(self) -> None:
        self.protocols = {}

    def title(self, _value) -> None:
        pass

    def geometry(self, _value) -> None:
        pass

    def minsize(self, _width, _height) -> None:
        pass

    def after_idle(self, callback) -> None:
        self.after_idle_callback = callback

    def after(self, _delay, callback):
        self.after_callback = callback
        return "after-id"

    def protocol(self, name, callback) -> None:
        self.protocols[name] = callback

    def destroy(self) -> None:
        pass


class FakeHomeView:
    def __init__(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs

    def build(self) -> None:
        pass

    def refresh_cards(self) -> None:
        pass


def test_missing_catalog_keeps_registered_overlay_disabled(tmp_path) -> None:
    build_services(root=tmp_path)

    assert _build_registered_card_overlay_runtime(object()) is None


def test_explicit_catalog_builds_stopped_registered_coordinator(tmp_path) -> None:
    build_services(root=tmp_path, card_preview_catalog=_catalog())

    runtime = _build_registered_card_overlay_runtime(object())

    assert runtime is not None
    assert runtime.started is False
    assert runtime.active_profile_id is None


def test_main_window_builds_and_manages_registered_overlay(monkeypatch, tmp_path) -> None:
    import main

    build_services(root=tmp_path, card_preview_catalog=_catalog())
    window = FakeWindow()
    runtime = FakeRuntime()
    monkeypatch.setattr(main, "Tk", lambda: window)
    monkeypatch.setattr(main, "HomeView", FakeHomeView)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(
        main,
        "_build_registered_card_overlay_runtime",
        lambda actual_window: runtime if actual_window is window else None,
    )

    created = create_main_window({}, main.AppContext.get(main.PathManager))

    assert created is window
    choices = created._home_view.kwargs["card_preview_choices_provider"]()
    assert [choice.display_name for choice in choices] == ["玩家選定方案"]
    created._home_view.kwargs["on_card_preview_select"]("player-selected")
    assert AppContext.get(CardPreviewSelectionService).snapshot().overlay_enabled is True
    assert runtime.start_calls == 1
    created._home_view.kwargs["on_card_preview_clear"]()
    assert AppContext.get(CardPreviewSelectionService).snapshot().overlay_enabled is False
    window.protocols["WM_DELETE_WINDOW"]()
    assert runtime.stop_calls == 1


def test_main_window_reports_card_preview_failure_without_internal_details(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    build_services(root=tmp_path, card_preview_catalog=_catalog())
    window = FakeWindow()
    shown = []
    monkeypatch.setattr(main, "Tk", lambda: window)
    monkeypatch.setattr(main, "HomeView", FakeHomeView)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(main, "_build_registered_card_overlay_runtime", lambda _window: None)
    monkeypatch.setattr(
        main.messagebox,
        "showerror",
        lambda title, message, parent: shown.append((title, message, parent)),
    )

    created = create_main_window({}, main.AppContext.get(main.PathManager))
    created._home_view.kwargs["on_card_preview_error"](
        "select",
        OSError("private disk path"),
    )

    assert shown == [
        (
            "輔｜提醒卡樣式",
            "無法套用提醒卡樣式，原本設定已保留。\n\n請稍後再試；錯誤已寫入紀錄。",
            window,
        )
    ]


def test_run_forwards_explicit_catalog_into_startup_services(tmp_path) -> None:
    exit_code = run(
        self_check_only=True,
        root=tmp_path,
        card_preview_catalog=_catalog(),
    )

    selection = AppContext.get(CardPreviewSelectionService)
    assert exit_code == 0
    assert selection is not None
    assert selection.snapshot().overlay_enabled is False
