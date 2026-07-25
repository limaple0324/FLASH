import json

from main import (
    CARD_PREVIEW_SELECTION_FILENAME,
    _build_registered_card_overlay_runtime,
    build_services,
    create_main_window,
    run,
)
from core.window_registry import WindowRegistry
from core.sp1_boundaries import ExternalAdapter
from services.app_context import AppContext
from services.card_display_settings_service import CardDisplaySettingsService
from services.card_preview_selection_service import CardPreviewSelectionService
from services.card_preview_selection_store import CardPreviewSelectionStore
from ui.builtin_card_preview_catalog import (
    BUILTIN_CARD_PREVIEW_DISPLAY_NAME,
    BUILTIN_CARD_PREVIEW_PROFILE_ID,
)
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
    def __init__(self, *, start_error: Exception | None = None) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.start_error = start_error
        self.last_error: Exception | None = None

    def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            self.last_error = self.start_error
            raise self.start_error

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

    def refresh_workspace(self) -> None:
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
    shown_info = []
    monkeypatch.setattr(
        main.messagebox,
        "showinfo",
        lambda title, message, parent: shown_info.append((title, message, parent)),
    )
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
    created._home_view.kwargs["on_start"]()
    assert "提醒卡浮層：已選擇樣式，可以顯示。" in shown_info[-1][1]
    created._home_view.kwargs["on_card_preview_clear"]()
    assert AppContext.get(CardPreviewSelectionService).snapshot().overlay_enabled is False
    assert created._home_view.kwargs["card_display_seconds_provider"]() == 30
    created._home_view.kwargs["on_card_display_seconds_update"](75)
    assert created._home_view.kwargs["card_display_seconds_provider"]() == 75
    assert (
        AppContext.get(CardDisplaySettingsService)
        .snapshot()
        .settings.lifetime_seconds
        == 75
    )
    created._home_view.kwargs["on_start"]()
    assert "提醒卡浮層：候選樣式已準備好，尚未選擇。" in shown_info[-1][1]
    assert "提醒卡顯示時間：目前設定為 75 秒。" in shown_info[-1][1]
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


def test_main_window_opens_selected_character_in_single_read_only_detail_window(
    monkeypatch,
    tmp_path,
) -> None:
    import main
    from domain.character import Character, CharacterImportance
    from domain.character_store import CharacterStore
    from core.window_registry_store import WindowRegistryStore

    registry = WindowRegistry()
    registry.register_character(
        "private-character-id",
        "小古",
        group="14支",
        role="古",
        note="守紀優先",
    )
    WindowRegistryStore(tmp_path / "data" / "window_registry.json").save(registry)
    CharacterStore(tmp_path / "data" / "characters.json").save(
        (
            Character(
                "private-character-id",
                "舊名稱",
                120,
                CharacterImportance.PRIMARY,
            ),
        )
    )
    build_services(root=tmp_path)
    window = FakeWindow()
    list_windows = []
    detail_windows = []

    class FakeCharacterListWindow:
        def __init__(self, master):
            self.master = master
            self.is_open = False
            self.open_calls = []
            self.close_calls = 0
            list_windows.append(self)

        def open_choices(self, choices):
            self.is_open = True
            self.open_calls.append(tuple(choices))

        def close(self):
            self.is_open = False
            self.close_calls += 1

    class FakeCharacterDetailWindow:
        def __init__(
            self,
            master,
            *,
            on_edit_soul_stone=None,
            on_edit_life_soul=None,
        ):
            self.master = master
            self.on_edit_soul_stone = on_edit_soul_stone
            self.on_edit_life_soul = on_edit_life_soul
            self.open_calls = []
            self.close_calls = 0
            detail_windows.append(self)

        def open(self, detail):
            self.open_calls.append(detail)

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(main, "Tk", lambda: window)
    monkeypatch.setattr(main, "HomeView", FakeHomeView)
    monkeypatch.setattr(main, "CharacterListWindow", FakeCharacterListWindow)
    monkeypatch.setattr(main, "CharacterDetailWindow", FakeCharacterDetailWindow)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(main, "_build_registered_card_overlay_runtime", lambda _window: None)

    created = create_main_window({}, main.AppContext.get(main.PathManager))
    created._home_view.kwargs["on_show_group_characters"]()

    choices = list_windows[0].open_calls[0]
    details = tuple(choice.detail for choice in choices)
    assert callable(detail_windows[0].on_edit_soul_stone)
    assert callable(detail_windows[0].on_edit_life_soul)
    assert len(details) == 1
    assert details[0].display_name == "小古"
    assert details[0].group == "14支"
    assert details[0].level == 120
    assert details[0].importance == "主號"
    assert details[0].role == "古"
    assert details[0].note == "守紀優先"
    assert "private-character-id" not in repr(details[0])

    choices[0].select()
    assert detail_windows[0].close_calls == 1
    assert detail_windows[0].open_calls == [details[0]]
    assert created._character_list_window is list_windows[0]
    assert created._character_detail_window is detail_windows[0]


