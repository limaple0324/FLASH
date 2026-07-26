from dataclasses import dataclass

from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_background_capture import CaptureSample
from adapters.windows_battle_restart import BattleRestartResult
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.sp1_boundaries import ReconnectState
from services.group_launch_service import GroupLaunchPlan, GroupLaunchTarget
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)


def make_window(
    handle,
    *,
    process_id=None,
    fingerprint=None,
    minimized=False,
):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=minimized,
        rect=(0, 0, 900, 600),
        process_id=process_id if process_id is not None else handle + 100,
        window_class="ShockwaveFlash",
        launch_fingerprint=(
            fingerprint if fingerprint is not None else f"{handle:064x}"
        ),
    )


class FakeWindowBackend:
    def __init__(self, windows):
        self.windows = list(windows)

    def list_windows(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.windows[0].handle if self.windows else None

    def top_window_at(self, _x, _y):
        return None


class FakeCaptureProvider:
    def __init__(self, states):
        self.states = dict(states)
        self.calls = []

    def capture(self, handle):
        self.calls.append(handle)
        marker = self.states.get(handle, 255)
        return CaptureSample(
            width=2,
            height=2,
            pixels=bytes([marker, 0, 0, 255] * 4),
            api_succeeded=True,
        )


class FakeRecognizer:
    def __init__(self, states, points=None, battle_markers=()):
        self.states = dict(states)
        self.points = dict(points or {})
        self.battle_markers = set(battle_markers)

    def recognize_capture(self, sample):
        marker = sample.pixels[0] if sample is not None else 255
        state = self.states.get(marker, ReconnectScreenState.UNKNOWN)
        return ScreenRecognition(
            state=state,
            score=0.0 if state is not ReconnectScreenState.UNKNOWN else None,
            click_point=self.points.get(marker),
            reference_name=state.value,
            battle_context=marker in self.battle_markers,
        )


class FakeMouseBackend:
    def __init__(self, *, invalid=(), unresponsive=(), fail=()):
        self.invalid = set(invalid)
        self.unresponsive = set(unresponsive)
        self.fail = set(fail)
        self.clicks = []

    def is_window(self, handle):
        return handle not in self.invalid

    def probe_responsive(self, handle, _timeout_ms):
        return handle not in self.unresponsive

    def click_relative(self, handle, point):
        self.clicks.append((handle, point))
        return handle not in self.fail


class FakeBattleRestarter:
    def __init__(self, *, succeeds=True, failure_code="battle_restart_failed"):
        self.succeeds = succeeds
        self.failure_code = failure_code
        self.calls = []
        self.reopen_calls = []

    def restart(self, window, target):
        self.calls.append((window, target))
        return BattleRestartResult(
            self.succeeds,
            None if self.succeeds else self.failure_code,
        )

    def reopen_missing(self, target, candidate_windows):
        self.reopen_calls.append((target, tuple(candidate_windows)))
        return BattleRestartResult(
            self.succeeds,
            None if self.succeeds else self.failure_code,
            shortcut_open_requested=self.succeeds,
        )


@dataclass
class Fixture:
    controller: WindowsSmartReconnectController
    capture: FakeCaptureProvider
    mouse: FakeMouseBackend


def make_controller(
    screen_states,
    *,
    windows=None,
    points=None,
    mouse=None,
    clock=None,
    state_path=None,
    battle_markers=(),
    battle_restarter=None,
    group_launch_plan=None,
    failure_status_service=None,
    failure_record_callback=None,
):
    windows = windows or [make_window(1), make_window(2)]
    capture = FakeCaptureProvider(
        {window.handle: marker for window, marker in zip(windows, screen_states)}
    )
    recognizer = FakeRecognizer(
        {
            1: ReconnectScreenState.CONNECTED,
            2: ReconnectScreenState.DISCONNECTED,
            3: ReconnectScreenState.LOGIN_START,
            4: ReconnectScreenState.LINE_SELECTION,
            5: ReconnectScreenState.CHARACTER_SELECTION,
            6: ReconnectScreenState.POST_LOGIN_ACTIVITY,
            7: ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
            8: ReconnectScreenState.RECONNECTING,
            255: ReconnectScreenState.UNKNOWN,
        },
        points
        or {
            2: (0.5, 0.5),
            3: (0.5, 0.8),
            4: (0.5, 0.3),
            5: (0.35, 0.85),
            6: (0.86, 0.12),
            7: (0.81, 0.18),
        },
        battle_markers=battle_markers,
    )
    mouse = mouse or FakeMouseBackend()
    controller = WindowsSmartReconnectController(
            expected_windows=2,
            title_keywords=("Adobe Flash Player",),
            window_backend=FakeWindowBackend(windows),
            capture_provider=capture,
            recognizer=recognizer,
            mouse_backend=mouse,
            monotonic_clock=clock or (lambda: 0.0),
            state_path=state_path,
            execution_enabled=True,
            battle_restarter=battle_restarter,
            failure_status_service=failure_status_service,
            failure_record_callback=failure_record_callback,
        )
    if group_launch_plan is not None:
        controller.set_group_launch_plan(group_launch_plan)
    return Fixture(
        controller=controller,
        capture=capture,
        mouse=mouse,
    )


def test_read_only_check_detects_reconnect_need_without_clicking():
    fixture = make_controller([1, 2])

    result = fixture.controller.check_connection()

    assert result.success is False
    assert result.code == "reconnect.required"
    assert fixture.controller.state is ReconnectState.DISCONNECTED
    assert fixture.mouse.clicks == []
    assert result.details["connected_windows"] == 1
    assert result.details["actionable_windows"] == 1
    assert result.details["captured_pixels_persisted"] is False


def test_selected_group_identity_excludes_other_open_flash_windows():
    windows = [make_window(1), make_window(2), make_window(3)]
    selected = {
        windows[0].launch_fingerprint,
        windows[2].launch_fingerprint,
    }
    fixture = make_controller(
        [2, 1, 4],
        windows=windows,
    )
    fixture.controller.set_expected_windows(2)
    fixture.controller.set_allowed_fingerprints(selected)

    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.details["discovered_windows"] == 2
    assert fixture.capture.calls == [1, 3]
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (3, (0.5, 0.3)),
    ]


