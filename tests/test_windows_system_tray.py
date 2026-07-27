from pathlib import Path

from adapters.windows_system_tray import (
    SystemTrayController,
    SystemTrayEvent,
)


class FakeTrayBackend:
    def __init__(self):
        self.started = []
        self.events = []
        self.stopped = 0

    def start(self, icon_path, tooltip):
        self.started.append((Path(icon_path), tooltip))
        return True

    def poll_events(self):
        events = tuple(self.events)
        self.events.clear()
        return events

    def stop(self):
        self.stopped += 1


class FakeWindow:
    def __init__(self):
        self.window_state = "normal"
        self.withdrawn = 0
        self.deiconified = 0
        self.lifted = 0
        self.focused = 0
        self.after_calls = []
        self.cancelled = []

    def after(self, delay_ms, callback):
        token = f"after-{len(self.after_calls) + 1}"
        self.after_calls.append((token, delay_ms, callback))
        return token

    def after_idle(self, callback):
        callback()

    def after_cancel(self, token):
        self.cancelled.append(token)

    def state(self, value=None):
        if value is not None:
            self.window_state = value
        return self.window_state

    def withdraw(self):
        self.withdrawn += 1
        self.window_state = "withdrawn"

    def deiconify(self):
        self.deiconified += 1
        self.window_state = "normal"

    def lift(self):
        self.lifted += 1

    def focus_force(self):
        self.focused += 1


def test_minimize_hides_to_tray_and_show_event_restores_exact_window(
    tmp_path,
):
    icon = tmp_path / "flash_icon.ico"
    icon.write_bytes(b"icon")
    window = FakeWindow()
    backend = FakeTrayBackend()
    closed = []
    controller = SystemTrayController(
        window,
        backend,
        icon_path=icon,
        tooltip="輔｜14支｜同步安全停止",
        on_close=lambda: closed.append(True),
    )

    assert controller.start() is True
    assert backend.started == [(icon, "輔｜14支｜同步安全停止")]
    window.window_state = "iconic"
    controller.handle_unmap()
    assert window.withdrawn == 1

    backend.events.append(SystemTrayEvent.SHOW)
    controller.poll()

    assert window.deiconified == 1
    assert window.window_state == "normal"
    assert window.lifted == 1
    assert window.focused == 1
    assert closed == []


def test_tray_close_uses_normal_close_callback_and_stop_removes_icon(
    tmp_path,
):
    icon = tmp_path / "flash_icon.ico"
    icon.write_bytes(b"icon")
    window = FakeWindow()
    backend = FakeTrayBackend()
    closed = []
    controller = SystemTrayController(
        window,
        backend,
        icon_path=icon,
        tooltip="輔",
        on_close=lambda: closed.append(True),
    )
    controller.start()
    first_poll = window.after_calls[0][0]
    backend.events.append(SystemTrayEvent.CLOSE)

    controller.poll()
    controller.stop()

    assert closed == [True]
    assert first_poll not in window.cancelled
    assert window.cancelled == [window.after_calls[-1][0]]
    assert backend.stopped == 1
