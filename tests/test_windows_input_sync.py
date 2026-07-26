import json

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
    normalize_approved_key,
    normalize_input_policy,
)
from adapters.windows_window import WindowInfo
from services.deferred_sync_operation_service import (
    DeferredSyncOperationService,
)


def make_windows(*, count=14, minimized=(), foreground=1):
    windows = []
    minimized = set(minimized)
    for index in range(1, count + 1):
        windows.append(
            WindowInfo(
                handle=index,
                title="Adobe Flash Player 11",
                visible=True,
                minimized=index in minimized,
                rect=(0, 0, 916, 629),
                process_id=1000 + index,
                window_class="ShockwaveFlash",
                launch_fingerprint=f"{index:064x}",
            )
        )
    return FakeWindowBackend(windows, foreground=foreground)


class FakeWindowBackend:
    def __init__(self, windows, *, foreground=1):
        self.windows = list(windows)
        self.foreground = foreground

    def list_windows(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.foreground

    def top_window_at(self, _x, _y):
        return self.foreground


class FakeMessageBackend:
    def __init__(self, *, invalid=(), unresponsive=(), rejected=()):
        self.invalid = set(invalid)
        self.unresponsive = set(unresponsive)
        self.rejected = set(rejected)
        self.probed = []
        self.sent = []

    def is_window(self, handle):
        return handle not in self.invalid

    def probe_responsive(self, handle, timeout_ms):
        self.probed.append((handle, timeout_ms))
        return handle not in self.unresponsive

    def send_virtual_key(self, handle, virtual_key):
        self.sent.append((handle, virtual_key))
        return handle not in self.rejected

    def send_key_chord(self, handle, virtual_keys):
        self.sent.append((handle, virtual_keys))
        return handle not in self.rejected

def controller(window_backend, message_backend=None):
    return WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=window_backend,
        message_backend=message_backend or FakeMessageBackend(),
    )


def test_normalizers_accept_complete_confirmed_shortcut_catalog():
    assert normalize_approved_key(" b ") == "B"
    assert normalize_approved_key("c") == "C"
    assert normalize_approved_key("A") == "A"
    assert normalize_approved_key("esc") == "ESC"
    assert normalize_approved_key("Ctrl + Up") == "CTRL+↑"
    assert normalize_approved_key("CTRL+↓") == "CTRL+↓"
    assert normalize_approved_key("F1") is None
    assert normalize_approved_key(None) is None

    assert normalize_input_policy("ALL") is WindowInputPolicy.ALL
    assert (
        normalize_input_policy("foreground_background")
        is WindowInputPolicy.FOREGROUND_BACKGROUND
    )
    assert normalize_input_policy("unknown") is None


def test_all_policy_preflights_every_independent_window_without_sending():
    messages = FakeMessageBackend()
    result = controller(
        make_windows(minimized={2, 3}),
        messages,
    ).send_approved_key("B", policy="all")

    assert result.ready is True
    assert result.passed is False
    assert result.eligible_windows == 14
    assert result.responsive_windows == 14
    assert result.sent_windows == 0
    assert result.minimized_windows == 2
    assert messages.sent == []


def test_all_policy_sends_b_to_foreground_background_and_minimized():
    messages = FakeMessageBackend()
    result = controller(
        make_windows(minimized={2, 3}),
        messages,
    ).send_approved_key("B", policy="all", execute=True)

    assert result.passed is True
    assert result.sent_windows == 14
    assert messages.sent == [(handle, 0x42) for handle in range(1, 15)]


def test_foreground_background_excludes_only_minimized_windows():
    messages = FakeMessageBackend()
    result = controller(
        make_windows(minimized={2, 3}),
        messages,
    ).send_approved_key(
        "C",
        policy="foreground_background",
        execute=True,
    )

    assert result.passed is True
    assert result.eligible_windows == 12
    assert result.skipped_windows == 2
    assert result.sent_windows == 12
    assert {handle for handle, _key in messages.sent} == {
        1,
        *range(4, 15),
    }


def test_foreground_only_sends_to_only_the_group_foreground():
    messages = FakeMessageBackend()
    result = controller(
        make_windows(foreground=8),
        messages,
    ).send_approved_key("C", policy="foreground_only", execute=True)

    assert result.passed is True
    assert result.eligible_windows == 1
    assert result.sent_windows == 1
    assert messages.sent == [(8, 0x43)]


def test_confirmed_ctrl_arrow_chord_is_delivered_in_one_batch_per_window():
    messages = FakeMessageBackend()
    result = controller(
        make_windows(),
        messages,
    ).send_approved_key("CTRL+UP", policy="all", execute=True)

    assert result.passed is True
    assert result.approved_key == "CTRL+↑"
    assert messages.sent == [
        (handle, (0x11, 0x26)) for handle in range(1, 15)
    ]


