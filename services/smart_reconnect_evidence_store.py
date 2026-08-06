"""Privacy-safe smart reconnect evidence recording and offline replay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from PIL import Image

from core.reconnect_policy import ReconnectAction, ReconnectScreenState


SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION = 3
SMART_RECONNECT_EVIDENCE_DIRNAME = "smart_reconnect_evidence"
SMART_RECONNECT_DEADLINE_MS = 60_000
SMART_RECONNECT_STAGE_RESPONSE_TARGET_MS = 2_000
SMART_RECONNECT_PROGRAM_OVERHEAD_TARGET_MS = 10_000
SMART_RECONNECT_TIME_BASELINE_MIN_CYCLES = 5
FORMAL_ACCEPTANCE_MIN_WINDOWS = 14
FORMAL_ACCEPTANCE_MIN_CYCLES = 100
FORMAL_ACCEPTANCE_MIN_DURATION_MS = 8 * 60 * 60 * 1000
FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW = 3
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_CODE = re.compile(r"^[a-z0-9_./:-]{1,160}$")
_PRESENTATION_STATES = frozenset(
    {
        "visible",
        "minimized",
        "partially_obscured",
        "fully_obscured",
        "overlapped",
        "other_window_foreground",
        "unknown",
    }
)
_SCENE_CONTEXTS = frozenset({"general", "battle", "unknown"})
_INPUT_CHANNELS = frozenset({"window_message", "window_control"})
_EXTERNAL_OBSTACLES = frozenset(
    {
        "network_unavailable",
        "server_unavailable",
        "server_queue",
        "account_rejected",
        "process_unrecoverable",
        "login_flow_changed",
    }
)
_EXTERNAL_OBSTACLE_BASES: dict[str, frozenset[str]] = {
    "network_unavailable": frozenset({"operating_system_network_probe"}),
    "server_unavailable": frozenset({"confirmed_server_response"}),
    "server_queue": frozenset({"confirmed_queue_screen"}),
    "account_rejected": frozenset({"confirmed_account_rejection_screen"}),
    "process_unrecoverable": frozenset({"process_restart_result"}),
    "login_flow_changed": frozenset({"versioned_flow_signature_mismatch"}),
}
_FORMAL_SCENARIOS = frozenset(
    {
        "visible",
        "minimized",
        "partially_obscured",
        "fully_obscured",
        "overlapped",
        "other_window_foreground",
        "single_window_disconnect",
        "multi_window_disconnect",
        "general_disconnect",
        "battle_disconnect",
        "login_stall",
        "line_selection",
        "character_selection",
        "post_login_popup",
        "original_role_and_line_restored",
        "auto_battle_restored",
        "unknown_safe",
    }
)
_POST_LOGIN_STATES = frozenset(
    {
        ReconnectScreenState.POST_LOGIN_ACTIVITY.value,
        ReconnectScreenState.POST_LOGIN_RECOMMENDATION.value,
        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON.value,
    }
)
_ACTION_STATES: dict[str, frozenset[str]] = {
    ReconnectAction.CONFIRM_DISCONNECT.value: frozenset(
        {ReconnectScreenState.DISCONNECTED.value}
    ),
    ReconnectAction.START_GAME.value: frozenset(
        {ReconnectScreenState.LOGIN_START.value}
    ),
    ReconnectAction.FORCE_LOGIN.value: frozenset(
        {ReconnectScreenState.FORCE_LOGIN_START.value}
    ),
    ReconnectAction.CONFIRM_FORCE_LOGIN_TIMEOUT.value: frozenset(
        {ReconnectScreenState.FORCE_LOGIN_TIMEOUT.value}
    ),
    ReconnectAction.SELECT_DEFAULT_LINE.value: frozenset(
        {ReconnectScreenState.LINE_SELECTION.value}
    ),
    ReconnectAction.ENTER_GAME.value: frozenset(
        {ReconnectScreenState.CHARACTER_SELECTION.value}
    ),
    ReconnectAction.CLOSE_ANNOUNCEMENT.value: _POST_LOGIN_STATES,
    "restart_window": frozenset({ReconnectScreenState.DISCONNECTED.value}),
    "scroll_line_list": frozenset({ReconnectScreenState.LINE_SELECTION.value}),
    "select_role_slot": frozenset(
        {ReconnectScreenState.CHARACTER_SELECTION.value}
    ),
    "reopen_window": frozenset(
        {
            ReconnectScreenState.DISCONNECTED.value,
            ReconnectScreenState.RECONNECTING.value,
        }
    ),
    "auto_battle_click": frozenset(
        {ReconnectScreenState.CONNECTED.value}
    ),
}
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {
        "role_name",
        "recent_login_role",
        "chat",
        "window_title",
        "handle",
        "process_id",
        "thread_id",
        "launch_fingerprint",
        "click_point",
        "coordinates",
        "pixels",
        "raw_arguments",
        "login_parameters",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeSourceIdentity:
    commit: str | None
    working_tree_dirty: bool | None
    source: str

    @property
    def acceptance_eligible(self) -> bool:
        return bool(
            isinstance(self.commit, str)
            and _COMMIT.fullmatch(self.commit) is not None
            and self.working_tree_dirty is False
        )


@dataclass(frozen=True, slots=True)
class ReplayFinding:
    code: str
    sequence: int | None = None
    window_id: str | None = None


@dataclass(frozen=True, slots=True)
class SmartReconnectReplayReport:
    source_commit: str | None
    event_count: int
    window_count: int
    recovered_cycles: int
    pending_actions: int
    session_duration_ms: int
    cycles: tuple["ReplayCycleResult", ...]
    recovered_cycles_by_window: tuple[tuple[str, int], ...]
    scenario_tags: tuple[str, ...]
    findings: tuple[ReplayFinding, ...]

    @property
    def acceptance_eligible(self) -> bool:
        return not self.findings and self.pending_actions == 0


@dataclass(frozen=True, slots=True)
class ReplayStageTiming:
    stage: str
    state_episode: int
    started_elapsed_ms: int
    finished_elapsed_ms: int
    scan_duration_ms: int | None
    recognition_upper_bound_ms: int | None
    post_recognition_action_ms: int
    program_response_upper_bound_ms: int | None
    next_stage: str | None
    next_stage_started_elapsed_ms: int | None
    transition_wait_ms: int | None


@dataclass(frozen=True, slots=True)
class ReplayCycleResult:
    window_id: str
    cycle: int
    started_elapsed_ms: int
    finished_elapsed_ms: int | None
    total_elapsed_ms: int | None
    stage_times_ms: tuple[tuple[str, int], ...]
    stage_timings: tuple[ReplayStageTiming, ...]
    program_overhead_upper_bound_ms: int | None
    transition_wait_ms: int | None
    sixty_second_target_met: bool | None
    stage_response_target_met: bool
    program_overhead_target_met: bool
    fallback_capture_used: bool
    fallback_recognition_used: bool
    window_state_restored: bool
    original_line_verified: bool
    original_role_verified: bool
    auto_battle_panel_verified: bool
    external_obstacles: tuple[str, ...]
    accepted: bool


@dataclass(frozen=True, slots=True)
class FormalAcceptanceReport:
    source_commit: str | None
    session_count: int
    distinct_windows_in_one_session: int
    recovered_cycles: int
    longest_session_duration_ms: int
    findings: tuple[ReplayFinding, ...]

    @property
    def acceptance_eligible(self) -> bool:
        return not self.findings


def _read_build_commit(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "commit":
            candidate = value.strip().casefold()
            return candidate if _COMMIT.fullmatch(candidate) else None
    return None


def resolve_runtime_source_identity(
    *,
    repository_root: Path | None = None,
    executable: Path | None = None,
) -> RuntimeSourceIdentity:
    """Resolve a build commit without persisting paths or command output."""

    executable_path = Path(executable or sys.executable).resolve()
    for candidate in (
        executable_path.with_name("BUILD_INFO.txt"),
        executable_path.parent.parent / "BUILD_INFO.txt",
    ):
        commit = _read_build_commit(candidate)
        if commit is not None:
            return RuntimeSourceIdentity(commit, False, "build_info")

    root = Path(repository_root or Path(__file__).resolve().parents[1])
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    try:
        commit_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=3,
            creationflags=creation_flags,
        )
        commit = commit_result.stdout.strip().casefold()
        if _COMMIT.fullmatch(commit) is None:
            raise ValueError("invalid source commit")
        status_result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=3,
            creationflags=creation_flags,
        )
        return RuntimeSourceIdentity(commit, bool(status_result.stdout), "git")
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return RuntimeSourceIdentity(None, None, "unresolved")


def _safe_code(value: object, *, fallback: str | None = None) -> str | None:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip().casefold().replace("\\", "/")
    if _SAFE_CODE.fullmatch(normalized) is None:
        return fallback
    return normalized


def _normalized_score(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        return round(float(value), 3)
    return None


def anonymous_screen_features(
    width: int,
    height: int,
    pixels: bytes,
) -> tuple[tuple[int, ...], str] | None:
    """Return non-reconstructive luminance and edge histograms.

    The output contains no pixel grid, text crop, colour image, or coordinates.
    It is useful for grouping repeated real failures without retaining a screen.
    """

    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or not isinstance(pixels, bytes)
        or len(pixels) != width * height * 4
    ):
        return None
    try:
        image = Image.frombytes(
            "RGBA",
            (width, height),
            pixels,
            "raw",
            "BGRA",
        ).convert("L")
        reduced = image.resize((32, 18), Image.Resampling.BOX)
    except (OSError, ValueError):
        return None
    values = tuple(int(value) for value in reduced.get_flattened_data())
    histogram = [0] * 16
    for value in values:
        histogram[min(15, value // 16)] += 1
    total = len(values)
    normalized_histogram = tuple(
        min(255, round(count * 255 / total)) for count in histogram
    )
    horizontal: list[int] = []
    for band in range(8):
        start = round(band * 18 / 8)
        end = max(start + 1, round((band + 1) * 18 / 8))
        differences = [
            abs(values[row * 32 + column] - values[row * 32 + column - 1])
            for row in range(start, min(18, end))
            for column in range(1, 32)
        ]
        horizontal.append(
            min(15, round((sum(differences) / max(1, len(differences))) / 16))
        )
    vertical: list[int] = []
    for band in range(8):
        start = round(band * 32 / 8)
        end = max(start + 1, round((band + 1) * 32 / 8))
        differences = [
            abs(values[row * 32 + column] - values[(row - 1) * 32 + column])
            for row in range(1, 18)
            for column in range(start, min(32, end))
        ]
        vertical.append(
            min(15, round((sum(differences) / max(1, len(differences))) / 16))
        )
    features = (*normalized_histogram, *horizontal, *vertical)
    digest = hashlib.sha256(bytes(features)).hexdigest()
    return features, digest


class SmartReconnectEvidenceRecorder:
    """Append-only evidence recorder with in-memory identity anonymisation."""

    def __init__(
        self,
        directory: Path,
        *,
        source_identity: RuntimeSourceIdentity,
        auto_battle_required: bool,
        session_id: str | None = None,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    ):
        self._lock = threading.RLock()
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._started_monotonic = float(monotonic_clock())
        self._session_id = session_id or secrets.token_hex(12)
        if re.fullmatch(r"[0-9a-f]{24}", self._session_id) is None:
            raise ValueError("session_id must be 24 lowercase hexadecimal characters")
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(
            float(wall_clock()), timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")
        self.path = self._directory / f"{stamp}-{self._session_id}.jsonl"
        self._source_identity = source_identity
        self._auto_battle_required = bool(auto_battle_required)
        self._sequence = 0
        self._window_ids: dict[str, str] = {}
        self._previous_state: dict[str, str] = {}
        self._state_episodes: dict[str, int] = {}
        self._cycles: dict[str, int] = {}
        self._cycle_started_at: dict[str, float] = {}
        self._cycle_active: dict[str, bool] = {}
        self._observation_attempts: dict[str, int] = {}
        self._observation_sequences: dict[str, list[int]] = {}
        self._proof_sequences: dict[str, list[int]] = {}
        self._last_observation_key: dict[str, tuple[object, ...]] = {}
        self._same_observation_count: dict[str, int] = {}
        self._last_observation_written_at: dict[str, float] = {}
        self._last_observation_attempt_at: dict[str, float] = {}
        self._line_verified: dict[str, bool | None] = {}
        self._role_verified: dict[str, bool | None] = {}
        self._auto_battle_verified: dict[str, bool | None] = {}
        self._fallback_capture_used: dict[str, bool] = {}
        self._fallback_recognition_used: dict[str, bool] = {}
        self._window_state_restored: dict[str, bool] = {}
        self._active_external_obstacles: dict[str, set[str]] = {}
        self._pending_post_action_observations: dict[str, int] = {}
        self._action_intents: dict[int, tuple[str, int, str, str]] = {}
        self._monitoring_enabled = False
        self._append(
            {
                "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                "record_type": "session",
                "session_id": self._session_id,
                "source_commit": source_identity.commit,
                "working_tree_dirty": source_identity.working_tree_dirty,
                "source_identity_kind": source_identity.source,
                "acceptance_eligible_source": source_identity.acceptance_eligible,
                "auto_battle_required": self._auto_battle_required,
                "reconnect_deadline_ms": SMART_RECONNECT_DEADLINE_MS,
                "reconnect_deadline_enforced": False,
                "stage_response_target_ms": (
                    SMART_RECONNECT_STAGE_RESPONSE_TARGET_MS
                ),
                "program_overhead_target_ms": (
                    SMART_RECONNECT_PROGRAM_OVERHEAD_TARGET_MS
                ),
                "time_baseline_min_cycles": (
                    SMART_RECONNECT_TIME_BASELINE_MIN_CYCLES
                ),
                "formal_acceptance_min_windows": FORMAL_ACCEPTANCE_MIN_WINDOWS,
                "formal_acceptance_min_cycles": FORMAL_ACCEPTANCE_MIN_CYCLES,
                "formal_acceptance_min_cycles_per_window": (
                    FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW
                ),
                "formal_acceptance_min_duration_ms": FORMAL_ACCEPTANCE_MIN_DURATION_MS,
                "started_at_utc": self._utc_now(),
                "privacy": {
                    "full_screens_saved": False,
                    "raw_pixels_saved": False,
                    "role_names_saved": False,
                    "chat_saved": False,
                    "window_handles_saved": False,
                    "process_ids_saved": False,
                    "login_parameters_saved": False,
                },
            },
            durable=True,
        )

    def record_monitoring_state(self, enabled: bool) -> int | None:
        with self._lock:
            normalized = enabled is True
            if self._monitoring_enabled == normalized:
                return None
            self._monitoring_enabled = normalized
            now = float(self._monotonic_clock())
            sequence = self._next_sequence()
            self._append(
                {
                    "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                    "record_type": "monitoring_state",
                    "session_id": self._session_id,
                    "sequence": sequence,
                    "recorded_at_utc": self._utc_now(),
                    "elapsed_ms": max(
                        0,
                        round((now - self._started_monotonic) * 1000),
                    ),
                    "enabled": normalized,
                },
                durable=True,
            )
            return sequence

    @classmethod
    def for_runtime(
        cls,
        data_dir: Path,
        *,
        auto_battle_required: bool,
    ) -> "SmartReconnectEvidenceRecorder":
        return cls(
            Path(data_dir) / SMART_RECONNECT_EVIDENCE_DIRNAME,
            source_identity=resolve_runtime_source_identity(),
            auto_battle_required=auto_battle_required,
        )

    def _utc_now(self) -> str:
        return datetime.fromtimestamp(
            float(self._wall_clock()), timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _window_id(self, raw_key: str) -> str:
        value = self._window_ids.get(raw_key)
        if value is None:
            value = f"window-{len(self._window_ids) + 1:02d}"
            self._window_ids[raw_key] = value
        return value

    def _append(self, payload: Mapping[str, object], *, durable: bool) -> None:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            if durable:
                os.fsync(stream.fileno())

    def record_observation(
        self,
        *,
        raw_window_key: str,
        state: str,
        capture_method: str | None,
        width: int | None,
        height: int | None,
        pixels: bytes | None,
        fresh: bool,
        identity_verified: bool,
        score: object,
        reference_code: object,
        failure_reason: object,
        recognition_method: object = "primary",
        fallback_capture_used: bool = False,
        fallback_recognition_used: bool = False,
        presentation_state: object = "unknown",
        scene_context: object = "unknown",
        recognition_basis: object = None,
        other_window_foreground: bool = False,
        overlapped_by_game_window: bool = False,
        scan_duration_ms: object = None,
    ) -> int | None:
        normalized_state = _safe_code(state, fallback=ReconnectScreenState.UNKNOWN.value)
        if normalized_state not in {item.value for item in ReconnectScreenState}:
            normalized_state = ReconnectScreenState.UNKNOWN.value
        safe_method = _safe_code(capture_method, fallback="unresolved")
        safe_reference = _safe_code(reference_code)
        safe_failure = _safe_code(failure_reason)
        safe_recognition_method = _safe_code(
            recognition_method,
            fallback="unresolved",
        )
        safe_presentation = _safe_code(
            presentation_state,
            fallback="unknown",
        )
        if safe_presentation not in _PRESENTATION_STATES:
            safe_presentation = "unknown"
        safe_scene_context = _safe_code(scene_context, fallback="unknown")
        if safe_scene_context not in _SCENE_CONTEXTS:
            safe_scene_context = "unknown"
        safe_recognition_basis = _safe_code(recognition_basis)
        frame_digest: str | None = None
        features: tuple[int, ...] = ()
        feature_digest: str | None = None
        visual_content_present = False
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
            and isinstance(pixels, bytes)
        ):
            frame_digest = hashlib.sha256(pixels).hexdigest()
            feature_result = anonymous_screen_features(width, height, pixels)
            if feature_result is not None:
                features, feature_digest = feature_result
                # Uniform black, white, or single-colour frames are not
                # gameplay proof.  Require actual edge structure as well as a
                # later cross-map fixed-UI match.
                visual_content_present = bool(
                    features and sum(features[16:]) > 0
                )
        normalized_score = _normalized_score(score)
        normalized_scan_duration_ms = (
            max(0, round(float(scan_duration_ms)))
            if isinstance(scan_duration_ms, (int, float))
            and not isinstance(scan_duration_ms, bool)
            and math.isfinite(float(scan_duration_ms))
            and float(scan_duration_ms) >= 0
            else None
        )
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            previous_state = self._previous_state.get(window_id)
            now = float(self._monotonic_clock())
            previous_attempt_at = self._last_observation_attempt_at.get(
                window_id
            )
            scan_interval_ms = (
                max(0, round((now - previous_attempt_at) * 1000))
                if previous_attempt_at is not None
                else None
            )
            self._last_observation_attempt_at[window_id] = now
            if previous_state != normalized_state:
                self._state_episodes[window_id] = (
                    self._state_episodes.get(window_id, 0) + 1
                )
            state_episode = self._state_episodes.get(window_id, 0)
            if (
                normalized_state == ReconnectScreenState.DISCONNECTED.value
                and not self._cycle_active.get(window_id, False)
            ):
                self._cycles[window_id] = self._cycles.get(window_id, 0) + 1
                self._cycle_started_at[window_id] = now
                self._cycle_active[window_id] = True
                self._observation_attempts[window_id] = 0
                self._line_verified[window_id] = None
                self._role_verified[window_id] = None
                self._auto_battle_verified[window_id] = None
                self._fallback_capture_used[window_id] = False
                self._fallback_recognition_used[window_id] = False
                self._window_state_restored[window_id] = True
                self._active_external_obstacles[window_id] = set()
                self._proof_sequences[window_id] = []
            cycle = self._cycles.get(window_id, 0)
            self._observation_attempts[window_id] = (
                self._observation_attempts.get(window_id, 0) + 1
            )
            self._fallback_capture_used[window_id] = bool(
                self._fallback_capture_used.get(window_id, False)
                or fallback_capture_used is True
            )
            self._fallback_recognition_used[window_id] = bool(
                self._fallback_recognition_used.get(window_id, False)
                or fallback_recognition_used is True
            )
            observation_key = (
                normalized_state,
                safe_method,
                width,
                height,
                safe_reference,
                safe_failure,
                safe_recognition_method,
                fallback_capture_used is True,
                fallback_recognition_used is True,
                safe_presentation,
                safe_scene_context,
                safe_recognition_basis,
                other_window_foreground is True,
                overlapped_by_game_window is True,
                None if normalized_score is None else round(normalized_score / 2),
            )
            if self._last_observation_key.get(window_id) == observation_key:
                repeated = self._same_observation_count.get(window_id, 1) + 1
            else:
                repeated = 1
            self._last_observation_key[window_id] = observation_key
            self._same_observation_count[window_id] = repeated
            heartbeat = (
                30.0
                if normalized_state
                in {
                    ReconnectScreenState.UNKNOWN.value,
                    ReconnectScreenState.RECONNECTING.value,
                    ReconnectScreenState.FAILED.value,
                }
                else 300.0
            )
            last_written = self._last_observation_written_at.get(window_id)
            should_write = bool(
                repeated <= 2
                or previous_state != normalized_state
                or self._pending_post_action_observations.get(window_id, 0) > 0
                or last_written is None
                or now - last_written >= heartbeat
            )
            self._previous_state[window_id] = normalized_state
            if (
                normalized_state == ReconnectScreenState.CONNECTED.value
                and fresh is True
                and identity_verified is True
                and visual_content_present
                and safe_recognition_basis == "cross_map_fixed_ui"
            ):
                self._cycle_active[window_id] = False
            if not should_write:
                return None
            sequence = self._next_sequence()
            elapsed_ms = max(
                0,
                round((now - self._started_monotonic) * 1000),
            )
            cycle_started = self._cycle_started_at.get(window_id)
            payload = {
                "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                "record_type": "observation",
                "session_id": self._session_id,
                "sequence": sequence,
                "recorded_at_utc": self._utc_now(),
                "elapsed_ms": elapsed_ms,
                "cycle_elapsed_ms": (
                    max(0, round((now - cycle_started) * 1000))
                    if cycle > 0 and cycle_started is not None
                    else None
                ),
                "window_id": window_id,
                "cycle": cycle,
                "state_episode": state_episode,
                "observation_attempt": self._observation_attempts[window_id],
                "previous_state": previous_state,
                "state": normalized_state,
                "capture_method": safe_method,
                "fallback_capture_used": fallback_capture_used is True,
                "recognition_method": safe_recognition_method,
                "fallback_recognition_used": fallback_recognition_used is True,
                "presentation_state": safe_presentation,
                "scene_context": safe_scene_context,
                "recognition_basis": safe_recognition_basis,
                "other_window_foreground": other_window_foreground is True,
                "overlapped_by_game_window": (
                    overlapped_by_game_window is True
                ),
                "width": width if isinstance(width, int) and width > 0 else None,
                "height": height if isinstance(height, int) and height > 0 else None,
                "frame_sha256": frame_digest,
                "feature_schema": "luma-edge-histogram-v1",
                "anonymous_features": list(features),
                "feature_sha256": feature_digest,
                "visual_content_present": visual_content_present,
                "fresh": fresh is True,
                "identity_verified": identity_verified is True,
                "recognition_score": normalized_score,
                "scan_duration_ms": normalized_scan_duration_ms,
                "scan_interval_ms": scan_interval_ms,
                "reference_code": safe_reference,
                "failure_reason": safe_failure,
                "original_line_verified": self._line_verified.get(window_id),
                "original_role_verified": self._role_verified.get(window_id),
                "auto_battle_panel_verified": self._auto_battle_verified.get(window_id),
                "active_external_obstacles": sorted(
                    self._active_external_obstacles.get(window_id, set())
                ),
            }
            self._append(payload, durable=False)
            self._last_observation_written_at[window_id] = now
            pending_observations = self._pending_post_action_observations.get(
                window_id,
                0,
            )
            if pending_observations > 1:
                self._pending_post_action_observations[window_id] = (
                    pending_observations - 1
                )
            else:
                self._pending_post_action_observations.pop(window_id, None)
            observations = self._observation_sequences.setdefault(window_id, [])
            observations.append(sequence)
            del observations[:-4]
            return sequence

    def record_decision_evidence(
        self,
        *,
        raw_window_key: str,
        state: str,
        decision_signature: str,
        capture_method: str | None,
        identity_verified: bool,
        authority_signature: str,
    ) -> int:
        if _SHA256.fullmatch(decision_signature) is None:
            raise ValueError("decision_signature must be a SHA-256 digest")
        if _SHA256.fullmatch(authority_signature) is None:
            raise ValueError("authority_signature must be a SHA-256 digest")
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            now = float(self._monotonic_clock())
            sequence = self._next_sequence()
            observation_sequence = (
                self._observation_sequences.get(window_id, [None])[-1]
            )
            payload = {
                "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                "record_type": "decision_evidence",
                "session_id": self._session_id,
                "sequence": sequence,
                "recorded_at_utc": self._utc_now(),
                "elapsed_ms": max(
                    0,
                    round((now - self._started_monotonic) * 1000),
                ),
                "window_id": window_id,
                "cycle": self._cycles.get(window_id, 0),
                "state_episode": self._state_episodes.get(window_id, 0),
                "state": _safe_code(
                    state,
                    fallback=ReconnectScreenState.UNKNOWN.value,
                ),
                "capture_method": _safe_code(
                    capture_method,
                    fallback="unresolved",
                ),
                "identity_verified": identity_verified is True,
                "decision_signature": decision_signature,
                "authority_signature": authority_signature,
                "observation_sequence": observation_sequence,
            }
            self._append(payload, durable=False)
            proofs = self._proof_sequences.setdefault(window_id, [])
            proofs.append(sequence)
            del proofs[:-4]
            return sequence

    def record_absence_evidence(
        self,
        *,
        raw_window_key: str,
        state: str,
        decision_signature: str,
        identity_verified: bool,
        shortcut_identity_verified: bool,
        target_absent: bool,
        authority_signature: str,
    ) -> int:
        """Record one fresh enumeration proving that one known target is absent."""

        if _SHA256.fullmatch(decision_signature) is None:
            raise ValueError("decision_signature must be a SHA-256 digest")
        if _SHA256.fullmatch(authority_signature) is None:
            raise ValueError("authority_signature must be a SHA-256 digest")
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            now = float(self._monotonic_clock())
            sequence = self._next_sequence()
            payload = {
                "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                "record_type": "absence_evidence",
                "session_id": self._session_id,
                "sequence": sequence,
                "recorded_at_utc": self._utc_now(),
                "elapsed_ms": max(
                    0,
                    round((now - self._started_monotonic) * 1000),
                ),
                "window_id": window_id,
                "cycle": self._cycles.get(window_id, 0),
                "state_episode": self._state_episodes.get(window_id, 0),
                "state": _safe_code(
                    state,
                    fallback=ReconnectScreenState.RECONNECTING.value,
                ),
                "capture_method": "fresh_window_enumeration",
                "identity_verified": identity_verified is True,
                "shortcut_identity_verified": (
                    shortcut_identity_verified is True
                ),
                "target_absent": target_absent is True,
                "decision_signature": decision_signature,
                "authority_signature": authority_signature,
            }
            self._append(payload, durable=False)
            proofs = self._proof_sequences.setdefault(window_id, [])
            proofs.append(sequence)
            del proofs[:-4]
            return sequence

    def record_action(
        self,
        *,
        raw_window_key: str,
        state: str,
        action: str,
        allowed: bool,
        performed: bool,
        clicked: bool,
        identity_verified: bool,
        restoration_verified: bool | None,
        failure_reason: object = None,
        original_line_verified: bool | None = None,
        original_role_verified: bool | None = None,
        auto_battle_panel_verified: bool | None = None,
        input_channel: str = "window_message",
        intent_sequence: int | None = None,
        authority_signature: str | None = None,
    ) -> int:
        safe_action = _safe_code(action, fallback="unknown_action")
        safe_state = _safe_code(state, fallback=ReconnectScreenState.UNKNOWN.value)
        safe_failure = _safe_code(failure_reason)
        safe_input_channel = _safe_code(input_channel, fallback="unresolved")
        if safe_input_channel not in _INPUT_CHANNELS:
            raise ValueError("input_channel is not an approved non-physical channel")
        if (
            authority_signature is not None
            and _SHA256.fullmatch(authority_signature) is None
        ):
            raise ValueError("authority_signature must be a SHA-256 digest")
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            now = float(self._monotonic_clock())
            if original_line_verified is not None:
                self._line_verified[window_id] = original_line_verified is True
            if original_role_verified is not None:
                self._role_verified[window_id] = original_role_verified is True
            if auto_battle_panel_verified is not None:
                self._auto_battle_verified[window_id] = (
                    auto_battle_panel_verified is True
                )
            if restoration_verified is not None:
                self._window_state_restored[window_id] = bool(
                    self._window_state_restored.get(window_id, True)
                    and restoration_verified is True
                )
            sequence = self._next_sequence()
            payload = {
                "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                "record_type": "action",
                "session_id": self._session_id,
                "sequence": sequence,
                "recorded_at_utc": self._utc_now(),
                "elapsed_ms": max(
                    0,
                    round((now - self._started_monotonic) * 1000),
                ),
                "window_id": window_id,
                "cycle": self._cycles.get(window_id, 0),
                "state_episode": self._state_episodes.get(window_id, 0),
                "state": safe_state,
                "action": safe_action,
                "allowed": allowed is True,
                "performed": performed is True,
                "clicked": clicked is True,
                "identity_verified": identity_verified is True,
                "restoration_verified": (
                    restoration_verified
                    if type(restoration_verified) is bool
                    else None
                ),
                "failure_reason": safe_failure,
                "input_channel": safe_input_channel,
                "intent_sequence": intent_sequence,
                "authority_signature": authority_signature,
                "decision_evidence_sequences": list(
                    self._proof_sequences.get(window_id, [])[-2:]
                ),
                "original_line_verified": self._line_verified.get(window_id),
                "original_role_verified": self._role_verified.get(window_id),
                "auto_battle_panel_verified": self._auto_battle_verified.get(window_id),
                "fallback_capture_used": self._fallback_capture_used.get(
                    window_id,
                    False,
                ),
                "fallback_recognition_used": self._fallback_recognition_used.get(
                    window_id,
                    False,
                ),
                "active_external_obstacles": sorted(
                    self._active_external_obstacles.get(window_id, set())
                ),
            }
            self._append(payload, durable=True)
            if performed:
                self._pending_post_action_observations[window_id] = max(
                    2,
                    self._pending_post_action_observations.get(window_id, 0),
                )
            return sequence

    def record_action_intent(
        self,
        *,
        raw_window_key: str,
        state: str,
        action: str,
        identity_verified: bool,
        input_channel: str = "window_message",
        authority_signature: str,
    ) -> int:
        safe_state = _safe_code(state, fallback=ReconnectScreenState.UNKNOWN.value)
        safe_action = _safe_code(action, fallback="unknown_action")
        safe_input_channel = _safe_code(input_channel, fallback="unresolved")
        if safe_input_channel not in _INPUT_CHANNELS:
            raise ValueError("input_channel is not an approved non-physical channel")
        if _SHA256.fullmatch(authority_signature) is None:
            raise ValueError("authority_signature must be a SHA-256 digest")
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            cycle = self._cycles.get(window_id, 0)
            now = float(self._monotonic_clock())
            sequence = self._next_sequence()
            payload = {
                "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                "record_type": "action_intent",
                "session_id": self._session_id,
                "sequence": sequence,
                "recorded_at_utc": self._utc_now(),
                "elapsed_ms": max(
                    0,
                    round((now - self._started_monotonic) * 1000),
                ),
                "window_id": window_id,
                "cycle": cycle,
                "state_episode": self._state_episodes.get(window_id, 0),
                "state": safe_state,
                "action": safe_action,
                "identity_verified": identity_verified is True,
                "input_channel": safe_input_channel,
                "authority_signature": authority_signature,
                "decision_evidence_sequences": list(
                    self._proof_sequences.get(window_id, [])[-2:]
                ),
            }
            self._append(payload, durable=True)
            self._action_intents[sequence] = (
                window_id,
                cycle,
                safe_state or ReconnectScreenState.UNKNOWN.value,
                safe_action or "unknown_action",
            )
            return sequence

    def record_external_obstacle(
        self,
        *,
        raw_window_key: str,
        obstacle: str,
        active: bool,
        classification_basis: str,
        evidence_signature: str,
    ) -> int:
        safe_obstacle = _safe_code(obstacle)
        if safe_obstacle not in _EXTERNAL_OBSTACLES:
            raise ValueError("unsupported external obstacle")
        safe_basis = _safe_code(classification_basis)
        if safe_basis not in _EXTERNAL_OBSTACLE_BASES[safe_obstacle]:
            raise ValueError("external obstacle basis does not prove this obstacle")
        if _SHA256.fullmatch(evidence_signature) is None:
            raise ValueError("evidence_signature must be a SHA-256 digest")
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            obstacles = self._active_external_obstacles.setdefault(window_id, set())
            if active:
                obstacles.add(safe_obstacle)
            else:
                obstacles.discard(safe_obstacle)
            now = float(self._monotonic_clock())
            sequence = self._next_sequence()
            self._append(
                {
                    "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                    "record_type": "external_obstacle",
                    "session_id": self._session_id,
                    "sequence": sequence,
                    "recorded_at_utc": self._utc_now(),
                    "elapsed_ms": max(
                        0,
                        round((now - self._started_monotonic) * 1000),
                    ),
                    "window_id": window_id,
                    "cycle": self._cycles.get(window_id, 0),
                    "state_episode": self._state_episodes.get(window_id, 0),
                    "obstacle": safe_obstacle,
                    "active": active is True,
                    "classification_basis": safe_basis,
                    "evidence_signature": evidence_signature,
                },
                durable=True,
            )
            return sequence

    def record_verification(
        self,
        *,
        raw_window_key: str,
        identity_verified: bool,
        verification_basis: str,
        evidence_signature: str,
        original_line_verified: bool | None = None,
        original_role_verified: bool | None = None,
        auto_battle_panel_verified: bool | None = None,
        window_state_restored: bool | None = None,
    ) -> int:
        """Persist a non-input verification without inventing an action."""

        values = (
            original_line_verified,
            original_role_verified,
            auto_battle_panel_verified,
            window_state_restored,
        )
        if not any(type(value) is bool for value in values):
            raise ValueError("at least one verification result is required")
        if any(value is not None and type(value) is not bool for value in values):
            raise TypeError("verification results must be bool or None")
        safe_basis = _safe_code(verification_basis)
        if safe_basis is None:
            raise ValueError("verification_basis is required")
        if _SHA256.fullmatch(evidence_signature) is None:
            raise ValueError("evidence_signature must be a SHA-256 digest")
        with self._lock:
            window_id = self._window_id(str(raw_window_key))
            if original_line_verified is not None:
                self._line_verified[window_id] = original_line_verified
            if original_role_verified is not None:
                self._role_verified[window_id] = original_role_verified
            if auto_battle_panel_verified is not None:
                self._auto_battle_verified[window_id] = auto_battle_panel_verified
            if window_state_restored is not None:
                self._window_state_restored[window_id] = bool(
                    self._window_state_restored.get(window_id, True)
                    and window_state_restored
                )
            now = float(self._monotonic_clock())
            sequence = self._next_sequence()
            self._append(
                {
                    "schema_version": SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION,
                    "record_type": "verification",
                    "session_id": self._session_id,
                    "sequence": sequence,
                    "recorded_at_utc": self._utc_now(),
                    "elapsed_ms": max(
                        0,
                        round((now - self._started_monotonic) * 1000),
                    ),
                    "window_id": window_id,
                    "cycle": self._cycles.get(window_id, 0),
                    "state_episode": self._state_episodes.get(window_id, 0),
                    "identity_verified": identity_verified is True,
                    "verification_basis": safe_basis,
                    "evidence_signature": evidence_signature,
                    "original_line_verified": original_line_verified,
                    "original_role_verified": original_role_verified,
                    "auto_battle_panel_verified": auto_battle_panel_verified,
                    "window_state_restored": window_state_restored,
                },
                durable=True,
            )
            return sequence


class SmartReconnectReplayValidator:
    """Replay persisted evidence and report safety or lifecycle violations."""

    def load(self, path: Path) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        with Path(path).open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid evidence JSON at line {line_number}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"invalid evidence record at line {line_number}"
                    )
                records.append(payload)
        if not records:
            raise ValueError("evidence file is empty")
        return tuple(records)

    @staticmethod
    def _contains_forbidden_key(value: object) -> bool:
        if isinstance(value, Mapping):
            return any(
                str(key).casefold() in _FORBIDDEN_PERSISTED_KEYS
                or SmartReconnectReplayValidator._contains_forbidden_key(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(
                SmartReconnectReplayValidator._contains_forbidden_key(item)
                for item in value
            )
        return False

    def validate(self, path: Path) -> SmartReconnectReplayReport:
        records = self.load(path)
        header = records[0]
        findings: list[ReplayFinding] = []
        if header.get("record_type") != "session":
            findings.append(ReplayFinding("session_header_missing"))
        if header.get("schema_version") != SMART_RECONNECT_EVIDENCE_SCHEMA_VERSION:
            findings.append(ReplayFinding("schema_version_mismatch"))
        if header.get("reconnect_deadline_ms") != SMART_RECONNECT_DEADLINE_MS:
            findings.append(ReplayFinding("reconnect_deadline_mismatch"))
        if header.get("reconnect_deadline_enforced") not in {None, False}:
            findings.append(ReplayFinding("reconnect_deadline_mode_mismatch"))
        if header.get(
            "stage_response_target_ms",
            SMART_RECONNECT_STAGE_RESPONSE_TARGET_MS,
        ) != SMART_RECONNECT_STAGE_RESPONSE_TARGET_MS:
            findings.append(ReplayFinding("stage_response_target_mismatch"))
        if header.get(
            "program_overhead_target_ms",
            SMART_RECONNECT_PROGRAM_OVERHEAD_TARGET_MS,
        ) != SMART_RECONNECT_PROGRAM_OVERHEAD_TARGET_MS:
            findings.append(ReplayFinding("program_overhead_target_mismatch"))
        if header.get(
            "time_baseline_min_cycles",
            SMART_RECONNECT_TIME_BASELINE_MIN_CYCLES,
        ) != SMART_RECONNECT_TIME_BASELINE_MIN_CYCLES:
            findings.append(ReplayFinding("time_baseline_requirement_mismatch"))
        if (
            header.get("formal_acceptance_min_cycles_per_window")
            != FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW
        ):
            findings.append(
                ReplayFinding("formal_cycle_per_window_requirement_mismatch")
            )
        source_commit = header.get("source_commit")
        if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
            findings.append(ReplayFinding("source_commit_missing"))
            normalized_commit = None
        else:
            normalized_commit = source_commit
        if header.get("working_tree_dirty") is not False:
            findings.append(ReplayFinding("source_not_clean"))
        if header.get("auto_battle_required") is not True:
            findings.append(ReplayFinding("auto_battle_not_required"))
        if self._contains_forbidden_key(records):
            findings.append(ReplayFinding("private_field_persisted"))

        by_sequence: dict[int, dict[str, object]] = {}
        observations: dict[tuple[str, int], list[dict[str, object]]] = {}
        proofs: dict[int, dict[str, object]] = {}
        intents: dict[int, dict[str, object]] = {}
        actions: list[dict[str, object]] = []
        verifications: dict[tuple[str, int], list[dict[str, object]]] = {}
        external_events: dict[tuple[str, int], list[dict[str, object]]] = {}
        monitoring_events: list[dict[str, object]] = []
        windows: set[str] = set()
        previous_sequence = 0
        last_event_elapsed_ms = 0
        monitoring_enabled = False
        for record in records[1:]:
            sequence = record.get("sequence")
            window_id = record.get("window_id")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence <= previous_sequence
            ):
                findings.append(ReplayFinding("sequence_invalid"))
                continue
            previous_sequence = sequence
            by_sequence[sequence] = record
            elapsed_ms = record.get("elapsed_ms")
            if (
                isinstance(elapsed_ms, int)
                and not isinstance(elapsed_ms, bool)
                and elapsed_ms >= 0
            ):
                last_event_elapsed_ms = max(last_event_elapsed_ms, elapsed_ms)
            else:
                findings.append(
                    ReplayFinding("elapsed_time_missing", sequence, window_id)
                )
            if isinstance(window_id, str):
                windows.add(window_id)
            cycle = record.get("cycle")
            key = (
                window_id if isinstance(window_id, str) else "",
                cycle if isinstance(cycle, int) and not isinstance(cycle, bool) else -1,
            )
            record_type = record.get("record_type")
            if record_type == "monitoring_state":
                if type(record.get("enabled")) is not bool:
                    findings.append(
                        ReplayFinding("monitoring_state_invalid", sequence)
                    )
                else:
                    monitoring_enabled = record.get("enabled") is True
                    monitoring_events.append(record)
                continue
            if not monitoring_enabled:
                findings.append(
                    ReplayFinding("event_outside_monitoring", sequence, key[0] or None)
                )
            state_episode = record.get("state_episode")
            if (
                not isinstance(state_episode, int)
                or isinstance(state_episode, bool)
                or state_episode <= 0
            ):
                findings.append(
                    ReplayFinding("state_episode_invalid", sequence, key[0] or None)
                )
            if record_type == "observation":
                observations.setdefault(key, []).append(record)
                if (
                    record.get("state") == ReconnectScreenState.UNKNOWN.value
                    and record.get("failure_reason") is None
                ):
                    findings.append(
                        ReplayFinding("unknown_reason_missing", sequence, key[0])
                    )
            elif record_type == "decision_evidence":
                proofs[sequence] = record
            elif record_type == "absence_evidence":
                proofs[sequence] = record
            elif record_type == "action_intent":
                intents[sequence] = record
            elif record_type == "action":
                actions.append(record)
            elif record_type == "verification":
                verifications.setdefault(key, []).append(record)
                verification_values = (
                    record.get("original_line_verified"),
                    record.get("original_role_verified"),
                    record.get("auto_battle_panel_verified"),
                    record.get("window_state_restored"),
                )
                if (
                    record.get("identity_verified") is not True
                    or not isinstance(record.get("verification_basis"), str)
                    or _SAFE_CODE.fullmatch(
                        str(record.get("verification_basis", ""))
                    )
                    is None
                    or not isinstance(record.get("evidence_signature"), str)
                    or _SHA256.fullmatch(
                        str(record.get("evidence_signature", ""))
                    )
                    is None
                    or not any(type(value) is bool for value in verification_values)
                    or any(
                        value is not None and type(value) is not bool
                        for value in verification_values
                    )
                ):
                    findings.append(
                        ReplayFinding("verification_invalid", sequence, key[0])
                    )
            elif record_type == "external_obstacle":
                external_events.setdefault(key, []).append(record)
                obstacle = record.get("obstacle")
                basis = record.get("classification_basis")
                if (
                    obstacle not in _EXTERNAL_OBSTACLES
                    or basis not in _EXTERNAL_OBSTACLE_BASES.get(
                        str(obstacle),
                        frozenset(),
                    )
                    or not isinstance(record.get("evidence_signature"), str)
                    or _SHA256.fullmatch(
                        str(record.get("evidence_signature", ""))
                    )
                    is None
                    or type(record.get("active")) is not bool
                ):
                    findings.append(
                        ReplayFinding("external_obstacle_invalid", sequence, key[0])
                    )
            else:
                findings.append(
                    ReplayFinding("record_type_unknown", sequence, key[0] or None)
                )

        session_duration_ms = 0
        monitoring_started: int | None = None
        for event in monitoring_events:
            event_elapsed = int(event.get("elapsed_ms", 0))
            if event.get("enabled") is True:
                if monitoring_started is None:
                    monitoring_started = event_elapsed
            elif monitoring_started is not None:
                session_duration_ms = max(
                    session_duration_ms,
                    max(0, event_elapsed - monitoring_started),
                )
                monitoring_started = None
        if monitoring_started is not None:
            session_duration_ms = max(
                session_duration_ms,
                max(0, last_event_elapsed_ms - monitoring_started),
            )
        if not monitoring_events:
            findings.append(ReplayFinding("monitoring_state_missing"))

        action_keys: set[tuple[str, int, int, str, str]] = set()
        pending_actions = 0
        line_verified: dict[tuple[str, int], bool] = {}
        role_verified: dict[tuple[str, int], bool] = {}
        auto_verified: dict[tuple[str, int], bool] = {}
        restoration_verified: dict[tuple[str, int], bool] = {}
        restoration_true_sequences: dict[tuple[str, int], list[int]] = {}
        unverified_window_control_sequences: dict[
            tuple[str, int], list[int]
        ] = {}
        referenced_intents: set[int] = set()

        def sticky_result(
            values: dict[tuple[str, int], bool],
            key: tuple[str, int],
            value: object,
        ) -> None:
            if type(value) is not bool:
                return
            if values.get(key) is False:
                return
            values[key] = value is True

        for key, items in verifications.items():
            for item in sorted(items, key=lambda value: int(value["sequence"])):
                sticky_result(
                    line_verified,
                    key,
                    item.get("original_line_verified"),
                )
                sticky_result(
                    role_verified,
                    key,
                    item.get("original_role_verified"),
                )
                sticky_result(
                    auto_verified,
                    key,
                    item.get("auto_battle_panel_verified"),
                )
                sticky_result(
                    restoration_verified,
                    key,
                    item.get("window_state_restored"),
                )
                if item.get("window_state_restored") is True:
                    restoration_true_sequences.setdefault(key, []).append(
                        int(item["sequence"])
                    )
        for action_record in actions:
            sequence = int(action_record["sequence"])
            window_id = str(action_record.get("window_id", ""))
            cycle = int(action_record.get("cycle", -1))
            state = str(action_record.get("state", ""))
            action = str(action_record.get("action", ""))
            state_episode = int(action_record.get("state_episode", -1))
            performed = action_record.get("performed") is True
            clicked = action_record.get("clicked") is True
            key = (window_id, cycle)
            action_key = (window_id, cycle, state_episode, state, action)
            if performed and action_key in action_keys:
                findings.append(
                    ReplayFinding("duplicate_stage_action", sequence, window_id)
                )
            if performed:
                action_keys.add(action_key)
            intent_sequence = action_record.get("intent_sequence")
            intent = (
                intents.get(intent_sequence)
                if isinstance(intent_sequence, int)
                and not isinstance(intent_sequence, bool)
                else None
            )
            intent_valid = bool(
                intent is not None
                and intent.get("window_id") == window_id
                and intent.get("cycle") == cycle
                and intent.get("state") == state
                and intent.get("action") == action
                and intent.get("state_episode") == state_episode
                and intent.get("identity_verified") is True
                and isinstance(intent.get("authority_signature"), str)
                and _SHA256.fullmatch(str(intent.get("authority_signature")))
                is not None
                and action_record.get("authority_signature")
                == intent.get("authority_signature")
                and action_record.get("input_channel")
                == intent.get("input_channel")
                and int(intent.get("sequence", 0)) < sequence
            )
            if intent_valid:
                referenced_intents.add(int(intent_sequence))
            elif performed:
                findings.append(
                    ReplayFinding("action_intent_missing", sequence, window_id)
                )
            if state == ReconnectScreenState.UNKNOWN.value and performed:
                findings.append(
                    ReplayFinding("unknown_screen_operated", sequence, window_id)
                )
            expected_states = _ACTION_STATES.get(action)
            if performed and (expected_states is None or state not in expected_states):
                findings.append(
                    ReplayFinding("action_state_mismatch", sequence, window_id)
                )
            if performed and action_record.get("allowed") is not True:
                findings.append(
                    ReplayFinding("action_not_allowed", sequence, window_id)
                )
            if performed and action_record.get("identity_verified") is not True:
                findings.append(
                    ReplayFinding("identity_not_verified", sequence, window_id)
                )
            if clicked and action_record.get("input_channel") != "window_message":
                findings.append(
                    ReplayFinding("pointer_interference_possible", sequence, window_id)
                )
            if performed and action_record.get("input_channel") not in _INPUT_CHANNELS:
                findings.append(
                    ReplayFinding("unsafe_input_channel", sequence, window_id)
                )
            if performed and action_record.get("restoration_verified") is False:
                findings.append(
                    ReplayFinding("window_not_restored", sequence, window_id)
                )
            if performed:
                sticky_result(
                    restoration_verified,
                    key,
                    action_record.get("restoration_verified"),
                )
                if action_record.get("restoration_verified") is True:
                    restoration_true_sequences.setdefault(key, []).append(sequence)
                elif (
                    action_record.get("input_channel") == "window_control"
                    and action_record.get("restoration_verified") is None
                ):
                    unverified_window_control_sequences.setdefault(key, []).append(
                        sequence
                    )
            if performed:
                evidence_sequences = action_record.get("decision_evidence_sequences")
                valid_proofs = (
                    [proofs.get(item) for item in evidence_sequences]
                    if isinstance(evidence_sequences, list)
                    and len(evidence_sequences) == 2
                    and all(isinstance(item, int) for item in evidence_sequences)
                    else []
                )
                common_proof_invalid = bool(
                    len(valid_proofs) != 2
                    or any(item is None for item in valid_proofs)
                    or valid_proofs[0].get("window_id") != window_id
                    or valid_proofs[1].get("window_id") != window_id
                    or valid_proofs[0].get("cycle") != cycle
                    or valid_proofs[1].get("cycle") != cycle
                    or valid_proofs[0].get("state") != state
                    or valid_proofs[1].get("state") != state
                    or valid_proofs[0].get("state_episode") != state_episode
                    or valid_proofs[1].get("state_episode") != state_episode
                    or valid_proofs[0].get("decision_signature")
                    != valid_proofs[1].get("decision_signature")
                    or valid_proofs[0].get("authority_signature")
                    != valid_proofs[1].get("authority_signature")
                    or valid_proofs[0].get("authority_signature")
                    != action_record.get("authority_signature")
                    or valid_proofs[0].get("identity_verified") is not True
                    or valid_proofs[1].get("identity_verified") is not True
                    or valid_proofs[0].get("sequence")
                    == valid_proofs[1].get("sequence")
                )
                if action == "reopen_window":
                    proof_invalid = bool(
                        common_proof_invalid
                        or any(
                            item.get("record_type") != "absence_evidence"
                            or item.get("target_absent") is not True
                            or item.get("shortcut_identity_verified") is not True
                            for item in valid_proofs
                        )
                    )
                else:
                    proof_observations = (
                        [
                            by_sequence.get(item.get("observation_sequence"))
                            if isinstance(item, Mapping)
                            else None
                            for item in valid_proofs
                        ]
                        if len(valid_proofs) == 2
                        else []
                    )
                    proof_invalid = bool(
                        common_proof_invalid
                        or any(
                            item.get("record_type") != "decision_evidence"
                            for item in valid_proofs
                        )
                        or valid_proofs[0].get("observation_sequence")
                        == valid_proofs[1].get("observation_sequence")
                        or len(proof_observations) != 2
                        or any(item is None for item in proof_observations)
                        or any(
                            item.get("record_type") != "observation"
                            or item.get("fresh") is not True
                            or item.get("identity_verified") is not True
                            for item in proof_observations
                        )
                    )
                if proof_invalid:
                    findings.append(
                        ReplayFinding("two_frame_evidence_missing", sequence, window_id)
                    )
                future_observation = next(
                    (
                        item
                        for item in observations.get(key, [])
                        if isinstance(item.get("sequence"), int)
                        and int(item["sequence"]) > sequence
                        and item.get("fresh") is True
                        and item.get("identity_verified") is True
                    ),
                    None,
                )
                if future_observation is None:
                    pending_actions += 1
            sticky_result(
                line_verified,
                key,
                action_record.get("original_line_verified"),
            )
            sticky_result(
                role_verified,
                key,
                action_record.get("original_role_verified"),
            )
            sticky_result(
                auto_verified,
                key,
                action_record.get("auto_battle_panel_verified"),
            )

        for intent_sequence, intent in intents.items():
            if intent_sequence not in referenced_intents:
                findings.append(
                    ReplayFinding(
                        "action_intent_without_result",
                        intent_sequence,
                        str(intent.get("window_id", "")) or None,
                    )
                )

        for key, items in observations.items():
            ordered_items = sorted(items, key=lambda item: int(item["sequence"]))
            for index, item in enumerate(ordered_items):
                if item.get("state") != ReconnectScreenState.UNKNOWN.value:
                    continue
                elapsed = item.get("elapsed_ms")
                future_items = [
                    candidate
                    for candidate in ordered_items[index + 1 :]
                    if isinstance(candidate.get("elapsed_ms"), int)
                    and isinstance(elapsed, int)
                    and int(candidate["elapsed_ms"]) - int(elapsed)
                    <= SMART_RECONNECT_DEADLINE_MS
                ]
                if not future_items:
                    findings.append(
                        ReplayFinding(
                            "unknown_not_retried",
                            int(item["sequence"]),
                            key[0],
                        )
                    )
                    continue
                fallback_seen = bool(
                    item.get("fallback_capture_used") is True
                    or item.get("fallback_recognition_used") is True
                    or any(
                        candidate.get("fallback_capture_used") is True
                        or candidate.get("fallback_recognition_used") is True
                        or candidate.get("capture_method")
                        != item.get("capture_method")
                        or candidate.get("recognition_method")
                        != item.get("recognition_method")
                        for candidate in future_items
                    )
                )
                if not fallback_seen:
                    findings.append(
                        ReplayFinding(
                            "unknown_fallback_missing",
                            int(item["sequence"]),
                            key[0],
                        )
                    )

        required_states = (
            ReconnectScreenState.DISCONNECTED.value,
            ReconnectScreenState.LOGIN_START.value,
            ReconnectScreenState.FORCE_LOGIN_START.value,
            ReconnectScreenState.LINE_SELECTION.value,
            ReconnectScreenState.CHARACTER_SELECTION.value,
            ReconnectScreenState.CONNECTED.value,
        )
        actions_by_cycle: dict[tuple[str, int], list[dict[str, object]]] = {}
        for action_record in actions:
            if action_record.get("performed") is not True:
                continue
            action_key = (
                str(action_record.get("window_id", "")),
                int(action_record.get("cycle", -1)),
            )
            actions_by_cycle.setdefault(action_key, []).append(action_record)

        recovered_cycles = 0
        cycle_results: list[ReplayCycleResult] = []
        for key, items in observations.items():
            if key[1] <= 0:
                continue
            ordered_items = sorted(items, key=lambda item: int(item["sequence"]))
            states = [str(item.get("state", "")) for item in ordered_items]
            if ReconnectScreenState.DISCONNECTED.value not in states:
                continue
            first_disconnect = states.index(ReconnectScreenState.DISCONNECTED.value)
            disconnect_context = str(
                ordered_items[first_disconnect].get("scene_context", "unknown")
            )
            disconnect_observation_elapsed = int(
                ordered_items[first_disconnect].get("elapsed_ms", 0)
            )
            disconnect_scan_interval = ordered_items[first_disconnect].get(
                "scan_interval_ms"
            )
            started_elapsed = max(
                0,
                disconnect_observation_elapsed
                - (
                    int(disconnect_scan_interval)
                    if isinstance(disconnect_scan_interval, int)
                    and not isinstance(disconnect_scan_interval, bool)
                    and disconnect_scan_interval >= 0
                    else 0
                ),
            )
            stage_times: list[tuple[str, int]] = []
            stage_positions: list[int] = []
            next_stage_position = first_disconnect
            for required_state in required_states:
                position = next(
                    (
                        index
                        for index in range(next_stage_position, len(ordered_items))
                        if ordered_items[index].get("state") == required_state
                    ),
                    -1,
                )
                if position < 0:
                    findings.append(
                        ReplayFinding("flow_stage_missing", None, key[0])
                    )
                    break
                stage_positions.append(position)
                next_stage_position = position + 1
                stage_times.append(
                    (
                        required_state,
                        int(ordered_items[position].get("elapsed_ms", 0))
                        - started_elapsed,
                    )
                )
            stages_valid = bool(
                len(stage_positions) == len(required_states)
                and disconnect_context in {"general", "battle"}
            )
            if disconnect_context not in {"general", "battle"}:
                findings.append(
                    ReplayFinding("disconnect_scene_unproven", None, key[0])
                )
            if len(stage_positions) == len(required_states) and not stages_valid:
                findings.append(
                    ReplayFinding("flow_stage_order_invalid", None, key[0])
                )

            cycle_actions = sorted(
                actions_by_cycle.get(key, []),
                key=lambda item: int(item["sequence"]),
            )
            disconnect_action = (
                ReconnectAction.CONFIRM_DISCONNECT.value
                if disconnect_context == "general"
                else "restart_window"
            )
            required_action_for_state: dict[str, str] = {
                ReconnectScreenState.DISCONNECTED.value: disconnect_action,
                ReconnectScreenState.LOGIN_START.value: (
                    ReconnectAction.START_GAME.value
                ),
                ReconnectScreenState.FORCE_LOGIN_START.value: (
                    ReconnectAction.FORCE_LOGIN.value
                ),
                ReconnectScreenState.FORCE_LOGIN_TIMEOUT.value: (
                    ReconnectAction.CONFIRM_FORCE_LOGIN_TIMEOUT.value
                ),
                ReconnectScreenState.LINE_SELECTION.value: (
                    ReconnectAction.SELECT_DEFAULT_LINE.value
                ),
                ReconnectScreenState.CHARACTER_SELECTION.value: (
                    ReconnectAction.ENTER_GAME.value
                ),
                **{
                    state: ReconnectAction.CLOSE_ANNOUNCEMENT.value
                    for state in _POST_LOGIN_STATES
                },
            }
            required_action_pairs: list[tuple[int, str, str, int]] = []
            required_base_states = set(required_states[:-1])
            seen_action_events: set[tuple[str, int]] = set()
            for position, observation in enumerate(ordered_items):
                state = str(observation.get("state", ""))
                state_episode = int(observation.get("state_episode", -1))
                event_key = (state, state_episode)
                if (
                    state in required_base_states
                    or state
                    in {
                        ReconnectScreenState.FORCE_LOGIN_TIMEOUT.value,
                        *_POST_LOGIN_STATES,
                    }
                ) and event_key not in seen_action_events:
                    action_name = required_action_for_state.get(state)
                    if action_name is not None:
                        required_action_pairs.append(
                            (
                                position,
                                state,
                                action_name,
                                state_episode,
                            )
                        )
                    seen_action_events.add(event_key)
            required_action_pairs.sort(key=lambda item: item[0])
            action_positions: list[int] = []
            next_action_position = 0
            actions_follow_evidence = True
            for (
                stage_position,
                required_state,
                required_action,
                required_episode,
            ) in required_action_pairs:
                position = next(
                    (
                        index
                        for index in range(next_action_position, len(cycle_actions))
                        if cycle_actions[index].get("action") == required_action
                        and cycle_actions[index].get("state") == required_state
                        and cycle_actions[index].get("state_episode")
                        == required_episode
                    ),
                    -1,
                )
                if position < 0:
                    findings.append(
                        ReplayFinding("flow_action_missing", None, key[0])
                    )
                    break
                action_positions.append(position)
                next_action_position = position + 1
                if int(cycle_actions[position].get("sequence", 0)) <= int(
                    ordered_items[stage_position].get("sequence", 0)
                ):
                    actions_follow_evidence = False
                    findings.append(
                        ReplayFinding("action_before_stage_evidence", None, key[0])
                    )
            actions_valid = bool(
                len(action_positions) == len(required_action_pairs)
                and disconnect_context in {"general", "battle"}
                and actions_follow_evidence
            )
            if (
                len(action_positions) == len(required_action_pairs)
                and not actions_valid
            ):
                findings.append(
                    ReplayFinding("flow_action_order_invalid", None, key[0])
                )

            wrong_disconnect_action = (
                "restart_window"
                if disconnect_context == "general"
                else ReconnectAction.CONFIRM_DISCONNECT.value
            )
            if any(
                item.get("state") == ReconnectScreenState.DISCONNECTED.value
                and item.get("action") == wrong_disconnect_action
                for item in cycle_actions
            ):
                actions_valid = False
                findings.append(
                    ReplayFinding("disconnect_action_mismatch", None, key[0])
                )

            last_action_sequence = max(
                (int(item["sequence"]) for item in cycle_actions),
                default=0,
            )
            connected_candidates = [
                item
                for item in ordered_items[first_disconnect + 1 :]
                if item.get("state") == ReconnectScreenState.CONNECTED.value
                and int(item.get("sequence", 0)) > last_action_sequence
            ]
            valid_connected = [
                item
                for item in connected_candidates
                if item.get("fresh") is True
                and item.get("identity_verified") is True
                and item.get("visual_content_present") is True
                and item.get("recognition_basis") == "cross_map_fixed_ui"
                and isinstance(item.get("reference_code"), str)
            ]
            confirmed_connected_pair: tuple[
                dict[str, object], dict[str, object]
            ] | None = None
            for index, first in enumerate(valid_connected):
                second = next(
                    (
                        item
                        for item in valid_connected[index + 1 :]
                        if item.get("reference_code")
                        == first.get("reference_code")
                        and item.get("recognition_basis")
                        == first.get("recognition_basis")
                        and int(item.get("sequence", 0))
                        > int(first.get("sequence", 0))
                    ),
                    None,
                )
                if second is not None:
                    confirmed_connected_pair = (first, second)
                    break
            finished_elapsed: int | None = None
            total_elapsed: int | None = None
            stage_timing_results: list[ReplayStageTiming] = []
            program_overhead_upper_bound: int | None = None
            transition_wait: int | None = None
            sixty_second_target_met: bool | None = None
            stage_response_target_met = False
            program_overhead_target_met = False
            if confirmed_connected_pair is not None:
                finished_elapsed = int(
                    confirmed_connected_pair[1].get("elapsed_ms", 0)
                )
                total_elapsed = max(0, finished_elapsed - started_elapsed)
                stage_times = []
                seen_stage_events: set[tuple[str, int]] = set()
                for observation in ordered_items[first_disconnect:]:
                    if int(observation.get("sequence", 0)) >= int(
                        confirmed_connected_pair[1].get("sequence", 0)
                    ):
                        break
                    state = str(observation.get("state", ""))
                    event_key = (
                        state,
                        int(observation.get("state_episode", -1)),
                    )
                    if (
                        state
                        in {
                            *required_states[:-1],
                            ReconnectScreenState.FORCE_LOGIN_TIMEOUT.value,
                            *_POST_LOGIN_STATES,
                        }
                        and event_key not in seen_stage_events
                    ):
                        stage_times.append(
                            (
                                state,
                                int(observation.get("elapsed_ms", 0))
                                - started_elapsed,
                            )
                        )
                        seen_stage_events.add(event_key)
                stage_times.append(
                    (
                        "connected_confirmed",
                        finished_elapsed - started_elapsed,
                    )
                )

                for action_record in cycle_actions:
                    action_sequence = int(action_record.get("sequence", 0))
                    action_state = str(action_record.get("state", ""))
                    action_episode = int(
                        action_record.get("state_episode", -1)
                    )
                    first_stage_observation = next(
                        (
                            observation
                            for observation in ordered_items[first_disconnect:]
                            if observation.get("state") == action_state
                            and int(observation.get("state_episode", -1))
                            == action_episode
                            and int(observation.get("sequence", 0))
                            < action_sequence
                        ),
                        None,
                    )
                    if first_stage_observation is None:
                        continue
                    action_elapsed = int(
                        action_record.get("elapsed_ms", 0)
                    )
                    stage_started = int(
                        first_stage_observation.get("elapsed_ms", 0)
                    )
                    scan_duration = first_stage_observation.get(
                        "scan_duration_ms"
                    )
                    normalized_scan_duration = (
                        int(scan_duration)
                        if isinstance(scan_duration, int)
                        and not isinstance(scan_duration, bool)
                        and scan_duration >= 0
                        else None
                    )
                    scan_interval = first_stage_observation.get(
                        "scan_interval_ms"
                    )
                    recognition_upper_bound = (
                        int(scan_interval)
                        if isinstance(scan_interval, int)
                        and not isinstance(scan_interval, bool)
                        and scan_interval >= 0
                        else None
                    )
                    post_recognition_action = max(
                        0,
                        action_elapsed - stage_started,
                    )
                    program_response_upper_bound = (
                        recognition_upper_bound + post_recognition_action
                        if recognition_upper_bound is not None
                        else None
                    )
                    next_stage_observation = next(
                        (
                            observation
                            for observation in ordered_items
                            if int(observation.get("sequence", 0))
                            > action_sequence
                            and observation.get("state")
                            not in {
                                action_state,
                                ReconnectScreenState.UNKNOWN.value,
                            }
                        ),
                        None,
                    )
                    next_stage_elapsed = (
                        int(next_stage_observation.get("elapsed_ms", 0))
                        if next_stage_observation is not None
                        else None
                    )
                    stage_timing_results.append(
                        ReplayStageTiming(
                            stage=action_state,
                            state_episode=action_episode,
                            started_elapsed_ms=stage_started,
                            finished_elapsed_ms=action_elapsed,
                            scan_duration_ms=normalized_scan_duration,
                            recognition_upper_bound_ms=(
                                recognition_upper_bound
                            ),
                            post_recognition_action_ms=(
                                post_recognition_action
                            ),
                            program_response_upper_bound_ms=(
                                program_response_upper_bound
                            ),
                            next_stage=(
                                str(next_stage_observation.get("state"))
                                if next_stage_observation is not None
                                else None
                            ),
                            next_stage_started_elapsed_ms=(
                                next_stage_elapsed
                            ),
                            transition_wait_ms=(
                                max(0, next_stage_elapsed - action_elapsed)
                                if next_stage_elapsed is not None
                                else None
                            ),
                        )
                    )

                first_connected, second_connected = confirmed_connected_pair
                connected_started = int(
                    first_connected.get("elapsed_ms", 0)
                )
                connected_finished = int(
                    second_connected.get("elapsed_ms", 0)
                )
                connected_scan_duration = first_connected.get(
                    "scan_duration_ms"
                )
                normalized_connected_scan_duration = (
                    int(connected_scan_duration)
                    if isinstance(connected_scan_duration, int)
                    and not isinstance(connected_scan_duration, bool)
                    and connected_scan_duration >= 0
                    else None
                )
                connected_scan_interval = first_connected.get(
                    "scan_interval_ms"
                )
                connected_recognition_upper_bound = (
                    int(connected_scan_interval)
                    if isinstance(connected_scan_interval, int)
                    and not isinstance(connected_scan_interval, bool)
                    and connected_scan_interval >= 0
                    else None
                )
                connected_confirmation_ms = max(
                    0,
                    connected_finished - connected_started,
                )
                connected_program_upper_bound = (
                    connected_recognition_upper_bound
                    + connected_confirmation_ms
                    if connected_recognition_upper_bound is not None
                    else None
                )
                stage_timing_results.append(
                    ReplayStageTiming(
                        stage="connected_confirmation",
                        state_episode=int(
                            second_connected.get("state_episode", -1)
                        ),
                        started_elapsed_ms=connected_started,
                        finished_elapsed_ms=connected_finished,
                        scan_duration_ms=(
                            normalized_connected_scan_duration
                        ),
                        recognition_upper_bound_ms=(
                            connected_recognition_upper_bound
                        ),
                        post_recognition_action_ms=(
                            connected_confirmation_ms
                        ),
                        program_response_upper_bound_ms=(
                            connected_program_upper_bound
                        ),
                        next_stage=None,
                        next_stage_started_elapsed_ms=None,
                        transition_wait_ms=None,
                    )
                )
                timing_evidence_complete = all(
                    item.program_response_upper_bound_ms is not None
                    for item in stage_timing_results
                )
                program_overhead_upper_bound = (
                    sum(
                        int(item.program_response_upper_bound_ms)
                        for item in stage_timing_results
                        if item.program_response_upper_bound_ms is not None
                    )
                    if timing_evidence_complete
                    else None
                )
                transition_wait = (
                    max(
                        0,
                        total_elapsed - program_overhead_upper_bound,
                    )
                    if program_overhead_upper_bound is not None
                    else None
                )
                sixty_second_target_met = bool(
                    total_elapsed <= SMART_RECONNECT_DEADLINE_MS
                )
                stage_response_target_met = bool(
                    timing_evidence_complete
                    and all(
                        int(item.program_response_upper_bound_ms)
                        <= SMART_RECONNECT_STAGE_RESPONSE_TARGET_MS
                        for item in stage_timing_results
                        if item.program_response_upper_bound_ms is not None
                    )
                )
                program_overhead_target_met = bool(
                    program_overhead_upper_bound is not None
                    and program_overhead_upper_bound
                    <= SMART_RECONNECT_PROGRAM_OVERHEAD_TARGET_MS
                )
                if not timing_evidence_complete:
                    findings.append(
                        ReplayFinding(
                            "program_timing_evidence_missing",
                            None,
                            key[0],
                        )
                    )
                if not stage_response_target_met:
                    findings.append(
                        ReplayFinding(
                            "stage_response_target_exceeded",
                            None,
                            key[0],
                        )
                    )
                if not program_overhead_target_met:
                    findings.append(
                        ReplayFinding(
                            "program_overhead_target_exceeded",
                            None,
                            key[0],
                        )
                    )
            elif connected_candidates:
                findings.append(
                    ReplayFinding("connected_confirmation_invalid", None, key[0])
                )
            else:
                findings.append(
                    ReplayFinding("reconnect_cycle_incomplete", None, key[0])
                )

            obstacle_names = tuple(
                sorted(
                    {
                        str(event.get("obstacle"))
                        for event in external_events.get(key, [])
                        if event.get("obstacle") in _EXTERNAL_OBSTACLES
                        and event.get("active") is True
                    }
                )
            )
            if obstacle_names:
                findings.append(
                    ReplayFinding("external_obstacle_cycle_excluded", None, key[0])
                )
            line_ok = line_verified.get(key, False)
            role_ok = role_verified.get(key, False)
            auto_ok = bool(
                header.get("auto_battle_required") is not True
                or auto_verified.get(key, False)
            )
            unverified_controls = unverified_window_control_sequences.get(key, [])
            restoration_sequences = restoration_true_sequences.get(key, [])
            controls_verified_later = all(
                any(proof_sequence > action_sequence for proof_sequence in restoration_sequences)
                for action_sequence in unverified_controls
            )
            restored_ok = bool(
                restoration_verified.get(key, False)
                and controls_verified_later
            )
            if not line_ok:
                findings.append(
                    ReplayFinding("original_line_not_verified", None, key[0])
                )
            if not role_ok:
                findings.append(
                    ReplayFinding("original_role_not_verified", None, key[0])
                )
            if not auto_ok:
                findings.append(
                    ReplayFinding("auto_battle_panel_not_verified", None, key[0])
                )
            if not restored_ok:
                findings.append(
                    ReplayFinding("window_state_not_restored", None, key[0])
                )
            fallback_capture = any(
                item.get("fallback_capture_used") is True for item in ordered_items
            ) or any(
                item.get("fallback_capture_used") is True for item in cycle_actions
            )
            fallback_recognition = any(
                item.get("fallback_recognition_used") is True
                for item in ordered_items
            ) or any(
                item.get("fallback_recognition_used") is True
                for item in cycle_actions
            )
            accepted = bool(
                stages_valid
                and actions_valid
                and confirmed_connected_pair is not None
                and stage_response_target_met
                and program_overhead_target_met
                and not obstacle_names
                and line_ok
                and role_ok
                and auto_ok
                and restored_ok
            )
            if accepted:
                recovered_cycles += 1
            cycle_results.append(
                ReplayCycleResult(
                    window_id=key[0],
                    cycle=key[1],
                    started_elapsed_ms=started_elapsed,
                    finished_elapsed_ms=finished_elapsed,
                    total_elapsed_ms=total_elapsed,
                    stage_times_ms=tuple(stage_times),
                    stage_timings=tuple(stage_timing_results),
                    program_overhead_upper_bound_ms=(
                        program_overhead_upper_bound
                    ),
                    transition_wait_ms=transition_wait,
                    sixty_second_target_met=sixty_second_target_met,
                    stage_response_target_met=stage_response_target_met,
                    program_overhead_target_met=(
                        program_overhead_target_met
                    ),
                    fallback_capture_used=fallback_capture,
                    fallback_recognition_used=fallback_recognition,
                    window_state_restored=restored_ok,
                    original_line_verified=line_ok,
                    original_role_verified=role_ok,
                    auto_battle_panel_verified=auto_ok,
                    external_obstacles=obstacle_names,
                    accepted=accepted,
                )
            )

        accepted_cycle_keys = {
            (item.window_id, item.cycle)
            for item in cycle_results
            if item.accepted
        }
        recovered_cycles_by_window: dict[str, int] = {}
        for result in cycle_results:
            if result.accepted:
                recovered_cycles_by_window[result.window_id] = (
                    recovered_cycles_by_window.get(result.window_id, 0) + 1
                )

        scenario_tags: set[str] = set()
        accepted_observations = [
            item
            for key, cycle_items in observations.items()
            if key in accepted_cycle_keys
            for item in cycle_items
        ]
        scenario_tags.update(
            str(item.get("presentation_state"))
            for item in accepted_observations
            if item.get("presentation_state") in _PRESENTATION_STATES
            and item.get("presentation_state") != "unknown"
        )
        if any(
            item.get("other_window_foreground") is True
            for item in accepted_observations
        ):
            scenario_tags.add("other_window_foreground")
        if any(
            item.get("overlapped_by_game_window") is True
            for item in accepted_observations
        ):
            scenario_tags.add("overlapped")
        accepted_states = [
            str(item.get("state", "")) for item in accepted_observations
        ]
        if ReconnectScreenState.LINE_SELECTION.value in accepted_states:
            scenario_tags.add("line_selection")
        if ReconnectScreenState.CHARACTER_SELECTION.value in accepted_states:
            scenario_tags.add("character_selection")
        if any(state in _POST_LOGIN_STATES for state in accepted_states):
            scenario_tags.add("post_login_popup")

        unsafe_unknown_episodes = {
            (
                str(item.get("window_id", "")),
                int(item.get("cycle", -1)),
                int(item.get("state_episode", -1)),
            )
            for item in actions
            if item.get("state") == ReconnectScreenState.UNKNOWN.value
            and item.get("performed") is True
        }
        unknown_safely_retried = False
        for key, cycle_items in observations.items():
            ordered_cycle_items = sorted(
                cycle_items,
                key=lambda item: int(item["sequence"]),
            )
            for index, item in enumerate(ordered_cycle_items):
                if item.get("state") != ReconnectScreenState.UNKNOWN.value:
                    continue
                episode_key = (
                    key[0],
                    key[1],
                    int(item.get("state_episode", -1)),
                )
                if episode_key in unsafe_unknown_episodes:
                    continue
                elapsed = item.get("elapsed_ms")
                future_items = [
                    candidate
                    for candidate in ordered_cycle_items[index + 1 :]
                    if isinstance(elapsed, int)
                    and isinstance(candidate.get("elapsed_ms"), int)
                    and int(candidate["elapsed_ms"]) - int(elapsed)
                    <= SMART_RECONNECT_DEADLINE_MS
                ]
                if not future_items:
                    continue
                if (
                    item.get("fallback_capture_used") is True
                    or item.get("fallback_recognition_used") is True
                    or any(
                        candidate.get("fallback_capture_used") is True
                        or candidate.get("fallback_recognition_used") is True
                        or candidate.get("capture_method")
                        != item.get("capture_method")
                        or candidate.get("recognition_method")
                        != item.get("recognition_method")
                        for candidate in future_items
                    )
                ):
                    unknown_safely_retried = True
                    break
            if unknown_safely_retried:
                break
        if unknown_safely_retried:
            scenario_tags.add("unknown_safe")
        for key in accepted_cycle_keys:
            cycle_items = observations.get(key, [])
            ordered_cycle_items = sorted(
                cycle_items,
                key=lambda item: int(item["sequence"]),
            )
            disconnected_item = next(
                (
                    item
                    for item in ordered_cycle_items
                    if item.get("state") == ReconnectScreenState.DISCONNECTED.value
                ),
                None,
            )
            if disconnected_item is not None:
                if disconnected_item.get("scene_context") == "general":
                    scenario_tags.add("general_disconnect")
                elif disconnected_item.get("scene_context") == "battle":
                    scenario_tags.add("battle_disconnect")
            if any(
                item.get("failure_reason") == "login_stalled"
                for item in ordered_cycle_items
            ):
                scenario_tags.add("login_stall")
        if any(
            item.accepted
            and item.original_line_verified
            and item.original_role_verified
            for item in cycle_results
        ):
            scenario_tags.add("original_role_and_line_restored")
        if any(
            item.accepted and item.auto_battle_panel_verified
            for item in cycle_results
        ):
            scenario_tags.add("auto_battle_restored")

        intervals = [
            (
                item.window_id,
                item.cycle,
                item.started_elapsed_ms,
                item.finished_elapsed_ms
                if item.finished_elapsed_ms is not None
                else last_event_elapsed_ms,
            )
            for item in cycle_results
            if item.accepted
        ]
        overlapping: set[tuple[str, int]] = set()
        for index, left in enumerate(intervals):
            for right in intervals[index + 1 :]:
                if left[0] == right[0]:
                    continue
                if max(left[2], right[2]) <= min(left[3], right[3]):
                    overlapping.add((left[0], left[1]))
                    overlapping.add((right[0], right[1]))
        if overlapping:
            scenario_tags.add("multi_window_disconnect")
        if any((item[0], item[1]) not in overlapping for item in intervals):
            scenario_tags.add("single_window_disconnect")

        return SmartReconnectReplayReport(
            source_commit=normalized_commit,
            event_count=max(0, len(records) - 1),
            window_count=len(windows),
            recovered_cycles=recovered_cycles,
            pending_actions=pending_actions,
            session_duration_ms=session_duration_ms,
            cycles=tuple(cycle_results),
            recovered_cycles_by_window=tuple(
                sorted(recovered_cycles_by_window.items())
            ),
            scenario_tags=tuple(sorted(scenario_tags)),
            findings=tuple(findings),
        )


def replay_many(paths: Iterable[Path]) -> tuple[SmartReconnectReplayReport, ...]:
    validator = SmartReconnectReplayValidator()
    return tuple(validator.validate(path) for path in paths)


def validate_formal_acceptance(paths: Iterable[Path]) -> FormalAcceptanceReport:
    reports = replay_many(paths)
    findings: list[ReplayFinding] = [
        finding for report in reports for finding in report.findings
    ]
    commits = {report.source_commit for report in reports}
    source_commit = (
        next(iter(commits))
        if len(commits) == 1 and None not in commits
        else None
    )
    if len(commits) != 1 or None in commits:
        findings.append(ReplayFinding("formal_source_commit_mismatch"))
    recovered_by_session = [
        dict(report.recovered_cycles_by_window) for report in reports
    ]
    distinct_windows = max(
        (len(counts) for counts in recovered_by_session),
        default=0,
    )
    if distinct_windows < FORMAL_ACCEPTANCE_MIN_WINDOWS:
        findings.append(ReplayFinding("formal_window_count_insufficient"))
    windows_with_minimum_cycles = max(
        (
            sum(
                count >= FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW
                for count in counts.values()
            )
            for counts in recovered_by_session
        ),
        default=0,
    )
    if windows_with_minimum_cycles < FORMAL_ACCEPTANCE_MIN_WINDOWS:
        findings.append(ReplayFinding("formal_window_cycle_count_insufficient"))
    recovered_cycles = sum(report.recovered_cycles for report in reports)
    if recovered_cycles < FORMAL_ACCEPTANCE_MIN_CYCLES:
        findings.append(ReplayFinding("formal_cycle_count_insufficient"))
    longest_duration = max(
        (report.session_duration_ms for report in reports),
        default=0,
    )
    if longest_duration < FORMAL_ACCEPTANCE_MIN_DURATION_MS:
        findings.append(ReplayFinding("formal_duration_insufficient"))
    same_session_qualified = any(
        report.session_duration_ms >= FORMAL_ACCEPTANCE_MIN_DURATION_MS
        and sum(
            count >= FORMAL_ACCEPTANCE_MIN_CYCLES_PER_WINDOW
            for _window_id, count in report.recovered_cycles_by_window
        )
        >= FORMAL_ACCEPTANCE_MIN_WINDOWS
        for report in reports
    )
    if not same_session_qualified:
        findings.append(ReplayFinding("formal_same_session_coverage_missing"))
    scenario_tags = {
        tag for report in reports for tag in report.scenario_tags
    }
    for missing in sorted(_FORMAL_SCENARIOS - scenario_tags):
        findings.append(ReplayFinding(f"formal_scenario_missing:{missing}"))
    if any(report.pending_actions for report in reports):
        findings.append(ReplayFinding("formal_pending_action"))
    return FormalAcceptanceReport(
        source_commit=source_commit,
        session_count=len(reports),
        distinct_windows_in_one_session=distinct_windows,
        recovered_cycles=recovered_cycles,
        longest_session_duration_ms=longest_duration,
        findings=tuple(findings),
    )
