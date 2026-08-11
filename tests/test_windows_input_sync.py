import ctypes
import json
from dataclasses import replace
from threading import Event
from time import monotonic, sleep

from adapters.windows_input_sync import (
    Win32WindowInstanceVerifier,
    WindowInputPolicy,
    WindowsInputSyncController,
    normalize_approved_key,
    normalize_input_policy,
    same_stable_window_instance,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.window_instance import WindowInstanceToken
from domain.sync_target_settings import SyncTargetSettings
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
                thread_id=2000 + index,
                window_class="ShockwaveFlash",
                launch_fingerprint=f"{index:064x}",
                process_lifecycle_token=3000 + index,
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


class SignalingMessageBackend(FakeMessageBackend):
    def __init__(self):
        super().__init__()
        self.completed = Event()

    def send_virtual_key(self, handle, virtual_key):
        delivered = super().send_virtual_key(handle, virtual_key)
        self.completed.set()
        return delivered


class MutableInstanceVerifier:
    def __init__(self, windows):
        self.current = {
            window.handle: WindowInstanceToken.from_window(window)
            for window in windows
        }
        self.checked = Event()

    def is_current(self, instance):
        self.checked.set()
        return same_stable_window_instance(
            self.current.get(instance.handle),
            instance,
        )


class Win32Function:
    def __init__(self, callback):
        self._callback = callback

    def __call__(self, *args):
        return self._callback(*args)


class InstanceUser32:
    def __init__(self):
        self.process_id = 1001
        self.thread_id = 2001
        self.window_class = "ShockwaveFlash"
        self.IsWindow = Win32Function(lambda _handle: True)
        self.GetWindowThreadProcessId = Win32Function(
            self._get_window_process
        )
        self.GetClassNameW = Win32Function(self._get_class_name)

    def _get_window_process(self, _handle, process_pointer):
        process_pointer._obj.value = self.process_id
        return self.thread_id

    def _get_class_name(self, _handle, buffer, _length):
        buffer.value = self.window_class
        return len(self.window_class)


class InstanceKernel32:
    def __init__(self):
        self.lifecycle = 3001
        self.open_result = 77
        self.closed = []
        self.OpenProcess = Win32Function(
            lambda _access, _inherit, _process_id: self.open_result
        )
        self.GetProcessTimes = Win32Function(self._get_process_times)
        self.CloseHandle = Win32Function(self._close)

    def _get_process_times(
        self,
        _handle,
        created,
        _exited,
        _kernel,
        _user,
    ):
        created._obj.dwHighDateTime = self.lifecycle >> 32
        created._obj.dwLowDateTime = self.lifecycle & 0xFFFFFFFF
        return True

    def _close(self, handle):
        self.closed.append(handle)
        return True

def controller(window_backend, message_backend=None, *, verifier=None):
    return WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=window_backend,
        message_backend=message_backend or FakeMessageBackend(),
        instance_verifier=verifier,
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


def test_win32_instance_verifier_requires_exact_live_window_and_process(
    monkeypatch,
):
    verifier = Win32WindowInstanceVerifier()
    user32 = InstanceUser32()
    kernel32 = InstanceKernel32()
    monkeypatch.setattr(verifier, "_user32", lambda: user32)
    monkeypatch.setattr(verifier, "_kernel32", lambda: kernel32)
    token = WindowInstanceToken(
        handle=1,
        process_id=1001,
        thread_id=2001,
        window_class="ShockwaveFlash",
        rect=(0, 0, 916, 629),
        minimized=False,
        process_lifecycle_token=3001,
    )

    assert verifier.is_current(token) is True
    assert kernel32.closed == [77]

    user32.process_id = 9999
    assert verifier.is_current(token) is False
    user32.process_id = token.process_id
    user32.window_class = "ReusedWindow"
    assert verifier.is_current(token) is False
    user32.window_class = token.window_class
    kernel32.open_result = 0
    assert verifier.is_current(token) is False
    user32.IsWindow = Win32Function(
        lambda _handle: (_ for _ in ()).throw(OSError("denied"))
    )
    assert verifier.is_current(token) is False


def test_win32_instance_verifier_rechecks_handle_after_lifecycle_query(
    monkeypatch,
):
    verifier = Win32WindowInstanceVerifier()
    user32 = InstanceUser32()
    kernel32 = InstanceKernel32()
    original_get_times = kernel32._get_process_times

    def replace_handle_during_lifecycle(*args):
        result = original_get_times(*args)
        user32.process_id = 9001
        user32.thread_id = 9002
        user32.window_class = "ReusedWindow"
        return result

    kernel32.GetProcessTimes = Win32Function(
        replace_handle_during_lifecycle
    )
    monkeypatch.setattr(verifier, "_user32", lambda: user32)
    monkeypatch.setattr(verifier, "_kernel32", lambda: kernel32)
    token = WindowInstanceToken.from_window(make_windows(count=1).windows[0])

    assert token is not None
    assert verifier.is_current(token) is False
    assert kernel32.closed == [77]


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


def test_partial_group_sends_only_to_members_already_in_game():
    backend = make_windows(count=2, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(backend.windows)
    allowed = tuple(f"{index:064x}" for index in range(1, 4))
    states = {
        allowed[0]: ReconnectScreenState.CONNECTED,
        allowed[1]: ReconnectScreenState.CONNECTED,
        allowed[2]: ReconnectScreenState.LOGIN_START,
    }
    deferred = DeferredSyncOperationService()
    sync = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        allowed_fingerprints=allowed,
        deferred_service=deferred,
        reconnecting_provider=lambda: (allowed[2],),
        screen_state_provider=states.get,
        require_expected_window_count=False,
        instance_verifier=verifier,
    )
    sync.set_controller_fingerprint(allowed[0])

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert result.passed is False
    assert result.discovered_windows == 2
    assert result.eligible_windows == 2
    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert messages.sent == [(2, 0x42)]
    assert deferred.pending() == 1

    reconnected = replace(
        backend.windows[1],
        handle=3,
        process_id=1003,
        thread_id=2003,
        launch_fingerprint=allowed[2],
        process_lifecycle_token=3003,
    )
    backend.windows.append(reconnected)
    verifier.current[3] = WindowInstanceToken.from_window(reconnected)
    states[allowed[2]] = ReconnectScreenState.CONNECTED
    deferred.process_ready(
        reconnecting_targets=(),
        failed_targets=(),
        ready_targets=(allowed[2],),
    )
    deadline = monotonic() + 1.0
    while deferred.pending() and monotonic() < deadline:
        sleep(0.01)

    assert deferred.pending() == 0
    assert messages.sent == [(2, 0x42), (3, 0x42)]


def test_login_screen_source_cannot_start_partial_group_sync():
    backend = make_windows(count=2, foreground=1)
    messages = FakeMessageBackend()
    allowed = tuple(f"{index:064x}" for index in range(1, 4))
    states = {
        allowed[0]: ReconnectScreenState.LOGIN_START,
        allowed[1]: ReconnectScreenState.CONNECTED,
    }
    sync = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        allowed_fingerprints=allowed,
        screen_state_provider=states.get,
        require_expected_window_count=False,
    )
    sync.set_controller_fingerprint(allowed[0])

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert result.failure_codes == ("source_not_in_game",)
    assert messages.sent == []


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


