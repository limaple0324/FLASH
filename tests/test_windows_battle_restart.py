import base64
import json
import os
import time
from dataclasses import replace

import pytest

from adapters.windows_battle_restart import (
    BattleReopenStage,
    BattleRestartResult,
    WindowsBattleWindowRestarter,
    WindowsShortcutOpenBackend,
    _POWERSHELL_REOPEN_SCRIPT,
    _PowerShellBattleReopenWorker,
)
from adapters.windows_window import WindowInfo
from services.group_launch_service import GroupLaunchTarget


def _window(
    handle=11,
    process_id=101,
    fingerprint="a" * 64,
    *,
    thread_id=501,
    process_lifecycle_token=1001,
    window_class="ShockwaveFlash",
    rect=(0, 0, 900, 600),
    minimized=False,
):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=minimized,
        rect=rect,
        process_id=process_id,
        window_class=window_class,
        launch_fingerprint=fingerprint,
        thread_id=thread_id,
        process_lifecycle_token=process_lifecycle_token,
    )


class _Windows:
    def __init__(self, windows, *, on_list=None):
        self.windows = list(windows)
        self.calls = 0
        self.on_list = on_list

    def list_windows(self):
        self.calls += 1
        if self.on_list is not None:
            self.on_list(self.calls)
        return list(self.windows)


class _FailingWindows:
    def list_windows(self):
        raise RuntimeError("enumeration failed")


class _Closer:
    def __init__(self, *, remains=0, before_close_boundary=None):
        self.remains = remains
        self.before_close_boundary = before_close_boundary
        self.closed = []

    def is_window(self, _handle):
        if not self.closed:
            return True
        if self.remains > 0:
            self.remains -= 1
            return True
        return False

    def close_window_if_instance_matches(
        self,
        handle,
        expected_identity,
        current_identity,
    ):
        if self.before_close_boundary is not None:
            self.before_close_boundary()
        if current_identity(handle) != expected_identity:
            return False, "battle_window_identity_changed"
        self.closed.append(handle)
        return True, None


class _Opener:
    def __init__(self, *, succeeds=True, before_open_boundary=None):
        self.succeeds = succeeds
        self.before_open_boundary = before_open_boundary
        self.targets = []

    def open_shortcut(self, target):
        self.targets.append(target)
        return self.succeeds

    def open_shortcut_if_target_absent(self, target, absence_check):
        if self.before_open_boundary is not None:
            self.before_open_boundary()
        failure_code = absence_check()
        if failure_code is not None:
            return False, failure_code
        if not self.open_shortcut(target):
            return False, "battle_shortcut_open_failed"
        return True, None


def _target(tmp_path, fingerprint="a" * 64):
    return GroupLaunchTarget(
        order=1,
        display_name="120古",
        shortcut_path=tmp_path / "120古.lnk",
        fingerprint=fingerprint,
        entry_id="entry-a",
        role_id="role-a",
    )


def _two_phase(restarter, windows, owner, target, *, pre, post, deadline=None):
    closed = restarter.close_verified(owner, pre, deadline=deadline)
    if not closed.success:
        return closed
    windows.windows = list(post)
    reopened = restarter.reopen_missing(target, post, deadline=deadline)
    return BattleRestartResult(
        reopened.success,
        reopened.failure_code,
        window_closed=True,
        shortcut_open_requested=reopened.shortcut_open_requested,
    )


def test_two_phase_closes_exact_owner_then_opens_after_static_post_contract(
    tmp_path,
):
    owner = _window()
    sibling = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )
    windows = _Windows([owner, sibling])
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(windows, closer, opener)

    result = _two_phase(
        restarter,
        windows,
        owner,
        _target(tmp_path),
        pre=(owner, sibling),
        post=(sibling,),
    )

    assert result.success is True
    assert closer.closed == [owner.handle]
    assert opener.targets == [_target(tmp_path)]


def test_two_phase_allows_precisely_mapped_sibling_candidates(tmp_path):
    owner = _window()
    mapped_duplicate_one = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )
    mapped_duplicate_two = _window(
        handle=13,
        process_id=103,
        fingerprint="b" * 64,
        thread_id=503,
        process_lifecycle_token=1003,
    )
    windows = _Windows([owner, mapped_duplicate_one, mapped_duplicate_two])
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(windows, closer, opener)

    result = _two_phase(
        restarter,
        windows,
        owner,
        _target(tmp_path),
        pre=(owner, mapped_duplicate_one, mapped_duplicate_two),
        post=(mapped_duplicate_one, mapped_duplicate_two),
    )

    assert result.success is True
    assert closer.closed == [owner.handle]
    assert opener.targets == [_target(tmp_path)]


@pytest.mark.parametrize(
    "sibling",
    (
        _window(
            handle=12,
            process_id=102,
            fingerprint="b" * 64,
            thread_id=0,
            process_lifecycle_token=1002,
        ),
        _window(
            handle=11,
            process_id=102,
            fingerprint="b" * 64,
            thread_id=502,
            process_lifecycle_token=1002,
        ),
        _window(
            handle=12,
            process_id=101,
            fingerprint="b" * 64,
            thread_id=502,
            process_lifecycle_token=1002,
        ),
    ),
)
def test_close_refuses_incomplete_or_identity_colliding_static_candidate(
    tmp_path,
    sibling,
):
    owner = _window()
    windows = _Windows([owner, sibling])
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(windows, closer, opener)

    result = restarter.close_verified(owner, (owner, sibling))

    assert result.success is False
    assert closer.closed == []
    assert opener.targets == []