def test_selected_group_missing_one_identity_fails_before_capture():
    windows = [make_window(1), make_window(2)]
    fixture = make_controller([2, 1], windows=windows)
    fixture.controller.set_allowed_fingerprints(
        {
            windows[0].launch_fingerprint,
            "f" * 64,
        }
    )

    result = fixture.controller.reconnect()

    assert result.success is False
    assert "window_count_mismatch" in result.details["failure_codes"]
    assert "group_identity_set_mismatch" in result.details["failure_codes"]
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []


def test_new_controller_starts_with_execution_hard_disabled():
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 2, 2: 1})
    mouse = FakeMouseBackend()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        recognizer=FakeRecognizer(
            {
                1: ReconnectScreenState.CONNECTED,
                2: ReconnectScreenState.DISCONNECTED,
            },
            {2: (0.5, 0.5)},
        ),
        mouse_backend=mouse,
    )

    result = controller.reconnect()

    assert result.details["clicked_windows"] == 0
    assert mouse.clicks == []


def test_reconnect_advances_different_known_states_independently():
    fixture = make_controller([2, 4])

    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.progressed"
    assert fixture.controller.state is ReconnectState.RECONNECTING
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (2, (0.5, 0.3)),
    ]
    assert result.details["clicked_windows"] == 2
    assert result.details["next_check_seconds"] == 2


def test_disconnect_context_uses_force_login_instead_of_start_game():
    fixture = make_controller([2, 1])

    first = fixture.controller.reconnect()
    fixture.capture.states[1] = 3
    second = fixture.controller.reconnect()

    assert first.code == "reconnect.progressed"
    assert second.code == "reconnect.progressed"
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.505, 0.856)),
    ]
    assert second.details["state_counts"] == {
        "connected": 1,
        "force_login_start": 1,
    }
    assert second.details["next_check_seconds"] == 10


