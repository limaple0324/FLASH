from pathlib import Path
from dataclasses import replace
import hashlib
import json
import pytest

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
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectSourceIdentity,
    ShortcutFileIdentity,
    ShortcutSeal,
)
from domain.character import CharacterImportance
from services.smart_reconnect_authorization_coordinator import (
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_capture_settings_service import SmartReconnectCaptureSettings


ROOT = Path("assets/reconnect_reference/auto_battle")
BATTLE_AUTO_SOURCE = Path(
    r"C:\Users\USER\AppData\Local\Temp\codex-clipboard-7dbb2ee0-ea3b-4e81-9bc8-20d77470a266.png"
)
BATTLE_AUTO_SOURCE_SHA256 = (
    "61c7408b3ecc44c47fd4cf63c8399ac5d95ab8ca625320a8e032820f075975a7"
)


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


def test_enabled_panel_is_same_size_background_independent_but_not_partial() -> None:
    recognizer = AutoBattleRecognizer(ROOT)
    varied = _enabled_battle_frame(
        (1336, 858),
        background_variant=True,
    )
    evidence = recognizer.read(varied)

    assert evidence.enabled is True
    assert evidence.disabled is False

    panel_box = (
        round(varied.width * 0.738),
        0,
        varied.width,
        round(varied.height * 0.22),
    )
    collage = Image.new("RGB", varied.size, "#112233")
    collage.paste(varied.crop(panel_box), panel_box[:2])
    assert recognizer.read(collage).enabled is False

    panel_only = varied.crop(panel_box).resize(
        varied.size,
        Image.Resampling.BILINEAR,
    )
    assert recognizer.read(panel_only).enabled is False


def test_every_loaded_or_derived_auto_battle_asset_has_source_traceability() -> None:
    records = json.loads((ROOT / "source_records.json").read_text(encoding="utf-8"))[
        "records"
    ]
    by_file = {item["file"]: item for item in records}
    required = {
        "disabled_red_x_with_context.png",
        "entry_icon.png",
        "battle_auto_button.png",
        "battle_manual_auto_structure.png",
        "enabled_full_panel.png",
        "enabled_battle_full_panel.png",
        "enabled_start_full_panel.png",
    }

    assert required <= set(by_file)
    for filename in required:
        path = ROOT / filename
        record = by_file[filename]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        with Image.open(path) as image:
            assert image.size == (record["width"], record["height"])
    assert not (ROOT / BATTLE_AUTO_SOURCE.name).exists()


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
        *,
        battle_context=False,
    ):
        self.state = state
        self.battle_context = battle_context

    def recognize_capture(self, _sample):
        return ScreenRecognition(
            self.state,
            1.0,
            None,
            self.state.value,
            battle_context=self.battle_context,
        )


class _Windows:
    def __init__(self, window): self.window = window
    def list_windows(self): return [self.window]
    def foreground_handle(self): return self.window.handle
    def top_window_at(self, _x, _y): return self.window.handle


class _PublishedShortcutSealResolver:
    def __init__(self):
        self._published = {}

    def publish(self, targets):
        self._published = {
            target.fingerprint: target.shortcut_seal
            for target in targets
            if target.shortcut_seal is not None
        }

    def revalidate(self, expected_seal):
        return bool(
            isinstance(expected_seal, ShortcutSeal)
            and self._published.get(expected_seal.launch_fingerprint)
            == expected_seal
        )


class _AuthorizationPreparation:
    def __init__(self, coordinator, window_backend):
        self.authorization_coordinator = coordinator
        self._window_backend = window_backend
        self._generation = 1
        self.shortcut_seal_resolver = _PublishedShortcutSealResolver()

    def prepare(self, *, launch_mode):
        assert launch_mode is ReconnectLaunchMode.IDENTITY_BOUND
        windows = tuple(self._window_backend.list_windows())
        targets = []
        for index, window in enumerate(windows, start=1):
            instance = WindowInstanceToken.from_window(window)
            if instance is None:
                return None
            targets.append(
                ReconnectAuthorizationTarget(
                    fingerprint=window.launch_fingerprint,
                    instance=instance,
                    character_id=f"auto-character-{index}",
                    role_aliases=(f"{index:03x}-auto-role",),
                    importance=(
                        CharacterImportance.PRIMARY
                        if index == 1
                        else CharacterImportance.SECONDARY
                    ),
                    original_slot_index=(index - 1) % 3,
                    original_line_number=1,
                    shortcut_seal=ShortcutSeal(
                        ShortcutFileIdentity(
                            f"C:/FLASH_TEST/auto-role-{index}.lnk",
                            1,
                            index,
                        ),
                        f"{index:064x}",
                        window.launch_fingerprint,
                    ),
                )
            )
        source = ReconnectSourceIdentity(
            identity_generation=self._generation,
            config_revision=1,
            group_id="auto-battle-test-group",
            group_name="auto-battle-test-group",
            character_ids=tuple(target.character_id for target in targets),
        )
        self._generation += 1
        batch = self.authorization_coordinator.publish(
            source,
            launch_mode,
            tuple(targets),
        )
        self.shortcut_seal_resolver.publish(batch.targets)
        return batch


