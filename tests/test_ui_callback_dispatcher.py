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

    assert dispatcher.pending_count == 1
    dispatcher.pause()
    assert scheduler.cancelled == [first]
    assert dispatcher.pending_count == 0
    assert dispatcher.dispatch(lambda: calls.append("blocked")) is None

    assert dispatcher.resume() is True
    second = dispatcher.dispatch(lambda: calls.append("second"))
    scheduler.run(second)
    assert calls == ["second"]
    assert dispatcher.pending_count == 0


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
    assert dispatcher.closed is True
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
    assert dispatcher.pending_count == 0