def test_one_reconnecting_role_defers_only_that_role():
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

    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert [handle for handle, _key in messages.sent] == list(range(2, 14))
    assert deferred.pending() == 1
    assert deferred.pending(f"{14:064x}") == 1


def test_reconnecting_group_preserves_press_time_policy_and_source_eligibility(
    tmp_path,
):
    windows = make_windows(minimized={2, 3}, foreground=1)
    deferred_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=deferred_path)
    fingerprints = tuple(
        window.launch_fingerprint for window in windows.windows
    )
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
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

    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert [handle for handle, _key in messages.sent] == list(range(4, 14))
    assert deferred.pending() == 1
    assert {
        item["target_id"] for item in payload["items"]
    } == {fingerprints[-1]}
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
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
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
    assert result.failure_codes == ()
    assert [handle for handle, _key in messages.sent] == [8]
    assert deferred.pending() == 0
    assert deferred_path.exists() is False


def test_closed_reconnecting_role_does_not_drop_new_keyboard_operation(tmp_path):
    windows = make_windows(count=13, foreground=1)
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    allowed = tuple(f"{index:064x}" for index in range(1, 15))
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
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

    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert result.eligible_windows == 13
    assert [handle for handle, _key in messages.sent] == list(range(2, 14))
    assert deferred.pending() == 1
    assert {item["target_id"] for item in saved["items"]} == {allowed[-1]}


def test_closed_reconnecting_role_foreground_only_defers_only_source():
    windows = make_windows(count=1, foreground=1)
    deferred = DeferredSyncOperationService()
    allowed = (f"{1:064x}", f"{2:064x}")
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
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

    assert result.failure_codes == ()
    assert [handle for handle, _key in messages.sent] == [1]
    assert deferred.pending() == 0
    assert deferred.pending(allowed[0]) == 0
    assert deferred.pending(allowed[1]) == 0


