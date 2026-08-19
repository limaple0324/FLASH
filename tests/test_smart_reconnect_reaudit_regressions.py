from __future__ import annotations

import hashlib

import adapters.windows_smart_reconnect_base as frozen_base
from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_background_capture import CaptureSample
from adapters.windows_smart_reconnect import (
    MouseClickResult,
    WindowInstanceToken,
    WindowsSmartReconnectController,
    _TcpState,
)
from adapters.windows_window import WindowInfo, monitored_window_instance_fingerprint
from core.reconnect_policy import ReconnectScreenState
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.target_window_contract_service import (
    ResolvedTargetWindows,
    TargetFailureEvidence,
)


class _WindowBackend:
    def __init__(self, windows):
        self.windows = tuple(windows)

    def list_windows(self):
        return self.windows

    def foreground_handle(self):
        return self.windows[0].handle if self.windows else None

    def top_window_at(self, _x, _y):
        return self.foreground_handle()


class _Capture:
    def capture(self, _handle):
        return CaptureSample(2, 2, bytes([1, 0, 0, 255] * 4), True)


class _Recognizer:
    def recognize_capture(self, _sample):
        return ScreenRecognition(
            ReconnectScreenState.CONNECTED,
            1.0,
            None,
            "connected",
        )


class _Mouse:
    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout_ms):
        return True

    def click_relative(self, *_args):
        return MouseClickResult(True, True, False, None)

    def scroll_relative(self, *_args):
        return MouseClickResult(True, True, False, None)


def _window(handle: int, fingerprint: str) -> WindowInfo:
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 900, 600),
        process_id=100 + handle,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
        thread_id=1000 + handle,
        process_lifecycle_token=10000 + handle,
    )


def _target(tmp_path, order, entry_id, fingerprint):
    shortcut = tmp_path / f"{entry_id}.lnk"
    shortcut.write_bytes(b"shortcut")
    return GroupLaunchTarget(
        order,
        entry_id,
        shortcut,
        fingerprint,
        entry_id=entry_id,
        role_id=entry_id,
    )


def _controller(windows, *, target_windows_provider=None, tcp_provider=None):
    return WindowsSmartReconnectController(
        expected_windows=max(1, len(windows)),
        title_keywords=("Adobe Flash Player",),
        window_backend=_WindowBackend(windows),
        capture_provider=_Capture(),
        primary_capture_is_trusted=True,
        recognizer=_Recognizer(),
        mouse_backend=_Mouse(),
        execution_enabled=False,
        require_expected_window_count=False,
        target_windows_provider=target_windows_provider,
        tcp_connection_count_provider=tcp_provider,
        step_scoped_live_reconnect=True,
    )


def test_mixed_manual_window_does_not_disable_configured_tcp_provider(monkeypatch):
    configured = _window(1, "a" * 64)
    manual = _window(2, "b" * 64)
    tcp_provider = lambda _ids: {}
    controller = _controller((configured, manual), tcp_provider=tcp_provider)
    controller._step_scoped_activation_ready = True
    controller._manual_live_fingerprints = frozenset((manual.launch_fingerprint,))
    observed = []

    def fake_scan(instance, *, execute):
        observed.append((instance._tcp_counts, execute))
        return "sentinel"

    monkeypatch.setattr(
        frozen_base.WindowsSmartReconnectController,
        "_scan_locked",
        fake_scan,
    )

    assert controller._scan_locked(execute=True) == "sentinel"
    assert observed == [(tcp_provider, True)]
    assert controller._tcp_counts is tcp_provider


def test_global_identity_collision_is_never_demoted_by_same_local_code():
    source = "a" * 64
    failed = _window(2, source)
    controller = _controller((failed,))
    resolved = ResolvedTargetWindows(
        windows=(),
        target_failure_evidence=(
            TargetFailureEvidence(
                "entry-failed",
                source,
                ("window_identity_duplicate",),
                (failed,),
            ),
        ),
        global_failure_codes=("window_identity_duplicate",),
    )

    global_failures, local_failures = controller._contract_failure_evidence(resolved)

    assert global_failures == ("window_identity_duplicate",)
    assert local_failures[source] == ("window_identity_duplicate",)


