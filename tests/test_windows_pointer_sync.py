import json
from dataclasses import replace
from threading import Event

from adapters.windows_input_sync import WindowInputPolicy
from adapters.windows_pointer_sync import (
    Win32PointerMessageBackend,
    WindowsPointerSyncController,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from domain.sync_target_settings import SyncTargetSettings
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
        self.probed = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout):
        self.probed.append((_handle, _timeout))
        return True

    def send_pointer(self, handle, x, y, event):
        self.sent.append((handle, x, y, event))
        return True


class AdjustedMessages(Messages):
    def __init__(self):
        super().__init__()
        self.adjusted = []
        self.completed = Event()

    def send_pointer_adjusted(
        self,
        handle,
        x,
        y,
        event,
        offset_x,
        offset_y,
    ):
        self.adjusted.append(
            (handle, x, y, event, offset_x, offset_y)
        )
        self.completed.set()
        return True


class Win32Function:
    def __init__(self, callback):
        self._callback = callback

    def __call__(self, *args):
        return self._callback(*args)


class Win32PointerApi:
    def __init__(self):
        self.messages = []
        self.GetClientRect = Win32Function(self._get_client_rect)
        self.IsWindow = Win32Function(lambda _handle: True)
        self.SendMessageTimeoutW = Win32Function(self._send_message)

    @staticmethod
    def _get_client_rect(_handle, pointer):
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = (0, 0, 900, 600)
        return True

    def _send_message(
        self,
        handle,
        message,
        wparam,
        lparam,
        flags,
        timeout,
        result_pointer,
    ):
        self.messages.append(
            (handle, message, wparam, lparam, flags, timeout)
        )
        result_pointer._obj.value = 1
        return True

def _controller(windows, messages):
    controller = WindowsPointerSyncController(
        expected_windows=len(windows),
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows),
        message_backend=messages,
    )
    allowed = tuple(window.launch_fingerprint for window in windows)
    controller.set_allowed_fingerprints(allowed)
    controller.set_controller_fingerprint(allowed[0])
    return controller


def test_win32_left_down_moves_before_press_and_confirms_delivery(monkeypatch):
    api = Win32PointerApi()
    backend = Win32PointerMessageBackend()
    monkeypatch.setattr(backend, "_user32", lambda: api)

    assert backend.send_pointer(123, 0.5, 0.25, "left_down") is True

    assert [item[1] for item in api.messages] == [
        backend.WM_MOUSEMOVE,
        backend.WM_LBUTTONDOWN,
    ]
    assert [item[2] for item in api.messages] == [0, backend.MK_LBUTTON]
    assert all(
        item[4]
        == (
            backend.SMTO_BLOCK
            | backend.SMTO_ABORTIFHUNG
            | backend.SMTO_ERRORONEXIT
        )
        and item[5] == backend.MESSAGE_TIMEOUT_MS
        for item in api.messages
    )


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


def test_fourteen_window_left_sync_sends_once_to_each_non_controller():
    windows = [_window(handle) for handle in range(1, 15)]
    messages = Messages()

    result = _controller(windows, messages).send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_up",
        policy=WindowInputPolicy.ALL,
    )

    assert result.passed is True
    assert [item[0] for item in messages.sent] == list(range(2, 15))


def test_partial_group_click_skips_login_screen_and_keeps_group_scope():
    windows = [_window(1), _window(2)]
    messages = Messages()
    allowed = tuple(f"{handle:064x}" for handle in range(1, 4))
    states = {
        allowed[0]: ReconnectScreenState.CONNECTED,
        allowed[1]: ReconnectScreenState.CONNECTED,
        allowed[2]: ReconnectScreenState.LOGIN_START,
    }
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=messages,
        screen_state_provider=states.get,
        require_expected_window_count=False,
    )
    controller.set_allowed_fingerprints(allowed)
    controller.set_controller_fingerprint(allowed[0])

    result = controller.send_click(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        policy=WindowInputPolicy.ALL,
        include_source=False,
    )

    assert result.passed is True
    assert result.discovered_windows == 2
    assert result.eligible_windows == 1
    assert [item[0] for item in messages.sent] == [2, 2]
    assert controller.source_is_eligible(1) is True