def test_close_refuses_unknown_or_changed_live_contract_without_closing(tmp_path):
    owner = _window()
    unexpected = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )
    windows = _Windows([owner, unexpected])
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(windows, closer, opener)

    result = restarter.close_verified(owner, (owner,))

    assert result.success is False
    assert result.failure_code == "battle_contract_identity_changed"
    assert closer.closed == []
    assert opener.targets == []


def test_close_rechecks_reused_handle_at_native_delivery_boundary(tmp_path):
    owner = _window()
    replacement = _window(
        process_id=999,
        thread_id=999,
        process_lifecycle_token=9999,
        window_class="ReplacementFlash",
    )
    windows = _Windows([owner])
    closer = _Closer(
        before_close_boundary=lambda: setattr(windows, "windows", [replacement])
    )
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(windows, closer, opener)

    result = restarter.close_verified(owner, (owner,))

    assert result.success is False
    assert result.failure_code == "battle_window_identity_changed"
    assert closer.closed == []
    assert opener.targets == []


def test_reopen_refuses_unexpected_or_incomplete_live_candidate(tmp_path):
    target = _target(tmp_path)
    unknown = _window(fingerprint=None)
    windows = _Windows([unknown])
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(windows, _Closer(), opener)

    result = restarter.reopen_missing(target, ())

    assert result.success is False
    assert result.failure_code == "battle_window_existing_state_unknown"
    assert opener.targets == []


def test_reopen_refuses_owner_self_reopen_before_shortcut_delivery(tmp_path):
    now = [0.0]
    owner = _window()
    reopened_owner = _window(
        handle=22,
        process_id=202,
        thread_id=502,
        process_lifecycle_token=2002,
    )
    windows = _Windows([])
    opener = _Opener(
        before_open_boundary=lambda: setattr(windows, "windows", [reopened_owner])
    )
    restarter = WindowsBattleWindowRestarter(
        windows,
        _Closer(),
        opener,
        absence_stability_seconds=0.1,
        poll_seconds=0.05,
        monotonic_clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    result = restarter.reopen_missing(_target(tmp_path), ())

    assert result.success is False
    assert result.failure_code == "battle_contract_identity_changed"
    assert opener.targets == []


def test_close_deadline_blocks_slow_preflight_before_native_close(tmp_path):
    now = [0.0]
    owner = _window()
    windows = _Windows(
        [owner],
        on_list=lambda count: now.__setitem__(0, 2.0) if count == 1 else None,
    )
    closer = _Closer()
    restarter = WindowsBattleWindowRestarter(
        windows,
        closer,
        _Opener(),
        monotonic_clock=lambda: now[0],
    )

    result = restarter.close_verified(owner, (owner,), deadline=1.0)

    assert result.success is False
    assert result.failure_code == "tcp_reconnect_timeout"
    assert closer.closed == []


def test_reopen_deadline_blocks_slow_absence_before_shortcut_delivery(tmp_path):
    now = [0.0]
    target = _target(tmp_path)
    windows = _Windows([], on_list=lambda _count: now.__setitem__(0, 2.0))
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        windows,
        _Closer(),
        opener,
        monotonic_clock=lambda: now[0],
    )

    result = restarter.reopen_missing(target, (), deadline=1.0)

    assert result.success is False
    assert result.failure_code == "tcp_reconnect_timeout"
    assert opener.targets == []


def test_reopen_refuses_when_live_enumeration_fails(tmp_path):
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _FailingWindows(),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(_target(tmp_path), ())

    assert result.success is False
    assert result.failure_code == "battle_window_enumeration_failed"
    assert opener.targets == []


class _ShortcutResolver:
    def __init__(self, resolved):
        self.resolved = resolved
        self.calls = []

    def resolve(self, shortcut_paths):
        paths = tuple(shortcut_paths)
        self.calls.append(paths)
        return self.resolved(paths)


def test_final_shortcut_delivery_rechecks_replaced_fingerprint(
    tmp_path,
    monkeypatch,
):
    target = _target(tmp_path)
    target.shortcut_path.write_bytes(b"shortcut")
    resolver = _ShortcutResolver(
        lambda _paths: {target.shortcut_path: "b" * 64}
    )
    backend = WindowsShortcutOpenBackend(resolver)
    starts = []
    monkeypatch.setattr(
        "adapters.windows_battle_restart.os.startfile",
        lambda path: starts.append(path),
        raising=False,
    )

    opened, failure_code = backend.open_shortcut_if_target_absent(
        target,
        lambda: None,
    )

    assert opened is False
    assert failure_code == "battle_shortcut_identity_changed"
    assert starts == []


