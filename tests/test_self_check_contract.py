from core.self_check import SelfCheck
from core.bootstrap import Bootstrap
from config.path_manager import PathManager
from main import build_services
from services.app_context import AppContext


def test_self_check_reports_all_required_checks_once(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()

    report = SelfCheck(context=AppContext, paths=paths).run_all()
    names = [item["name"] for item in report["checks"]]

    assert names == [
        "path_manager",
        "config_manager",
        "logger_service",
        "event_bus",
        "window_registry",
        "recovery_boundary",
        "smart_reconnect_boundary",
        "external_adapter",
    ]
    assert len(names) == len(set(names))