def test_partial_group_login_source_cannot_start_click_sync():
    windows = [_window(1), _window(2)]
    messages = Messages()
    allowed = tuple(f"{handle:064x}" for handle in range(1, 4))
    states = {
        allowed[0]: ReconnectScreenState.LOGIN_START,
        allowed[1]: ReconnectScreenState.CONNECTED,
    }
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=messages,
        screen_state_provider=states.get,
        require_expected_window_count=False,
    )
    controller.set_allowed_fingerprints(allowed)
    controller.set_controller_fingerprint(allowed[0])

    result = controller.send_click(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        policy=WindowInputPolicy.ALL,
        include_source=False,
    )

    assert result.failure_codes == ("source_not_in_game",)
    assert messages.sent == []
    assert controller.source_is_eligible(1) is False


def test_send_click_uses_configured_order_and_one_preflight():
    windows = [_window(2), _window(3), _window(1)]
    messages = Messages()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=messages,
    )
    controller.set_allowed_fingerprints(
        (
            f"{3:064x}",
            f"{1:064x}",
            f"{2:064x}",
        )
    )

    result = controller.send_click(
        source_handle=1,
        x_ratio=0.25,
        y_ratio=0.75,
        policy=WindowInputPolicy.ALL,
    )

    assert result.passed is True
    assert result.event == "click"
    assert [item[0] for item in messages.sent] == [3, 3, 1, 1, 2, 2]
    assert [item[3] for item in messages.sent] == [
        "left_down",
        "left_up",
        "left_down",
        "left_up",
        "left_down",
        "left_up",
    ]
    assert len(messages.probed) == 3


def test_pointer_result_contains_nonnegative_safe_timings():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()

    result = _controller(windows, messages).send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_up",
        policy=WindowInputPolicy.ALL,
    )
    report = result.to_dict()

    assert result.controller_elapsed_ns >= 0
    assert result.preflight_elapsed_ns >= 0
    assert result.dispatch_spread_ns >= 0
    assert report["controller_elapsed_ns"] == result.controller_elapsed_ns
    assert report["preflight_elapsed_ns"] == result.preflight_elapsed_ns
    assert report["dispatch_spread_ns"] == result.dispatch_spread_ns
    assert report["timing_scope"] == "controller_postmessage_scheduling_only"
    assert report["game_receipt_verified"] is False
    assert "handle" not in json.dumps(report)


def test_execution_guard_releases_partial_left_down_batch():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    controller = _controller(windows, messages)
    decisions = iter((True, False))

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
        execution_guard=lambda: next(decisions, False),
    )

    assert result.sent_windows == 1
    assert "execution_stopped" in result.failure_codes
    assert messages.sent == [
        (2, 0.5, 0.5, "left_down"),
        (2, 0.5, 0.5, "left_up"),
    ]
    assert controller.release_pressed_targets() == 0


def test_per_role_pointer_offset_and_delay_are_applied_independently():
    windows = [_window(1), _window(2)]
    messages = AdjustedMessages()
    controller = _controller(windows, messages)
    target_fingerprint = windows[1].launch_fingerprint
    controller.set_target_settings(
        {
            target_fingerprint: SyncTargetSettings(
                offset_enabled=True,
                offset_x=12,
                offset_y=-7,
                delay_ms=20,
            )
        }
    )
    try:
        result = controller.send(
            source_handle=1,
            x_ratio=0.25,
            y_ratio=0.75,
            event="left_up",
            policy=WindowInputPolicy.ALL,
        )

        assert result.passed is True
        assert result.sent_windows == 0
        assert result.scheduled_windows == 1
        assert messages.completed.wait(0.5)
        assert messages.adjusted == [
            (2, 0.25, 0.75, "left_up", 12, -7)
        ]
    finally:
        controller._dispatch_scheduler.close()