def test_final_shortcut_delivery_rechecks_absence_after_identity_resolution(
    tmp_path,
    monkeypatch,
):
    target = _target(tmp_path)
    target.shortcut_path.write_bytes(b"shortcut")
    target_present = [False]
    calls = []

    def resolve(paths):
        calls.append("resolve")
        target_present[0] = True
        return {path: target.fingerprint for path in paths}

    backend = WindowsShortcutOpenBackend(_ShortcutResolver(resolve))
    starts = []
    monkeypatch.setattr(
        "adapters.windows_battle_restart.os.startfile",
        lambda path: starts.append(path),
        raising=False,
    )

    def absence_check():
        calls.append("absence")
        return "battle_window_already_exists" if target_present[0] else None

    opened, failure_code = backend.open_shortcut_if_target_absent(
        target,
        absence_check,
    )

    assert opened is False
    assert failure_code == "battle_window_already_exists"
    assert calls == ["absence", "resolve", "absence"]
    assert starts == []


class _BoundedWorker:
    def __init__(self, stages=()):
        self._events = []
        self._running = True
        self.authorize_calls = 0
        self.terminate_calls = 0
        self.cleanup_calls = 0
        self.job_id = "test-job"
        self.emit(*stages)

    def emit(self, *stages):
        for stage in stages:
            self._events.append(
                {
                    "stage": stage,
                    "timestamp_ms": 1_000 + len(self._events),
                }
            )

    def poll_events(self):
        events = tuple(self._events)
        self._events.clear()
        return events

    def is_running(self):
        return self._running

    def authorize_launch(self):
        self.authorize_calls += 1
        return self._running and self.authorize_calls == 1

    def terminate_and_wait(self):
        self.terminate_calls += 1
        self._running = False
        return True

    def cleanup(self):
        self.cleanup_calls += 1


def _snapshot_identity_key(window, component_count):
    stable = (
        str(window.launch_fingerprint),
        str(window.handle),
        str(window.process_id),
        str(window.thread_id),
        base64.b64encode(window.window_class.encode("utf-8")).decode("ascii"),
        str(window.process_lifecycle_token),
    )
    if component_count == len(stable):
        return "|".join(stable)
    if component_count == len(stable) + 5:
        return "|".join(
            stable
            + tuple(str(value) for value in window.rect)
            + ("1" if window.minimized else "0",)
        )
    raise AssertionError("unexpected bounded identity schema")


class _SnapshotContractWorker(_BoundedWorker):
    def __init__(self, payload, current_windows):
        super().__init__()
        self.emit(BattleReopenStage.FIRST_ABSENCE_STARTED.value)
        expected = tuple(payload["expected_identity_keys"])
        if any(
            window.launch_fingerprint is None
            or not window.window_class
            or not window.thread_id
            or not window.process_lifecycle_token
            for window in current_windows
        ):
            failure_code = "battle_window_existing_state_unknown"
        else:
            component_count = len(expected[0].split("|")) if expected else 6
            actual = tuple(
                sorted(
                    _snapshot_identity_key(window, component_count)
                    for window in current_windows
                )
            )
            failure_code = (
                None
                if actual == tuple(sorted(expected))
                else "battle_contract_identity_changed"
            )
            if (
                failure_code is None
                and any(
                    window.launch_fingerprint == payload["fingerprint"]
                    for window in current_windows
                )
            ):
                failure_code = "battle_window_already_exists"
        if failure_code is not None:
            self._events.append(
                {
                    "stage": BattleReopenStage.FAILED.value,
                    "timestamp_ms": 1_000 + len(self._events),
                    "failure_code": failure_code,
                }
            )
            return
        self.emit(
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )


def _begin_bounded_with_peer_snapshot(
    tmp_path,
    *,
    original_windows,
    current_windows,
):
    now = [0.0]
    payloads = []

    def worker_factory(payload):
        payloads.append(payload)
        return _SnapshotContractWorker(payload, current_windows)

    restarter = _bounded_restarter(worker_factory, now)
    target = _target(tmp_path)
    result = restarter.begin_bounded_reopen(
        owner=target.fingerprint,
        entry_id=target.entry_id,
        original_instance=(
            target.fingerprint,
            11,
            101,
            501,
            "ShockwaveFlash",
            1001,
            (0, 0, 900, 600),
            False,
        ),
        target=target,
        candidate_windows=original_windows,
        deadline=60.0,
    )
    return result, payloads


def _bounded_restarter(worker_factory, now):
    return WindowsBattleWindowRestarter(
        _Windows([]),
        _Closer(),
        _Opener(),
        absence_stability_seconds=0.1,
        poll_seconds=0.05,
        reopen_enumeration_timeout_seconds=1.0,
        reopen_fingerprint_timeout_seconds=1.0,
        reopen_launch_timeout_seconds=1.0,
        monotonic_clock=lambda: now[0],
        wall_clock=lambda: 1_700_000_000.0 + now[0],
        sleeper=lambda _seconds: None,
        reopen_worker_factory=worker_factory,
    )


def _begin_bounded(restarter, tmp_path, *, deadline=60.0):
    target = _target(tmp_path)
    return restarter.begin_bounded_reopen(
        owner=target.fingerprint,
        entry_id=target.entry_id,
        original_instance=(
            target.fingerprint,
            11,
            101,
            501,
            "ShockwaveFlash",
            1001,
            (0, 0, 900, 600),
            False,
        ),
        target=target,
        candidate_windows=(),
        deadline=deadline,
    )


