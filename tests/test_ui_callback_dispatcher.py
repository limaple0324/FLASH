import threading

from services.ui_callback_dispatcher import UiCallbackDispatcher


class Scheduler:
    def __init__(self):
        self.callbacks = {}
        self.cancelled = []

    def schedule(self, _delay, callback):
        token = f"callback-{len(self.callbacks) + 1}"
        self.callbacks[token] = callback
        return token

    def cancel(self, token):
        self.cancelled.append(token)
        self.callbacks.pop(token, None)

    def run(self, token):
        self.callbacks.pop(token)()


def test_pause_cancels_queued_callbacks_and_resume_accepts_new_work():
    scheduler = Scheduler()
    dispatcher = UiCallbackDispatcher(
        scheduler.schedule,
        scheduler.cancel,
    )
    calls = []
    first = dispatcher.dispatch(lambda: calls.append("first"))

    assert first in scheduler.callbacks
    dispatcher.pause()
    assert scheduler.cancelled == [first]
    assert first not in scheduler.callbacks
    assert dispatcher.dispatch(lambda: calls.append("blocked")) is None

    assert dispatcher.resume() is True
    second = dispatcher.dispatch(lambda: calls.append("second"))
    scheduler.run(second)
    assert calls == ["second"]
    assert scheduler.callbacks == {}


def test_close_prevents_callbacks_even_if_scheduler_delivers_late():
    scheduler = Scheduler()
    dispatcher = UiCallbackDispatcher(
        scheduler.schedule,
        scheduler.cancel,
    )
    calls = []
    token = dispatcher.dispatch(lambda: calls.append("late"))
    guarded = scheduler.callbacks[token]

    dispatcher.close()
    guarded()

    assert calls == []
    assert dispatcher.resume() is False
    assert dispatcher.dispatch(lambda: calls.append("never")) is None


def test_scheduler_failure_is_isolated_without_a_pending_token():
    dispatcher = UiCallbackDispatcher(
        lambda _delay, _callback: (_ for _ in ()).throw(
            RuntimeError("fault injection")
        ),
        lambda _token: None,
    )

    assert dispatcher.dispatch(lambda: None) is None


def test_worker_dispatch_does_not_deadlock_main_thread_callback():
    callback_finished = threading.Event()
    callback_finished_before_schedule_returned = False

    def schedule(_delay, callback):
        nonlocal callback_finished_before_schedule_returned

        def run_callback():
            callback()
            callback_finished.set()

        thread = threading.Thread(target=run_callback)
        thread.start()
        callback_finished_before_schedule_returned = callback_finished.wait(1)
        thread.join(1)
        return "callback-1"

    cancelled = []
    dispatcher = UiCallbackDispatcher(schedule, cancelled.append)
    calls = []

    token = dispatcher.dispatch(lambda: calls.append("completed"))

    assert token == "callback-1"
    assert callback_finished_before_schedule_returned is True
    assert callback_finished.is_set()
    assert calls == ["completed"]
    dispatcher.pause()
    assert cancelled == []


def test_pause_resume_rejects_dispatch_started_before_pause():
    schedule_started = threading.Event()
    allow_schedule_return = threading.Event()
    scheduled_callback = {}
    cancelled = []

    def schedule(_delay, callback):
        scheduled_callback["callback"] = callback
        schedule_started.set()
        assert allow_schedule_return.wait(1)
        return "callback-1"

    dispatcher = UiCallbackDispatcher(schedule, cancelled.append)
    calls = []
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            dispatcher.dispatch(lambda: calls.append("stale"))
        )
    )

    worker.start()
    assert schedule_started.wait(1)
    dispatcher.pause()
    assert dispatcher.resume() is True
    allow_schedule_return.set()
    worker.join(1)

    assert worker.is_alive() is False
    assert result == [None]
    assert cancelled == ["callback-1"]

    scheduled_callback["callback"]()
    assert calls == []