def test_closed_reconnecting_role_is_in_background_policy_but_known_minimized_is_not(
    tmp_path,
):
    windows = make_windows(count=13, minimized={2}, foreground=1)
    state_path = tmp_path / "deferred.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    allowed = tuple(f"{index:064x}" for index in range(1, 15))
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
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

    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert allowed[0] not in targets
    assert allowed[1] not in targets
    assert targets == {allowed[-1]}
    assert [handle for handle, _key in messages.sent] == list(range(3, 14))
    assert deferred.pending() == 1


def test_background_policy_still_defers_missing_role_when_source_is_only_visible():
    windows = make_windows(count=1, foreground=1)
    deferred = DeferredSyncOperationService()
    allowed = (f"{1:064x}", f"{2:064x}")
    messages = FakeMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
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

    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert messages.sent == []
    assert deferred.pending() == 1
    assert deferred.pending(allowed[-1]) == 1


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


def test_non_controller_group_member_cannot_start_keyboard_sync():
    windows = make_windows(foreground=2)
    messages = FakeMessageBackend()
    sync = controller(windows, messages)
    allowed = tuple(
        window.launch_fingerprint for window in windows.windows
    )
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])

    result = sync.send_approved_key(
        "C",
        policy="all",
        execute=True,
        source_handle=2,
    )

    assert result.failure_codes == ("source_not_controller",)
    assert messages.sent == []


def test_unresponsive_window_skips_only_that_target_before_input():
    messages = FakeMessageBackend(unresponsive={7})
    result = controller(
        make_windows(),
        messages,
    ).send_approved_key("B", policy="all", execute=True)

    assert result.passed is False
    assert result.sent_windows == 13
    assert "input_target_unresponsive" in result.failure_codes
    assert {handle for handle, _key in messages.sent} == (
        set(range(1, 15)) - {7}
    )


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


def test_per_role_keyboard_delay_is_scheduled_from_same_batch():
    windows = make_windows(count=2, foreground=1)
    messages = SignalingMessageBackend()
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
    )
    allowed = tuple(
        window.launch_fingerprint for window in windows.windows
    )
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])
    sync.set_target_settings(
        {
            allowed[1]: SyncTargetSettings(delay_ms=20),
        }
    )
    try:
        result = sync.send_approved_key(
            "B",
            policy="all",
            execute=True,
            exclude_foreground=True,
            source_handle=1,
        )

        assert result.passed is True
        assert result.sent_windows == 0
        assert result.scheduled_windows == 1
        assert messages.completed.wait(0.5)
        assert messages.sent == [(2, 0x42)]
    finally:
        sync._dispatch_scheduler.close()


def test_keyboard_immediate_skips_reused_target_and_sends_safe_sibling():
    windows = make_windows(count=3, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(windows.windows)
    verifier.current[2] = WindowInstanceToken.from_window(
        replace(
            windows.windows[1],
            process_lifecycle_token=9002,
        )
    )
    sync = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        instance_verifier=verifier,
    )
    allowed = tuple(window.launch_fingerprint for window in windows.windows)
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert messages.sent == [(3, 0x42)]
    assert result.sent_windows == 1
    assert "input_target_instance_changed" in result.failure_codes


def test_keyboard_source_replacement_stops_all_remaining_targets():
    windows = make_windows(count=3, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(windows.windows)
    verifier.current[1] = WindowInstanceToken.from_window(
        replace(
            windows.windows[0],
            process_lifecycle_token=9001,
        )
    )
    sync = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        instance_verifier=verifier,
    )
    allowed = tuple(window.launch_fingerprint for window in windows.windows)
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )

    assert messages.sent == []
    assert "source_instance_changed" in result.failure_codes


def test_keyboard_delayed_delivery_rejects_captured_target_replacement():
    windows = make_windows(count=2, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(windows.windows)
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        instance_verifier=verifier,
    )
    allowed = tuple(window.launch_fingerprint for window in windows.windows)
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])
    sync.set_target_settings({allowed[1]: SyncTargetSettings(delay_ms=30)})
    try:
        result = sync.send_approved_key(
            "B",
            policy="all",
            execute=True,
            exclude_foreground=True,
            source_handle=1,
        )
        verifier.current[2] = WindowInstanceToken.from_window(
            replace(
                windows.windows[1],
                process_lifecycle_token=9002,
            )
        )

        assert result.scheduled_windows == 1
        assert verifier.checked.wait(0.5)
        sleep(0.05)
        assert messages.sent == []
    finally:
        sync._dispatch_scheduler.close()


