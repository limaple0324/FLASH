from pathlib import Path
from dataclasses import replace
import hashlib
import json

from PIL import Image

from adapters.windows_auto_battle import AutoBattleRecognizer
from adapters.windows_background_capture import CaptureSample
from adapters.windows_smart_reconnect import (
    MouseClickResult,
    WindowInstanceToken,
    WindowsSmartReconnectController,
)
from adapters.windows_window import WindowInfo
from adapters.game_screen_recognizer import ScreenRecognition
from core.reconnect_policy import ReconnectScreenState
from services.smart_reconnect_capture_settings_service import SmartReconnectCaptureSettings


ROOT = Path("assets/reconnect_reference/auto_battle")


def test_four_saved_images_are_offline_and_fail_closed() -> None:
    recognizer = AutoBattleRecognizer(ROOT)
    assert recognizer.ready
    for name in (
        "enabled_full_panel.png",
        "enabled_battle_full_panel.png",
        "enabled_start_full_panel.png",
    ):
        with Image.open(ROOT / name) as image:
            evidence = recognizer.read(image)
        assert evidence.enabled and not evidence.disabled
    with Image.open(ROOT / "disabled_red_x_with_context.png") as image:
        evidence = recognizer.read(image)
    assert not evidence.disabled and not evidence.enabled
    with Image.open(ROOT / "normal_game_with_entry.png") as image:
        evidence = recognizer.read(image)
    assert not evidence.enabled and not evidence.disabled


def test_red_x_requires_current_full_image_box_in_approved_search_area() -> None:
    recognizer = AutoBattleRecognizer(ROOT)
    with Image.open(ROOT / "disabled_red_x_with_context.png") as image:
        template = image.convert("RGB")
    full = Image.new("RGB", (900, 600), "#112233")
    full.paste(template, (810, 500))
    evidence = recognizer.read(full)
    assert evidence.disabled
    assert evidence.red_x_box == (810, 500, 875, 571)
    assert evidence.red_x_center == (842.5, 535.5)
    outside = Image.new("RGB", (900, 600), "#112233")
    outside.paste(template, (200, 100))
    assert not recognizer.read(outside).disabled


class _ConnectedRecognizer:
    def __init__(
        self,
        state=ReconnectScreenState.CONNECTED,
    ):
        self.state = state

    def recognize_capture(self, _sample):
        return ScreenRecognition(
            self.state,
            1.0,
            None,
            self.state.value,
        )


class _Windows:
    def __init__(self, window): self.window = window
    def list_windows(self): return [self.window]
    def foreground_handle(self): return self.window.handle
    def top_window_at(self, _x, _y): return self.window.handle


class _Capture:
    def __init__(self, frames, after=None, clock=None):
        self.frames = list(frames)
        self.after = after
        self.clock = clock
        self.calls = 0
        self.capture_times = []

    def capture(self, _handle):
        frame = self.frames.pop(0) if self.frames else None
        self.calls += 1
        if self.clock is not None:
            self.capture_times.append(self.clock())
        if self.after is not None: self.after(self.calls)
        return frame


class _ForbiddenCapture:
    def __init__(self):
        self.calls = 0

    def capture(self, _handle):
        self.calls += 1
        raise AssertionError("forbidden capture path was used")


class _FailedStageCapture:
    def __init__(self, stage):
        self.last_failure_stage = stage
        self.calls = 0

    def capture(self, _handle):
        self.calls += 1
        return None


class _Mouse:
    def __init__(self): self.clicks = []
    def is_window(self, _handle): return True
    def probe_responsive(self, _handle, _timeout): return True
    def click_relative(self, handle, point, _pid, _token):
        self.clicks.append((handle, point))
        if hasattr(self, "after_click"):
            self.after_click()
        return MouseClickResult(True, True, False, None)


def _sample(image):
    rgba = image.convert("RGBA")
    return CaptureSample(
        rgba.width, rgba.height, rgba.tobytes("raw", "BGRA"), True
    )


def _red_x_full_sample():
    with Image.open(ROOT / "disabled_red_x_with_context.png") as image:
        template = image.convert("RGB")
    frame = Image.new("RGB", (900, 600), "#112233")
    frame.paste(template, (810, 500))
    return _sample(frame)


