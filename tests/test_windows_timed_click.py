import ast
from collections import Counter
from dataclasses import replace
from pathlib import Path
from time import sleep

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
    def __init__(self, *, after_probe=None, after_send=None):
        self.sent = []
        self.responsiveness_timeouts = []
        self.after_probe = after_probe
        self.after_send = after_send

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, timeout):
        self.responsiveness_timeouts.append(timeout)
        if self.after_probe is not None:
            self.after_probe()
        return True

    def send_pointer(self, handle, x_ratio, y_ratio, event):
        self.sent.append((handle, x_ratio, y_ratio, event))
        if self.after_send is not None:
            self.after_send(event)
        return True


class Markers:
    def __init__(self, *, fail_handles=()):
        self.fail_handles = set(fail_handles)
        self.shown = []
        self.erased = []

    def draw(self, window, target):
        self.shown.append((window.handle, target.x_ratio, target.y_ratio))
        if window.handle in self.fail_handles:
            return None
        return window.handle

    def erase(self, token):
        self.erased.append(token)
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


def test_preflight_then_reused_handle_never_receives_input():
    windows = Windows([make_window()])
    messages = Messages(
        after_probe=lambda: windows.windows.__setitem__(
            0,
            make_window(10, "b" * 64),
        )
    )
    backend = WindowsTimedClickBackend(windows, messages)

    assert backend.press(
        TimedClickTarget(FINGERPRINT, 0.5, 0.5),
        (FINGERPRINT,),
    ) is None
    assert messages.sent == []


def test_preflight_then_process_id_change_never_receives_input():
    original = make_window()
    windows = Windows([original])
    messages = Messages(
        after_probe=lambda: windows.windows.__setitem__(
            0,
            replace(original, process_id=original.process_id + 1),
        )
    )
    backend = WindowsTimedClickBackend(windows, messages)

    assert backend.press(
        TimedClickTarget(FINGERPRINT, 0.5, 0.5),
        (FINGERPRINT,),
    ) is None
    assert messages.sent == []


def test_preflight_then_thread_id_change_never_receives_input():
    original = make_window()
    windows = Windows([original])
    messages = Messages(
        after_probe=lambda: windows.windows.__setitem__(
            0,
            replace(original, thread_id=original.thread_id + 1),
        )
    )
    backend = WindowsTimedClickBackend(windows, messages)

    assert backend.press(
        TimedClickTarget(FINGERPRINT, 0.5, 0.5),
        (FINGERPRINT,),
    ) is None
    assert messages.sent == []


def test_preflight_then_lifecycle_change_never_receives_input():
    original = make_window()
    windows = Windows([original])
    messages = Messages(
        after_probe=lambda: windows.windows.__setitem__(
            0,
            replace(
                original,
                process_lifecycle_token=(
                    original.process_lifecycle_token + 1
                ),
            ),
        )
    )
    backend = WindowsTimedClickBackend(windows, messages)

    assert backend.press(
        TimedClickTarget(FINGERPRINT, 0.5, 0.5),
        (FINGERPRINT,),
    ) is None
    assert messages.sent == []


def test_instance_change_after_down_never_moves_or_releases_new_instance():
    original = make_window()
    windows = Windows([original])

    def replace_after_down(event):
        if event == "left_down":
            windows.windows[0] = replace(
                original,
                process_id=original.process_id + 1,
            )

    messages = Messages(after_send=replace_after_down)
    backend = WindowsTimedClickBackend(windows, messages)

    assert backend.press(
        TimedClickTarget(FINGERPRINT, 0.5, 0.5),
        (FINGERPRINT,),
    ) is None
    assert [item[3] for item in messages.sent] == ["left_down"]


def test_release_rechecks_original_complete_instance():
    original = make_window()
    windows = Windows([original])
    messages = Messages()
    backend = WindowsTimedClickBackend(windows, messages)
    target = TimedClickTarget(FINGERPRINT, 0.5, 0.5)

    receipt = backend.press(target, (FINGERPRINT,))
    windows.windows[0] = replace(
        original,
        process_lifecycle_token=original.process_lifecycle_token + 1,
    )

    assert backend.release(receipt) is False
    assert [item[3] for item in messages.sent] == ["left_down", "move"]


