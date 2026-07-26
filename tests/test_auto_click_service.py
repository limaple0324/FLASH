from services.auto_click_service import (
    AutoClickHotkeyMonitor,
    AutoClickService,
    AutoClickSettings,
)


class Scheduler:
    def __init__(self):
        self.calls = []
        self.cancelled = []

    def schedule(self, delay, callback):
        token = (delay, callback)
        self.calls.append(token)
        return token

    def cancel(self, token):
        self.cancelled.append(token)

    def fire(self):
        _delay, callback = self.calls.pop(0)
        callback()


class Clicker:
    def __init__(self, results=()):
        self.results = list(results)
        self.buttons = []

    def click(self, button):
        self.buttons.append(button)
        return self.results.pop(0) if self.results else True


class Keys:
    def __init__(self):
        self.down = False

    def is_down(self, _key):
        return self.down


def test_continuous_click_uses_legacy_defaults_and_stops_explicitly():
    scheduler = Scheduler()
    clicker = Clicker()
    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )

    assert service.start(AutoClickSettings()) is True
    assert clicker.buttons == ["left"]
    assert scheduler.calls[0][0] == 20

    scheduler.fire()
    assert clicker.buttons == ["left", "left"]
    assert service.stop() is True
    assert service.running is False


def test_finite_right_click_count_stops_at_exact_count():
    scheduler = Scheduler()
    clicker = Clicker()
    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )

    service.start(
        AutoClickSettings(
            interval_ms=5,
            button="right",
            repeat_forever=False,
            repeat_count=2,
        )
    )
    scheduler.fire()

    assert clicker.buttons == ["right", "right"]
    assert service.snapshot().sent_count == 2
    assert service.running is False
    assert scheduler.calls == []


def test_delivery_failure_stops_without_rescheduling():
    scheduler = Scheduler()
    service = AutoClickService(
        Clicker([False]),
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )

    service.start(AutoClickSettings())

    assert service.running is False
    assert scheduler.calls == []


def test_f1_toggles_once_per_rising_edge():
    scheduler = Scheduler()
    keys = Keys()
    toggles = []
    monitor = AutoClickHotkeyMonitor(
        lambda: toggles.append("toggle"),
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()

    keys.down = True
    scheduler.fire()
    scheduler.fire()
    keys.down = False
    scheduler.fire()
    keys.down = True
    scheduler.fire()

    assert toggles == ["toggle", "toggle"]