def test_release_pressed_targets_uses_latest_successful_move_position():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    controller = _controller(windows, messages)
    controller.send(
        source_handle=1,
        x_ratio=0.25,
        y_ratio=0.75,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )
    controller.send(
        source_handle=1,
        x_ratio=0.6,
        y_ratio=0.4,
        event="move",
        policy=WindowInputPolicy.ALL,
    )

    assert controller.release_pressed_targets() == 2
    assert messages.sent[-2:] == [
        (2, 0.6, 0.4, "left_up"),
        (3, 0.6, 0.4, "left_up"),
    ]
    assert controller.release_pressed_targets() == 0


def test_release_pressed_targets_keeps_failed_release_for_retry():
    class RejectingReleaseMessages(Messages):
        def __init__(self):
            super().__init__()
            self.reject_release = True

        def send_pointer(self, handle, x, y, event):
            self.sent.append((handle, x, y, event))
            return event != "left_up" or not self.reject_release

    windows = [_window(1), _window(2)]
    messages = RejectingReleaseMessages()
    controller = _controller(windows, messages)
    controller.send(
        source_handle=1,
        x_ratio=0.25,
        y_ratio=0.75,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert controller.release_pressed_targets() == 0
    messages.reject_release = False
    assert controller.release_pressed_targets() == 1
    assert controller.release_pressed_targets() == 0
    assert messages.sent == [
        (2, 0.25, 0.75, "left_down"),
        (2, 0.25, 0.75, "left_up"),
        (2, 0.25, 0.75, "left_up"),
    ]


def test_source_eligibility_requires_complete_foreground_group():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    controller = _controller(windows, messages)

    assert controller.source_is_eligible(1) is True
    assert controller.source_is_eligible(2) is False
    controller.set_expected_windows(4)
    assert controller.source_is_eligible(1) is False


def test_only_configured_controller_can_start_pointer_sync():
    windows = [_window(1), _window(2), _window(3)]
    backend = Windows(windows, foreground=2)
    messages = Messages()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
    )
    controller.set_allowed_fingerprints(
        window.launch_fingerprint for window in windows
    )
    controller.set_controller_fingerprint(windows[0].launch_fingerprint)

    assert controller.source_is_eligible(2) is False
    result = controller.send(
        source_handle=2,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert result.failure_codes == ("source_not_controller",)
    assert messages.sent == []


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


def test_reconnecting_pointer_preserves_background_policy_eligibility(tmp_path):
    windows = [_window(1), _window(2, minimized=True), _window(3)]
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows),
        message_backend=Messages(),
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
        policy=WindowInputPolicy.FOREGROUND_BACKGROUND,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 1
    assert saved["items"][0]["target_id"] == windows[2].launch_fingerprint
    assert saved["items"][0]["payload"]["policy"] == (
        WindowInputPolicy.FOREGROUND_BACKGROUND.value
    )


def test_closed_reconnecting_role_is_deferred_for_pointer_all_policy(tmp_path):
    windows = [_window(1), _window(2)]
    allowed = (f"{1:064x}", f"{2:064x}", f"{3:064x}")
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )
    controller.set_allowed_fingerprints(allowed)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert result.eligible_windows == 2
    assert {item["target_id"] for item in saved["items"]} == {
        allowed[1],
        allowed[2],
    }


def test_closed_reconnecting_pointer_foreground_only_defers_only_source():
    windows = [_window(1)]
    allowed = (f"{1:064x}", f"{2:064x}")
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )
    controller.set_allowed_fingerprints(allowed)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.FOREGROUND_ONLY,
        include_source=True,
    )

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 1
    assert deferred.pending(allowed[0]) == 1
    assert deferred.pending(allowed[1]) == 0


def test_closed_reconnecting_pointer_background_policy_excludes_known_minimized(
    tmp_path,
):
    windows = [_window(1), _window(2, minimized=True)]
    allowed = (f"{1:064x}", f"{2:064x}", f"{3:064x}")
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )
    controller.set_allowed_fingerprints(allowed)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.FOREGROUND_BACKGROUND,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert [item["target_id"] for item in saved["items"]] == [allowed[-1]]