def test_battle_disconnect_restarts_exact_target_without_clicking(tmp_path):
    windows = [make_window(1), make_window(2)]
    first_shortcut = tmp_path / "first.lnk"
    second_shortcut = tmp_path / "second.lnk"
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                first_shortcut,
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                second_shortcut,
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
    )

    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.details["restarted_windows"] == 1
    assert result.details["clicked_windows"] == 0
    assert fixture.mouse.clicks == []
    assert restarter.calls == [(windows[0], plan.targets[0])]
    assert result.details["next_check_seconds"] == 2


def test_battle_disconnect_without_unique_target_waits_one_minute():
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        battle_markers={2},
        battle_restarter=restarter,
    )

    result = fixture.controller.reconnect()

    assert result.success is False
    assert "battle_restart_identity_unresolved" in result.details["failure_codes"]
    assert result.details["restarted_windows"] == 0
    assert result.details["clicked_windows"] == 0
    assert result.details["next_check_seconds"] == 60
    assert fixture.mouse.clicks == []
    assert restarter.calls == []


def test_battle_failure_keeps_one_named_status_until_connected(tmp_path):
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    statuses = ReconnectFailureStatusService()
    restarter = FakeBattleRestarter(succeeds=False)
    fixture = make_controller(
        [2, 1],
        windows=windows,
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=statuses,
    )

    fixture.controller.reconnect()
    fixture.controller.reconnect()
    assert statuses.messages() == ("120古－重連失敗",)

    fixture.capture.states[1] = 1
    fixture.controller.reconnect()
    assert statuses.messages() == ()


def test_unknown_battle_identity_uses_group_unknown_status():
    statuses = ReconnectFailureStatusService()
    fixture = make_controller(
        [2, 1],
        battle_markers={2},
        battle_restarter=FakeBattleRestarter(),
        failure_status_service=statuses,
    )

    fixture.controller.reconnect()

    assert statuses.messages() == (
        "目前組別中的未知角色－重連失敗",
    )


def test_missing_reopen_retries_immediately_without_touching_other_roles(
    tmp_path,
):
    now = [0.0]
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=(
            GroupLaunchTarget(
                1,
                "120古",
                tmp_path / "first.lnk",
                windows[0].launch_fingerprint,
            ),
            GroupLaunchTarget(
                2,
                "120靈",
                tmp_path / "second.lnk",
                windows[1].launch_fingerprint,
            ),
        ),
    )
    restarter = FakeBattleRestarter()
    fixture = make_controller(
        [2, 1],
        windows=windows,
        clock=lambda: now[0],
        battle_markers={2},
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=ReconnectFailureStatusService(),
    )

    first = fixture.controller.reconnect()
    assert first.details["restarted_windows"] == 1
    fixture.controller._window_backend.windows = [windows[1]]

    now[0] = 59.0
    before = fixture.controller.reconnect()
    now[0] = 60.0
    retry = fixture.controller.reconnect()
    now[0] = 119.0
    no_duplicate = fixture.controller.reconnect()

    assert before.details["restarted_windows"] == 0
    assert retry.details["restarted_windows"] == 1
    assert no_duplicate.details["restarted_windows"] == 1
    assert len(restarter.reopen_calls) == 2


def test_each_known_role_failure_records_then_restarts_only_that_role(
    tmp_path,
):
    windows = [make_window(1), make_window(2)]
    plan = GroupLaunchPlan(
        "120",
        targets=tuple(
            GroupLaunchTarget(
                index,
                name,
                tmp_path / f"{index}.lnk",
                window.launch_fingerprint,
            )
            for index, (name, window) in enumerate(
                (("120古", windows[0]), ("120靈", windows[1])),
                start=1,
            )
        ),
    )
    restarter = FakeBattleRestarter(succeeds=False)
    records = []
    fixture = make_controller(
        [1, 1],
        windows=windows,
        battle_restarter=restarter,
        group_launch_plan=plan,
        failure_status_service=ReconnectFailureStatusService(),
        failure_record_callback=lambda role, detail: records.append(
            (role, detail)
        ),
    )

    fixture.controller._report_reconnect_failure(
        windows[0].launch_fingerprint
    )
    fixture.controller._report_reconnect_failure(
        windows[0].launch_fingerprint
    )

    assert [call[0].handle for call in restarter.calls] == [1, 1]
    assert all(call[0].handle != 2 for call in restarter.calls)
    assert records[0] == ("120古", "重連失敗")
    assert records[2] == ("120古", "重連失敗")
    assert len(records) == 4


