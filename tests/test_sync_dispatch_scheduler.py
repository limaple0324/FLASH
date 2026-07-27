from threading import Event

from services.sync_dispatch_scheduler import SyncDispatchScheduler


def test_independent_delays_run_in_due_order_without_cumulative_wait():
    scheduler = SyncDispatchScheduler(thread_name="test-sync-delay")
    completed = Event()
    observed = []
    try:
        scheduler.schedule(30, lambda: observed.append("later"))
        scheduler.schedule(
            10,
            lambda: (observed.append("earlier"), completed.set()),
        )

        assert completed.wait(0.5)
        assert observed[0] == "earlier"
    finally:
        scheduler.close()


def test_invalidate_cancels_pending_delayed_dispatch():
    scheduler = SyncDispatchScheduler(thread_name="test-sync-cancel")
    delivered = Event()
    try:
        scheduler.schedule(30, delivered.set)
        scheduler.invalidate()

        assert not delivered.wait(0.1)
    finally:
        scheduler.close()