@pytest.mark.parametrize(
    ("original_peer", "current_peer"),
    (
        (
            _window(fingerprint="b" * 64),
            _window(fingerprint="b" * 64, rect=(20, 30, 920, 630)),
        ),
        (
            _window(fingerprint="b" * 64),
            _window(fingerprint="b" * 64, rect=(0, 0, 1024, 768)),
        ),
        (
            _window(fingerprint="b" * 64, minimized=True),
            _window(fingerprint="b" * 64, minimized=False),
        ),
    ),
    ids=("move", "resize", "minimize-restore"),
)
def test_bounded_reopen_peer_layout_change_preserves_stable_contract(
    tmp_path,
    original_peer,
    current_peer,
):
    result, _payloads = _begin_bounded_with_peer_snapshot(
        tmp_path,
        original_windows=(original_peer,),
        current_windows=(current_peer,),
    )

    assert result.pending is True
    assert result.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    assert result.failure_code is None


@pytest.mark.parametrize(
    ("current_peer", "changed_field"),
    (
        (
            _window(
                handle=13,
                process_id=102,
                fingerprint="b" * 64,
                thread_id=502,
                process_lifecycle_token=1002,
            ),
            "handle",
        ),
        (
            _window(
                handle=12,
                process_id=103,
                fingerprint="b" * 64,
                thread_id=502,
                process_lifecycle_token=1002,
            ),
            "process-id",
        ),
        (
            _window(
                handle=12,
                process_id=102,
                fingerprint="b" * 64,
                thread_id=503,
                process_lifecycle_token=1002,
            ),
            "thread-id",
        ),
        (
            _window(
                handle=12,
                process_id=102,
                fingerprint="b" * 64,
                thread_id=502,
                process_lifecycle_token=1003,
            ),
            "lifecycle",
        ),
    ),
    ids=("handle", "process-id", "thread-id", "lifecycle"),
)
def test_bounded_reopen_rejects_single_stable_peer_identity_change(
    tmp_path,
    current_peer,
    changed_field,
):
    original_peer = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )

    result, payloads = _begin_bounded_with_peer_snapshot(
        tmp_path,
        original_windows=(original_peer,),
        current_windows=(current_peer,),
    )

    assert changed_field
    assert result.failure_code == "battle_contract_identity_changed"
    assert result.pending is False
    assert payloads[0]["expected_identity_keys"]


@pytest.mark.parametrize(
    "current_windows",
    (
        (
            _window(
                handle=12,
                process_id=102,
                fingerprint=None,
                thread_id=502,
                process_lifecycle_token=1002,
            ),
        ),
        (
            _window(
                handle=12,
                process_id=102,
                fingerprint="b" * 64,
                thread_id=502,
                process_lifecycle_token=1002,
            ),
        )
        * 2,
    ),
    ids=("unknown", "duplicate"),
)
def test_bounded_reopen_rejects_unknown_or_duplicate_peer_snapshot(
    tmp_path,
    current_windows,
):
    original_peer = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )

    result, _payloads = _begin_bounded_with_peer_snapshot(
        tmp_path,
        original_windows=(original_peer,),
        current_windows=current_windows,
    )

    assert result.failure_code in {
        "battle_window_existing_state_unknown",
        "battle_contract_identity_changed",
    }
    assert result.pending is False


def test_bounded_reopen_rejects_missing_healthy_peer(tmp_path):
    first_peer = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )
    second_peer = _window(
        handle=13,
        process_id=103,
        fingerprint="c" * 64,
        thread_id=503,
        process_lifecycle_token=1003,
    )

    result, _payloads = _begin_bounded_with_peer_snapshot(
        tmp_path,
        original_windows=(first_peer, second_peer),
        current_windows=(first_peer,),
    )

    assert result.failure_code == "battle_contract_identity_changed"
    assert result.pending is False


def test_bounded_reopen_rejects_old_owner_reappearing(tmp_path):
    peer = _window(
        handle=12,
        process_id=102,
        fingerprint="b" * 64,
        thread_id=502,
        process_lifecycle_token=1002,
    )
    old_owner = _window(
        handle=11,
        process_id=101,
        fingerprint="a" * 64,
        thread_id=501,
        process_lifecycle_token=1001,
    )

    result, payloads = _begin_bounded_with_peer_snapshot(
        tmp_path,
        original_windows=(peer,),
        current_windows=(peer, old_owner),
    )

    assert result.failure_code == "battle_contract_identity_changed"
    assert result.pending is False
    assert payloads[0]["fingerprint"] == old_owner.launch_fingerprint


def test_bounded_worker_identity_key_excludes_layout_and_minimized_state():
    key_block = _POWERSHELL_REOPEN_SCRIPT.split("$key = @(", 1)[1].split(
        ") -join '|'",
        1,
    )[0]

    for stable_field in (
        "$fingerprint",
        "$row.Handle",
        "$row.ProcessId",
        "$row.ThreadId",
        "$classB64",
        "$row.Lifecycle",
    ):
        assert stable_field in key_block
    for layout_field in (
        "$row.Left",
        "$row.Top",
        "$row.Right",
        "$row.Bottom",
        "$row.Minimized",
    ):
        assert layout_field not in key_block
    assert "$actual.Count -ne $expected.Count" in _POWERSHELL_REOPEN_SCRIPT
    assert "Sort-Object" in _POWERSHELL_REOPEN_SCRIPT
    assert "[StringComparison]::Ordinal" in _POWERSHELL_REOPEN_SCRIPT
    assert "$snapshot.Fingerprints" in _POWERSHELL_REOPEN_SCRIPT


