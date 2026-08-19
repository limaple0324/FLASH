import hashlib

import pytest

import adapters.windows_smart_reconnect_base as smart_reconnect_base
from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_background_capture import CaptureSample
from adapters.windows_smart_reconnect import (
    MouseClickResult,
    WindowsSmartReconnectController,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.window_registry import WindowRegistry
from main import build_configured_reconnect_plan
from services.group_configuration_service import GroupConfigurationService
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.sync_scope_service import SyncScopeService
from services.target_window_contract_service import (
    ResolvedTargetWindows,
    TargetFailureEvidence,
    TargetWindowContractService,
)
from ui.home import HomeView, SmartReconnectToggleViewResult


class PartialResolver:
    def __init__(self, unavailable=()):
        self.unavailable = {path.resolve() for path in unavailable}

    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
            if path.resolve() not in self.unavailable
        }


class WindowBackend:
    def __init__(self, windows=(), foreground=None, obscured=False):
        self.windows = tuple(windows)
        self.foreground = foreground
        self.obscured = obscured

    def list_windows(self):
        return self.windows

    def foreground_handle(self):
        return self.foreground

    def top_window_at(self, _x, _y):
        if self.obscured:
            return 999999
        return self.windows[0].handle if self.windows else None


class MarkerCapture:
    def __init__(self, markers):
        self.markers = dict(markers)
        self.calls = []

    def capture(self, handle):
        self.calls.append(handle)
        marker = self.markers.get(handle)
        if marker is None:
            return None
        return CaptureSample(
            width=2,
            height=2,
            pixels=bytes([marker, 0, 0, 255] * 4),
            api_succeeded=True,
        )


class MarkerRecognizer:
    def recognize_capture(self, sample):
        marker = sample.pixels[0]
        if marker == 3:
            return ScreenRecognition(
                ReconnectScreenState.LOGIN_START,
                0.0,
                (0.5, 0.8),
                "login_start",
            )
        if marker == 2:
            return ScreenRecognition(
                ReconnectScreenState.DISCONNECTED,
                0.0,
                (0.5, 0.5),
                "disconnected",
            )
        return ScreenRecognition(
            ReconnectScreenState.CONNECTED,
            0.0,
            None,
            "connected",
        )


class MouseBackend:
    def __init__(self):
        self.clicks = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout_ms):
        return True

    def click_relative(
        self,
        handle,
        point,
        _expected_process_id,
        _instance_token,
    ):
        self.clicks.append((handle, point))
        return MouseClickResult(True, True, False, None)

    def scroll_relative(
        self,
        handle,
        point,
        _delta,
        _expected_process_id,
        _instance_token,
    ):
        self.clicks.append((handle, point))
        return MouseClickResult(True, True, False, None)


def _shortcut(tmp_path, name):
    path = tmp_path / f"{name}.lnk"
    path.write_bytes(b"shortcut")
    return path


def _window(handle, fingerprint, *, minimized=False):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=minimized,
        rect=(0, 0, 900, 600),
        process_id=100 + handle,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
        thread_id=1000 + handle,
        process_lifecycle_token=10000 + handle,
    )


def _controller(
    windows,
    markers,
    *,
    target_windows_provider=None,
    capture_access_preparer=None,
    minimized_refresh_capture_provider=None,
    manual_shortcut_resolver=None,
    tcp_connection_count_provider=None,
):
    backend = WindowBackend(
        windows,
        foreground=(windows[0].handle if windows else None),
    )
    capture = MarkerCapture(markers)
    mouse = MouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=max(1, len(windows)),
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        primary_capture_is_trusted=True,
        recognizer=MarkerRecognizer(),
        mouse_backend=mouse,
        execution_enabled=False,
        require_expected_window_count=False,
        target_windows_provider=target_windows_provider,
        capture_access_preparer=capture_access_preparer,
        step_scoped_live_reconnect=True,
        minimized_refresh_capture_provider=(
            minimized_refresh_capture_provider
        ),
        manual_shortcut_resolver=manual_shortcut_resolver,
        tcp_connection_count_provider=tcp_connection_count_provider,
    )
    return controller, backend, capture, mouse


def test_global_reconnect_plan_survives_one_unresolved_saved_shortcut(tmp_path):
    usable = _shortcut(tmp_path, "可用角色")
    broken = _shortcut(tmp_path, "失效角色")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("可用組", (usable,))
    configuration.add_shortcuts("失效組", (broken,))
    service = SyncScopeService(
        configuration,
        PartialResolver((broken,)),
    )

    scope = service.configured_scope()
    plan = build_configured_reconnect_plan(
        scope,
        configuration.groups(),
        (),
        (),
    )

    assert scope.ready is True
    assert len(scope.isolated_entry_ids) == 1
    assert plan is not None
    assert plan.ready is True
    assert len(plan.targets) == 2
    assert plan.targets[0].fingerprint == hashlib.sha256(
        str(usable).encode()
    ).hexdigest()
    assert plan.targets[1].fingerprint == scope.entry_fingerprints[1]
    assert plan.targets[0].fingerprint != plan.targets[1].fingerprint


