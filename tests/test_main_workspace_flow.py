from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from core.sp1_boundaries import ExternalAdapter, OperationResult
from core.target_window_observation import TargetWindowObservation
from domain.group import CharacterGroup
from main import build_services, create_main_window
from services.app_context import AppContext
from services.event_bus import EventBus
from services.target_window_state_service import (
    TARGET_WINDOW_OBSERVED_EVENT,
    TargetWindowStateService,
)
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


class _FakeWindow:
    def __init__(self) -> None:
        self.idle_callbacks = []
        self.protocols = {}

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

    def protocol(self, name, callback) -> None:
        self.protocols[name] = callback

    def destroy(self) -> None:
        pass


class _FakeHomeView:
    def __init__(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs
        self.workspace_refreshes = []
        self.target_window_refreshes = []

    def build(self) -> None:
        pass

    def refresh_cards(self) -> None:
        pass

    def refresh_workspace(self) -> None:
        self.workspace_refreshes.append(
            self.kwargs["workspace_state_provider"]()
        )

    def refresh_target_window(self) -> None:
        self.target_window_refreshes.append(
            self.kwargs["target_window_state_provider"]()
        )


class _ReadOnlyAdapter:
    def __init__(
        self,
        result: OperationResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.health_checks = 0
        self.input_calls = 0

    @property
    def name(self) -> str:
        return "read_only_target"

    def health_check(self) -> OperationResult:
        self.health_checks += 1
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("A health-check result is required.")
        return self.result

    def shutdown(self) -> None:
        pass

    def send_input(self) -> None:
        self.input_calls += 1
        raise AssertionError("status refresh must never send input")


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


def test_main_window_wires_event_backed_target_window_provider_and_refresh(
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

    created = create_main_window(
        {"self_check_passed": True},
        AppContext.get(main.PathManager),
    )
    provider = created._home_view.kwargs["target_window_state_provider"]

    assert provider() == TargetWindowObservation.not_observed()
    assert "target_window_state_service" not in created._home_view.kwargs

    observed = TargetWindowObservation.from_detection(
        {
            "configured": True,
            "safe": True,
            "code": "window.ready",
        }
    )
    AppContext.get(EventBus).publish(TARGET_WINDOW_OBSERVED_EVENT, observed)

    assert len(window.idle_callbacks) == 1
    window.idle_callbacks[0]()
    assert created._home_view.target_window_refreshes == [observed]


def test_view_current_status_rechecks_target_window_without_sending_input(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    build_services(root=tmp_path)
    adapter = _ReadOnlyAdapter(
        result=OperationResult(
            success=True,
            code="window.ready",
            message="private adapter message",
            details={"handle": 987654, "title": "Private Game Window"},
        )
    )
    AppContext.register(ExternalAdapter, adapter)
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
        "showinfo",
        lambda title, message, parent: shown.append((title, message, parent)),
    )

    created = create_main_window(
        {"self_check_passed": True},
        AppContext.get(main.PathManager),
    )
    created._home_view.kwargs["on_start"]()
    observation = created._home_view.kwargs[
        "target_window_state_provider"
    ]()

    assert adapter.health_checks == 1
    assert adapter.input_calls == 0
    assert observation.safe is True
    assert observation.code == "window.ready"
    assert "已找到可安全辨識的遊戲視窗" in shown[0][1]
    assert "987654" not in shown[0][1]
    assert "Private Game Window" not in shown[0][1]


def test_view_current_status_failure_keeps_previous_fact_and_reports_chinese(
    monkeypatch,
    tmp_path,
) -> None:
    import main

    build_services(root=tmp_path)
    previous = TargetWindowObservation.from_detection(
        {
            "configured": False,
            "safe": False,
            "code": "window.not_configured",
        }
    )
    AppContext.get(EventBus).publish(TARGET_WINDOW_OBSERVED_EVENT, previous)
    adapter = _ReadOnlyAdapter(
        error=OSError(r"C:\private\target-window.json")
    )
    AppContext.register(ExternalAdapter, adapter)
    window = _FakeWindow()
    shown_info = []
    shown_errors = []
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
        "showinfo",
        lambda title, message, parent: shown_info.append(
            (title, message, parent)
        ),
    )
    monkeypatch.setattr(
        main.messagebox,
        "showerror",
        lambda title, message, parent: shown_errors.append(
            (title, message, parent)
        ),
    )

    created = create_main_window(
        {"self_check_passed": True},
        AppContext.get(main.PathManager),
    )
    created._home_view.kwargs["on_start"]()

    assert adapter.health_checks == 1
    assert adapter.input_calls == 0
    assert AppContext.get(TargetWindowStateService).snapshot() is previous
    assert shown_info == []
    assert shown_errors == [
        (
            "輔｜遊戲視窗狀態",
            (
                "無法更新遊戲視窗狀態，已保留上次顯示的內容。\n\n"
                "所有遊戲操作仍保持停用；錯誤已寫入紀錄。"
            ),
            window,
        )
    ]
    assert r"C:\private\target-window.json" not in shown_errors[0][1]
    assert r"C:\private\target-window.json" in (
        tmp_path / "logs" / "flash.log"
    ).read_text(encoding="utf-8")


def test_main_window_target_window_refresh_error_is_chinese_and_private(
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

    created = create_main_window(
        {"self_check_passed": True},
        AppContext.get(main.PathManager),
    )
    created._home_view.kwargs["on_target_window_error"](
        OSError(r"C:\private\target-window.json")
    )

    assert shown == [
        (
            "輔｜遊戲視窗狀態",
            (
                "無法更新遊戲視窗狀態，已保留上次顯示的內容。\n\n"
                "所有遊戲操作仍保持停用；錯誤已寫入紀錄。"
            ),
            window,
        )
    ]
    assert r"C:\private\target-window.json" not in shown[0][1]
    assert r"C:\private\target-window.json" in (
        tmp_path / "logs" / "flash.log"
    ).read_text(encoding="utf-8")


def test_closing_main_window_unsubscribes_target_window_refresh(
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

    create_main_window(
        {"self_check_passed": True},
        AppContext.get(main.PathManager),
    )
    window.protocols["WM_DELETE_WINDOW"]()
    AppContext.get(EventBus).publish(
        TARGET_WINDOW_OBSERVED_EVENT,
        TargetWindowObservation.from_detection(
            {
                "configured": True,
                "safe": True,
                "code": "window.ready",
            }
        ),
    )

    assert window.idle_callbacks == []
