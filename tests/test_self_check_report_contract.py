from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from main import build_services
from services.app_context import AppContext


def test_self_check_report_contract_is_stable(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()

    report = SelfCheck(context=AppContext, paths=paths).run_all()

    assert set(report) == {"passed", "checks"}
    assert isinstance(report["passed"], bool)
    assert isinstance(report["checks"], list)
    assert report["checks"]
    for item in report["checks"]:
        assert set(item) == {"name", "passed", "message"}
        assert isinstance(item["name"], str) and item["name"]
        assert isinstance(item["passed"], bool)
        assert isinstance(item["message"], str) and item["message"]
