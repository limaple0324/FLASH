from datetime import datetime, timezone

from cards.history_store import CardHistoryStore
from cards.service import CardService
from services.card_coordinator import CardCoordinator
from services.card_history_service import CardHistoryService
from services.farm_timer_monitor import FarmTimerMonitor
from services.farm_timer_service import FarmTimerService


def test_monitor_checks_immediately_and_cancels_one_pending_callback(tmp_path):
    service = FarmTimerService(
        CardCoordinator(
            CardService(),
            CardHistoryService(CardHistoryStore(tmp_path / "history.json")),
        )
    )
    calls = []
    cancelled = []
    original_poll = service.poll
    service.poll = lambda now: calls.append(now) or ()
    scheduled = []

    def schedule(delay, callback):
        scheduled.append((delay, callback))
        return "farm-tick"

    monitor = FarmTimerMonitor(
        service,
        schedule,
        cancelled.append,
    )

    assert monitor.start() is True
    assert len(calls) == 1
    assert calls[0].tzinfo == timezone.utc
    assert scheduled[0][0] == 15000
    assert monitor.stop() is True
    assert cancelled == ["farm-tick"]
    service.poll = original_poll
