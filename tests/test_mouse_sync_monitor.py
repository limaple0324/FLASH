import threading

from services.mouse_sync_monitor import MouseSample, MouseSyncMonitor


class Controller:
    def __init__(self):
        self.calls = []
        self.release_calls = 0

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs

    def release_pressed_targets(self):
        self.release_calls += 1
        return 0

    def has_pressed_targets(self):
        return False


class Scheduler:
    def __init__(self):
        self.calls = []

    def schedule(self, delay, callback):
        self.calls.append((delay, callback))
        return callback

    def cancel(self, _token):
        return None

    def fire(self):
        _delay, callback = self.calls.pop(0)
        callback()


class Mouse:
    def __init__(self, samples):
        self.samples = list(samples)

    def sample(self):
        return self.samples.pop(0)


def test_left_press_drag_release_are_mirrored(monkeypatch):
    controller = Controller()
    scheduler = Scheduler()
    mouse = Mouse(
        [
            MouseSample(1, 0.1, 0.2, False),
            MouseSample(1, 0.1, 0.2, True),
            MouseSample(1, 0.2, 0.3, True),
            MouseSample(1, 0.2, 0.3, False),
        ]
    )
    threads = []

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            threads.append(self)
            self.target()

    monkeypatch.setattr(
        "services.mouse_sync_monitor.threading.Thread",
        ImmediateThread,
    )
    monitor = MouseSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=mouse,
    )
    monitor.start()

    for _ in range(4):
        scheduler.fire()

    assert [call["event"] for call in controller.calls] == [
        "left_down",
        "move",
        "left_up",
    ]


def test_default_poll_interval_is_two_milliseconds_with_one_millisecond_floor():
    controller = Controller()
    scheduler = Scheduler()
    monitor = MouseSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=Mouse([]),
    )

    monitor.start()

    assert scheduler.calls[0][0] == 2
    monitor.stop()

    floor_scheduler = Scheduler()
    floor_monitor = MouseSyncMonitor(
        Controller(),
        policy_provider=lambda: "all",
        schedule=floor_scheduler.schedule,
        cancel=floor_scheduler.cancel,
        state_backend=Mouse([]),
        interval_ms=0,
    )
    floor_monitor.start()

    assert floor_scheduler.calls[0][0] == 1
    floor_monitor.stop()


def test_overlapping_mouse_events_are_delivered_once_each_in_arrival_order():
    entered_first = threading.Event()
    release_first = threading.Event()
    delivered_all = threading.Event()

    class BlockingController(Controller):
        def send(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                entered_first.set()
                assert release_first.wait(1)
            if len(self.calls) == 2:
                delivered_all.set()
            return kwargs

    controller = BlockingController()
    monitor = MouseSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=Mouse([]),
    )

    monitor.start()
    monitor._dispatch(MouseSample(1, 0.1, 0.2, True), "left_down")
    assert entered_first.wait(1)
    monitor._dispatch(MouseSample(1, 0.2, 0.3, False), "left_up")
    release_first.set()

    assert delivered_all.wait(1)
    assert [call["event"] for call in controller.calls] == [
        "left_down",
        "left_up",
    ]
    monitor.stop()


def test_stop_clears_waiting_mouse_events_and_closes_active_execution_guard():
    entered_first = threading.Event()
    release_first = threading.Event()
    finished_first = threading.Event()
    guard_after_stop = []

    class BlockingController(Controller):
        def send(self, **kwargs):
            self.calls.append(kwargs)
            entered_first.set()
            assert release_first.wait(1)
            guard_after_stop.append(kwargs["execution_guard"]())
            finished_first.set()
            return kwargs

    controller = BlockingController()
    monitor = MouseSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=Mouse([]),
    )

    monitor.start()
    monitor._dispatch(MouseSample(1, 0.1, 0.2, True), "left_down")
    assert entered_first.wait(1)
    monitor._dispatch(MouseSample(1, 0.2, 0.3, False), "left_up")
    monitor.stop()
    release_first.set()

    assert finished_first.wait(1)
    assert [call["event"] for call in controller.calls] == ["left_down"]
    assert guard_after_stop == [False]
    assert controller.release_calls == 1


def test_many_moves_are_coalesced_and_release_discards_stale_moves():
    entered = threading.Event()
    release_first = threading.Event()
    delivered = threading.Event()

    class BlockingController(Controller):
        def send(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                entered.set()
                assert release_first.wait(1)
            elif kwargs["event"] == "left_up":
                delivered.set()
            return kwargs

    controller = BlockingController()
    monitor = MouseSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=Mouse([]),
    )
    monitor.start()
    monitor._dispatch(MouseSample(1, 0.1, 0.2, True), "left_down")
    assert entered.wait(1)
    for index in range(100):
        monitor._dispatch(
            MouseSample(1, index / 100, index / 100, True),
            "move",
        )
    monitor._dispatch(MouseSample(999, 0.9, 0.9, False), "left_up")
    release_first.set()

    assert delivered.wait(1)
    assert [call["event"] for call in controller.calls] == [
        "left_down",
        "left_up",
    ]
    monitor.stop()


def test_release_is_not_blocked_by_an_already_running_move():
    move_started = threading.Event()
    finish_move = threading.Event()

    class MoveBlockingController(Controller):
        def send(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["event"] == "move":
                move_started.set()
                assert finish_move.wait(1)
            return kwargs

    controller = MoveBlockingController()
    monitor = MouseSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=Mouse([]),
    )
    monitor.start()
    monitor._dispatch(MouseSample(1, 0.2, 0.3, True), "move")
    assert move_started.wait(1)

    monitor._dispatch(MouseSample(999, 0.9, 0.9, False), "left_up")

    assert controller.release_calls == 1
    finish_move.set()
    monitor.stop()