def test_active_sync_sends_each_timed_press_to_fourteen_windows_once():
    fingerprints = tuple(f"{index:064x}" for index in range(1, 15))
    windows = tuple(
        make_window(10 + index, fingerprint)
        for index, fingerprint in enumerate(fingerprints)
    )
    messages = Messages()
    backend = WindowsTimedClickBackend(
        Windows(windows),
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


def test_marker_requests_all_fourteen_sync_windows_without_pointer_input():
    fingerprints = tuple(f"{index:064x}" for index in range(1, 15))
    windows = tuple(
        make_window(10 + index, fingerprint)
        for index, fingerprint in enumerate(fingerprints)
    )
    messages = Messages()
    markers = Markers()
    backend = WindowsTimedClickBackend(
        Windows(windows),
        messages,
        marker_backend=markers,
        synchronized_windows_provider=lambda: windows,
    )
    target = TimedClickTarget(fingerprints[0], 0.25, 0.75)

    assert backend.show_target_markers(target, fingerprints) is True
    assert markers.shown == [
        (window.handle, 0.25, 0.75) for window in windows
    ]
    assert messages.sent == []


def test_marker_identity_incomplete_fails_closed_without_pointer_input():
    windows = list(make_window(10 + index, f"{index + 1:064x}") for index in range(3))
    windows[1] = replace(windows[1], thread_id=None)
    messages = Messages()
    markers = Markers()
    backend = WindowsTimedClickBackend(
        Windows(windows),
        messages,
        marker_backend=markers,
        synchronized_windows_provider=lambda: tuple(windows),
    )
    fingerprints = tuple(window.launch_fingerprint for window in windows)

    assert backend.show_target_markers(
        TimedClickTarget(fingerprints[0], 0.5, 0.5),
        fingerprints,
    ) is False
    assert markers.shown == []
    assert messages.sent == []


def test_any_initial_marker_failure_erases_partial_markers_and_fails_closed():
    fingerprints = tuple(f"{index:064x}" for index in range(1, 5))
    windows = tuple(
        make_window(10 + index, fingerprint)
        for index, fingerprint in enumerate(fingerprints)
    )
    messages = Messages()
    markers = Markers(fail_handles={12})
    backend = WindowsTimedClickBackend(
        Windows(windows),
        messages,
        marker_backend=markers,
        synchronized_windows_provider=lambda: windows,
    )

    assert backend.show_target_markers(
        TimedClickTarget(fingerprints[0], 0.5, 0.5),
        fingerprints,
    ) is False
    assert set(markers.erased) == {10, 11, 13}
    assert messages.sent == []


def test_marker_cleanup_does_not_touch_a_reused_handle():
    original = make_window(10, FINGERPRINT)
    replacement = replace(original, process_id=original.process_id + 1)
    live_windows = Windows([original])

    class ReusingMarkers(Markers):
        def draw(self, window, target):
            token = super().draw(window, target)
            live_windows.windows[0] = replacement
            return token

    markers = ReusingMarkers()
    backend = WindowsTimedClickBackend(
        live_windows,
        Messages(),
        marker_backend=markers,
        synchronized_windows_provider=lambda: tuple(live_windows.windows),
    )

    assert backend.show_target_markers(
        TimedClickTarget(FINGERPRINT, 0.5, 0.5),
        (FINGERPRINT,),
        duration_seconds=0.01,
    ) is True
    sleep(0.05)
    assert markers.erased == []


def test_main_wires_native_marker_and_marker_failure_gate():
    source = (Path(__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "marker_backend=Win32FocusMarkerBackend()" in source
    assert "show_target_markers(" in source
    assert '"target_marker_failed"' in source


def test_marker_preview_failure_does_not_clear_captured_target():
    source = (Path(__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    capture = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "capture_timed_click_target"
    )
    clear_calls = [
        node
        for node in ast.walk(capture)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear_target"
    ]
    assert clear_calls == []
    assert '"按鈕位置已設定，但定位預覽未能完整顯示。"' in source


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