def _authorization_services(window_backend):
    coordinator = SmartReconnectAuthorizationCoordinator()
    return coordinator, _AuthorizationPreparation(coordinator, window_backend)


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


def _battle_button_frame():
    frame = Image.new("RGB", (1336, 858), "#112233")
    with Image.open(ROOT / "battle_auto_button.png") as image:
        frame.paste(image.convert("RGB"), (1148, 498))
    return frame


def _enabled_battle_frame(size=(900, 600), *, background_variant=False):
    with Image.open(ROOT / "enabled_battle_full_panel.png") as image:
        enabled = image.convert("RGB").resize(
            size,
            Image.Resampling.BILINEAR,
        )
    if background_variant:
        with Image.open(ROOT / "enabled_full_panel.png") as image:
            changed = image.convert("RGB").resize(
                size,
                Image.Resampling.BILINEAR,
            )
        panel_box = (
            round(size[0] * 0.738),
            0,
            size[0],
            round(size[1] * 0.22),
        )
        changed.paste(enabled.crop(panel_box), panel_box[:2])
        enabled = changed
    return enabled


def _enabled_battle_sample(size=(900, 600), *, background_variant=False):
    return _sample(
        _enabled_battle_frame(
            size,
            background_variant=background_variant,
        )
    )


def _controller(
    frames,
    *,
    monotonic_clock=None,
    general_state=ReconnectScreenState.CONNECTED,
    battle_context=False,
    shortcut_seal_resolver="published",
):
    width = frames[0].width if frames else 900
    height = frames[0].height if frames else 600
    window = WindowInfo(1, "Adobe Flash Player", True, False, (0, 0, width, height), 2, "ShockwaveFlash", "a" * 64, 3, 4)
    mouse = _Mouse()
    capture = _Capture(frames, clock=monotonic_clock)
    clock = monotonic_clock or (lambda: 0.0)
    backend = _Windows(window)
    authorization, preparation = _authorization_services(backend)
    if shortcut_seal_resolver == "published":
        shortcut_seal_resolver = preparation.shortcut_seal_resolver
    controller = WindowsSmartReconnectController(
        expected_windows=1, title_keywords=("Adobe Flash Player",),
        window_backend=backend, capture_provider=capture,
        recognizer=_ConnectedRecognizer(
            general_state,
            battle_context=battle_context,
        ), mouse_backend=mouse,
        primary_capture_is_trusted=True,
        primary_capture_is_fresh_without_visibility=True,
        execution_enabled=False, require_expected_window_count=False,
        monotonic_clock=clock,
        auto_battle_enabled=True, auto_battle_recognizer=AutoBattleRecognizer(ROOT),
        authorization_coordinator=authorization,
        preparation_service=preparation,
        shortcut_seal_resolver=shortcut_seal_resolver,
    )
    prepared = controller.prepare_execution_snapshot()
    assert prepared.success is True
    controller.set_execution_enabled(True)
    return controller, window, mouse


@pytest.mark.parametrize(
    "seal_failure",
    ("missing", "rejected", "error", "mismatch"),
)
def test_auto_battle_requires_the_exact_published_shortcut_seal(
    seal_failure,
) -> None:
    class FailingResolver:
        def revalidate(self, _expected_seal):
            if seal_failure == "error":
                raise OSError("shortcut seal unavailable")
            return False

    disabled = _red_x_full_sample()
    resolver = (
        None
        if seal_failure == "missing"
        else (
            "published"
            if seal_failure == "mismatch"
            else FailingResolver()
        )
    )
    controller, window, mouse = _controller(
        [disabled, _enabled_battle_sample()],
        shortcut_seal_resolver=resolver,
    )
    if seal_failure == "mismatch":
        batch = controller._authorization_batch
        assert batch is not None
        target = batch.target_for(window.launch_fingerprint)
        assert target is not None
        expected_seal = target.shortcut_seal
        assert expected_seal is not None
        controller._shortcut_seals._published[target.fingerprint] = (
            ShortcutSeal(
                ShortcutFileIdentity(
                    expected_seal.file_identity.normalized_path,
                    expected_seal.file_identity.volume_serial_number,
                    expected_seal.file_identity.file_index + 1,
                ),
                "f" * 64,
                target.fingerprint,
            )
        )
        assert controller._shortcut_seals.revalidate(expected_seal) is False

    delivered = controller._run_auto_battle_for_connected(
        window,
        window.launch_fingerprint,
        WindowInstanceToken.from_window(window),
        disabled,
        "visible",
        0,
        controller._source_state_generation,
    )

    assert delivered is False
    assert mouse.clicks == []
    assert not controller._auto_battle_evidence


