from collections import Counter

from adapters.windows_timed_click import (
    Win32LegacySyncStatusProvider,
    WindowsTimedClickBackend,
    legacy_sync_group_from_title,
)
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
        thread_id=200 + handle,
        process_lifecycle_token=300 + handle,
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


def test_active_sync_sends_each_timed_press_to_fourteen_windows_once():
    fingerprints = tuple(f"{index:064x}" for index in range(1, 15))
    windows = tuple(
        make_window(10 + index, fingerprint)
        for index, fingerprint in enumerate(fingerprints)
    )
    messages = Messages()
    backend = WindowsTimedClickBackend(
        Windows([windows[0]]),
        messages,
        point_reader=Points(),
        synchronized_windows_provider=lambda: windows,
        synchronization_active_provider=lambda: True,
    )
    target = TimedClickTarget(fingerprints[0], 0.25, 0.75)

    receipt = backend.press(target, fingerprints)
    released = backend.release(receipt)

    assert released is True
    assert receipt.handles == tuple(window.handle for window in windows)
    delivered = Counter((handle, event) for handle, _x, _y, event in messages.sent)
    for window in windows:
        assert delivered[(window.handle, "left_down")] == 1
        assert delivered[(window.handle, "move")] == 1
        assert delivered[(window.handle, "left_up")] == 1
    assert len(messages.sent) == 14 * 3
    assert messages.responsiveness_timeouts == [1_000] * 14


def test_inactive_sync_preserves_single_window_timed_click():
    messages = Messages()
    backend = WindowsTimedClickBackend(
        Windows([make_window()]),
        messages,
        point_reader=Points(),
        synchronized_windows_provider=lambda: (),
        synchronization_active_provider=lambda: None,
    )
    target = TimedClickTarget(FINGERPRINT, 0.5, 0.5)

    receipt = backend.press(target, (FINGERPRINT,))
    backend.release(receipt)

    assert receipt.handles == (10,)
    assert [item[3] for item in messages.sent] == [
        "left_down",
        "move",
        "left_up",
    ]


def test_sync_mismatch_or_incomplete_group_fails_without_input():
    second_fingerprint = "b" * 64
    complete = (
        make_window(10, FINGERPRINT),
        make_window(11, second_fingerprint),
    )
    target = TimedClickTarget(FINGERPRINT, 0.5, 0.5)

    mismatched_messages = Messages()
    mismatched = WindowsTimedClickBackend(
        Windows([complete[0]]),
        mismatched_messages,
        synchronized_windows_provider=lambda: complete,
        synchronization_active_provider=lambda: False,
    )
    assert mismatched.press(target, (FINGERPRINT, second_fingerprint)) is None
    assert mismatched_messages.sent == []

    incomplete_messages = Messages()
    incomplete = WindowsTimedClickBackend(
        Windows([complete[0]]),
        incomplete_messages,
        synchronized_windows_provider=lambda: (
            complete[0],
            WindowInfo(
                handle=11,
                title="Adobe Flash Player 11",
                visible=True,
                minimized=False,
                rect=(0, 0, 900, 600),
                process_id=111,
                window_class="ShockwaveFlash",
                launch_fingerprint=second_fingerprint,
            ),
        ),
        synchronization_active_provider=lambda: True,
    )
    assert incomplete.press(target, (FINGERPRINT, second_fingerprint)) is None
    assert incomplete_messages.sent == []


def test_legacy_sync_status_accepts_only_verified_running_legacy_window():
    legacy_window = WindowInfo(
        handle=50,
        title="輔V0.2 - 魔心次元組 - 同步中",
        visible=True,
        minimized=False,
        rect=(0, 0, 500, 500),
        process_id=500,
        window_class="TkTopLevel",
    )
    provider = Win32LegacySyncStatusProvider(
        Windows(
            [
                legacy_window,
                WindowInfo(
                    handle=51,
                    title="輔V0.2 - 其他組 - 未開啟",
                    visible=True,
                    minimized=False,
                    rect=(0, 0, 500, 500),
                    process_id=501,
                    window_class="TkTopLevel",
                ),
                WindowInfo(
                    handle=52,
                    title="輔V0.2 - 假組 - 同步中",
                    visible=True,
                    minimized=False,
                    rect=(0, 0, 500, 500),
                    process_id=502,
                    window_class="OtherClass",
                ),
            ]
        ),
        process_path_provider=lambda process_id: (
            r"C:\old\輔V0.2.exe"
            if process_id == 500
            else r"C:\old\other.exe"
        ),
    )

    assert legacy_sync_group_from_title(legacy_window.title) == "魔心次元組"
    assert legacy_sync_group_from_title("輔V0.2 - 魔心次元組 - 未開啟") is None
    assert provider.active_group_names() == ("魔心次元組",)
