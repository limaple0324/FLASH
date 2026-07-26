import threading

from services.auto_click_service import (
    AutoClickHotkeyMonitor,
    AutoClickPointerSource,
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


def test_left_click_uses_ordered_direct_sync_without_physical_mouse_event():
    scheduler = Scheduler()
    clicker = Clicker()
    source = AutoClickPointerSource(101, 0.25, 0.75)
    delivered = []
    completed = threading.Event()
    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )
    service.configure_direct_left_sync(
        source_provider=lambda: source,
        eligible=lambda value: value == source,
        deliver=lambda value: (
            delivered.append(value),
            completed.set(),
            True,
        )[-1],
        enabled=lambda: True,
    )

    service.start(
        AutoClickSettings(
            repeat_forever=False,
            repeat_count=1,
        )
    )

    assert completed.wait(1.0)
    assert delivered == [source]
    assert clicker.buttons == []
    assert service.snapshot().sent_count == 1
    service.close()


def test_right_click_remains_a_single_physical_foreground_click():
    scheduler = Scheduler()
    clicker = Clicker()
    direct = []
    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )
    service.configure_direct_left_sync(
        source_provider=lambda: AutoClickPointerSource(101, 0.5, 0.5),
        eligible=lambda _source: True,
        deliver=lambda source: direct.append(source) is None,
        enabled=lambda: True,
    )

    service.start(
        AutoClickSettings(
            button="right",
            repeat_forever=False,
            repeat_count=1,
        )
    )

    assert clicker.buttons == ["right"]
    assert direct == []
    service.close()


def test_non_group_foreground_falls_back_to_one_physical_click():
    scheduler = Scheduler()
    clicker = Clicker()
    source = AutoClickPointerSource(999, 0.5, 0.5)
    direct = []
    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )
    service.configure_direct_left_sync(
        source_provider=lambda: source,
        eligible=lambda _source: False,
        deliver=lambda value: direct.append(value) is None,
        enabled=lambda: True,
    )

    service.start(
        AutoClickSettings(
            repeat_forever=False,
            repeat_count=1,
        )
    )

    assert clicker.buttons == ["left"]
    assert direct == []
    service.close()


def test_incomplete_group_blocks_click_instead_of_falling_back_to_physical():
    scheduler = Scheduler()
    clicker = Clicker()
    source = AutoClickPointerSource(101, 0.5, 0.5)
    direct = []
    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )
    service.configure_direct_left_sync(
        source_provider=lambda: source,
        eligible=lambda _source: False,
        deliver=lambda value: direct.append(value) is None,
        enabled=lambda: True,
        block_physical_fallback=lambda value: value == source,
    )

    service.start(AutoClickSettings())

    assert service.running is False
    assert clicker.buttons == []
    assert direct == []
    assert scheduler.calls == []
    service.close()


def test_invalidating_sync_session_discards_queued_clicks_without_duplicates():
    scheduler = Scheduler()
    clicker = Clicker()
    source = AutoClickPointerSource(101, 0.5, 0.5)
    first_started = threading.Event()
    release_first = threading.Event()
    delivered = []
    execution_allowed = []
    service = None

    def deliver(value):
        delivered.append(value)
        execution_allowed.append(
            service.direct_sync_execution_allowed()
        )
        first_started.set()
        release_first.wait(1.0)
        execution_allowed.append(
            service.direct_sync_execution_allowed()
        )
        return True

    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )
    service.configure_direct_left_sync(
        source_provider=lambda: source,
        eligible=lambda _source: True,
        deliver=deliver,
        enabled=lambda: True,
    )
    service.start(AutoClickSettings())
    assert first_started.wait(1.0)
    scheduler.fire()

    service.invalidate_direct_sync()
    release_first.set()
    service.close()

    assert delivered == [source]
    assert clicker.buttons == []
    assert execution_allowed == [True, False]


def test_first_direct_failure_blocks_later_same_generation_without_tk_tick():
    scheduler = Scheduler()
    clicker = Clicker()
    source = AutoClickPointerSource(101, 0.5, 0.5)
    first_started = threading.Event()
    release_first = threading.Event()
    delivered = []

    def deliver(value):
        delivered.append(value)
        first_started.set()
        release_first.wait(1)
        return False

    service = AutoClickService(
        clicker,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
    )
    service.configure_direct_left_sync(
        source_provider=lambda: source,
        eligible=lambda _source: True,
        deliver=deliver,
        enabled=lambda: True,
    )
    service.start(AutoClickSettings())
    assert first_started.wait(1)
    scheduler.fire()
    release_first.set()
    service._direct_queue.join()

    assert delivered == [source]
    assert clicker.buttons == []
    service.close()
