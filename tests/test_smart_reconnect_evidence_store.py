from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import services.smart_reconnect_evidence_store as evidence_store
from services.smart_reconnect_evidence_store import (
    FORMAL_ACCEPTANCE_MIN_CYCLES,
    FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW,
    FORMAL_ACCEPTANCE_MIN_DURATION_MS,
    FORMAL_ACCEPTANCE_MIN_WINDOWS,
    RuntimeSourceIdentity,
    SMART_RECONNECT_DEADLINE_MS,
    SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
    SmartReconnectEvidenceRecorder,
    SmartReconnectReplayReport,
    SmartReconnectReplayValidator,
    anonymous_screen_features,
    validate_formal_acceptance,
)


COMMIT = "a" * 40
AUTHORITY = "c" * 64
LIVE_SOURCE_GENERATION_FAILURE = (
    Path(__file__).parent
    / "fixtures"
    / "smart_reconnect"
    / "live_failures"
    / "20260806T014703Z-source-generation-cancelled.jsonl"
)
LIVE_SOURCE_GENERATION_FAILURE_SHA256 = (
    "cf68b7fe1e4f01043a7ec3aa08918a48cff9d9db8d71e8937ed3b849904b4cbd"
)
LIVE_OBSCURED_CAPTURE_TIMEOUT = (
    Path(__file__).parent
    / "fixtures"
    / "smart_reconnect"
    / "live_failures"
    / "20260806T022926Z-obscured-capture-timeout.jsonl"
)
LIVE_OBSCURED_CAPTURE_TIMEOUT_SHA256 = (
    "39398aa8c3b36cc65105db4646d391421299589d1a4013653b2d7886dbbbd408"
)


class _Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 0.1) -> None:
        self.value += seconds


def _pixels(width: int = 8, height: int = 8, value: int = 40) -> bytes:
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            low = value // 2
            shade = low if (x + y) % 2 == 0 else 255 - low
            pixels.extend((shade, (shade + 17) % 256, (shade + 31) % 256, 255))
    return bytes(pixels)