def _controller(
    frames,
    *,
    monotonic_clock=None,
    general_state=ReconnectScreenState.CONNECTED,
):
    window = WindowInfo(1, "Adobe Flash Player", True, False, (0, 0, 900, 600), 2, "ShockwaveFlash", "a" * 64, 3, 4)
    mouse = _Mouse()
    capture = _Capture(frames, clock=monotonic_clock)
    clock = monotonic_clock or (lambda: 0.0)
    controller = WindowsSmartReconnectController(
        expected_windows=1, title_keywords=("Adobe Flash Player",),
        window_backend=_Windows(window), capture_provider=capture,
        recognizer=_ConnectedRecognizer(general_state), mouse_backend=mouse,
        primary_capture_is_trusted=True,
        primary_capture_is_fresh_without_visibility=True,
        execution_enabled=False, require_expected_window_count=False,
        monotonic_clock=clock,
        auto_battle_enabled=True, auto_battle_recognizer=AutoBattleRecognizer(ROOT),
    )
    prepared = controller.prepare_execution_snapshot()
    assert prepared.success is True
    controller.set_execution_enabled(True)
    return controller, window, mouse


def test_connected_auto_battle_clicks_once_then_requires_third_panel() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    first = Image.new("RGB", (900, 600), "#112233"); first.paste(template, (810, 500))
    with Image.open(ROOT / "enabled_battle_full_panel.png") as i: enabled = i.convert("RGB")
    controller, window, mouse = _controller([_sample(first), _sample(enabled)])
    assert controller._run_auto_battle_for_connected(window, "a" * 64, WindowInstanceToken.from_window(window), _sample(first), "visible", 0, controller._source_state_generation)
    assert len(mouse.clicks) == 1


def test_connected_auto_battle_third_unconfirmed_never_retries_or_reports_success() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    frame = Image.new("RGB", (900, 600), "#112233"); frame.paste(template, (810, 500))
    controller, window, mouse = _controller([_sample(frame), _sample(frame)])
    assert not controller._run_auto_battle_for_connected(window, "a" * 64, WindowInstanceToken.from_window(window), _sample(frame), "visible", 0, controller._source_state_generation)
    assert len(mouse.clicks) == 1
    assert not controller._auto_battle_evidence


def test_auto_battle_preclick_generation_token_route_and_revision_changes_never_click() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    frame = Image.new("RGB", (900, 600), "#112233"); frame.paste(template, (810, 500))
    for generation_offset, route, revision, token_change in ((1, "visible", 0, False), (0, "minimized", 0, False), (0, "visible", 1, False), (0, "visible", 0, True)):
        controller, window, mouse = _controller([_sample(frame), _sample(frame)])
        expected_generation = (
            controller._source_state_generation + generation_offset
        )
        token = WindowInstanceToken.from_window(window)
        if token_change: token = replace(token, process_id=999)
        assert not controller._run_auto_battle_for_connected(window, "a" * 64, token, _sample(frame), route, revision, expected_generation)
        assert mouse.clicks == [] and not controller._auto_battle_evidence


def test_auto_battle_postclick_source_or_identity_change_is_not_success() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    frame = Image.new("RGB", (900, 600), "#112233"); frame.paste(template, (810, 500))
    with Image.open(ROOT / "enabled_battle_full_panel.png") as i: enabled = i.convert("RGB")
    for change_identity in (False, True):
        controller, window, mouse = _controller([_sample(frame), _sample(enabled)])
        def revoke():
            if change_identity: controller._window_backend.window = replace(window, process_id=99)
            else: controller._source_state_generation += 1
        mouse.after_click = revoke
        assert not controller._run_auto_battle_for_connected(
            window,
            "a" * 64,
            WindowInstanceToken.from_window(window),
            _sample(frame),
            "visible",
            0,
            controller._source_state_generation,
        )
        assert len(mouse.clicks) == 1 and not controller._auto_battle_evidence


def test_auto_battle_single_frame_and_total_switch_cannot_reuse_old_evidence() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    frame = Image.new("RGB", (900, 600), "#112233"); frame.paste(template, (810, 500))
    controller, window, mouse = _controller([])
    assert not controller._run_auto_battle_for_connected(
        window,
        "a" * 64,
        WindowInstanceToken.from_window(window),
        _sample(frame),
        "visible",
        0,
        controller._source_state_generation,
    )
    assert mouse.clicks == []
    controller._auto_battle_evidence["a" * 64] = (WindowInstanceToken.from_window(window), "visible", 0, 0, (810, 500, 875, 571))
    controller.set_execution_enabled(False)
    assert controller.auto_battle_enabled is True
    assert not controller.auto_battle_execution_allowed()
    controller.set_execution_enabled(True)
    assert not controller._auto_battle_evidence
    assert controller.auto_battle_execution_allowed()
    controller.set_auto_battle_enabled(False)
    assert not controller.auto_battle_execution_allowed()


