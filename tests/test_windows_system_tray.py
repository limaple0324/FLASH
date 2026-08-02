from pathlib import Path

from adapters.windows_system_tray import (
    SystemTrayController,
    SystemTrayEvent,
    WindowsSystemTrayBackend,
)


class FakeTrayBackend:
    def __init__(self, *, stop_result=True):
        self.started = []
        self.events = []
        self.stopped = 0
        self.stop_timeouts = []
        self.stop_result = stop_result

    def start(self, icon_path, tooltip):
        self.started.append((Path(icon_path), tooltip))
        return True

    def poll_events(self):
        events = tuple(self.events)
        self.events.clear()
        return events

    def stop(self, timeout_seconds=2.0):
        self.stopped += 1
        self.stop_timeouts.append(timeout_seconds)
        return self.stop_result


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
    exited = []
    controller = SystemTrayController(
        window,
        backend,
        icon_path=icon,
        tooltip="輔｜14支｜同步安全停止",
        on_stop_all=lambda: True,
        on_exit=lambda: exited.append(True),
    )

    assert controller.start() is True
    assert backend.started == [(icon, "輔｜14支｜同步安全停止")]
    window.window_state = "iconic"
    controller.handle_unmap()
    assert window.withdrawn == 1

    backend.events.append(SystemTrayEvent.RESTORE)
    controller.poll()

    assert window.deiconified == 1
    assert window.window_state == "normal"
    assert window.lifted == 1
    assert window.focused == 1
    assert exited == []


def test_tray_menu_show_hide_restore_and_stop_all_share_state(
    tmp_path,
):
    icon = tmp_path / "flash_icon.ico"
    icon.write_bytes(b"icon")
    window = FakeWindow()
    backend = FakeTrayBackend()
    stop_all_calls = []
    controller = SystemTrayController(
        window,
        backend,
        icon_path=icon,
        tooltip="輔",
        on_stop_all=lambda: stop_all_calls.append(True) or True,
        on_exit=lambda: True,
    )
    controller.start()

    backend.events.extend(
        (
            SystemTrayEvent.HIDE,
            SystemTrayEvent.SHOW,
            SystemTrayEvent.RESTORE,
            SystemTrayEvent.STOP_ALL,
        )
    )
    controller.poll()

    assert window.withdrawn == 1
    assert window.deiconified == 2
    assert window.lifted == 2
    assert window.focused == 2
    assert controller.window_visible is True
    assert stop_all_calls == [True]
    assert controller.operations_stopped is True
    controller.mark_operations_running()
    assert controller.operations_stopped is False


def test_tray_complete_exit_uses_normal_exit_callback_and_removes_icon(
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
        on_stop_all=lambda: True,
        on_exit=lambda: closed.append(True) or True,
    )
    controller.start()
    first_poll = window.after_calls[0][0]
    backend.events.append(SystemTrayEvent.EXIT)

    controller.poll()
    controller.stop()

    assert closed == [True]
    assert first_poll not in window.cancelled
    assert window.cancelled == [window.after_calls[-1][0]]
    assert backend.stopped == 1
    assert backend.stop_timeouts == [2.0]


def test_tray_stop_timeout_keeps_real_running_state_and_polling(tmp_path):
    icon = tmp_path / "flash_icon.ico"
    icon.write_bytes(b"icon")
    window = FakeWindow()
    backend = FakeTrayBackend(stop_result=False)
    controller = SystemTrayController(
        window,
        backend,
        icon_path=icon,
        tooltip="輔",
        on_stop_all=lambda: True,
        on_exit=lambda: True,
    )
    controller.start()

    assert controller.stop(timeout_seconds=0) is False
    assert controller.running is True
    assert backend.stop_timeouts == [0]
    assert window.after_calls[-1][1] == 100


def test_native_tray_stop_timeout_keeps_live_thread_reference():
    class LiveThread:
        def __init__(self):
            self.alive = True
            self.join_timeouts = []

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.join_timeouts.append(timeout)

    backend = WindowsSystemTrayBackend()
    worker = LiveThread()
    backend._thread = worker

    assert backend.stop(timeout_seconds=0) is False
    assert backend._thread is worker
    assert worker.join_timeouts == [0]

    worker.alive = False
    assert backend.stop(timeout_seconds=1) is True
    assert backend._thread is None


def test_failed_exit_callback_keeps_controller_available(tmp_path):
    icon = tmp_path / "flash_icon.ico"
    icon.write_bytes(b"icon")
    window = FakeWindow()
    backend = FakeTrayBackend()
    controller = SystemTrayController(
        window,
        backend,
        icon_path=icon,
        tooltip="輔",
        on_stop_all=lambda: True,
        on_exit=lambda: False,
    )
    controller.start()
    backend.events.append(SystemTrayEvent.EXIT)

    controller.poll()

    assert controller.running is True
    assert controller.exiting is False
    assert window.after_calls[-1][1] == 100


def test_native_tray_menu_contains_all_confirmed_actions():
    source = Path("adapters/windows_system_tray.py").read_text(
        encoding="utf-8"
    )

    for label in (
        "顯示主程式",
        "隱藏主視窗",
        "恢復主視窗",
        "停止全部",
        "完全關閉程式",
    ):
        assert label in source