@pytest.mark.parametrize(
    ("stages", "expected_stage", "expected_failure"),
    (
        (
            (),
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            "battle_first_absence_hard_timeout",
        ),
        (
            (
                BattleReopenStage.FIRST_ABSENCE_STARTED.value,
                BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
                BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            ),
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            "battle_shortcut_fingerprint_hard_timeout",
        ),
        (
            (
                BattleReopenStage.FIRST_ABSENCE_STARTED.value,
                BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
                BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
                BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
                BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            ),
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            "battle_second_absence_hard_timeout",
        ),
    ),
)
def test_bounded_prelaunch_hard_timeouts_stop_worker_and_allow_safe_retry(
    tmp_path,
    stages,
    expected_stage,
    expected_failure,
):
    now = [0.0]
    workers = []

    def factory(_payload):
        worker = _BoundedWorker(stages)
        workers.append(worker)
        return worker

    restarter = _bounded_restarter(factory, now)
    pending = _begin_bounded(restarter, tmp_path)
    assert pending.pending is True
    assert pending.stage == expected_stage

    now[0] = 1.1
    failed = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )

    assert failed.failure_code == expected_failure
    assert failed.hard_timeout is True
    assert failed.retry_allowed is True
    assert failed.delivery_boundary_crossed is False
    assert failed.wait_new_instance_only is False
    assert workers[0].terminate_calls == 1
    assert workers[0].is_running() is False

    retried = _begin_bounded(restarter, tmp_path)
    assert retried.pending is True
    assert len(workers) == 2


def test_timeout_evidence_survives_safe_retry_that_reaches_launch_ready(
    tmp_path,
):
    now = [0.0]
    ready_stages = (
        BattleReopenStage.FIRST_ABSENCE_STARTED.value,
        BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
        BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
        BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
        BattleReopenStage.SECOND_ABSENCE_STARTED.value,
        BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
        BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
    )
    workers = [_BoundedWorker(), _BoundedWorker(ready_stages)]
    restarter = _bounded_restarter(lambda _payload: workers.pop(0), now)
    _begin_bounded(restarter, tmp_path)
    now[0] = 1.1
    first = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert first.failure_code == "battle_first_absence_hard_timeout"

    now[0] = 2.0
    second = _begin_bounded(restarter, tmp_path)

    assert second.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    assert any(
        item.failure_reason == "battle_first_absence_hard_timeout"
        and item.hard_timeout
        for item in second.stage_evidence
    )
    assert second.stage_evidence[-1].stage == (
        BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    )

    authorized = restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    first_failure_index = next(
        index
        for index, item in enumerate(authorized.stage_evidence)
        if item.failure_reason == "battle_first_absence_hard_timeout"
    )
    second_prepared_index = max(
        index
        for index, item in enumerate(authorized.stage_evidence)
        if item.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    )
    assert first_failure_index < second_prepared_index
    assert authorized.stage_evidence[first_failure_index].hard_timeout is True
    assert authorized.stage_evidence == second.stage_evidence
    assert authorized.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    assert workers == []


def test_launch_call_hang_is_nonblocking_consumed_and_never_authorized_twice(
    tmp_path,
):
    now = [0.0]
    worker = _BoundedWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    factory_calls = []
    restarter = _bounded_restarter(
        lambda payload: factory_calls.append(payload) or worker,
        now,
    )
    ready = _begin_bounded(restarter, tmp_path)
    assert ready.pending is True
    authorized = restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    assert authorized.pending is True
    worker.emit(BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value)

    before = time.perf_counter()
    entered = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    elapsed = time.perf_counter() - before
    assert elapsed < 0.1
    assert entered.pending is True
    assert entered.delivery_boundary_crossed is True
    assert entered.wait_new_instance_only is True

    now[0] = 1.1
    timed_out = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert timed_out.failure_code == "battle_shortcut_launch_hard_timeout"
    assert timed_out.hard_timeout is True
    assert timed_out.retry_allowed is False
    assert timed_out.wait_new_instance_only is True
    assert worker.authorize_calls == 1
    assert len(factory_calls) == 1


def test_target_appearing_during_ack_wait_never_enters_or_retries_launch(
    tmp_path,
):
    now = [0.0]
    worker = _BoundedWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    factory_calls = []
    restarter = _bounded_restarter(
        lambda payload: factory_calls.append(payload) or worker,
        now,
    )
    ready = _begin_bounded(restarter, tmp_path)
    assert ready.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    worker._events.append(
        {
            "stage": BattleReopenStage.FAILED.value,
            "timestamp_ms": 2_000,
            "failure_code": "battle_window_already_exists",
        }
    )

    failed = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    repeated = _begin_bounded(restarter, tmp_path)

    ack = _POWERSHELL_REOPEN_SCRIPT.index(
        "Wait-TokenFile $script:ackPath 65"
    )
    final_absence = _POWERSHELL_REOPEN_SCRIPT.index(
        "$failure = Test-TargetAbsent",
        ack,
    )
    entered = _POWERSHELL_REOPEN_SCRIPT.index(
        "Emit-Stage 'shortcut_launch_entered'"
    )
    shell_execute = _POWERSHELL_REOPEN_SCRIPT.index(
        "[FlashReopenNative]::LaunchWithoutActivation("
    )
    assert ack < final_absence < entered < shell_execute
    assert failed.failure_code == "battle_window_already_exists"
    assert failed.delivery_boundary_crossed is False
    assert failed.shortcut_open_requested is False
    assert failed.retry_allowed is False
    assert failed.wait_new_instance_only is True
    assert repeated.failure_code == "battle_window_already_exists"
    assert not any(
        item.stage == BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value
        for item in failed.stage_evidence
    )
    assert worker.authorize_calls == 1
    assert worker.terminate_calls == 1
    assert len(factory_calls) == 1