def test_activation_snapshot_preserves_saved_auto_battle_sub_switch() -> None:
    controller, _window, _mouse = _controller([])
    controller.set_execution_enabled(False)

    prepared = controller.prepare_execution_snapshot()

    assert prepared.success is True
    assert controller.auto_battle_enabled is True
    assert not controller.auto_battle_execution_allowed()
    controller.set_execution_enabled(True)
    assert controller.auto_battle_execution_allowed()


def test_three_role_auto_battle_only_delivers_to_confirmed_role() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    red_x = Image.new("RGB", (900, 600), "#112233"); red_x.paste(template, (810, 500))
    with Image.open(ROOT / "enabled_battle_full_panel.png") as i: enabled = i.convert("RGB")
    alpha = WindowInfo(1, "Adobe Flash Player", True, False, (0, 0, 900, 600), 2, "ShockwaveFlash", "a" * 64, 3, 4)
    beta = WindowInfo(2, "Adobe Flash Player", True, False, (0, 0, 900, 600), 5, "ShockwaveFlash", "b" * 64, 6, 7)
    gamma = WindowInfo(3, "Adobe Flash Player", True, False, (0, 0, 900, 600), 8, "ShockwaveFlash", "c" * 64, 9, 10)
    backend = _Windows(alpha); backend.list_windows = lambda: [alpha, beta, gamma]
    mouse = _Mouse()
    controller = WindowsSmartReconnectController(
        expected_windows=3, title_keywords=("Adobe Flash Player",), window_backend=backend,
        capture_provider=_Capture([_sample(red_x), _sample(enabled)]), recognizer=_ConnectedRecognizer(),
        mouse_backend=mouse, primary_capture_is_trusted=True, primary_capture_is_fresh_without_visibility=True,
        execution_enabled=False, require_expected_window_count=False, auto_battle_enabled=True,
        auto_battle_recognizer=AutoBattleRecognizer(ROOT),
    )
    prepared = controller.prepare_execution_snapshot()
    assert prepared.success is True
    controller.set_execution_enabled(True)
    beta_token, gamma_token = WindowInstanceToken.from_window(beta), WindowInstanceToken.from_window(gamma)
    controller._auto_battle_evidence["b" * 64] = (beta_token, "visible", 0, 0, (1, 1, 2, 2))
    controller._auto_battle_evidence["c" * 64] = (gamma_token, "visible", 0, 0, (1, 1, 2, 2))
    assert controller._run_auto_battle_for_connected(
        alpha,
        "a" * 64,
        WindowInstanceToken.from_window(alpha),
        _sample(red_x),
        "visible",
        0,
        controller._source_state_generation,
    )
    assert [handle for handle, _point in mouse.clicks] == [alpha.handle]
    assert controller._auto_battle_evidence["b" * 64][0] == beta_token
    assert controller._auto_battle_evidence["c" * 64][0] == gamma_token
    controller.set_auto_battle_enabled(False)
    controller.set_auto_battle_enabled(True)
    assert not controller._auto_battle_evidence


def test_public_reconnect_auto_battle_success_and_revision_changes_fail_closed() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    red_x = Image.new("RGB", (900, 600), "#112233"); red_x.paste(template, (810, 500))
    with Image.open(ROOT / "enabled_battle_full_panel.png") as i: enabled = i.convert("RGB")
    for change_on, expected_clicks in ((0, 1), (2, 0), (3, 1)):
        controller, _window, mouse = _controller([_sample(red_x), _sample(red_x), _sample(enabled)])
        if change_on:
            controller._capture_provider.after = lambda call: (
                controller.set_capture_settings(SmartReconnectCaptureSettings(visible=False))
                if call == change_on else None
            )
        controller.reconnect()
        assert len(mouse.clicks) == expected_clicks
        assert not controller._auto_battle_evidence


def test_public_scan_finishes_three_frame_auto_battle_without_next_cycle() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as image:
        template = image.convert("RGB")
    disabled = Image.new("RGB", (900, 600), "#112233")
    disabled.paste(template, (810, 500))
    with Image.open(ROOT / "enabled_battle_full_panel.png") as image:
        enabled = image.convert("RGB")
    now = 125.0
    controller, _window, mouse = _controller(
        [_sample(disabled), _sample(disabled), _sample(enabled)],
        monotonic_clock=lambda: now,
    )
    capture = controller._capture_provider

    result = controller.reconnect()

    assert result.success is True
    assert capture.calls == 3
    assert capture.capture_times == [now, now, now]
    assert len(mouse.clicks) == 1
    diagnostics = result.details["capture_diagnostics"]
    assert [item["stage"] for item in diagnostics] == [
        "scan",
        "auto_battle_second_frame",
        "auto_battle_third_frame",
    ]