def test_identity_failure_aborts_before_any_input():
    backend = make_windows()
    backend.windows[1] = WindowInfo(
        handle=2,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 916, 629),
        process_id=1001,
        window_class="ShockwaveFlash",
        launch_fingerprint=backend.windows[0].launch_fingerprint,
    )
    messages = FakeMessageBackend()

    result = controller(backend, messages).send_approved_key(
        "B",
        policy="all",
        execute=True,
    )

    assert result.passed is False
    assert "process_identity_missing_or_duplicate" in result.failure_codes
    assert "fingerprint_missing_or_duplicate" in result.failure_codes
    assert messages.probed == []
    assert messages.sent == []


def test_count_mismatch_aborts_before_any_input():
    messages = FakeMessageBackend()
    result = controller(
        make_windows(count=13),
        messages,
    ).send_approved_key("B", policy="all", execute=True)

    assert result.passed is False
    assert result.failure_codes == ("window_count_mismatch",)
    assert messages.sent == []


def test_selected_group_identity_excludes_other_open_flash_windows():
    windows = make_windows(count=3)
    messages = FakeMessageBackend()
    selected = {
        windows.windows[0].launch_fingerprint,
        windows.windows[2].launch_fingerprint,
    }
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        allowed_fingerprints=selected,
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
    )

    assert result.passed is True
    assert result.discovered_windows == 2
    assert messages.sent == [(1, 0x42), (3, 0x42)]


def test_selected_group_dispatch_uses_configured_fingerprint_order():
    windows = make_windows(count=3)
    windows.windows = [
        windows.windows[1],
        windows.windows[2],
        windows.windows[0],
    ]
    messages = FakeMessageBackend()
    configured_order = (
        f"{3:064x}",
        f"{1:064x}",
        f"{2:064x}",
    )
    sync = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        allowed_fingerprints=configured_order,
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
    )

    assert result.passed is True
    assert [handle for handle, _key in messages.sent] == [3, 1, 2]


def test_selected_group_missing_one_identity_fails_closed():
    windows = make_windows(count=2)
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        allowed_fingerprints={
            windows.windows[0].launch_fingerprint,
            "f" * 64,
        },
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
    )

    assert result.passed is False
    assert "window_count_mismatch" in result.failure_codes
    assert "group_identity_set_mismatch" in result.failure_codes
    assert messages.sent == []


def test_one_reconnecting_role_pauses_new_sync_for_entire_group():
    windows = make_windows()
    messages = FakeMessageBackend()
    deferred = DeferredSyncOperationService()
    fingerprints = {
        window.launch_fingerprint for window in windows.windows
    }
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        allowed_fingerprints=fingerprints,
        deferred_service=deferred,
        reconnecting_provider=lambda: (f"{14:064x}",),
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
    )

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert messages.sent == []
    assert deferred.pending() == 13


