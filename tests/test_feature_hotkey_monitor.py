from services.feature_hotkey_monitor import (
    FeatureHotkeyMonitor,
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

    assert toggles == ["sync", "reconnect"]


def test_unset_or_invalid_hotkey_never_toggles() -> None:
    scheduler = Scheduler()
    keys = Keys()
    toggles = []
    monitor = FeatureHotkeyMonitor(
        {"sync": lambda: toggles.append("sync")},
        hotkeys_provider=lambda: {"sync": "ESC"},
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        state_backend=keys,
    )
    monitor.start()
    keys.down.add(0x1B)
    scheduler.fire()

    assert toggles == []
    assert normalize_feature_hotkey("f12") == "F12"
    assert normalize_feature_hotkey("escape") == ""