def test_public_unknown_gameplay_uses_exact_auto_battle_evidence_only() -> None:
    disabled = _red_x_full_sample()
    with Image.open(ROOT / "enabled_battle_full_panel.png") as image:
        enabled = _sample(image.convert("RGB"))
    controller, _window, mouse = _controller(
        [disabled, disabled, enabled],
        general_state=ReconnectScreenState.UNKNOWN,
    )

    result = controller.reconnect()

    assert controller._capture_provider.calls == 3
    assert len(mouse.clicks) == 1
    assert result.details["state_counts"] == {"unknown": 1}
    assert "screen_unknown" in result.details["failure_codes"]


def test_public_unknown_gameplay_with_unknown_auto_evidence_never_clicks() -> None:
    with Image.open(ROOT / "normal_game_with_entry.png") as image:
        unknown = _sample(image.convert("RGB"))
    controller, _window, mouse = _controller(
        [unknown],
        general_state=ReconnectScreenState.UNKNOWN,
    )

    result = controller.reconnect()

    assert controller._capture_provider.calls == 1
    assert mouse.clicks == []
    assert result.details["state_counts"] == {"unknown": 1}


def test_public_unknown_enabled_panel_is_confirmed_without_click() -> None:
    with Image.open(ROOT / "enabled_battle_full_panel.png") as image:
        enabled = _sample(image.convert("RGB"))
    controller, _window, mouse = _controller(
        [enabled],
        general_state=ReconnectScreenState.UNKNOWN,
    )

    result = controller.reconnect()

    assert controller._capture_provider.calls == 1
    assert mouse.clicks == []
    assert result.details["state_counts"] == {"unknown": 1}


def test_public_known_non_gameplay_states_exclude_auto_battle() -> None:
    excluded_states = (
        ReconnectScreenState.DISCONNECTED,
        ReconnectScreenState.LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_START,
        ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
        ReconnectScreenState.LINE_SELECTION,
        ReconnectScreenState.CHARACTER_SELECTION,
        ReconnectScreenState.POST_LOGIN_ACTIVITY,
        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
        ReconnectScreenState.RECONNECTING,
        ReconnectScreenState.FAILED,
        ReconnectScreenState.CHECK_DISABLED,
    )
    for state in excluded_states:
        controller, _window, mouse = _controller(
            [_red_x_full_sample()],
            general_state=state,
        )

        controller.reconnect()

        assert mouse.clicks == [], state


def test_auto_battle_off_in_final_authorization_gap_never_clicks() -> None:
    disabled = _red_x_full_sample()
    controller, _window, mouse = _controller([disabled, disabled])
    original_confirmation = controller._action_is_confirmed
    original_current_window = controller._current_action_window
    final_gap_armed = {"value": False}

    def confirm_then_arm(*args, **kwargs):
        confirmed = original_confirmation(*args, **kwargs)
        if confirmed:
            final_gap_armed["value"] = True
        return confirmed

    def turn_off_after_preflight(*args, **kwargs):
        current = original_current_window(*args, **kwargs)
        if final_gap_armed["value"]:
            final_gap_armed["value"] = False
            controller.set_auto_battle_enabled(False)
        return current

    controller._action_is_confirmed = confirm_then_arm
    controller._current_action_window = turn_off_after_preflight

    controller.reconnect()

    assert controller.auto_battle_enabled is False
    assert mouse.clicks == []
    assert not controller._auto_battle_evidence


def test_manual_auto_battle_off_during_same_scan_clears_and_blocks_click() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as image:
        template = image.convert("RGB")
    disabled = Image.new("RGB", (900, 600), "#112233")
    disabled.paste(template, (810, 500))
    controller, _window, mouse = _controller(
        [_sample(disabled), _sample(disabled)],
    )
    controller._capture_provider.after = lambda call: (
        controller.set_auto_battle_enabled(False)
        if call == 2
        else None
    )

    controller.reconnect()

    assert controller.auto_battle_enabled is False
    assert mouse.clicks == []
    assert not controller._auto_battle_evidence