def test_reconnecting_group_preserves_press_time_policy_and_source_eligibility(
    tmp_path,
):
    windows = make_windows(minimized={2, 3}, foreground=1)
    deferred_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=deferred_path)
    fingerprints = tuple(
        window.launch_fingerprint for window in windows.windows
    )
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=fingerprints,
        deferred_service=deferred,
        reconnecting_provider=lambda: (fingerprints[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="foreground_background",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )
    payload = json.loads(deferred_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 11
    assert {
        item["target_id"] for item in payload["items"]
    } == set(fingerprints[3:])
    assert {
        item["payload"]["policy"] for item in payload["items"]
    } == {"foreground_background"}
    assert {
        item["payload"]["source_eligible_at_capture"]
        for item in payload["items"]
    } == {True}


def test_reconnecting_foreground_only_keeps_only_press_time_source(tmp_path):
    windows = make_windows(foreground=8)
    deferred_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=deferred_path)
    fingerprints = tuple(
        window.launch_fingerprint for window in windows.windows
    )
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=fingerprints,
        deferred_service=deferred,
        reconnecting_provider=lambda: (fingerprints[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="foreground_only",
        execute=True,
        source_handle=8,
    )
    payload = json.loads(deferred_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 1
    assert payload["items"][0]["target_id"] == fingerprints[7]
    assert payload["items"][0]["payload"]["policy"] == "foreground_only"


def test_closed_reconnecting_role_does_not_drop_new_keyboard_operation(tmp_path):
    windows = make_windows(count=13, foreground=1)
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    allowed = tuple(f"{index:064x}" for index in range(1, 15))
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert result.eligible_windows == 13
    assert deferred.pending() == 13
    assert {item["target_id"] for item in saved["items"]} == set(
        allowed[1:]
    )


def test_closed_reconnecting_role_foreground_only_defers_only_source():
    windows = make_windows(count=1, foreground=1)
    deferred = DeferredSyncOperationService()
    allowed = (f"{1:064x}", f"{2:064x}")
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="foreground_only",
        execute=True,
        exclude_foreground=False,
        source_handle=1,
    )

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 1
    assert deferred.pending(allowed[0]) == 1
    assert deferred.pending(allowed[1]) == 0


def test_closed_reconnecting_role_is_in_background_policy_but_known_minimized_is_not(
    tmp_path,
):
    windows = make_windows(count=13, minimized={2}, foreground=1)
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    allowed = tuple(f"{index:064x}" for index in range(1, 15))
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="foreground_background",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    targets = {item["target_id"] for item in saved["items"]}

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert allowed[0] not in targets
    assert allowed[1] not in targets
    assert allowed[-1] in targets
    assert deferred.pending() == 12


def test_background_policy_still_defers_missing_role_when_source_is_only_visible():
    windows = make_windows(count=1, foreground=1)
    deferred = DeferredSyncOperationService()
    allowed = (f"{1:064x}", f"{2:064x}")
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="foreground_background",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert result.failure_codes == ("sync_group_deferred_reconnect",)
    assert deferred.pending() == 1


def test_missing_non_reconnecting_role_still_fails_closed():
    windows = make_windows(count=13, foreground=1)
    deferred = DeferredSyncOperationService()
    allowed = tuple(f"{index:064x}" for index in range(1, 15))
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (),
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert "window_count_mismatch" in result.failure_codes
    assert deferred.pending() == 0


def test_partial_reconnect_with_visible_unknown_flash_identity_fails_closed():
    windows = make_windows(count=13, foreground=1)
    windows.windows.append(
        WindowInfo(
            handle=99,
            title="Adobe Flash Player 11",
            visible=True,
            minimized=False,
            rect=(0, 0, 916, 629),
            process_id=1099,
            window_class="ShockwaveFlash",
            launch_fingerprint=None,
        )
    )
    deferred = DeferredSyncOperationService()
    allowed = tuple(f"{index:064x}" for index in range(1, 15))
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=FakeMessageBackend(),
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[-1],),
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert "window_count_mismatch" in result.failure_codes
    assert deferred.pending() == 0


def test_captured_keyboard_source_is_used_even_if_focus_changes_before_delivery():
    windows = make_windows(foreground=1)
    messages = FakeMessageBackend()
    sync = controller(windows, messages)
    windows.foreground = 999

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert result.passed is True
    assert {handle for handle, _key in messages.sent} == set(range(2, 15))


def test_reconnecting_group_ignores_key_from_non_game_window():
    windows = make_windows(foreground=999)
    messages = FakeMessageBackend()
    deferred = DeferredSyncOperationService()
    fingerprints = {
        window.launch_fingerprint for window in windows.windows
    }
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        allowed_fingerprints=fingerprints,
        deferred_service=deferred,
        reconnecting_provider=lambda: (f"{14:064x}",),
    )

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
    )

    assert result.failure_codes == ("foreground_not_in_group",)
    assert messages.sent == []
    assert deferred.pending() == 0


def test_normal_group_ignores_key_from_non_game_window():
    windows = make_windows(foreground=999)
    messages = FakeMessageBackend()

    result = controller(
        windows,
        messages,
    ).send_approved_key("C", policy="all", execute=True)

    assert result.failure_codes == ("foreground_not_in_group",)
    assert messages.sent == []


def test_unresponsive_window_aborts_entire_batch_before_input():
    messages = FakeMessageBackend(unresponsive={7})
    result = controller(
        make_windows(),
        messages,
    ).send_approved_key("B", policy="all", execute=True)

    assert result.passed is False
    assert result.sent_windows == 0
    assert "input_target_unresponsive" in result.failure_codes
    assert messages.sent == []


def test_delivery_failure_is_isolated_and_reported_without_redirecting():
    messages = FakeMessageBackend(rejected={7})
    result = controller(
        make_windows(),
        messages,
    ).send_approved_key("C", policy="all", execute=True)

    assert result.passed is False
    assert result.sent_windows == 13
    assert "input_delivery_failed" in result.failure_codes
    assert len(messages.sent) == 14
    assert {handle for handle, _key in messages.sent} == set(range(1, 15))


def test_report_never_contains_handles_process_ids_or_fingerprints():
    result = controller(make_windows()).send_approved_key(
        "B",
        policy="all",
        execute=True,
    )
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.passed is True
    assert "launch_fingerprint" not in serialized
    assert "process_id" not in serialized
    assert "handle" not in serialized
    assert f"{1:064x}" not in serialized
    assert result.to_dict()["raw_arguments_emitted"] is False
    assert result.to_dict()["fingerprints_emitted"] is False


def test_report_contains_nonnegative_aggregate_dispatch_timings():
    result = controller(make_windows()).send_approved_key(
        "B",
        policy="all",
        execute=True,
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


def test_execution_guard_stops_before_the_next_keyboard_target():
    messages = FakeMessageBackend()
    decisions = iter((True, False))
    result = controller(
        make_windows(count=3),
        messages,
    )
    result.set_expected_windows(3)

    report = result.send_approved_key(
        "B",
        policy="all",
        execute=True,
        execution_guard=lambda: next(decisions, False),
    )

    assert report.sent_windows == 1
    assert "execution_stopped" in report.failure_codes
    assert messages.sent == [(1, 0x42)]
