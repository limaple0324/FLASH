from cards.history_store import CardHistoryStore
from core.bootstrap import Bootstrap
from core.self_check import SelfCheck
from main import CARD_HISTORY_FILENAME, build_services
from services.app_context import AppContext
from services.card_history_service import CardHistoryService


def _checks(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    report = SelfCheck(context=AppContext, paths=paths).run_all()
    return report, {item["name"]: item for item in report["checks"]}


def test_self_check_reports_missing_card_history_service(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    Bootstrap(context=AppContext).start()
    AppContext._services.pop(CardHistoryService)

    report = SelfCheck(context=AppContext, paths=paths).run_all()
    checks = {item["name"]: item for item in report["checks"]}

    assert report["passed"] is False
    assert checks["card_history"]["message"] == "CardHistoryService is not registered."


def test_self_check_rejects_history_outside_managed_data(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    outside_store = CardHistoryStore(tmp_path / "outside" / CARD_HISTORY_FILENAME)
    AppContext.register(CardHistoryStore, outside_store)
    AppContext.register(CardHistoryService, CardHistoryService(outside_store))
    Bootstrap(context=AppContext).start()

    report = SelfCheck(context=AppContext, paths=paths).run_all()
    checks = {item["name"]: item for item in report["checks"]}

    assert report["passed"] is False
    assert checks["card_history"]["message"] == (
        "Card history path is outside the managed data directory."
    )


def test_self_check_reports_corrupt_history_recovery(tmp_path):
    history_path = tmp_path / "data" / CARD_HISTORY_FILENAME
    history_path.parent.mkdir(parents=True)
    history_path.write_text("{broken", encoding="utf-8")

    report, checks = _checks(tmp_path)

    assert report["passed"] is True
    assert checks["card_history"]["passed"] is True
    assert "recovered from corruption" in checks["card_history"]["message"]
    assert history_path.with_suffix(".json.corrupt").exists()