def test_connected_auto_battle_clicks_once_then_requires_third_panel() -> None:
    with Image.open(ROOT / "disabled_red_x_with_context.png") as i: template = i.convert("RGB")
    first = Image.new("RGB", (900, 600), "#112233"); first.paste(template, (810, 500))
    controller, window, mouse = _controller([_sample(first), _enabled_battle_sample()])
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
    for change_identity in (False, True):
        controller, window, mouse = _controller([_sample(frame), _enabled_battle_sample()])
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
    assert controller.prepare_execution_snapshot().success is True
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
    alpha = WindowInfo(1, "Adobe Flash Player", True, False, (0, 0, 900, 600), 2, "ShockwaveFlash", "a" * 64, 3, 4)
    beta = WindowInfo(2, "Adobe Flash Player", True, False, (0, 0, 900, 600), 5, "ShockwaveFlash", "b" * 64, 6, 7)
    gamma = WindowInfo(3, "Adobe Flash Player", True, False, (0, 0, 900, 600), 8, "ShockwaveFlash", "c" * 64, 9, 10)
    backend = _Windows(alpha); backend.list_windows = lambda: [alpha, beta, gamma]
    mouse = _Mouse()
    authorization, preparation = _authorization_services(backend)
    controller = WindowsSmartReconnectController(
        expected_windows=3, title_keywords=("Adobe Flash Player",), window_backend=backend,
        capture_provider=_Capture([_sample(red_x), _enabled_battle_sample()]), recognizer=_ConnectedRecognizer(),
        mouse_backend=mouse, primary_capture_is_trusted=True, primary_capture_is_fresh_without_visibility=True,
        execution_enabled=False, require_expected_window_count=False, auto_battle_enabled=True,
        auto_battle_recognizer=AutoBattleRecognizer(ROOT),
        authorization_coordinator=authorization,
        preparation_service=preparation,
        shortcut_seal_resolver=preparation.shortcut_seal_resolver,
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
    for change_on, expected_clicks in ((0, 1), (2, 0), (3, 1)):
        controller, _window, mouse = _controller([_sample(red_x), _sample(red_x), _enabled_battle_sample()])
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
    now = 125.0
    controller, _window, mouse = _controller(
        [_sample(disabled), _sample(disabled), _enabled_battle_sample()],
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
        "auto_battle_normal-red-x_second_frame",
        "auto_battle_normal-red-x_third_frame",
    ]


def test_public_unknown_gameplay_uses_exact_auto_battle_evidence_only() -> None:
    disabled = _red_x_full_sample()
    controller, _window, mouse = _controller(
        [disabled, disabled, _enabled_battle_sample()],
        general_state=ReconnectScreenState.UNKNOWN,
    )

    result = controller.reconnect()

    assert controller._capture_provider.calls == 3
    assert len(mouse.clicks) == 1
    assert result.details["state_counts"] == {"unknown": 1}
    assert "screen_unknown" in result.details["failure_codes"]


@pytest.mark.parametrize("change_kind", ("source", "identity"))
def test_public_unknown_auto_battle_rechecks_authority_before_input(
    change_kind,
) -> None:
    disabled = _red_x_full_sample()
    controller, window, mouse = _controller(
        [disabled, disabled, _enabled_battle_sample()],
        general_state=ReconnectScreenState.UNKNOWN,
    )

    def change_after_second_frame(call):
        if call != 2:
            return
        if change_kind == "source":
            controller._source_state_generation += 1
        else:
            controller._window_backend.window = replace(
                window,
                process_id=99,
            )

    controller._capture_provider.after = change_after_second_frame

    controller.reconnect()

    assert controller._capture_provider.calls == 2
    assert mouse.clicks == []
    assert not controller._auto_battle_evidence


def test_public_check_disabled_never_uses_auto_battle_evidence() -> None:
    disabled = _red_x_full_sample()
    controller, _window, mouse = _controller(
        [disabled, disabled, _enabled_battle_sample()],
        general_state=ReconnectScreenState.CHECK_DISABLED,
    )

    result = controller.reconnect()

    assert controller._capture_provider.calls == 1
    assert mouse.clicks == []
    assert result.details["state_counts"] == {"check_disabled": 1}
    assert not controller._auto_battle_evidence


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
    controller, _window, mouse = _controller(
        [_enabled_battle_sample()],
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
    authorization, preparation = _authorization_services(backend)
    controller = WindowsSmartReconnectController(
        expected_windows=4,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=capture,
        recognizer=_ConnectedRecognizer(),
        mouse_backend=_Mouse(),
        primary_capture_is_trusted=True,
        primary_capture_is_fresh_without_visibility=True,
        execution_enabled=False,
        require_expected_window_count=False,
        auto_battle_enabled=False,
        authorization_coordinator=authorization,
        preparation_service=preparation,
        shortcut_seal_resolver=preparation.shortcut_seal_resolver,
    )
    assert controller.prepare_execution_snapshot().success is True
    controller.set_execution_enabled(True)

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
    authorization, preparation = _authorization_services(backend)
    controller = WindowsSmartReconnectController(
        expected_windows=8,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=passive,
        active_refresh_capture_provider=active,
        recognizer=_ConnectedRecognizer(),
        mouse_backend=_Mouse(),
        execution_enabled=False,
        require_expected_window_count=False,
        auto_battle_enabled=False,
        authorization_coordinator=authorization,
        preparation_service=preparation,
        shortcut_seal_resolver=preparation.shortcut_seal_resolver,
    )
    assert controller.prepare_execution_snapshot().success is True
    controller.set_execution_enabled(True)

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
    backend = _Windows(window)
    authorization, preparation = _authorization_services(backend)
    controller = WindowsSmartReconnectController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=_ForbiddenCapture(),
        active_refresh_capture_provider=active,
        recognizer=_ConnectedRecognizer(),
        mouse_backend=_Mouse(),
        execution_enabled=False,
        require_expected_window_count=False,
        auto_battle_enabled=False,
        authorization_coordinator=authorization,
        preparation_service=preparation,
        shortcut_seal_resolver=preparation.shortcut_seal_resolver,
    )
    assert controller.prepare_execution_snapshot().success is True
    controller.set_execution_enabled(True)

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


@pytest.mark.skipif(
    not BATTLE_AUTO_SOURCE.is_file(),
    reason="私密實機來源只留在提供者本機",
)
def test_private_battle_auto_button_source_has_exact_hash_and_box() -> None:
    assert hashlib.sha256(BATTLE_AUTO_SOURCE.read_bytes()).hexdigest() == (
        BATTLE_AUTO_SOURCE_SHA256
    )
    recognizer = AutoBattleRecognizer(ROOT)
    with Image.open(BATTLE_AUTO_SOURCE) as image:
        evidence = recognizer.read(image.convert("RGB"))

    assert image.size == (1336, 858)
    assert evidence.enabled is False
    assert evidence.disabled is False
    assert evidence.battle_button_box == (1148, 498, 1272, 546)
    assert evidence.battle_button_center == (1210.0, 522.0)


@pytest.mark.parametrize(
    "mutation",
    ("partial", "offset", "scaled", "cropped"),
)
def test_battle_auto_button_incomplete_variants_fail_closed(mutation) -> None:
    frame = _battle_button_frame()
    if mutation == "partial":
        frame.paste("#112233", (1210, 498, 1272, 546))
    elif mutation == "offset":
        shifted = Image.new("RGB", frame.size, "#112233")
        shifted.paste(frame.crop((1148, 498, 1272, 546)), (900, 300))
        frame = shifted
    elif mutation == "scaled":
        changed = Image.new("RGB", frame.size, "#112233")
        button = frame.crop((1148, 498, 1272, 546)).resize((100, 39))
        changed.paste(button, (1148, 498))
        frame = changed
    else:
        frame = frame.crop((80, 60, 1250, 800)).resize((1336, 858))

    evidence = AutoBattleRecognizer(ROOT).read(frame)

    assert evidence.battle_button_box is None
    assert evidence.enabled is False


def test_battle_button_absent_first_frame_retries_within_two_seconds() -> None:
    now = [0.0]
    blank = _sample(Image.new("RGB", (1336, 858), "#112233"))
    controller, _window, mouse = _controller(
        [blank],
        monotonic_clock=lambda: now[0],
        battle_context=True,
    )

    first = controller.reconnect()

    assert first.details["next_check_seconds"] <= 2
    assert mouse.clicks == []
    assert "a" * 64 in controller._auto_battle_button_windows

    controller._capture_provider.frames.extend(
        [
            _sample(_battle_button_frame()),
            _sample(_battle_button_frame()),
            _enabled_battle_sample((1336, 858)),
        ]
    )
    now[0] = 1.5
    confirmed = controller.reconnect()

    assert confirmed.details["clicked_windows"] == 0
    assert len(mouse.clicks) == 1
    assert mouse.clicks[0][0] == 1
    assert mouse.clicks[0][1] == pytest.approx(
        (1210 / 1336, 522 / 858)
    )
    assert controller._capture_provider.calls == 4
    assert "a" * 64 not in controller._auto_battle_button_windows
    assert "a" * 64 in controller._auto_battle_confirmed_instances


def test_battle_button_window_expires_at_twenty_four_seconds() -> None:
    now = [0.0]
    blank = _sample(Image.new("RGB", (1336, 858), "#112233"))
    controller, _window, mouse = _controller(
        [blank],
        monotonic_clock=lambda: now[0],
        battle_context=True,
    )
    controller.reconnect()

    controller._capture_provider.frames.extend(
        [_sample(_battle_button_frame())]
    )
    now[0] = 24.0
    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []
    assert "a" * 64 not in controller._auto_battle_button_windows


def test_normal_red_x_remains_first_priority() -> None:
    disabled = _red_x_full_sample()
    controller, _window, mouse = _controller(
        [disabled, disabled, _enabled_battle_sample()],
        battle_context=False,
    )

    controller.reconnect()

    assert len(mouse.clicks) == 1
    assert any(
        item[2] == "normal-red-x"
        for item in controller._auto_battle_attempted_actions
    )
    assert all(
        item[2] != "battle-button"
        for item in controller._auto_battle_attempted_actions
    )


def test_normal_confirmation_suppresses_later_battle_button() -> None:
    controller, window, mouse = _controller([])
    instance = WindowInstanceToken.from_window(window)
    enabled = _enabled_battle_sample()

    assert controller._run_auto_battle_for_connected(
        window,
        "a" * 64,
        instance,
        enabled,
        "visible",
        0,
        controller._source_state_generation,
        first_battle_context=False,
    )
    assert controller._run_auto_battle_for_connected(
        window,
        "a" * 64,
        instance,
        _sample(_battle_button_frame()),
        "visible",
        0,
        controller._source_state_generation,
        first_battle_context=True,
    )

    assert mouse.clicks == []
    assert controller._capture_provider.calls == 0


def test_normal_incomplete_then_battle_button_completes_same_scan() -> None:
    now = [0.0]
    controller, window, mouse = _controller(
        [
            _sample(_battle_button_frame()),
            _enabled_battle_sample((1336, 858)),
        ],
        monotonic_clock=lambda: now[0],
        battle_context=True,
    )
    instance = WindowInstanceToken.from_window(window)
    normal_unknown = _sample(Image.new("RGB", (900, 600), "#112233"))

    assert not controller._run_auto_battle_for_connected(
        window,
        "a" * 64,
        instance,
        normal_unknown,
        "visible",
        0,
        controller._source_state_generation,
        first_battle_context=False,
    )
    now[0] = 1.0
    assert controller._run_auto_battle_for_connected(
        window,
        "a" * 64,
        instance,
        _sample(_battle_button_frame()),
        "visible",
        0,
        controller._source_state_generation,
        first_battle_context=True,
    )

    assert len(mouse.clicks) == 1
    assert controller._capture_provider.calls == 2


def test_auto_battle_switch_or_source_revoke_clears_second_entry_authority() -> None:
    controller, window, mouse = _controller([])
    fingerprint = "a" * 64
    instance = WindowInstanceToken.from_window(window)
    key = (instance, "visible", 0, controller._source_state_generation)
    controller._auto_battle_confirmed_instances[fingerprint] = key
    controller.set_auto_battle_enabled(False)

    assert controller._auto_battle_confirmed_instances == {}
    assert controller._auto_battle_button_windows == {}
    assert mouse.clicks == []

    controller.set_auto_battle_enabled(True)
    controller._auto_battle_confirmed_instances[fingerprint] = key
    controller._revoke_source_failure_evidence(
        frozenset((fingerprint,)),
        refresh_source_generation=True,
    )

    assert controller._auto_battle_confirmed_instances == {}
    assert mouse.clicks == []
