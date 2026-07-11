from config.config_manager import ConfigManager
from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from core.window_registry_store import WindowRegistryStore
from main import build_services
from services.app_context import AppContext


def test_self_check_continues_after_one_check_fails(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    AppContext._services.pop(ConfigManager)

    report = SelfCheck(context=AppContext, paths=paths).run_all()
    checks = {item["name"]: item for item in report["checks"]}

    assert report["passed"] is False
    assert checks["config_manager"]["passed"] is False
    assert checks["logger_service"]["passed"] is True
    assert checks["event_bus"]["passed"] is True
    assert checks["window_registry"]["passed"] is True


def test_self_check_rejects_registry_store_outside_managed_data_directory(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    AppContext.register(
        WindowRegistryStore,
        WindowRegistryStore(tmp_path / "outside" / "window_registry.json"),
    )

    report = SelfCheck(context=AppContext, paths=paths).run_all()
    checks = {item["name"]: item for item in report["checks"]}

    assert report["passed"] is False
    assert checks["window_registry"]["passed"] is False
    assert checks["window_registry"]["message"] == (
        "Window registry storage path is outside the managed data directory."
    )