def _recorder(tmp_path: Path, *, auto_battle: bool = True):
    wall = _Clock()
    monotonic = _Clock()
    recorder = SmartReconnectEvidenceRecorder(
        tmp_path,
        source_identity=RuntimeSourceIdentity(COMMIT, False, "git"),
        auto_battle_required=auto_battle,
        session_id="1" * 24,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    recorder.record_monitoring_state(True)
    return recorder, wall, monotonic


def _observe(
    recorder: SmartReconnectEvidenceRecorder,
    clock: _Clock,
    state: str,
    *,
    value: int = 40,
    failure_reason: str | None = None,
    capture_method: str = "visible",
    fallback_capture: bool = False,
    fallback_recognition: bool = False,
    presentation_state: str = "visible",
    scene_context: str = "general",
    raw_window_key: str = "private-launch-fingerprint",
    pixels: bytes | None = None,
) -> int | None:
    clock.advance()
    return recorder.record_observation(
        raw_window_key=raw_window_key,
        state=state,
        capture_method=capture_method,
        width=8,
        height=8,
        pixels=_pixels(value=value) if pixels is None else pixels,
        fresh=True,
        identity_verified=True,
        score=1.5,
        reference_code=f"reference/{state}.png",
        failure_reason=failure_reason,
        recognition_method=(
            "fallback" if fallback_recognition else "primary"
        ),
        fallback_capture_used=fallback_capture,
        fallback_recognition_used=fallback_recognition,
        presentation_state=presentation_state,
        scene_context=scene_context,
        recognition_basis=(
            "cross_map_fixed_ui" if state == "connected" else None
        ),
    )


def _prove(
    recorder: SmartReconnectEvidenceRecorder,
    state: str,
    signature: str,
    *,
    raw_window_key: str = "private-launch-fingerprint",
) -> None:
    recorder.record_decision_evidence(
        raw_window_key=raw_window_key,
        state=state,
        decision_signature=signature,
        capture_method="visible",
        identity_verified=True,
        authority_signature=AUTHORITY,
    )


def _action(
    recorder: SmartReconnectEvidenceRecorder,
    *,
    state: str,
    action: str,
    original_line: bool | None = None,
    original_role: bool | None = None,
    auto_battle: bool | None = None,
    raw_window_key: str = "private-launch-fingerprint",
    clicked: bool = True,
    input_channel: str = "window_message",
    restoration_verified: bool | None = True,
) -> None:
    intent_sequence = recorder.record_action_intent(
        raw_window_key=raw_window_key,
        state=state,
        action=action,
        identity_verified=True,
        input_channel=input_channel,
        authority_signature=AUTHORITY,
    )
    recorder.record_action(
        raw_window_key=raw_window_key,
        state=state,
        action=action,
        allowed=True,
        performed=True,
        clicked=clicked,
        identity_verified=True,
        restoration_verified=restoration_verified,
        original_line_verified=original_line,
        original_role_verified=original_role,
        auto_battle_panel_verified=auto_battle,
        input_channel=input_channel,
        intent_sequence=intent_sequence,
        authority_signature=AUTHORITY,
    )


def _two_proofs_and_action(
    recorder: SmartReconnectEvidenceRecorder,
    clock: _Clock,
    *,
    state: str,
    action: str,
    value: int,
    original_line: bool | None = None,
    original_role: bool | None = None,
    auto_battle: bool | None = None,
    raw_window_key: str = "private-launch-fingerprint",
    scene_context: str = "general",
    clicked: bool = True,
    input_channel: str = "window_message",
    restoration_verified: bool | None = True,
) -> None:
    signature = hashlib.sha256(f"{state}:{action}".encode()).hexdigest()
    _observe(
        recorder,
        clock,
        state,
        value=value,
        raw_window_key=raw_window_key,
        scene_context=scene_context,
    )
    _prove(recorder, state, signature, raw_window_key=raw_window_key)
    _observe(
        recorder,
        clock,
        state,
        value=value + 1,
        raw_window_key=raw_window_key,
        scene_context=scene_context,
    )
    _prove(recorder, state, signature, raw_window_key=raw_window_key)
    _action(
        recorder,
        state=state,
        action=action,
        original_line=original_line,
        original_role=original_role,
        auto_battle=auto_battle,
        raw_window_key=raw_window_key,
        clicked=clicked,
        input_channel=input_channel,
        restoration_verified=restoration_verified,
    )


def test_anonymous_features_are_histograms_not_pixels() -> None:
    pixels = _pixels(16, 12, 90)
    result = anonymous_screen_features(16, 12, pixels)
    assert result is not None
    features, digest = result
    assert len(features) == 32
    assert all(isinstance(item, int) and 0 <= item <= 255 for item in features)
    assert digest == hashlib.sha256(bytes(features)).hexdigest()
    assert pixels not in bytes(features)


def test_recorder_never_persists_private_identity_or_pixels(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    pixels = _pixels(value=73)
    _observe(recorder, monotonic, "unknown", value=73, failure_reason="screen_unknown")
    text = recorder.path.read_text(encoding="utf-8")
    assert "private-launch-fingerprint" not in text
    assert pixels.hex() not in text
    records = [json.loads(line) for line in text.splitlines()]
    persisted_keys = {
        str(key)
        for record in records
        for key in record
    }
    assert persisted_keys.isdisjoint(
        {
            "role_name",
            "recent_login_role",
            "chat",
            "click_point",
            "handle",
            "process_id",
            "thread_id",
            "launch_fingerprint",
        }
    )
    observation = records[-1]
    assert observation["window_id"] == "window-01"
    assert observation["state"] == "unknown"
    assert observation["failure_reason"] == "screen_unknown"
    assert len(observation["anonymous_features"]) == 32


def test_recorder_limits_unchanged_observations_but_keeps_post_action_frame(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    assert _observe(recorder, monotonic, "disconnected") is not None
    assert _observe(recorder, monotonic, "disconnected") is not None
    assert _observe(recorder, monotonic, "disconnected") is None
    signature = "b" * 64
    _prove(recorder, "disconnected", signature)
    _prove(recorder, "disconnected", signature)
    _action(
        recorder,
        state="disconnected",
        action="confirm_disconnect",
    )
    assert _observe(recorder, monotonic, "disconnected") is not None


def _record_full_lifecycle(
    recorder: SmartReconnectEvidenceRecorder,
    monotonic: _Clock,
    *,
    delay_before_connected: float = 0.0,
    external_obstacle: str | None = None,
    scene_context: str = "general",
    auto_action: bool = True,
    final_confirmations: int = 2,
    raw_window_key: str = "private-launch-fingerprint",
    disconnect_action_override: str | None = None,
    timeout_retries: int = 0,
) -> None:
    disconnect_action = disconnect_action_override or (
        "restart_window"
        if scene_context == "battle"
        else "confirm_disconnect"
    )
    _two_proofs_and_action(
        recorder,
        monotonic,
        state="disconnected",
        action=disconnect_action,
        value=10,
        raw_window_key=raw_window_key,
        scene_context=scene_context,
        clicked=scene_context != "battle",
        input_channel=("window_control" if scene_context == "battle" else "window_message"),
        restoration_verified=(None if scene_context == "battle" else True),
    )
    if scene_context == "battle":
        recorder.record_verification(
            raw_window_key=raw_window_key,
            identity_verified=True,
            verification_basis="replacement_window_layout_match",
            evidence_signature="f" * 64,
            window_state_restored=True,
        )
    if external_obstacle is not None:
        recorder.record_external_obstacle(
            raw_window_key=raw_window_key,
            obstacle=external_obstacle,
            active=True,
            classification_basis="confirmed_server_response",
            evidence_signature="e" * 64,
        )
    _two_proofs_and_action(
        recorder,
        monotonic,
        state="login_start",
        action="start_game",
        value=20,
        raw_window_key=raw_window_key,
    )
    _two_proofs_and_action(
        recorder,
        monotonic,
        state="force_login_start",
        action="force_login",
        value=30,
        raw_window_key=raw_window_key,
    )
    for retry in range(timeout_retries):
        _two_proofs_and_action(
            recorder,
            monotonic,
            state="force_login_timeout",
            action="confirm_force_login_timeout",
            value=34 + retry * 2,
            raw_window_key=raw_window_key,
        )
        _two_proofs_and_action(
            recorder,
            monotonic,
            state="force_login_start",
            action="force_login",
            value=35 + retry * 2,
            raw_window_key=raw_window_key,
        )
    _two_proofs_and_action(
        recorder,
        monotonic,
        state="line_selection",
        action="select_default_line",
        value=40,
        original_line=True,
        raw_window_key=raw_window_key,
    )
    _two_proofs_and_action(
        recorder,
        monotonic,
        state="character_selection",
        action="enter_game",
        value=50,
        original_role=True,
        raw_window_key=raw_window_key,
    )
    _two_proofs_and_action(
        recorder,
        monotonic,
        state="post_login_activity",
        action="close_announcement",
        value=60,
        raw_window_key=raw_window_key,
    )
    monotonic.advance(delay_before_connected)
    if auto_action:
        _two_proofs_and_action(
            recorder,
            monotonic,
            state="connected",
            action="auto_battle_click",
            value=70,
            auto_battle=True,
            raw_window_key=raw_window_key,
        )
    else:
        recorder.record_verification(
            raw_window_key=raw_window_key,
            identity_verified=True,
            verification_basis="complete_auto_battle_panel",
            evidence_signature="9" * 64,
            auto_battle_panel_verified=True,
        )
    for index in range(final_confirmations):
        _observe(
            recorder,
            monotonic,
            "connected",
            value=80 + index,
            raw_window_key=raw_window_key,
        )


def test_replay_accepts_one_full_anonymous_recovery_lifecycle(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic)

    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert report.findings == ()
    assert report.pending_actions == 0
    assert report.recovered_cycles == 1
    assert report.recovered_cycles_by_window == (("window-01", 1),)
    assert report.window_count == 1
    assert report.source_commit == COMMIT
    assert report.cycles[0].total_elapsed_ms is not None
    assert report.cycles[0].total_elapsed_ms <= SMART_RECONNECT_DEADLINE_MS
    assert report.cycles[0].accepted is True


def test_real_source_generation_failure_replays_as_incomplete() -> None:
    assert hashlib.sha256(LIVE_SOURCE_GENERATION_FAILURE.read_bytes()).hexdigest() == (
        LIVE_SOURCE_GENERATION_FAILURE_SHA256
    )

    report = SmartReconnectReplayValidator().validate(
        LIVE_SOURCE_GENERATION_FAILURE
    )
    finding_codes = {finding.code for finding in report.findings}

    assert report.source_commit == "34bfadcbb7441da69ed718572445591fd1f444ac"
    assert report.event_count == 892
    assert report.window_count == 9
    assert report.recovered_cycles == 0
    assert report.pending_actions == 0
    assert report.session_duration_ms == 181_203
    assert "unknown_not_retried" in finding_codes
    assert "reconnect_cycle_incomplete" in finding_codes
    assert "flow_action_missing" in finding_codes
    assert all(cycle.accepted is False for cycle in report.cycles)


def test_real_obscured_capture_timeout_replays_as_incomplete() -> None:
    assert hashlib.sha256(LIVE_OBSCURED_CAPTURE_TIMEOUT.read_bytes()).hexdigest() == (
        LIVE_OBSCURED_CAPTURE_TIMEOUT_SHA256
    )

    validator = SmartReconnectReplayValidator()
    records = validator.load(LIVE_OBSCURED_CAPTURE_TIMEOUT)
    report = validator.validate(LIVE_OBSCURED_CAPTURE_TIMEOUT)
    observations = [
        record
        for record in records
        if record.get("record_type") == "observation"
    ]

    assert report.source_commit == "e8ec61a70f7855df85917d1ed840d4c679c99538"
    assert report.event_count == 229
    assert report.window_count == 9
    assert report.recovered_cycles == 0
    assert report.pending_actions == 0
    assert sum(
        record.get("failure_reason") == "capture_failed"
        for record in observations
    ) == 93
    assert sum(
        record.get("record_type") == "action"
        for record in records
    ) == 6
    assert all(cycle.accepted is False for cycle in report.cycles)


def test_replay_rejects_unknown_click_duplicate_and_missing_proof(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _observe(
        recorder,
        monotonic,
        "unknown",
        failure_reason="screen_unknown",
    )
    _action(
        recorder,
        state="unknown",
        action="auto_battle_click",
    )
    _action(
        recorder,
        state="unknown",
        action="auto_battle_click",
    )
    _observe(
        recorder,
        monotonic,
        "unknown",
        failure_reason="screen_unknown",
    )

    report = SmartReconnectReplayValidator().validate(recorder.path)
    codes = {item.code for item in report.findings}
    assert "unknown_screen_operated" in codes
    assert "duplicate_stage_action" in codes
    assert "action_state_mismatch" in codes
    assert "two_frame_evidence_missing" in codes


def test_replay_rejects_dirty_source_and_private_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    records = (
        {
            "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
            "record_type": "session",
            "session_id": "2" * 24,
            "source_commit": COMMIT,
            "working_tree_dirty": True,
            "auto_battle_required": True,
            "reconnect_deadline_ms": SMART_RECONNECT_DEADLINE_MS,
            "role_name": "不得保存",
        },
    )
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    report = SmartReconnectReplayValidator().validate(path)
    assert {item.code for item in report.findings} >= {
        "source_not_clean",
        "private_field_persisted",
    }


def test_replay_rejects_recovery_completed_after_sixty_seconds(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(
        recorder,
        monotonic,
        delay_before_connected=61.0,
    )
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "reconnect_deadline_exceeded" in {
        item.code for item in report.findings
    }
    assert report.recovered_cycles == 0
    assert report.cycles[0].accepted is False
    assert report.cycles[0].total_elapsed_ms > SMART_RECONNECT_DEADLINE_MS


def test_external_obstacle_is_separate_and_excludes_cycle_from_acceptance(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(
        recorder,
        monotonic,
        external_obstacle="server_unavailable",
    )
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "external_obstacle_cycle_excluded" in {
        item.code for item in report.findings
    }
    assert report.cycles[0].external_obstacles == ("server_unavailable",)
    assert report.recovered_cycles == 0


def test_unknown_requires_retry_and_a_fallback_path(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _observe(
        recorder,
        monotonic,
        "unknown",
        failure_reason="screen_unknown",
    )
    _observe(
        recorder,
        monotonic,
        "unknown",
        failure_reason="screen_unknown",
        capture_method="temporarily_revealed",
        fallback_capture=True,
        fallback_recognition=True,
    )
    _observe(
        recorder,
        monotonic,
        "connected",
        value=91,
        capture_method="temporarily_revealed",
        fallback_capture=True,
        fallback_recognition=True,
    )
    report = SmartReconnectReplayValidator().validate(recorder.path)
    codes = {item.code for item in report.findings}
    assert "unknown_fallback_missing" not in codes
    assert "unknown_not_retried" not in codes
    records = [
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        item.get("fallback_capture_used") is True
        and item.get("fallback_recognition_used") is True
        for item in records
    )


def test_durable_action_intent_without_result_is_rejected(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _observe(recorder, monotonic, "disconnected")
    recorder.record_action_intent(
        raw_window_key="private-launch-fingerprint",
        state="disconnected",
        action="confirm_disconnect",
        identity_verified=True,
        authority_signature=AUTHORITY,
    )
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "action_intent_without_result" in {
        item.code for item in report.findings
    }


def test_reopen_requires_two_fresh_absence_proofs_and_window_control(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    decision = "d" * 64
    for _ in range(2):
        recorder.record_absence_evidence(
            raw_window_key="private-launch-fingerprint",
            state="reconnecting",
            decision_signature=decision,
            identity_verified=True,
            shortcut_identity_verified=True,
            target_absent=True,
            authority_signature=AUTHORITY,
        )
    intent = recorder.record_action_intent(
        raw_window_key="private-launch-fingerprint",
        state="reconnecting",
        action="reopen_window",
        identity_verified=True,
        input_channel="window_control",
        authority_signature=AUTHORITY,
    )
    recorder.record_action(
        raw_window_key="private-launch-fingerprint",
        state="reconnecting",
        action="reopen_window",
        allowed=True,
        performed=True,
        clicked=False,
        identity_verified=True,
        restoration_verified=None,
        input_channel="window_control",
        intent_sequence=intent,
        authority_signature=AUTHORITY,
    )
    _observe(recorder, monotonic, "login_start", value=93)
    report = SmartReconnectReplayValidator().validate(recorder.path)
    codes = {item.code for item in report.findings}
    assert "two_frame_evidence_missing" not in codes
    assert "action_intent_missing" not in codes
    assert "unsafe_input_channel" not in codes
    assert "pointer_interference_possible" not in codes
    assert report.pending_actions == 0


def test_formal_acceptance_requires_fourteen_windows_hundred_cycles_and_eight_hours(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic)
    report = validate_formal_acceptance((recorder.path,))
    assert report.distinct_windows_in_one_session < FORMAL_ACCEPTANCE_MIN_WINDOWS
    assert report.recovered_cycles < FORMAL_ACCEPTANCE_MIN_CYCLES
    assert report.longest_session_duration_ms < FORMAL_ACCEPTANCE_MIN_DURATION_MS
    codes = {item.code for item in report.findings}
    assert {
        "formal_window_count_insufficient",
        "formal_window_cycle_count_insufficient",
        "formal_cycle_count_insufficient",
        "formal_duration_insufficient",
        "formal_same_session_coverage_missing",
    } <= codes
    assert FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW == 3


def test_transient_unknown_keeps_the_same_disconnect_cycle(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _observe(recorder, monotonic, "disconnected", value=10)
    _observe(
        recorder,
        monotonic,
        "unknown",
        value=11,
        failure_reason="screen_unknown",
        capture_method="temporarily_revealed",
        fallback_capture=True,
        fallback_recognition=True,
    )
    _observe(recorder, monotonic, "disconnected", value=12)
    observations = [
        item
        for item in (
            json.loads(line)
            for line in recorder.path.read_text(encoding="utf-8").splitlines()
        )
        if item.get("record_type") == "observation"
    ]
    assert {item["cycle"] for item in observations} == {1}


def test_new_timeout_state_episode_can_retry_without_being_a_duplicate(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic, timeout_retries=1)
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "duplicate_stage_action" not in {item.code for item in report.findings}
    assert report.findings == ()
    assert report.recovered_cycles == 1


def test_disconnect_action_is_bound_to_proven_scene_context(tmp_path: Path) -> None:
    battle, _wall, battle_clock = _recorder(tmp_path / "battle")
    _record_full_lifecycle(battle, battle_clock, scene_context="battle")
    assert SmartReconnectReplayValidator().validate(battle.path).findings == ()

    wrong_general, _wall, general_clock = _recorder(tmp_path / "wrong-general")
    _record_full_lifecycle(
        wrong_general,
        general_clock,
        scene_context="general",
        disconnect_action_override="restart_window",
    )
    general_codes = {
        item.code
        for item in SmartReconnectReplayValidator().validate(
            wrong_general.path
        ).findings
    }
    assert "disconnect_action_mismatch" in general_codes

    wrong_battle, _wall, wrong_battle_clock = _recorder(
        tmp_path / "wrong-battle"
    )
    _record_full_lifecycle(
        wrong_battle,
        wrong_battle_clock,
        scene_context="battle",
        disconnect_action_override="confirm_disconnect",
    )
    battle_codes = {
        item.code
        for item in SmartReconnectReplayValidator().validate(
            wrong_battle.path
        ).findings
    }
    assert "disconnect_action_mismatch" in battle_codes


def test_final_connected_requires_two_new_structured_frames(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic, final_confirmations=1)
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "connected_confirmation_invalid" in {
        item.code for item in report.findings
    }
    assert report.recovered_cycles == 0


def test_uniform_frames_never_prove_final_gameplay(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic, final_confirmations=0)
    uniform = bytes((255, 255, 255, 255)) * 8 * 8
    _observe(recorder, monotonic, "connected", pixels=uniform)
    _observe(recorder, monotonic, "connected", pixels=uniform)
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "connected_confirmation_invalid" in {
        item.code for item in report.findings
    }
    assert report.recovered_cycles == 0


def test_external_obstacle_requires_its_own_strong_evidence(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _observe(recorder, monotonic, "disconnected", value=10)
    with pytest.raises(ValueError):
        recorder.record_external_obstacle(
            raw_window_key="private-launch-fingerprint",
            obstacle="server_unavailable",
            active=True,
            classification_basis="operating_system_network_probe",
            evidence_signature="e" * 64,
        )
    with pytest.raises(ValueError):
        recorder.record_external_obstacle(
            raw_window_key="private-launch-fingerprint",
            obstacle="server_unavailable",
            active=True,
            classification_basis="confirmed_server_response",
            evidence_signature="not-a-digest",
        )


def test_already_active_auto_battle_uses_verification_not_a_fake_click(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic, auto_action=False)
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert report.findings == ()
    assert report.cycles[0].auto_battle_panel_verified is True
    records = [
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
    ]
    assert not any(
        item.get("record_type") == "action"
        and item.get("action") == "auto_battle_click"
        for item in records
    )


def test_failed_verification_cannot_be_overwritten_by_a_later_true_value(
    tmp_path: Path,
) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic, auto_action=False)
    recorder.record_verification(
        raw_window_key="private-launch-fingerprint",
        identity_verified=True,
        verification_basis="complete_auto_battle_panel",
        evidence_signature="7" * 64,
        auto_battle_panel_verified=False,
    )
    recorder.record_verification(
        raw_window_key="private-launch-fingerprint",
        identity_verified=True,
        verification_basis="complete_auto_battle_panel",
        evidence_signature="8" * 64,
        auto_battle_panel_verified=True,
    )
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "auto_battle_panel_not_verified" in {
        item.code for item in report.findings
    }
    assert report.recovered_cycles == 0


def _formal_report(
    *,
    counts: tuple[tuple[str, int], ...],
    recovered_cycles: int,
    duration_ms: int,
) -> SmartReconnectReplayReport:
    return SmartReconnectReplayReport(
        source_commit=COMMIT,
        event_count=1,
        window_count=len(counts),
        recovered_cycles=recovered_cycles,
        pending_actions=0,
        session_duration_ms=duration_ms,
        cycles=(),
        recovered_cycles_by_window=counts,
        scenario_tags=tuple(sorted(evidence_store._FORMAL_SCENARIOS)),
        findings=(),
    )


def test_formal_acceptance_requires_three_cycles_per_window_in_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = tuple(
        (f"window-{index:02d}", 26 if index == 1 else 2)
        for index in range(1, FORMAL_ACCEPTANCE_MIN_WINDOWS + 1)
    )
    report = _formal_report(
        counts=counts,
        recovered_cycles=sum(value for _window, value in counts),
        duration_ms=FORMAL_ACCEPTANCE_MIN_DURATION_MS,
    )
    monkeypatch.setattr(evidence_store, "replay_many", lambda _paths: (report, report))
    result = validate_formal_acceptance((Path("one"), Path("two")))
    codes = {item.code for item in result.findings}
    assert "formal_window_count_insufficient" not in codes
    assert "formal_cycle_count_insufficient" not in codes
    assert "formal_duration_insufficient" not in codes
    assert "formal_window_cycle_count_insufficient" in codes
    assert "formal_same_session_coverage_missing" in codes


def test_formal_acceptance_aggregator_accepts_one_qualifying_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = tuple(
        (f"window-{index:02d}", 61 if index == 1 else 3)
        for index in range(1, FORMAL_ACCEPTANCE_MIN_WINDOWS + 1)
    )
    report = _formal_report(
        counts=counts,
        recovered_cycles=sum(value for _window, value in counts),
        duration_ms=FORMAL_ACCEPTANCE_MIN_DURATION_MS,
    )
    monkeypatch.setattr(evidence_store, "replay_many", lambda _paths: (report,))
    result = validate_formal_acceptance((Path("qualified"),))
    assert result.findings == ()
    assert result.acceptance_eligible is True


def test_rejected_cycle_cannot_supply_formal_scenario_tags(tmp_path: Path) -> None:
    recorder, _wall, monotonic = _recorder(tmp_path)
    _record_full_lifecycle(recorder, monotonic, final_confirmations=1)
    report = SmartReconnectReplayValidator().validate(recorder.path)
    assert "general_disconnect" not in report.scenario_tags
    assert "line_selection" not in report.scenario_tags