def test_shared_source_failure_uses_entry_local_key_not_healthy_source(tmp_path):
    source = "c" * 64
    healthy = _window(1, source)
    failed_target = _target(tmp_path, 2, "entry-failed", source)
    healthy_target = _target(tmp_path, 1, "entry-healthy", source)
    plan = GroupLaunchPlan("configured", (healthy_target, failed_target))
    controller = _controller((healthy,))
    controller.set_group_launch_plan(plan)
    resolved = ResolvedTargetWindows(
        windows=(healthy,),
        sync_windows=(healthy,),
        sync_entry_ids=("entry-healthy",),
        sync_scope_entry_ids=("entry-healthy", "entry-failed"),
        sync_controller_entry_id="entry-healthy",
        target_failure_evidence=(
            TargetFailureEvidence(
                "entry-failed",
                source,
                ("shortcut_identity_unresolved",),
                (),
            ),
        ),
        global_failure_codes=("shortcut_identity_unresolved",),
    )

    global_failures, local_failures = controller._contract_failure_evidence(resolved)

    expected_local_key = hashlib.sha256(
        b"fu-smart-reconnect-local-failure-v1\0"
        + b"entry-failed\0"
        + source.encode("ascii")
    ).hexdigest()
    assert global_failures == ()
    assert local_failures == {
        expected_local_key: ("shortcut_identity_unresolved",)
    }
    assert source not in local_failures


def test_shared_source_activation_and_tcp_binding_keep_healthy_entry(tmp_path):
    source = "d" * 64
    healthy = _window(1, source)
    healthy_target = _target(tmp_path, 1, "entry-healthy", source)
    failed_target = _target(tmp_path, 2, "entry-failed", source)
    plan = GroupLaunchPlan("configured", (healthy_target, failed_target))
    resolved = ResolvedTargetWindows(
        windows=(healthy,),
        sync_windows=(healthy,),
        sync_entry_ids=("entry-healthy",),
        sync_scope_entry_ids=("entry-healthy", "entry-failed"),
        sync_controller_entry_id="entry-healthy",
        target_failure_evidence=(
            TargetFailureEvidence(
                "entry-failed",
                source,
                ("shortcut_identity_unresolved",),
                (),
            ),
        ),
        global_failure_codes=("shortcut_identity_unresolved",),
    )
    controller = _controller((healthy,), target_windows_provider=lambda: resolved)
    controller.set_group_launch_plan(plan)

    monitor = monitored_window_instance_fingerprint(healthy)
    token = WindowInstanceToken.from_window(healthy)
    assert monitor is not None and token is not None
    complete_instances = {monitor: (healthy, token)}
    sources = {monitor: source}

    verified = controller._verified_group_activation_snapshot(
        resolved,
        complete_instances,
        sources,
    )

    assert verified is not None
    instances, verified_sources, detection_only, isolated = verified
    assert tuple(instances) == (monitor,)
    assert verified_sources == {monitor: source}
    assert detection_only == frozenset()
    assert isolated == 1

    controller._activation_snapshot_instances = {monitor: token}
    controller._activation_snapshot_source_fingerprints = {monitor: source}
    assert controller._tcp_id(resolved, monitor, token) == "entry-healthy"


def test_manual_confirmed_tcp_owner_is_eligible_after_formal_queue_is_empty(tmp_path):
    configured_source = "e" * 64
    manual_source = "f" * 64
    configured = _window(1, configured_source)
    manual = _window(2, manual_source)
    plan = GroupLaunchPlan(
        "configured",
        (_target(tmp_path, 1, "entry-configured", configured_source),),
    )
    controller = _controller((configured, manual))
    controller.set_group_launch_plan(plan)
    controller._manual_live_fingerprints = frozenset((manual_source,))
    manual_token = WindowInstanceToken.from_window(manual)
    assert manual_token is not None
    manual_state = _TcpState(manual_token, None, online=True)

    owner, state, failure = controller._ordered_tcp_owner(
        ((manual_source, manual_state),)
    )

    assert failure is None
    assert owner == manual_source
    assert state is manual_state