def test_same_screen_action_is_not_repeated_before_one_minute_retry():
    now = [0.0]
    fixture = make_controller([2, 1], clock=lambda: now[0])

    first = fixture.controller.reconnect()
    second = fixture.controller.reconnect()
    now[0] = 59.0
    third = fixture.controller.reconnect()
    now[0] = 60.0
    fourth = fixture.controller.reconnect()

    assert first.details["clicked_windows"] == 1
    assert second.details["clicked_windows"] == 0
    assert third.details["clicked_windows"] == 0
    assert fourth.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.5, 0.5)),
    ]


def test_disconnect_context_survives_controller_restart(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    first = make_controller([2, 1], state_path=state_path)

    first.controller.reconnect()
    second = make_controller([3, 1], state_path=state_path)
    result = second.controller.reconnect()

    assert result.details["state_counts"] == {
        "connected": 1,
        "force_login_start": 1,
    }
    assert second.mouse.clicks == [(1, (0.505, 0.856))]


def test_one_minute_retry_survives_controller_restart(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    first = make_controller(
        [4, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )

    first.controller.reconnect()
    now[0] = 1059.0
    second = make_controller(
        [4, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    before_deadline = second.controller.reconnect()
    now[0] = 1060.0
    after_deadline = second.controller.reconnect()

    assert before_deadline.details["clicked_windows"] == 0
    assert after_deadline.details["clicked_windows"] == 1
    assert second.mouse.clicks == [(1, (0.5, 0.3))]


def test_popup_automation_context_survives_controller_restart(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    first = make_controller([3, 1], state_path=state_path)

    first.controller.reconnect()
    second = make_controller([6, 1], state_path=state_path)
    result = second.controller.reconnect()

    assert result.code == "reconnect.progressed"
    assert second.mouse.clicks == [(1, (0.86, 0.12))]


def test_brief_connected_screen_does_not_drop_delayed_popup_context(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    first = make_controller(
        [3, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    first.controller.reconnect()

    now[0] = 1010.0
    connected = make_controller(
        [1, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    connected.controller.reconnect()

    now[0] = 1020.0
    popup = make_controller(
        [7, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    result = popup.controller.reconnect()

    assert result.code == "reconnect.progressed"
    assert popup.mouse.clicks == [(1, (0.81, 0.18))]


def test_delayed_popup_context_expires_and_restores_player_control(tmp_path):
    state_path = tmp_path / "smart_reconnect_state.json"
    now = [1000.0]
    first = make_controller(
        [3, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    first.controller.reconnect()

    now[0] = 1181.0
    expired = make_controller(
        [7, 1],
        clock=lambda: now[0],
        state_path=state_path,
    )
    result = expired.controller.reconnect()

    assert result.code == "reconnect.waiting"
    assert result.details["actionable_windows"] == 0
    assert expired.mouse.clicks == []


def test_changed_screen_allows_next_action_without_waiting_one_minute():
    fixture = make_controller([2, 1])

    fixture.controller.reconnect()
    fixture.capture.states[1] = 5
    result = fixture.controller.reconnect()

    assert result.details["clicked_windows"] == 1
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.5)),
        (1, (0.35, 0.85)),
    ]


def test_character_selection_confirms_preferred_slot_before_entering_game():
    windows = [make_window(1), make_window(2)]
    capture = FakeCaptureProvider({1: 5, 2: 1})
    mouse = FakeMouseBackend()

    class CharacterSequenceRecognizer:
        def __init__(self):
            self.selected = False

        def recognize_capture(self, sample):
            if sample.pixels[0] == 1:
                return ScreenRecognition(
                    state=ReconnectScreenState.CONNECTED,
                    score=0.0,
                    click_point=None,
                    reference_name="connected",
                )
            return ScreenRecognition(
                state=ReconnectScreenState.CHARACTER_SELECTION,
                score=0.0,
                click_point=(
                    (0.353, 0.854)
                    if self.selected
                    else (0.651, 0.706)
                ),
                reference_name="character_selection",
                character_level=160,
                character_slot_index=2,
                character_slot_selected=self.selected,
            )

    recognizer = CharacterSequenceRecognizer()
    controller = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=FakeWindowBackend(windows),
        capture_provider=capture,
        recognizer=recognizer,
        mouse_backend=mouse,
        execution_enabled=True,
    )

    first = controller.reconnect()
    recognizer.selected = True
    second = controller.reconnect()

    assert first.details["clicked_windows"] == 1
    assert second.details["clicked_windows"] == 1
    assert mouse.clicks == [
        (1, (0.651, 0.706)),
        (1, (0.353, 0.854)),
    ]


def test_manual_activity_popup_is_recognized_but_not_closed():
    fixture = make_controller([6, 1])

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert result.details["state_counts"] == {
        "connected": 1,
        "post_login_activity": 1,
    }
    assert result.details["actionable_windows"] == 0
    assert fixture.mouse.clicks == []


def test_popup_is_closed_only_inside_controller_started_login_flow():
    fixture = make_controller([3, 1])

    fixture.controller.reconnect()
    fixture.capture.states[1] = 6
    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.progressed"
    assert fixture.mouse.clicks == [
        (1, (0.5, 0.8)),
        (1, (0.86, 0.12)),
    ]


def test_fresh_login_screen_still_uses_normal_start_game_action():
    fixture = make_controller([3, 1])

    result = fixture.controller.reconnect()

    assert result.code == "reconnect.progressed"
    assert fixture.mouse.clicks == [(1, (0.5, 0.8))]
    assert result.details["state_counts"] == {
        "connected": 1,
        "login_start": 1,
    }


def test_force_login_progress_waits_ten_seconds_without_clicking():
    fixture = make_controller([8, 1])

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert fixture.mouse.clicks == []
    assert result.details["next_check_seconds"] == 10


def test_one_unresponsive_window_is_isolated_without_redirecting_other_click():
    mouse = FakeMouseBackend(unresponsive={1})
    fixture = make_controller([2, 3], mouse=mouse)

    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.progressed_with_isolation"
    assert fixture.mouse.clicks == [(2, (0.5, 0.8))]
    assert result.details["clicked_windows"] == 1
    assert "input_target_unresponsive" in result.details["failure_codes"]


def test_group_identity_failure_aborts_before_capture_or_click():
    windows = [
        make_window(1, fingerprint="a" * 64),
        make_window(2, fingerprint="a" * 64),
    ]
    fixture = make_controller([2, 3], windows=windows)

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert fixture.capture.calls == []
    assert fixture.mouse.clicks == []
    assert "fingerprint_missing_or_duplicate" in result.details["failure_codes"]
    assert result.details["validated_windows"] == 0


def test_unknown_screen_never_clicks_and_uses_one_minute_retry():
    fixture = make_controller([1, 255])

    result = fixture.controller.reconnect()

    assert result.success is False
    assert result.code == "reconnect.waiting"
    assert fixture.mouse.clicks == []
    assert result.details["unknown_windows"] == 1
    assert result.details["next_check_seconds"] == 60
    assert "screen_unknown" in result.details["failure_codes"]


def test_all_connected_is_reported_without_sending_input():
    fixture = make_controller([1, 1])

    result = fixture.controller.reconnect()

    assert result.success is True
    assert result.code == "reconnect.connected"
    assert fixture.controller.state is ReconnectState.CONNECTED
    assert fixture.mouse.clicks == []
    assert result.details["all_connected"] is True
    assert result.details["next_check_seconds"] == 5


def test_safe_report_never_contains_identifiers_pixels_or_coordinates():
    fixture = make_controller([2, 3])

    payload = fixture.controller.reconnect().details
    serialized = repr(payload)

    assert "handle" not in serialized
    assert "process_id" not in serialized
    assert "launch_fingerprint" not in serialized
    assert not any(isinstance(value, bytes) for value in payload.values())
    assert "(0.5, 0.5)" not in serialized
    assert payload["raw_arguments_emitted"] is False
    assert payload["fingerprints_emitted"] is False
    assert payload["captured_pixels_persisted"] is False
    assert payload["click_coordinates_emitted"] is False
