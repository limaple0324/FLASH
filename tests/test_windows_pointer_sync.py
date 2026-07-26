from adapters.windows_input_sync import WindowInputPolicy
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_window import WindowInfo
from services.deferred_sync_operation_service import (
    DeferredSyncOperationService,
)


def _window(handle, *, minimized=False):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=minimized,
        rect=(0, 0, 900, 600),
        process_id=100 + handle,
        window_class="Flash",
        launch_fingerprint=f"{handle:064x}",
    )


class Windows:
    def __init__(self, windows, foreground=1):
        self.windows = windows
        self.foreground = foreground

    def list_windows(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.foreground


class Messages:
    def __init__(self):
        self.sent = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout):
        return True

    def send_pointer(self, handle, x, y, event):
        self.sent.append((handle, x, y, event))
        return True


def _controller(windows, messages):
    controller = WindowsPointerSyncController(
        expected_windows=len(windows),
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows),
        message_backend=messages,
    )
    controller.set_allowed_fingerprints(
        window.launch_fingerprint for window in windows
    )
    return controller


def test_background_policy_mirrors_left_click_except_source_and_minimized():
    windows = [_window(1), _window(2), _window(3, minimized=True)]
    messages = Messages()

    result = _controller(windows, messages).send(
        source_handle=1,
        x_ratio=0.25,
        y_ratio=0.75,
        event="left_down",
        policy=WindowInputPolicy.FOREGROUND_BACKGROUND,
    )

    assert result.passed is True
    assert messages.sent == [(2, 0.25, 0.75, "left_down")]


def test_all_policy_includes_minimized_and_deduplicated_recursive_scope():
    windows = [_window(1), _window(2), _window(3, minimized=True)]
    messages = Messages()

    result = _controller(windows, messages).send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_up",
        policy=WindowInputPolicy.ALL,
    )

    assert result.passed is True
    assert [item[0] for item in messages.sent] == [2, 3]


def test_identity_mismatch_sends_nothing():
    windows = [_window(1), _window(2)]
    messages = Messages()
    controller = _controller(windows, messages)
    controller.set_expected_windows(3)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert "window_count_mismatch" in result.failure_codes
    assert messages.sent == []


def test_one_reconnecting_role_pauses_pointer_sync_for_entire_group():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows),
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: (windows[2].launch_fingerprint,),
    )
    controller.set_allowed_fingerprints(
        window.launch_fingerprint for window in windows
    )

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert messages.sent == []
    assert deferred.pending() == 2
