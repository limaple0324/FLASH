from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.group import CharacterGroup
from main import build_services, create_main_window
from services.app_context import AppContext
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


class _FakeWindow:
    def __init__(self) -> None:
        self.idle_callbacks = []

    def title(self, _value) -> None:
        pass

    def geometry(self, _value) -> None:
        pass

    def minsize(self, _width, _height) -> None:
        pass

    def after_idle(self, callback) -> None:
        self.idle_callbacks.append(callback)

    def after(self, _delay, callback):
        self.after_callback = callback
        return "after-id"

    def protocol(self, _name, _callback) -> None:
        pass

    def destroy(self) -> None:
        pass


class _FakeHomeView:
    def __init__(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs
        self.workspace_refreshes = []

    def build(self) -> None:
        pass

    def refresh_cards(self) -> None:
        pass

    def refresh_workspace(self) -> None:
        self.workspace_refreshes.append(
            self.kwargs["workspace_state_provider"]()
        )


def test_startup_registers_a_fresh_empty_workspace_without_deriving_registry_data(
    tmp_path,
) -> None:
    registry = WindowRegistry()
    registry.register_character(
        "private-character-id",
        "小古",
        group="十四支",
    )
    WindowRegistryStore(tmp_path / "data" / "window_registry.json").save(
        registry
    )

    build_services(root=tmp_path)
    first = AppContext.get(WorkspaceService)

    assert first is not None
    assert first.snapshot() == WorkspaceState()

    first.set_current_group(
        CharacterGroup(group_id="confirmed-group", name="十四支")
    )
    build_services(root=tmp_path)
    second = AppContext.get(WorkspaceService)

    assert second is not first
    assert second.snapshot() == WorkspaceState()


def test_main_window_wires_a_read_only_workspace_provider_and_schedules_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    build_services(root=tmp_path)
    window = _FakeWindow()
    monkeypatch.setattr(main, "Tk", lambda: window)
    monkeypatch.setattr(main, "HomeView", _FakeHomeView)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(
        main,
        "_build_registered_card_overlay_runtime",
        lambda _window: None,
    )

    created = create_main_window({}, AppContext.get(main.PathManager))
    service = AppContext.get(WorkspaceService)
    provider = created._home_view.kwargs["workspace_state_provider"]

    assert provider() == WorkspaceState()
    assert "workspace_service" not in created._home_view.kwargs
    assert not hasattr(created._home_view, "set_current_group")

    state = service.set_current_group(
        CharacterGroup(group_id="confirmed-group", name="十四支")
    )

    assert len(window.idle_callbacks) == 1
    window.idle_callbacks[0]()
    assert created._home_view.workspace_refreshes == [state]


def test_main_window_workspace_error_is_chinese_and_hides_internal_details(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    build_services(root=tmp_path)
    window = _FakeWindow()
    shown = []
    monkeypatch.setattr(main, "Tk", lambda: window)
    monkeypatch.setattr(main, "HomeView", _FakeHomeView)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(
        main,
        "_build_registered_card_overlay_runtime",
        lambda _window: None,
    )
    monkeypatch.setattr(
        main.messagebox,
        "showerror",
        lambda title, message, parent: shown.append((title, message, parent)),
    )

    created = create_main_window({}, AppContext.get(main.PathManager))
    created._home_view.kwargs["on_workspace_error"](
        OSError(r"C:\private\workspace.json")
    )

    assert shown == [
        (
            "輔｜工作區",
            (
                "無法更新工作區畫面，已保留上次顯示的內容。\n\n"
                "請稍後再試；錯誤已寫入紀錄。"
            ),
            window,
        )
    ]
    assert r"C:\private\workspace.json" not in shown[0][1]
    assert r"C:\private\workspace.json" in (
        tmp_path / "logs" / "flash.log"
    ).read_text(encoding="utf-8")
