import pytest

from adapters.windows_battle_restart import (
    BattleRestartResult,
    WindowsBattleWindowRestarter,
    WindowsShortcutOpenBackend,
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
):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 900, 600),
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
        sleeper=lambda _seconds: None,
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
