from adapters.windows_timed_click import WindowsTimedClickBackend
from adapters.windows_window import WindowInfo
from services.game_time_timed_click_service import TimedClickTarget


FINGERPRINT = "a" * 64


def make_window(handle=10, fingerprint=FINGERPRINT):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 900, 600),
        process_id=100 + handle,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
    )


class Windows:
    def __init__(self, windows, top_handle=10):
        self.windows = list(windows)
        self.top_handle = top_handle

    def list_windows(self):
        return list(self.windows)

    def top_window_at(self, _x, _y):
        return self.top_handle


class Points:
    def screen_position(self):
        return 450, 300

    def read(self, handle, screen_position=None):
        assert handle == 10
        assert screen_position == (450, 300)
        return 0.5, 0.5


class Messages:
    def __init__(self):
        self.sent = []
        self.responsiveness_timeouts = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, timeout):
        self.responsiveness_timeouts.append(timeout)
        return True

    def send_pointer(self, handle, x_ratio, y_ratio, event):
        self.sent.append((handle, x_ratio, y_ratio, event))
        return True


def test_capture_and_click_stay_on_the_same_unique_fingerprint():
    messages = Messages()
    backend = WindowsTimedClickBackend(
        Windows([make_window()]),
        messages,
        point_reader=Points(),
    )

    target = backend.capture_target((FINGERPRINT,))
    receipt = backend.press(target, (FINGERPRINT,))
    released = backend.release(receipt)

    assert target == TimedClickTarget(
        FINGERPRINT,
        0.5,
        0.5,
        "Adobe Flash Player 11",
    )
    assert released is True
    assert [item[3] for item in messages.sent] == [
        "left_down",
        "move",
        "left_up",
    ]
    assert messages.responsiveness_timeouts == [1_000]


def test_duplicate_or_changed_identity_never_receives_input():
    messages = Messages()
    backend = WindowsTimedClickBackend(
        Windows([make_window(), make_window(11)]),
        messages,
        point_reader=Points(),
    )
    target = TimedClickTarget(FINGERPRINT, 0.5, 0.5)

    assert backend.capture_target((FINGERPRINT,)) is None
    assert backend.press(target, ("b" * 64,)) is None
    assert messages.sent == []
