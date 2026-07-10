from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from main import build_services
from services.app_context import AppContext


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
    assert "not registered yet" in checks["recovery_boundary"]["message"]
    assert "not registered yet" in checks["smart_reconnect_boundary"]["message"]
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