def test_keyboard_delayed_delivery_accepts_same_instance_move_and_minimize():
    windows = make_windows(count=2, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(windows.windows)
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        instance_verifier=verifier,
    )
    allowed = tuple(window.launch_fingerprint for window in windows.windows)
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])
    sync.set_target_settings({allowed[1]: SyncTargetSettings(delay_ms=30)})
    try:
        result = sync.send_approved_key(
            "B",
            policy="all",
            execute=True,
            exclude_foreground=True,
            source_handle=1,
        )
        moved = replace(
            windows.windows[1],
            rect=(40, 50, 956, 679),
            minimized=True,
        )
        windows.windows[1] = moved
        verifier.current[2] = WindowInstanceToken.from_window(moved)
        deadline = monotonic() + 1.0
        while not messages.sent and monotonic() < deadline:
            sleep(0.01)

        assert result.scheduled_windows == 1
        assert messages.sent == [(2, 0x42)]
    finally:
        sync._dispatch_scheduler.close()


def test_keyboard_reconnect_delivery_persists_source_and_binds_new_target(
    tmp_path,
):
    windows = make_windows(count=2, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(windows.windows)
    state_path = tmp_path / "deferred-instances.json"
    deferred = DeferredSyncOperationService(state_path=state_path)
    target = windows.windows[1].launch_fingerprint
    states = {
        windows.windows[0].launch_fingerprint: ReconnectScreenState.CONNECTED,
        target: ReconnectScreenState.LOGIN_START,
    }
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: (target,),
        screen_state_provider=states.get,
        instance_verifier=verifier,
    )
    allowed = tuple(window.launch_fingerprint for window in windows.windows)
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])

    result = sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    replacement = replace(
        windows.windows[1],
        handle=20,
        process_id=9020,
        thread_id=8020,
        process_lifecycle_token=7020,
    )
    windows.windows[1] = replacement
    verifier.current.pop(2)
    verifier.current[20] = WindowInstanceToken.from_window(replacement)
    states[target] = ReconnectScreenState.CONNECTED
    deferred.process_ready(
        reconnecting_targets=(),
        failed_targets=(),
        ready_targets=(target,),
    )
    deadline = monotonic() + 1.0
    while deferred.pending() and monotonic() < deadline:
        sleep(0.01)

    assert result.failure_codes == ("sync_deferred_reconnect",)
    assert "source_instance" in saved["items"][0]["payload"]
    assert saved["items"][0]["payload"]["target_fingerprint"] == target
    assert saved["items"][0]["payload"]["reconnect_target"] is True
    assert "target_instance" not in saved["items"][0]["payload"]
    assert deferred.pending() == 0
    assert messages.sent == [(20, 0x42)]
    assert deferred.failures() == ()


def test_keyboard_reconnect_delivery_rejects_replaced_source(tmp_path):
    windows = make_windows(count=2, foreground=1)
    messages = FakeMessageBackend()
    verifier = MutableInstanceVerifier(windows.windows)
    deferred = DeferredSyncOperationService(
        state_path=tmp_path / "deferred-source.json"
    )
    target = windows.windows[1].launch_fingerprint
    states = {
        windows.windows[0].launch_fingerprint: ReconnectScreenState.CONNECTED,
        target: ReconnectScreenState.LOGIN_START,
    }
    sync = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=windows,
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: (target,),
        screen_state_provider=states.get,
        instance_verifier=verifier,
    )
    allowed = tuple(window.launch_fingerprint for window in windows.windows)
    sync.set_allowed_fingerprints(allowed)
    sync.set_controller_fingerprint(allowed[0])

    sync.send_approved_key(
        "B",
        policy="all",
        execute=True,
        exclude_foreground=True,
        source_handle=1,
    )
    verifier.current[1] = WindowInstanceToken.from_window(
        replace(windows.windows[0], process_lifecycle_token=9001)
    )
    states[target] = ReconnectScreenState.CONNECTED
    deferred.process_ready(
        reconnecting_targets=(),
        failed_targets=(),
        ready_targets=(target,),
    )
    deadline = monotonic() + 1.0
    while deferred.pending() and monotonic() < deadline:
        sleep(0.01)

    assert deferred.pending() == 0
    assert messages.sent == []
    assert len(deferred.failures()) == 1