def test_pointer_background_policy_defers_missing_when_source_is_only_visible():
    windows = [_window(1)]
    allowed = (f"{1:064x}", f"{2:064x}")
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )
    controller.set_allowed_fingerprints(allowed)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.FOREGROUND_BACKGROUND,
    )

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 1


def test_closed_non_reconnecting_pointer_role_fails_closed():
    windows = [_window(1), _window(2)]
    allowed = (f"{1:064x}", f"{2:064x}", f"{3:064x}")
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
        deferred_service=deferred,
        reconnecting_provider=lambda: (),
    )
    controller.set_allowed_fingerprints(allowed)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert "window_count_mismatch" in result.failure_codes
    assert deferred.pending() == 0


def test_partial_pointer_reconnect_with_unknown_flash_identity_fails_closed():
    windows = [_window(1), _window(2)]
    windows.append(replace(_window(99), launch_fingerprint=None))
    allowed = (f"{1:064x}", f"{2:064x}", f"{3:064x}")
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )
    controller.set_allowed_fingerprints(allowed)

    result = controller.send(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert "window_count_mismatch" in result.failure_codes
    assert deferred.pending() == 0


def test_release_uses_original_pressed_targets_after_source_leaves_group():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    controller = _controller(windows, messages)
    controller.send(
        source_handle=1,
        x_ratio=0.2,
        y_ratio=0.3,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )
    controller._window_backend.foreground = 999

    result = controller.send(
        source_handle=999,
        x_ratio=0.8,
        y_ratio=0.9,
        event="left_up",
        policy=WindowInputPolicy.ALL,
    )

    assert result.passed is True
    assert messages.sent[-2:] == [
        (2, 0.2, 0.3, "left_up"),
        (3, 0.2, 0.3, "left_up"),
    ]


def test_reconnecting_group_persists_atomic_click_for_every_target(tmp_path):
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
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

    result = controller.send_click(
        source_handle=1,
        x_ratio=0.5,
        y_ratio=0.5,
        policy=WindowInputPolicy.ALL,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 3
    assert messages.sent == []
    assert [item["payload"]["event"] for item in saved["items"]] == [
        "click",
        "click",
        "click",
    ]


def test_reconnecting_group_ignores_click_from_non_game_window():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=999),
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: (windows[2].launch_fingerprint,),
    )
    controller.set_allowed_fingerprints(
        window.launch_fingerprint for window in windows
    )

    result = controller.send(
        source_handle=999,
        x_ratio=0.25,
        y_ratio=0.75,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert result.failure_codes == ("source_not_in_group",)
    assert messages.sent == []
    assert deferred.pending() == 0


def test_reconnecting_group_requires_source_game_window_to_be_foreground():
    windows = [_window(1), _window(2), _window(3)]
    messages = Messages()
    deferred = DeferredSyncOperationService()
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=999),
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: (windows[2].launch_fingerprint,),
    )
    controller.set_allowed_fingerprints(
        window.launch_fingerprint for window in windows
    )

    result = controller.send(
        source_handle=1,
        x_ratio=0.25,
        y_ratio=0.75,
        event="left_down",
        policy=WindowInputPolicy.ALL,
    )

    assert result.failure_codes == ("source_not_foreground",)
    assert messages.sent == []
    assert deferred.pending() == 0


def test_unknown_flash_identity_must_block_physical_fallback():
    windows = [_window(1), _window(2)]
    windows[0] = replace(windows[0], launch_fingerprint=None)
    controller = WindowsPointerSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=Windows(windows, foreground=1),
        message_backend=Messages(),
    )
    controller.set_allowed_fingerprints((f"{1:064x}", f"{2:064x}"))

    assert controller.source_is_eligible(1) is False
    assert controller.source_must_block_physical_fallback(1) is True
