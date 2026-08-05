from datetime import datetime

from cards.history_store import CardHistoryStore
from cards.service import CardService
from domain.progress import TAIPEI_TIMEZONE
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.confirmed_activity_rule_monitor import (
    CONFIRMED_ACTIVITY_RULE_CHECK_MS,
    ConfirmedActivityRuleMonitor,
)
from services.confirmed_activity_rule_service import ConfirmedActivityRuleService


def test_monitor_only_polls_the_confirmed_rule_service_and_stops_cleanly(
    tmp_path,
    monkeypatch,
):
    service = ConfirmedActivityRuleService(
        CardCoordinator(
            CardService(),
            CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
        )
    )
    observed = []
    scheduled = []
    cancelled = []
    now = datetime(2026, 8, 3, 12, tzinfo=TAIPEI_TIMEZONE)
    monkeypatch.setattr(service, "poll", lambda value: observed.append(value) or ())

    monitor = ConfirmedActivityRuleMonitor(
        service,
        lambda milliseconds, callback: scheduled.append(
            (milliseconds, callback)
        )
        or "scheduled",
        cancelled.append,
        now=lambda: now,
    )

    assert monitor.start() is True
    assert observed == [now]
    assert scheduled[0][0] == CONFIRMED_ACTIVITY_RULE_CHECK_MS
    scheduled[0][1]()
    assert observed == [now, now]
    assert monitor.stop() is True
    assert cancelled == ["scheduled"]
