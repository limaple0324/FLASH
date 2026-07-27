import threading

from services.keyboard_sync_monitor import KeyboardSyncMonitor


class FakeController:
    def __init__(self):
        self.calls = []
        self.called = threading.Event()

    def send_approved_key(self, key, **kwargs):
        self.calls.append((key, kwargs))
        self.called.set()
        return object()


class FakeKeyboardState:
    def __init__(self):
        self.foreground = True
        self.foreground_handle = 101
        self.down = set()

    def foreground_is_game(self):
        return self.foreground

    def foreground_game_handle(self):
        return self.foreground_handle if self.foreground else None

    def is_down(self, virtual_key):
        return virtual_key in self.down

    def conflicting_modifier_down(self):
        return any(value in self.down for value in (0x11, 0x12, 0x5B, 0x5C))


def monitor_fixture():
    controller = FakeController()
    keyboard = FakeKeyboardState()
    scheduled = []
    cancelled = []

    def schedule(delay, callback):
        token = (delay, callback)
        scheduled.append(token)
        return token

    monitor = KeyboardSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=schedule,
        cancel=cancelled.append,
        state_backend=keyboard,
    )
    return monitor, controller, keyboard, scheduled, cancelled


def test_player_must_explicitly_start_monitor_before_any_key_is_seen():
    monitor, controller, keyboard, scheduled, _cancelled = monitor_fixture()
    keyboard.down.add(0x42)

    monitor.poll()

    assert monitor.enabled is False
    assert controller.calls == []
    assert scheduled == []


def test_default_poll_interval_is_five_milliseconds_with_two_millisecond_floor():
    monitor, _controller, _keyboard, scheduled, _cancelled = monitor_fixture()

    monitor.start()

    assert scheduled[0][0] == 5
    monitor.stop()

    floor_scheduled = []
    floor_monitor = KeyboardSyncMonitor(
        FakeController(),
        policy_provider=lambda: "all",
        schedule=lambda delay, callback: floor_scheduled.append(
            (delay, callback)
        ),
        cancel=lambda _token: None,
        state_backend=FakeKeyboardState(),
        interval_ms=0,
    )
    floor_monitor.start()

    assert floor_scheduled[0][0] == 2
    floor_monitor.stop()


def test_rising_edge_from_foreground_game_mirrors_without_resending_to_master():
    monitor, controller, keyboard, _scheduled, _cancelled = monitor_fixture()
    monitor.start()
    keyboard.down.add(0x42)

    monitor.poll()
    assert controller.called.wait(1)

    key, options = controller.calls[0]
    assert key == "B"
    assert options == {
        "policy": "all",
        "execute": True,
        "exclude_foreground": True,
        "source_handle": 101,
        "execution_guard": options["execution_guard"],
    }
    assert options["execution_guard"]() is True

    monitor.poll()
    assert len(controller.calls) == 1
    monitor.stop()


def test_ctrl_arrow_is_one_confirmed_chord_not_multiple_letter_actions():
    monitor, controller, keyboard, _scheduled, _cancelled = monitor_fixture()
    monitor.start()
    keyboard.down.update({0x11, 0x26})

    monitor.poll()
    assert controller.called.wait(1)

    assert [item[0] for item in controller.calls] == ["CTRL+↑"]
    monitor.stop()


def test_only_player_checked_keys_are_polled() -> None:
    controller = FakeController()
    keyboard = FakeKeyboardState()
    selected = ["ESC"]
    monitor = KeyboardSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        selected_keys_provider=lambda: tuple(selected),
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=keyboard,
    )
    monitor.start()
    keyboard.down.add(0x42)
    monitor.poll()
    assert controller.calls == []

    keyboard.down.clear()
    keyboard.down.add(0x1B)
    monitor.poll()
    assert controller.called.wait(1)
    assert [item[0] for item in controller.calls] == ["ESC"]
    monitor.stop()


def test_standalone_ctrl_and_shift_can_be_selected() -> None:
    controller = FakeController()
    keyboard = FakeKeyboardState()
    selected = ["CTRL", "SHIFT"]
    monitor = KeyboardSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        selected_keys_provider=lambda: tuple(selected),
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=keyboard,
    )
    monitor.start()

    keyboard.down.add(0x11)
    monitor.poll()
    assert controller.called.wait(1)
    keyboard.down.clear()
    monitor.poll()
    keyboard.down.add(0x10)
    monitor.poll()

    for _attempt in range(100):
        if len(controller.calls) == 2:
            break
        threading.Event().wait(0.005)
    assert [item[0] for item in controller.calls] == ["CTRL", "SHIFT"]
    monitor.stop()


def test_non_game_foreground_never_dispatches_and_stop_cancels_poll():
    monitor, controller, keyboard, scheduled, cancelled = monitor_fixture()
    monitor.start()
    keyboard.foreground = False
    keyboard.down.add(0x42)

    monitor.poll()
    monitor.stop()

    assert controller.calls == []
    assert scheduled
    assert cancelled


def test_overlapping_keys_are_delivered_once_each_in_arrival_order():
    entered_first = threading.Event()
    release_first = threading.Event()
    delivered_all = threading.Event()

    class BlockingController(FakeController):
        def send_approved_key(self, key, **kwargs):
            self.calls.append((key, kwargs))
            if len(self.calls) == 1:
                entered_first.set()
                assert release_first.wait(1)
            if len(self.calls) == 2:
                delivered_all.set()
            return object()

    controller = BlockingController()
    monitor = KeyboardSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=FakeKeyboardState(),
    )

    monitor.start()
    monitor._dispatch("B")
    assert entered_first.wait(1)
    monitor._dispatch("C")
    release_first.set()

    assert delivered_all.wait(1)
    assert [key for key, _options in controller.calls] == ["B", "C"]
    monitor.stop()


def test_stop_clears_waiting_keys_and_closes_active_execution_guard():
    entered_first = threading.Event()
    release_first = threading.Event()
    finished_first = threading.Event()
    guard_after_stop = []

    class BlockingController(FakeController):
        def send_approved_key(self, key, **kwargs):
            self.calls.append((key, kwargs))
            entered_first.set()
            assert release_first.wait(1)
            guard_after_stop.append(kwargs["execution_guard"]())
            finished_first.set()
            return object()

    controller = BlockingController()
    monitor = KeyboardSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=FakeKeyboardState(),
    )

    monitor.start()
    monitor._dispatch("B")
    assert entered_first.wait(1)
    monitor._dispatch("C")
    monitor.stop()
    release_first.set()

    assert finished_first.wait(1)
    assert [key for key, _options in controller.calls] == ["B"]
    assert guard_after_stop == [False]


def test_key_keeps_the_source_handle_captured_at_press_time():
    entered = threading.Event()
    release = threading.Event()

    class BlockingController(FakeController):
        def send_approved_key(self, key, **kwargs):
            entered.set()
            assert release.wait(1)
            self.calls.append((key, kwargs))
            self.called.set()
            return object()

    keyboard = FakeKeyboardState()
    controller = BlockingController()
    monitor = KeyboardSyncMonitor(
        controller,
        policy_provider=lambda: "all",
        schedule=lambda _delay, _callback: object(),
        cancel=lambda _token: None,
        state_backend=keyboard,
    )
    monitor.start()
    monitor._dispatch("B", source_handle=101)
    assert entered.wait(1)
    keyboard.foreground_handle = 202
    release.set()
    assert controller.called.wait(1)

    assert controller.calls[0][1]["source_handle"] == 101
    monitor.stop()
