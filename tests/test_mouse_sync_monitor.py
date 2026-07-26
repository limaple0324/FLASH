from services.mouse_sync_monitor import MouseSample, MouseSyncMonitor


class Controller:
    def __init__(self):
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


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