def test_final_absence_after_ack_has_dedicated_hard_timeout(tmp_path):
    now = [0.0]
    worker = _BoundedWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    restarter = _bounded_restarter(lambda _payload: worker, now)
    _begin_bounded(restarter, tmp_path)
    restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )

    now[0] = 1.1
    failed = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )

    assert failed.failure_code == "battle_final_absence_hard_timeout"
    assert failed.hard_timeout is True
    assert failed.retry_allowed is False
    assert failed.wait_new_instance_only is True
    assert failed.delivery_boundary_crossed is False
    assert worker.authorize_calls == 1
    assert worker.terminate_calls == 1


def test_new_instance_race_absorbs_entered_event_and_proves_boundary(
    tmp_path,
):
    now = [0.0]
    worker = _BoundedWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    factory_calls = []
    restarter = _bounded_restarter(
        lambda payload: factory_calls.append(payload) or worker,
        now,
    )
    ready = _begin_bounded(restarter, tmp_path)
    assert ready.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    worker.emit(BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value)

    completed = restarter.complete_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )

    stages = tuple(item.stage for item in completed.stage_evidence)
    assert completed.success is True
    assert completed.stage == BattleReopenStage.NEW_INSTANCE_APPEARED.value
    assert completed.delivery_boundary_crossed is True
    assert completed.shortcut_open_requested is True
    assert BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value in stages
    assert BattleReopenStage.SHORTCUT_LAUNCH_RETURNED.value not in stages
    assert completed.stage_evidence[-1].delivery_boundary_crossed is True
    assert completed.stage_evidence[-1].wait_new_instance_only is True
    assert worker.authorize_calls == 1
    assert worker.terminate_calls == 1
    assert worker.cleanup_calls == 1
    assert len(factory_calls) == 1


@pytest.mark.parametrize("launch_entered", (False, True))
def test_new_instance_completion_keeps_unreaped_worker_active(
    tmp_path,
    launch_entered,
):
    now = [0.0]

    class UnreapableWorker(_BoundedWorker):
        def terminate_and_wait(self):
            self.terminate_calls += 1
            return False

    worker = UnreapableWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    factory_calls = []
    restarter = _bounded_restarter(
        lambda payload: factory_calls.append(payload) or worker,
        now,
    )
    _begin_bounded(restarter, tmp_path)
    restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    if launch_entered:
        worker.emit(BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value)

    failed = restarter.complete_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    still_blocked = _begin_bounded(restarter, tmp_path)

    assert failed.failure_code == "battle_reopen_worker_unreaped"
    assert failed.success is False
    assert failed.delivery_boundary_crossed is True
    assert failed.retry_allowed is False
    assert failed.wait_new_instance_only is True
    assert failed.stage == BattleReopenStage.FAILED.value
    assert still_blocked.failure_code == "battle_reopen_worker_unreaped"
    assert restarter._active_reopen_job is not None
    assert restarter._reopen_evidence_history == ()
    assert (
        any(
            item.stage == BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value
            for item in failed.stage_evidence
        )
        is launch_entered
    )
    assert worker.authorize_calls == 1
    assert worker.terminate_calls == 1
    assert worker.cleanup_calls == 0
    assert len(factory_calls) == 1


@pytest.mark.parametrize(
    ("next_owner", "next_handle", "next_shortcut"),
    (
        ("b" * 64, 22, None),
        ("a" * 64, 99, None),
        ("a" * 64, 11, "different-shortcut.lnk"),
    ),
)
def test_unreaped_active_job_conflicts_by_complete_event_key_without_evidence(
    tmp_path,
    next_owner,
    next_handle,
    next_shortcut,
):
    now = [0.0]

    class UnreapableWorker(_BoundedWorker):
        def terminate_and_wait(self):
            self.terminate_calls += 1
            return False

    worker = UnreapableWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    factory_calls = []
    restarter = _bounded_restarter(
        lambda payload: factory_calls.append(payload) or worker,
        now,
    )
    _begin_bounded(restarter, tmp_path)
    restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    failed = restarter.complete_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )
    target = _target(tmp_path, fingerprint=next_owner)
    if next_shortcut is not None:
        target = replace(
            target,
            shortcut_path=tmp_path / next_shortcut,
        )
    next_instance = (
        (
            "a" * 64,
            11,
            101,
            501,
            "ShockwaveFlash",
            1001,
            (0, 0, 900, 600),
            False,
        )
        if next_shortcut is not None
        else (
            next_owner,
            next_handle,
            100 + next_handle,
            500 + next_handle,
            "ShockwaveFlash",
            1000 + next_handle,
            (0, 0, 900, 600),
            False,
        )
    )

    conflict = restarter.begin_bounded_reopen(
        owner=next_owner,
        entry_id=target.entry_id,
        original_instance=next_instance,
        target=target,
        candidate_windows=(),
        deadline=60.0,
    )

    assert failed.failure_code == "battle_reopen_worker_unreaped"
    assert conflict.failure_code == "battle_reopen_job_conflict"
    assert conflict.stage is None
    assert conflict.delivery_boundary_crossed is False
    assert conflict.stage_evidence == ()
    assert restarter._active_reopen_job is not None
    assert restarter._active_reopen_job.original_instance[1] == 11
    assert worker.authorize_calls == 1
    assert len(factory_calls) == 1


