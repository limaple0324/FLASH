from services.feature_hotkey_monitor import (
    FeatureHotkeyMonitor,
    GroupLaunchHotkeyMonitor,
    feature_hotkey_virtual_key,
    normalize_feature_hotkey,
)


class Scheduler:
    def __init__(self):
        self.calls = []

    def schedule(self, delay, callback):
        token = (delay, callback)
        self.calls.append(token)
        return token

    def cancel(self, token):
        if token in self.calls:
            self.calls.remove(token)

    def fire(self):
        _delay, callback = self.calls.pop(0)
        callback()


class Keys:
    def __init__(self):
        self.down = set()

    def is_down(self, virtual_key):
        return virtual_key in self.down


def test_three_feature_hotkeys_toggle_on_independent_rising_edges() -> None:
    scheduler = Scheduler()
    keys = Keys()
    toggles = []
    hotkeys = {
        "sync": "XBUTTON1",
        "reconnect": "F2",
        "auto_click": "F1",
    }
    monitor = FeatureHotkeyMonitor(
        {
            "sync": lambda: toggles.append("sync"),
            "reconnect": lambda: toggles.append("reconnect"),
            "auto_click": lambda: toggles.append("auto_click"),
        },
        hotkeys_provider=lambda: hotkeys,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()

    keys.down.add(0x05)
    scheduler.fire()
    scheduler.fire()
    keys.down.clear()
    scheduler.fire()
    keys.down.add(0x71)
    scheduler.fire()
    keys.down.clear()
    scheduler.fire()
    keys.down.add(0x70)
    scheduler.fire()
    scheduler.fire()

    assert toggles == ["sync", "reconnect", "auto_click"]


def test_unset_or_invalid_hotkey_never_toggles() -> None:
    scheduler = Scheduler()
    keys = Keys()
    toggles = []
    monitor = FeatureHotkeyMonitor(
        {"sync": lambda: toggles.append("sync")},
        hotkeys_provider=lambda: {"sync": "not-a-key"},
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()
    keys.down.add(0x1B)
    scheduler.fire()

    assert toggles == []
    assert normalize_feature_hotkey("f12") == "F12"
    assert normalize_feature_hotkey("not-a-key") == ""


def test_standard_windows_keyboard_and_mouse_hotkeys_are_distinct() -> None:
    assert normalize_feature_hotkey("mouseRight") == "RBUTTON"
    assert normalize_feature_hotkey("escape") == "ESC"
    assert normalize_feature_hotkey("spacebar") == "SPACE"
    assert normalize_feature_hotkey("arrowDown") == "DOWN"
    assert normalize_feature_hotkey("f24") == "F24"
    assert feature_hotkey_virtual_key("1") == 0x31
    assert feature_hotkey_virtual_key("NUMPAD1") == 0x61
    assert feature_hotkey_virtual_key("VK_E8") == 0xE8
    assert normalize_feature_hotkey("VK_00") == ""


def test_right_mouse_and_numpad_hotkeys_toggle_independently() -> None:
    scheduler = Scheduler()
    keys = Keys()
    toggles = []
    hotkeys = {"sync": "RBUTTON", "reconnect": "NUMPAD1"}
    monitor = FeatureHotkeyMonitor(
        {
            "sync": lambda: toggles.append("sync"),
            "reconnect": lambda: toggles.append("reconnect"),
        },
        hotkeys_provider=lambda: hotkeys,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()

    keys.down.add(0x02)
    scheduler.fire()
    keys.down.clear()
    scheduler.fire()
    keys.down.add(0x61)
    scheduler.fire()

    assert toggles == ["sync", "reconnect"]


def test_group_launch_hotkey_uses_group_mapping_and_rising_edge() -> None:
    scheduler = Scheduler()
    keys = Keys()
    launched = []
    hotkeys = {"14支": "F3", "120": "XBUTTON2"}
    monitor = GroupLaunchHotkeyMonitor(
        launched.append,
        hotkeys_provider=lambda: hotkeys,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()

    keys.down.add(0x72)
    scheduler.fire()
    scheduler.fire()
    keys.down.clear()
    scheduler.fire()
    keys.down.add(0x06)
    scheduler.fire()

    assert launched == ["14支", "120"]


def test_removed_group_hotkey_does_not_keep_stale_pressed_state() -> None:
    scheduler = Scheduler()
    keys = Keys()
    launched = []
    hotkeys = {"14支": "F3"}
    monitor = GroupLaunchHotkeyMonitor(
        launched.append,
        hotkeys_provider=lambda: dict(hotkeys),
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()
    keys.down.add(0x72)
    scheduler.fire()
    hotkeys.clear()
    scheduler.fire()
    hotkeys["14支"] = "F3"
    scheduler.fire()

    assert launched == ["14支", "14支"]