def test_one_bad_saved_shortcut_does_not_remove_a_live_manual_flash_target(
    tmp_path,
):
    usable = _shortcut(tmp_path, "手動開啟角色")
    broken = _shortcut(tmp_path, "失效舊角色")
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("可用組", (usable,))
    configuration.add_shortcuts("失效組", (broken,))
    usable_entry_id = configuration.group("可用組").main_entry.entry_id
    broken_entry_id = configuration.group("失效組").main_entry.entry_id
    resolver = PartialResolver((broken,))
    scope_service = SyncScopeService(configuration, resolver)
    usable_fingerprint = hashlib.sha256(str(usable).encode()).hexdigest()
    manual_window = _window(11, usable_fingerprint)
    contract = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        WindowBackend((manual_window,), foreground=11),
    )

    resolved = contract.configured_reconnect_targets()
    plan = build_configured_reconnect_plan(
        contract.configured_scope(),
        configuration.groups(),
        (),
        (),
    )

    assert resolved.global_failure_codes == ()
    assert tuple(window.handle for window in resolved.windows) == (11,)
    assert resolved.sync_entry_ids == (usable_entry_id,)
    assert len(resolved.target_failure_evidence) == 1
    assert resolved.target_failure_evidence[0].entry_id == broken_entry_id
    assert resolved.target_failure_evidence[0].failure_codes == ("window_offline",)
    assert plan is not None
    assert plan.ready is True


def test_empty_configured_scope_builds_a_clean_empty_enable_plan(tmp_path):
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    service = SyncScopeService(configuration, PartialResolver())

    inputs = service.configured_inputs()
    scope = service.configured_scope()
    plan = build_configured_reconnect_plan(
        scope,
        configuration.groups(),
        (),
        (),
    )

    assert inputs.group_name == "configured"
    assert inputs.entry_ids == ()
    assert inputs.failure_codes == ()
    assert inputs.ready is True
    assert scope.group_name == "configured"
    assert scope.entry_ids == ()
    assert scope.failure_codes == ()
    assert scope.ready is True
    assert plan is not None
    assert plan.group_name == "configured"
    assert plan.targets == ()
    assert plan.failure_codes == ()


def test_manual_flash_snapshot_falls_back_to_live_instance_without_role_plan():
    fingerprint = "a" * 64
    window = _window(1, fingerprint)
    permission_calls = []
    controller, _backend, _capture, mouse = _controller(
        (window,),
        {window.handle: 3},
        target_windows_provider=lambda: ResolvedTargetWindows(
            (),
            global_failure_codes=("group_name_invalid",),
        ),
        capture_access_preparer=lambda: permission_calls.append(True) or False,
    )
    controller.set_group_launch_plan(GroupLaunchPlan("configured", ()))

    prepared = controller.prepare_execution_snapshot()
    controller.set_execution_enabled(True)
    first = controller.reconnect()
    second = controller.reconnect()

    assert controller._manual_empty_plan_requested is True
    assert prepared.success is True
    assert prepared.details["window_count"] == 1
    assert permission_calls == []
    assert first.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 1
    assert [handle for handle, _point in mouse.clicks] == [window.handle]


def test_manual_unique_shortcut_grants_only_reopen_source(tmp_path):
    fingerprint = "d" * 64
    window = _window(4, fingerprint)
    shortcut = _shortcut(tmp_path, "手動角色")
    controller, _backend, _capture, _mouse = _controller(
        (window,),
        {window.handle: 1},
        target_windows_provider=lambda: ResolvedTargetWindows(
            (),
            global_failure_codes=("group_name_invalid",),
        ),
        manual_shortcut_resolver=(
            lambda requested: shortcut if requested == fingerprint else None
        ),
    )
    controller.set_group_launch_plan(GroupLaunchPlan("configured", ()))

    prepared = controller.prepare_execution_snapshot()

    assert prepared.success is True
    assert controller._manual_live_fingerprints == frozenset((fingerprint,))
    target = controller._manual_reopen_targets[fingerprint]
    assert target.shortcut_path == shortcut
    assert target.fingerprint == fingerprint
    assert target.entry_id == ""
    assert target.role_id == ""
    assert controller._group_launch_plan is not None
    assert controller._manual_runtime_plan_installed is True


