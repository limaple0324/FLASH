import json

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
    normalize_approved_key,
    normalize_input_policy,
)
from adapters.windows_window import WindowInfo


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

def controller(window_backend, message_backend=None):
    return WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=window_backend,
        message_backend=message_backend or FakeMessageBackend(),
    )


def test_normalizers_accept_only_confirmed_values():
    assert normalize_approved_key(" b ") == "B"
    assert normalize_approved_key("c") == "C"
    assert normalize_approved_key("A") is None
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
