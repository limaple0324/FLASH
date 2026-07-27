import json
from dataclasses import replace

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
        self.probed = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout):
        self.probed.append((_handle, _timeout))
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
    allowed = tuple(window.launch_fingerprint for window in windows)
    controller.set_allowed_fingerprints(allowed)
    controller.set_controller_fingerprint(allowed[0])
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


def test_group_member_remains_known_when_the_configured_group_is_incomplete():
    windows = [_window(1), _window(2), _window(99)]
    backend = Windows(windows, foreground=1)
    controller = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=Messages(),
    )
    controller.set_allowed_fingerprints(
        (
            f"{1:064x}",
            f"{2:064x}",
            f"{3:064x}",
        )
    )

    assert controller.source_is_eligible(1) is False
    assert controller.source_is_group_member(1) is True

    backend.foreground = 99
    assert controller.source_is_group_member(99) is False


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