def test_manual_without_unique_shortcut_keeps_live_actions_but_no_reopen_target():
    fingerprint = "e" * 64
    window = _window(5, fingerprint)
    controller, _backend, _capture, _mouse = _controller(
        (window,),
        {window.handle: 1},
        target_windows_provider=lambda: ResolvedTargetWindows(
            (),
            global_failure_codes=("group_name_invalid",),
        ),
        manual_shortcut_resolver=lambda _requested: None,
    )
    controller.set_group_launch_plan(GroupLaunchPlan("configured", ()))

    prepared = controller.prepare_execution_snapshot()

    assert prepared.success is True
    assert controller._manual_live_fingerprints == frozenset((fingerprint,))
    assert controller._manual_reopen_targets == {}
    assert controller._group_launch_plan is None


def test_detection_only_manual_flash_can_advance_login_without_formal_role(
    tmp_path,
):
    configured_shortcut = _shortcut(tmp_path, "configured")
    configured = _window(1, "a" * 64)
    manual = _window(2, "b" * 64)
    plan = GroupLaunchPlan(
        "configured",
        (
            GroupLaunchTarget(
                1,
                "configured-role",
                configured_shortcut,
                configured.launch_fingerprint,
                entry_id="entry-configured",
                role_id="configured-role",
            ),
        ),
    )
    resolved = ResolvedTargetWindows(
        windows=(configured,),
        sync_windows=(configured,),
        sync_entry_ids=("entry-configured",),
        sync_scope_entry_ids=("entry-configured",),
        sync_controller_entry_id="entry-configured",
        detection_only_windows=(manual,),
    )
    controller, _backend, _capture, mouse = _controller(
        (configured, manual),
        {
            configured.handle: 1,
            manual.handle: 3,
        },
        target_windows_provider=lambda: resolved,
    )
    controller.set_group_launch_plan(plan)

    prepared = controller.prepare_execution_snapshot()
    controller.set_execution_enabled(True)
    controller.reconnect()
    result = controller.reconnect()

    assert prepared.success is True
    assert manual.launch_fingerprint in controller._detection_only_fingerprints
    assert manual.launch_fingerprint in controller._manual_live_fingerprints
    assert manual.launch_fingerprint in controller._initial_login_authorizations or (
        manual.launch_fingerprint in controller._pending_reconnect_fingerprints
    )
    assert result.details["clicked_windows"] == 1
    assert [handle for handle, _point in mouse.clicks] == [manual.handle]
    assert manual.launch_fingerprint not in controller._login_only_recovery_fingerprints


def test_attributable_unknown_target_failure_stays_local_even_if_source_mirrors_it():
    healthy = _window(1, "a" * 64)
    failed = _window(2, "b" * 64)
    controller, _backend, _capture, _mouse = _controller(
        (healthy, failed),
        {healthy.handle: 1, failed.handle: 1},
    )
    resolved = ResolvedTargetWindows(
        windows=(healthy,),
        target_failure_evidence=(
            TargetFailureEvidence(
                "entry-failed",
                failed.launch_fingerprint,
                ("new_target_local_failure",),
                (failed,),
            ),
        ),
        global_failure_codes=("new_target_local_failure",),
    )

    global_failures, local_failures = (
        controller._contract_failure_evidence(resolved)
    )

    assert global_failures == ()
    assert local_failures == {
        failed.launch_fingerprint: ("new_target_local_failure",)
    }


def test_activation_isolates_one_attributable_bad_configured_window(tmp_path):
    healthy_shortcut = _shortcut(tmp_path, "healthy")
    failed_shortcut = _shortcut(tmp_path, "failed")
    healthy = _window(1, "a" * 64)
    failed = _window(2, "b" * 64)
    plan = GroupLaunchPlan(
        "configured",
        (
            GroupLaunchTarget(
                1,
                "healthy",
                healthy_shortcut,
                healthy.launch_fingerprint,
                entry_id="entry-healthy",
                role_id="healthy",
            ),
            GroupLaunchTarget(
                2,
                "failed",
                failed_shortcut,
                failed.launch_fingerprint,
                entry_id="entry-failed",
                role_id="failed",
            ),
        ),
    )
    resolved = ResolvedTargetWindows(
        windows=(healthy,),
        sync_windows=(healthy,),
        sync_entry_ids=("entry-healthy",),
        sync_scope_entry_ids=("entry-healthy", "entry-failed"),
        sync_controller_entry_id="entry-healthy",
        target_failure_evidence=(
            TargetFailureEvidence(
                "entry-failed",
                failed.launch_fingerprint,
                ("new_target_local_failure",),
                (failed,),
            ),
        ),
        global_failure_codes=("new_target_local_failure",),
    )
    controller, _backend, _capture, _mouse = _controller(
        (healthy, failed),
        {healthy.handle: 1, failed.handle: 1},
        target_windows_provider=lambda: resolved,
    )
    controller.set_group_launch_plan(plan)

    prepared = controller.prepare_execution_snapshot()

    assert prepared.success is True
    assert prepared.details["window_count"] == 1
    assert prepared.details["isolated_window_count"] >= 1
    assert tuple(controller._activation_snapshot_instances) == (
        healthy.launch_fingerprint,
    )


