from pathlib import Path
from dataclasses import replace

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
    def recognize_capture(self, _sample):
        return ScreenRecognition(ReconnectScreenState.CONNECTED, 1.0, None, "connected")


class _Windows:
    def __init__(self, window): self.window = window
    def list_windows(self): return [self.window]
    def foreground_handle(self): return self.window.handle
    def top_window_at(self, _x, _y): return self.window.handle


class _Capture:
    def __init__(self, frames, after=None): self.frames, self.after, self.calls = list(frames), after, 0
    def capture(self, _handle):
        frame = self.frames.pop(0) if self.frames else None
        self.calls += 1
        if self.after is not None: self.after(self.calls)
        return frame


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


def _controller(frames):
    window = WindowInfo(1, "Adobe Flash Player", True, False, (0, 0, 900, 600), 2, "ShockwaveFlash", "a" * 64, 3, 4)
    mouse = _Mouse()
    controller = WindowsSmartReconnectController(
        expected_windows=1, title_keywords=("Adobe Flash Player",),
        window_backend=_Windows(window), capture_provider=_Capture(frames),
        recognizer=_ConnectedRecognizer(), mouse_backend=mouse,
        primary_capture_is_trusted=True,
        primary_capture_is_fresh_without_visibility=True,
        execution_enabled=True, require_expected_window_count=False,
        auto_battle_enabled=True, auto_battle_recognizer=AutoBattleRecognizer(ROOT),
    )
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
    for expected_generation, route, revision, token_change in ((1, "visible", 0, False), (0, "minimized", 0, False), (0, "visible", 1, False), (0, "visible", 0, True)):
        controller, window, mouse = _controller([_sample(frame), _sample(frame)])
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
        assert not controller._run_auto_battle_for_connected(window, "a" * 64, WindowInstanceToken.from_window(window), _sample(frame), "visible", 0, 0)
        assert len(mouse.clicks) == 1 and not controller._auto_battle_evidence


def test_auto_battle_single_frame_and_total_switch_cannot_reuse_old_evidence() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    frame = Image.new("RGB", (900, 600), "#112233"); frame.paste(template, (810, 500))
    controller, window, mouse = _controller([])
    assert not controller._run_auto_battle_for_connected(window, "a" * 64, WindowInstanceToken.from_window(window), _sample(frame), "visible", 0, 0)
    assert mouse.clicks == []
    controller._auto_battle_evidence["a" * 64] = (WindowInstanceToken.from_window(window), "visible", 0, 0, (810, 500, 875, 571))
    controller.set_execution_enabled(False)
    controller.set_execution_enabled(True)
    assert not controller._auto_battle_evidence


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
        execution_enabled=True, require_expected_window_count=False, auto_battle_enabled=True,
        auto_battle_recognizer=AutoBattleRecognizer(ROOT),
    )
    beta_token, gamma_token = WindowInstanceToken.from_window(beta), WindowInstanceToken.from_window(gamma)
    controller._auto_battle_evidence["b" * 64] = (beta_token, "visible", 0, 0, (1, 1, 2, 2))
    controller._auto_battle_evidence["c" * 64] = (gamma_token, "visible", 0, 0, (1, 1, 2, 2))
    assert controller._run_auto_battle_for_connected(alpha, "a" * 64, WindowInstanceToken.from_window(alpha), _sample(red_x), "visible", 0, 0)
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


def test_public_reconnect_duplicate_identity_never_delivers_auto_battle_click() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    red_x = Image.new("RGB", (900, 600), "#112233"); red_x.paste(template, (810, 500))
    controller, window, mouse = _controller([_sample(red_x), _sample(red_x), _sample(red_x)])
    duplicate = replace(window, handle=2, process_id=5, thread_id=6, process_lifecycle_token=7)
    controller._window_backend.list_windows = lambda: [window, duplicate]
    controller.reconnect()
    assert mouse.clicks == []
    assert not controller._auto_battle_evidence