def test_four_window_diagnostics_are_anonymous_hash_only_records() -> None:
    image = Image.new("RGB", (900, 600), "#123456")
    frame = _sample(image)
    windows = tuple(
        WindowInfo(
            index,
            "Adobe Flash Player",
            True,
            False,
            (0, 0, 900, 600),
            100 + index,
            "ShockwaveFlash",
            f"{index:064x}",
            200 + index,
            300 + index,
        )
        for index in range(1, 5)
    )
    backend = _Windows(windows[0])
    backend.list_windows = lambda: list(windows)
    capture = _Capture([frame] * 4)
    controller = WindowsSmartReconnectController(
        expected_windows=4,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        recognizer=_ConnectedRecognizer(),
        mouse_backend=_Mouse(),
        primary_capture_is_trusted=True,
        primary_capture_is_fresh_without_visibility=True,
        execution_enabled=True,
        require_expected_window_count=False,
        auto_battle_enabled=False,
    )

    result = controller.reconnect()

    diagnostics = result.details["capture_diagnostics"]
    assert len(diagnostics) == 4
    assert [item["window_index"] for item in diagnostics] == [1, 2, 3, 4]
    assert all(item["capture_path"] == "visible" for item in diagnostics)
    assert all(item["width"] == 900 and item["height"] == 600 for item in diagnostics)
    assert all(item["sha256"] == hashlib.sha256(frame.pixels).hexdigest() for item in diagnostics)
    assert all(item["rejection_gate"] is None for item in diagnostics)
    serialized = json.dumps(diagnostics)
    for forbidden in ("fingerprint", "handle", "role", "pixels", "arguments"):
        assert forbidden not in serialized


def test_eight_minimized_windows_use_only_active_fresh_capture_path() -> None:
    image = Image.new("RGB", (900, 600), "#654321")
    frame = _sample(image)
    windows = tuple(
        WindowInfo(
            index,
            "Adobe Flash Player",
            True,
            True,
            (0, 0, 900, 600),
            100 + index,
            "ShockwaveFlash",
            f"{index:064x}",
            200 + index,
            300 + index,
        )
        for index in range(1, 9)
    )
    backend = _Windows(windows[0])
    backend.list_windows = lambda: list(windows)
    passive = _ForbiddenCapture()
    active = _Capture([frame] * 8)
    controller = WindowsSmartReconnectController(
        expected_windows=8,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=passive,
        active_refresh_capture_provider=active,
        recognizer=_ConnectedRecognizer(),
        mouse_backend=_Mouse(),
        execution_enabled=True,
        require_expected_window_count=False,
        auto_battle_enabled=False,
    )

    result = controller.reconnect()

    assert result.success is True
    assert active.calls == 8
    assert passive.calls == 0
    assert result.details["captured_windows"] == 8
    assert all(
        item["capture_path"] == "minimized"
        and item["rejection_gate"] is None
        for item in result.details["capture_diagnostics"]
    )


def test_minimized_restore_failure_exposes_only_anonymous_stage() -> None:
    window = WindowInfo(
        1,
        "Adobe Flash Player",
        True,
        True,
        (0, 0, 900, 600),
        101,
        "ShockwaveFlash",
        "a" * 64,
        201,
        301,
    )
    active = _FailedStageCapture("restoration_barrier_failed")
    controller = WindowsSmartReconnectController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=_Windows(window),
        capture_provider=_ForbiddenCapture(),
        active_refresh_capture_provider=active,
        recognizer=_ConnectedRecognizer(),
        mouse_backend=_Mouse(),
        execution_enabled=True,
        require_expected_window_count=False,
        auto_battle_enabled=False,
    )

    result = controller.reconnect()

    diagnostic = result.details["capture_diagnostics"][0]
    assert diagnostic == {
        "window_index": 1,
        "stage": "scan",
        "capture_path": "minimized",
        "width": None,
        "height": None,
        "sha256": None,
        "recognition_score": None,
        "rejection_gate": "restoration_barrier_failed",
    }


def test_public_reconnect_duplicate_identity_never_delivers_auto_battle_click() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    red_x = Image.new("RGB", (900, 600), "#112233"); red_x.paste(template, (810, 500))
    controller, window, mouse = _controller([_sample(red_x), _sample(red_x), _sample(red_x)])
    duplicate = replace(window, handle=2, process_id=5, thread_id=6, process_lifecycle_token=7)
    controller._window_backend.list_windows = lambda: [window, duplicate]
    controller.reconnect()
    assert mouse.clicks == []
    assert not controller._auto_battle_evidence
