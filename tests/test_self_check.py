from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from core.window_registry_store import WindowRegistryStore
from main import build_services
from services.app_context import AppContext
from services.target_window_state_service import TargetWindowStateService


def test_self_check_passes_for_bootstrapped_core(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()

    report = SelfCheck(context=AppContext, paths=paths).run_all()

    assert report["passed"] is True
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["path_manager"]["passed"] is True
    assert checks["config_manager"]["passed"] is True
    assert checks["logger_service"]["passed"] is True
    assert checks["event_bus"]["passed"] is True
    assert checks["window_registry"]["passed"] is True
    assert "Character registry loaded" in checks["window_registry"]["message"]
    assert checks["target_window_state"]["passed"] is True
    assert checks["target_window_state"]["message"] == (
        "Target-window state service provides a valid read-only observation."
    )
    assert "window.not_observed" not in checks["target_window_state"]["message"]
    assert checks["card_history"]["passed"] is True
    assert "Card history loaded" in checks["card_history"]["message"]
    assert "not registered yet" in checks["recovery_boundary"]["message"]
    assert (
        checks["smart_reconnect_boundary"]["message"]
        == "Registered SmartReconnectBoundary implementation is valid."
    )
    assert "not registered yet" in checks["external_adapter"]["message"]


def test_self_check_reports_missing_required_service(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    AppContext._services.pop(ConfigManager)

    report = SelfCheck(context=AppContext, paths=paths).run_all()

    assert report["passed"] is False
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["config_manager"]["passed"] is False
    assert checks["config_manager"]["message"] == "ConfigManager is not registered."


def test_self_check_reports_missing_registry_store(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    AppContext._services.pop(WindowRegistryStore)

    report = SelfCheck(context=AppContext, paths=paths).run_all()

    assert report["passed"] is False
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["window_registry"]["passed"] is False
    assert checks["window_registry"]["message"] == "WindowRegistryStore is not registered."


def test_self_check_reports_missing_target_window_state_service(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    AppContext._services.pop(TargetWindowStateService)

    report = SelfCheck(context=AppContext, paths=paths).run_all()

    assert report["passed"] is False
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["target_window_state"]["passed"] is False
    assert checks["target_window_state"]["message"] == (
        "TargetWindowStateService is not registered."
    )
