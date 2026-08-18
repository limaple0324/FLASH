import json

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
import main
from main import build_services, registry_status, save_registry
from services.app_context import AppContext


def test_build_services_loads_and_registers_registry(tmp_path):
    store = WindowRegistryStore(tmp_path / "data" / "window_registry.json")
    registry = WindowRegistry()
    registry.register_character("160-old", "160古")
    store.save(registry)

    build_services(root=tmp_path)

    loaded = AppContext.get(WindowRegistry)
    assert loaded is not None
    assert loaded.get("160-old").display_name == "160古"
    assert AppContext.get(WindowRegistryStore) is not None
    assert registry_status()["count"] == 1


def test_save_registry_persists_current_records(tmp_path):
    build_services(root=tmp_path)
    registry = AppContext.get(WindowRegistry)
    assert registry is not None
    registry.register_character("120-old", "120古")

    save_registry()

    payload = json.loads((tmp_path / "data" / "window_registry.json").read_text(encoding="utf-8"))
    assert payload["characters"][0]["display_name"] == "120古"


def test_corrupt_registry_is_rebuilt_and_reported(tmp_path):
    path = tmp_path / "data" / "window_registry.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    build_services(root=tmp_path)
    status = registry_status()

    assert status["loaded"] is True
    assert status["recovered"] is True
    assert status["count"] == 0
    assert list(path.parent.glob("window_registry.json.corrupt*"))


def test_create_main_window_assembles_group_window_launch_with_registry(
    tmp_path, monkeypatch
):
    class _NoopTrayController:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return False

        def mark_operations_running(self):
            pass

        def mark_operations_stopped(self):
            pass

        def handle_unmap(self, *_args):
            pass

        def close(self):
            return True

    monkeypatch.setattr(main, "SystemTrayController", _NoopTrayController)
    paths, _logger = build_services(root=tmp_path)

    window = None
    try:
        window = main.create_main_window(registry_status(), paths)
    finally:
        if window is not None:
            window.destroy()

    assert AppContext.get(WindowRegistry) is not None
    assert AppContext.get(WindowRegistryStore) is not None
