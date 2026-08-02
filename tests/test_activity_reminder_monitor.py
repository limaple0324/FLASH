from datetime import datetime

from domain.progress import TAIPEI_TIMEZONE
from services.activity_reminder_monitor import (
    ACTIVITY_REMINDER_CHECK_MS,
    ActivityReminderMonitor,
)


class _Service:
    def __init__(self):
        self.calls = []

    def poll(self, now):
        self.calls.append(now)
        return ()


class _Schedule:
    def __init__(self):
        self.calls = []
        self.cancelled = []

    def schedule(self, delay, callback):
        token = object()
        self.calls.append((delay, callback, token))
        return token

    def cancel(self, token):
        self.cancelled.append(token)


def test_monitor_polls_immediately_and_stops_its_pending_callback():
    service = _Service()
    schedule = _Schedule()
    now = datetime(2026, 7, 27, 12, 55, tzinfo=TAIPEI_TIMEZONE)
    monitor = ActivityReminderMonitor(
        service,
        schedule.schedule,
        schedule.cancel,
        now=lambda: now,
    )

    monitor.start()
    pending = schedule.calls[0][2]
    monitor.stop()

    assert service.calls == [now]
    assert schedule.calls[0][0] == ACTIVITY_REMINDER_CHECK_MS
    assert schedule.cancelled == [pending]
    assert monitor.running is False
