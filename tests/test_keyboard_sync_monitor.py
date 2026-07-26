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
        self.down = set()

    def foreground_is_game(self):
        return self.foreground

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
    }

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