def test_mixed_manual_scope_temporarily_uses_visual_recovery_without_losing_tcp_provider(
    monkeypatch,
):
    fingerprint = "a" * 64
    window = _window(1, fingerprint)
    tcp_provider = lambda _ids: {}
    controller, _backend, _capture, _mouse = _controller(
        (window,),
        {window.handle: 1},
        tcp_connection_count_provider=tcp_provider,
    )
    controller._step_scoped_activation_ready = True
    controller._manual_live_fingerprints = frozenset((fingerprint,))

    observed = []

    def fake_scan(instance, *, execute):
        observed.append((instance._tcp_counts, execute))
        return "sentinel"

    monkeypatch.setattr(
        smart_reconnect_base.WindowsSmartReconnectController,
        "_scan_locked",
        fake_scan,
    )

    result = controller._scan_locked(execute=True)

    assert result == "sentinel"
    assert observed == [(None, True)]
    assert controller._tcp_counts is tcp_provider


def test_minimized_login_can_reach_mouse_backend_through_recovering_route():
    fingerprint = "c" * 64
    window = _window(3, fingerprint, minimized=True)
    refresh = MarkerCapture({window.handle: 3})
    controller, _backend, primary, mouse = _controller(
        (window,),
        {window.handle: 3},
        minimized_refresh_capture_provider=refresh,
    )

    prepared = controller.prepare_execution_snapshot()
    controller.set_execution_enabled(True)
    first = controller.reconnect()
    second = controller.reconnect()

    assert prepared.success is True
    assert first.details["clicked_windows"] == 0
    assert second.details["clicked_windows"] == 1
    assert len(refresh.calls) >= 2
    assert set(refresh.calls) == {window.handle}
    assert primary.calls == []
    assert [handle for handle, _point in mouse.clicks] == [window.handle]


def test_minimized_activation_uses_dedicated_refresh_provider_without_wgc_access():
    fingerprint = "f" * 64
    window = _window(6, fingerprint, minimized=True)
    refresh = MarkerCapture({window.handle: 1})
    permission_calls = []
    controller, _backend, primary, mouse = _controller(
        (window,),
        {window.handle: 1},
        capture_access_preparer=lambda: permission_calls.append(True) or False,
        minimized_refresh_capture_provider=refresh,
    )

    prepared = controller.prepare_execution_snapshot()
    controller.set_execution_enabled(True)
    result = controller.reconnect()

    assert prepared.success is True
    assert permission_calls == []
    assert refresh.calls == [window.handle]
    assert primary.calls == []
    assert result.details["connected_windows"] == 1
    assert mouse.clicks == []


def test_production_scope_wording_never_requires_manual_window_to_join_current_group():
    result_init = SmartReconnectToggleViewResult.__init__
    display_method = HomeView._smart_reconnect_failure_display
    result_marker = getattr(
        SmartReconnectToggleViewResult,
        "_fu_reconnect_scope_normalized",
        None,
    )
    home_marker = getattr(HomeView, "_fu_reconnect_scope_normalized", None)
    try:
        WindowsSmartReconnectController._install_product_scope_message_normalization()
        result = SmartReconnectToggleViewResult(
            False,
            False,
            "目前組別的安全視窗身分尚未完成，智慧重連未啟用。",
        )
        view = object.__new__(HomeView)
        view._smart_reconnect_failure_message = result.message
        display = view._smart_reconnect_failure_display()

        assert "目前組別" not in result.message
        assert "加入目前組別" not in display
        assert "唯一可靠捷徑來源" in display
        assert "智慧重連安全操作尚未完成" in display
    finally:
        SmartReconnectToggleViewResult.__init__ = result_init
        HomeView._smart_reconnect_failure_display = display_method
        if result_marker is None:
            try:
                delattr(
                    SmartReconnectToggleViewResult,
                    "_fu_reconnect_scope_normalized",
                )
            except AttributeError:
                pass
        else:
            SmartReconnectToggleViewResult._fu_reconnect_scope_normalized = (
                result_marker
            )
        if home_marker is None:
            try:
                delattr(HomeView, "_fu_reconnect_scope_normalized")
            except AttributeError:
                pass
        else:
            HomeView._fu_reconnect_scope_normalized = home_marker