def test_delayed_launch_return_has_one_authorization_and_then_waits_for_instance(
    tmp_path,
):
    now = [0.0]
    worker = _BoundedWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    factory_calls = []
    restarter = _bounded_restarter(
        lambda payload: factory_calls.append(payload) or worker,
        now,
    )
    _begin_bounded(restarter, tmp_path)
    restarter.authorize_bounded_reopen(owner="a" * 64, entry_id="entry-a")
    worker.emit(BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value)
    entered = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert entered.pending is True
    now[0] = 0.5
    still_waiting = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert still_waiting.pending is True

    worker.emit(BattleReopenStage.SHORTCUT_LAUNCH_RETURNED.value)
    worker._running = False
    returned = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )

    assert returned.success is True
    assert returned.stage == BattleReopenStage.WAITING_NEW_INSTANCE.value
    assert returned.shortcut_open_requested is True
    assert worker.authorize_calls == 1
    assert len(factory_calls) == 1
    launch_started = next(
        item
        for item in returned.stage_evidence
        if item.stage == BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value
    )
    assert launch_started.stage_ended_at is not None

    evidence_count = len(returned.stage_evidence)
    for observed_at in (0.6, 0.8):
        now[0] = observed_at
        stable = restarter.poll_bounded_reopen(
            owner="a" * 64,
            entry_id="entry-a",
            deadline=60.0,
        )
        assert stable.pending is True
        assert stable.stage == BattleReopenStage.WAITING_NEW_INSTANCE.value
        assert len(stable.stage_evidence) == evidence_count
    assert worker.authorize_calls == 1
    assert len(factory_calls) == 1

    now[0] = 60.0
    deadline_failure = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert deadline_failure.failure_code == "tcp_reconnect_timeout"
    assert deadline_failure.hard_timeout is True
    timed_out_evidence_count = len(deadline_failure.stage_evidence)
    repeated = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert repeated.failure_code == "tcp_reconnect_timeout"
    assert len(repeated.stage_evidence) == timed_out_evidence_count
    assert worker.authorize_calls == 1
    assert len(factory_calls) == 1


def test_launch_ack_at_owner_deadline_is_refused_without_shortcut_delivery(
    tmp_path,
):
    now = [0.0]
    worker = _BoundedWorker(
        (
            BattleReopenStage.FIRST_ABSENCE_STARTED.value,
            BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
            BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
            BattleReopenStage.SECOND_ABSENCE_STARTED.value,
            BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
            BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
        )
    )
    restarter = _bounded_restarter(lambda _payload: worker, now)
    ready = _begin_bounded(restarter, tmp_path, deadline=1.0)
    assert ready.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value

    now[0] = 1.0
    blocked = restarter.authorize_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
    )

    assert blocked.failure_code == "tcp_reconnect_timeout"
    assert blocked.hard_timeout is True
    assert blocked.retry_allowed is False
    assert blocked.delivery_boundary_crossed is False
    assert worker.authorize_calls == 0
    assert worker.terminate_calls == 1


@pytest.mark.parametrize(
    ("next_fingerprint", "next_handle", "next_shortcut"),
    (
        ("b" * 64, 11, None),
        ("a" * 64, 99, None),
        ("a" * 64, 11, "different-shortcut.lnk"),
    ),
)
def test_reopen_evidence_history_isolated_by_complete_recovery_event(
    tmp_path,
    next_fingerprint,
    next_handle,
    next_shortcut,
):
    now = [0.0]
    ready_stages = (
        BattleReopenStage.FIRST_ABSENCE_STARTED.value,
        BattleReopenStage.FIRST_ABSENCE_COMPLETED.value,
        BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value,
        BattleReopenStage.SHORTCUT_FINGERPRINT_COMPLETED.value,
        BattleReopenStage.SECOND_ABSENCE_STARTED.value,
        BattleReopenStage.SECOND_ABSENCE_COMPLETED.value,
        BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value,
    )
    workers = [_BoundedWorker(), _BoundedWorker(ready_stages)]
    restarter = _bounded_restarter(lambda _payload: workers.pop(0), now)
    _begin_bounded(restarter, tmp_path)
    now[0] = 1.1
    first = restarter.poll_bounded_reopen(
        owner="a" * 64,
        entry_id="entry-a",
        deadline=60.0,
    )
    assert first.failure_code == "battle_first_absence_hard_timeout"

    target = _target(tmp_path, fingerprint=next_fingerprint)
    if next_shortcut is not None:
        target = replace(
            target,
            shortcut_path=tmp_path / next_shortcut,
        )
    original_instance = (
        (
            "a" * 64,
            11,
            101,
            501,
            "ShockwaveFlash",
            1001,
            (0, 0, 900, 600),
            False,
        )
        if next_shortcut is not None
        else (
            next_fingerprint,
            next_handle,
            200 + next_handle,
            500 + next_handle,
            "ShockwaveFlash",
            2000 + next_handle,
            (0, 0, 900, 600),
            False,
        )
    )
    now[0] = 2.0
    second = restarter.begin_bounded_reopen(
        owner=next_fingerprint,
        entry_id=target.entry_id,
        original_instance=original_instance,
        target=target,
        candidate_windows=(),
        deadline=60.0,
    )

    assert second.stage == BattleReopenStage.SHORTCUT_LAUNCH_PREPARED.value
    assert sum(
        item.failure_reason == "battle_first_absence_hard_timeout"
        for item in second.stage_evidence
    ) == 0
    assert {
        (item.owner, item.entry_id, item.original_instance)
        for item in second.stage_evidence
    } == {(next_fingerprint, "entry-a", original_instance)}