def test_main_window_reports_card_display_time_failure_without_internal_details(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    build_services(root=tmp_path)
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
    created._home_view.kwargs["on_card_display_seconds_error"](
        OSError("private disk path"),
    )

    assert shown == [
        (
            "輔｜提醒卡顯示時間",
            (
                "無法儲存提醒卡顯示時間，原本設定已保留。\n\n"
                "請輸入大於 0 的完整秒數後再試；錯誤已寫入紀錄。"
            ),
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
    assert [choice.profile_id for choice in selection.available_choices()] == [
        "player-selected"
    ]


def test_run_uses_builtin_catalog_without_implicitly_enabling_overlay(
    tmp_path,
) -> None:
    exit_code = run(self_check_only=True, root=tmp_path)

    selection = AppContext.get(CardPreviewSelectionService)
    store = AppContext.get(CardPreviewSelectionStore)

    assert exit_code == 0
    assert selection is not None
    assert store is not None
    assert selection.snapshot().overlay_enabled is False
    assert selection.snapshot().selected_profile_id is None
    choices = selection.available_choices()
    assert len(choices) == 1
    choice = choices[0]
    assert choice.profile_id == BUILTIN_CARD_PREVIEW_PROFILE_ID
    assert choice.display_name == BUILTIN_CARD_PREVIEW_DISPLAY_NAME
    assert choice.selected is False
    assert store.path == (
        tmp_path / "data" / CARD_PREVIEW_SELECTION_FILENAME
    )
    assert not store.path.exists()
    assert AppContext.get(ExternalAdapter) is None
    report = json.loads(
        (tmp_path / "data" / "self_check.json").read_text(encoding="utf-8")
    )
    selection_check = next(
        item
        for item in report["self_check"]
        if item["name"] == "card_preview_selection"
    )
    assert selection_check["passed"] is True
    assert "has not selected" in selection_check["message"]
    assert report["target_window"]["code"] == "window.not_configured"
    assert report["target_window"]["safe"] is False
    runtime = _build_registered_card_overlay_runtime(object())
    assert runtime is not None
    assert runtime.started is False
    assert runtime.active_profile_id is None



def test_run_persists_restores_and_clears_builtin_profile(tmp_path) -> None:
    assert run(self_check_only=True, root=tmp_path) == 0
    first_selection = AppContext.get(CardPreviewSelectionService)
    first_store = AppContext.get(CardPreviewSelectionStore)
    first_selection.select(BUILTIN_CARD_PREVIEW_PROFILE_ID)
    assert first_store.path.exists()

    assert run(self_check_only=True, root=tmp_path) == 0
    restored_selection = AppContext.get(CardPreviewSelectionService)
    restored_store = AppContext.get(CardPreviewSelectionStore)

    assert restored_selection.snapshot().selected_profile_id == (
        BUILTIN_CARD_PREVIEW_PROFILE_ID
    )
    assert restored_selection.snapshot().overlay_enabled is True
    report = json.loads(
        (tmp_path / "data" / "self_check.json").read_text(encoding="utf-8")
    )
    selection_check = next(
        item
        for item in report["self_check"]
        if item["name"] == "card_preview_selection"
    )
    assert selection_check["passed"] is True
    assert "ready" in selection_check["message"]
    assert BUILTIN_CARD_PREVIEW_PROFILE_ID in selection_check["message"]

    restored_selection.clear()
    assert restored_selection.snapshot().overlay_enabled is False
    assert not restored_store.path.exists()

    assert run(self_check_only=True, root=tmp_path) == 0
    restarted_selection = AppContext.get(CardPreviewSelectionService)
    assert restarted_selection.snapshot().selected_profile_id is None
    assert restarted_selection.snapshot().overlay_enabled is False
    assert not restored_store.path.exists()


def test_run_can_explicitly_disable_builtin_catalog(tmp_path) -> None:
    exit_code = run(
        self_check_only=True,
        root=tmp_path,
        card_preview_catalog=None,
    )

    assert exit_code == 0
    assert AppContext.get(CardPreviewSelectionService) is None
    assert AppContext.get(CardPreviewSelectionStore) is None
    assert not (
        tmp_path / "data" / CARD_PREVIEW_SELECTION_FILENAME
    ).exists()
    report = json.loads(
        (tmp_path / "data" / "self_check.json").read_text(encoding="utf-8")
    )
    selection_check = next(
        item
        for item in report["self_check"]
        if item["name"] == "card_preview_selection"
    )
    assert selection_check["passed"] is True
    assert "not configured" in selection_check["message"]


def test_run_does_not_fall_back_from_unavailable_saved_profile(tmp_path) -> None:
    selection_path = (
        tmp_path / "data" / CARD_PREVIEW_SELECTION_FILENAME
    )
    CardPreviewSelectionStore(selection_path).save("retired-profile")

    exit_code = run(self_check_only=True, root=tmp_path)
    selection = AppContext.get(CardPreviewSelectionService)

    assert exit_code == 0
    assert selection is not None
    assert selection.unavailable_stored_profile_id == "retired-profile"
    assert selection.snapshot().selected_profile_id is None
    assert selection.snapshot().overlay_enabled is False
    assert selection_path.exists()
    report = json.loads(
        (tmp_path / "data" / "self_check.json").read_text(encoding="utf-8")
    )
    selection_check = next(
        item
        for item in report["self_check"]
        if item["name"] == "card_preview_selection"
    )
    assert selection_check["passed"] is True
    assert "disabled" in selection_check["message"]
    assert "retired-profile" in selection_check["message"]


def test_saved_builtin_overlay_failure_does_not_block_normal_main_window(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    assert run(self_check_only=True, root=tmp_path) == 0
    AppContext.get(CardPreviewSelectionService).select(
        BUILTIN_CARD_PREVIEW_PROFILE_ID
    )
    assert run(self_check_only=True, root=tmp_path) == 0

    window = FakeWindow()
    runtime_error = RuntimeError("overlay start failed")
    runtime = FakeRuntime(start_error=runtime_error)
    warnings = []
    shown_info = []
    monkeypatch.setattr(main, "Tk", lambda: window)
    monkeypatch.setattr(main, "HomeView", FakeHomeView)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(
        main,
        "_build_registered_card_overlay_runtime",
        lambda actual_window: runtime if actual_window is window else None,
    )
    monkeypatch.setattr(
        main.messagebox,
        "showwarning",
        lambda title, message, parent: warnings.append((title, message, parent)),
    )
    monkeypatch.setattr(
        main.messagebox,
        "showinfo",
        lambda title, message, parent: shown_info.append((title, message, parent)),
    )

    created = create_main_window(
        {},
        AppContext.get(main.PathManager),
    )

    assert created is window
    assert window._card_overlay_start_error is runtime_error
    assert warnings
    assert "主程式仍可正常使用" in warnings[0][1]
    assert (
        AppContext.get(CardPreviewSelectionService)
        .snapshot()
        .selected_profile_id
        == BUILTIN_CARD_PREVIEW_PROFILE_ID
    )

    created._home_view.kwargs["on_start"]()
    assert "樣式選擇已保留，但目前無法可靠顯示" in shown_info[-1][1]