def test_partial_stage_file_cannot_advance_sequence_or_launch_boundary(tmp_path):
    worker = object.__new__(_PowerShellBattleReopenWorker)
    worker._event_path = tmp_path / "stages.jsonl"
    worker._seen_lines = 0
    worker._next_sequence = 1
    worker._job_id = "job-a"
    worker._token = "token-a"
    event = json.dumps(
        {
            "job_id": worker._job_id,
            "token": worker._token,
            "sequence": 1,
            "stage": BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value,
            "timestamp_ms": 1_000,
            "failure_code": "",
        },
        separators=(",", ":"),
    )
    split_at = len(event) // 2
    worker._event_path.write_text(event[:split_at], encoding="utf-8")

    assert worker.poll_events() == ()
    assert worker._seen_lines == 0
    assert worker._next_sequence == 1

    with worker._event_path.open("a", encoding="utf-8") as stream:
        stream.write(event[split_at:] + "\n")
    completed = worker.poll_events()

    assert len(completed) == 1
    assert completed[0]["stage"] == (
        BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value
    )
    assert worker._seen_lines == 1
    assert worker._next_sequence == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows worker only")
def test_real_windows_worker_starts_reports_first_stage_and_cleans_up(tmp_path):
    worker = _PowerShellBattleReopenWorker(
        {
            "fingerprint": "a" * 64,
            "shortcut_path": str(tmp_path / "never-launch.lnk"),
            "title_keywords": (
                "Codex bounded reopen worker test no matching window",
            ),
            "absence_stability_seconds": 1.0,
            "poll_seconds": 0.1,
            "expected_identity_keys": (),
        }
    )
    temporary_dir = worker._temporary_dir
    ack_path = worker._ack_path
    observed = []
    deadline = time.monotonic() + 10.0
    try:
        while time.monotonic() < deadline:
            observed.extend(worker.poll_events())
            if any(
                event["stage"]
                == BattleReopenStage.FAILED.value
                for event in observed
            ):
                break
            if not worker.is_running():
                observed.extend(worker.poll_events())
                break
            time.sleep(0.02)
        assert any(
            event["stage"]
            == BattleReopenStage.FIRST_ABSENCE_STARTED.value
            for event in observed
        )
        assert any(
            event["stage"]
            == BattleReopenStage.SHORTCUT_FINGERPRINT_STARTED.value
            for event in observed
        )
        assert any(
            event["stage"] == BattleReopenStage.FAILED.value
            and event["failure_code"]
            == "battle_shortcut_identity_unresolved"
            for event in observed
        )
        assert not any(
            event["stage"]
            == BattleReopenStage.SHORTCUT_LAUNCH_ENTERED.value
            for event in observed
        )
        assert ack_path.exists() is False
    finally:
        assert worker.terminate_and_wait() is True
        worker.cleanup()

    assert temporary_dir.exists() is False


def test_reopen_worker_has_no_foreground_focus_cursor_visibility_or_z_order_mutation():
    script = _POWERSHELL_REOPEN_SCRIPT.casefold()

    assert "setforegroundwindow" not in script
    assert "setfocus" not in script
    assert "setcursorpos" not in script
    assert "showwindow" not in script
    assert "setwindowpos" not in script
    assert "bringwindowtotop" not in script
    assert "mouse_event" not in script


def test_formal_reopen_worker_launches_shortcut_once_without_activation():
    script = _POWERSHELL_REOPEN_SCRIPT
    launch_block = script.split(
        "Emit-Stage 'shortcut_launch_entered'",
        1,
    )[1].split("Emit-Stage 'shortcut_launch_returned'", 1)[0]

    assert "ShellExecuteExW" in script
    assert "SW_SHOWNOACTIVATE" in script
    assert "SEE_MASK_NOASYNC = 0x00000100" in script
    assert (
        "fMask = SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC"
        in script
    )
    assert "if (!ShellExecuteExW(ref info))" in script
    assert "Win32Exception" in script
    assert launch_block.count("LaunchWithoutActivation") == 1
    assert "Fail-Reopen 'battle_shortcut_open_failed'" in launch_block
    assert "ProcessStartInfo" not in launch_block
    assert "UseShellExecute" not in launch_block
