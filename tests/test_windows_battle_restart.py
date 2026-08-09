import pytest

from adapters.windows_battle_restart import (
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
    def __init__(self, windows):
        self.windows = list(windows)
        self.calls = 0

    def list_windows(self):
        self.calls += 1
        return list(self.windows)


class _FailingWindows:
    def list_windows(self):
        raise RuntimeError("enumeration failed")


class _Closer:
    def __init__(
        self,
        *,
        close=True,
        remains=0,
        before_close_boundary=None,
    ):
        self.close = close
        self.remains = remains
        self.before_close_boundary = before_close_boundary
        self.closed = []
        self.checks = 0

    def is_window(self, _handle):
        self.checks += 1
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
        return (
            (True, None)
            if self.close
            else (False, "battle_window_close_failed")
        )


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
    )


def test_exact_window_is_closed_then_same_shortcut_is_opened(tmp_path):
    window = _window()
    closer = _Closer(remains=1)
    opener = _Opener()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    restarter = WindowsBattleWindowRestarter(
        _Windows([window]),
        closer,
        opener,
        monotonic_clock=lambda: now[0],
        sleeper=sleep,
    )

    result = restarter.restart(window, _target(tmp_path))

    assert result.success is True
    assert closer.closed == [window.handle]
    assert opener.targets == [_target(tmp_path)]


def test_changed_identity_never_closes_or_opens_any_window(tmp_path):
    original = _window()
    changed = _window(process_id=999)
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([changed]),
        closer,
        opener,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_identity_changed"
    assert closer.closed == []
    assert opener.targets == []


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    (
        ("thread_id", 999),
        ("process_lifecycle_token", 2002),
        ("window_class", "ReplacementFlash"),
    ),
)
def test_reused_handle_with_changed_instance_never_closes_replacement(
    tmp_path,
    changed_field,
    changed_value,
):
    original = _window()
    replacement_values = {
        "thread_id": original.thread_id,
        "process_lifecycle_token": original.process_lifecycle_token,
        "window_class": original.window_class,
    }
    replacement_values[changed_field] = changed_value
    replacement = _window(**replacement_values)
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([replacement]),
        closer,
        opener,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_identity_changed"
    assert closer.closed == []
    assert opener.targets == []


def test_instance_is_rechecked_after_is_window_and_before_close(tmp_path):
    original = _window()
    replacement = _window(
        process_id=999,
        thread_id=999,
        process_lifecycle_token=9999,
        window_class="ReplacementFlash",
    )
    windows = _Windows([original])
    opener = _Opener()

    class _ReplaceDuringFinalProbe:
        def __init__(self):
            self.closed = []

        def is_window(self, _handle):
            windows.windows = [replacement]
            return True

        def close_window_if_instance_matches(
            self,
            handle,
            expected_identity,
            current_identity,
        ):
            if current_identity(handle) != expected_identity:
                return False, "battle_window_identity_changed"
            self.closed.append(handle)
            return True, None

    closer = _ReplaceDuringFinalProbe()
    restarter = WindowsBattleWindowRestarter(
        windows,
        closer,
        opener,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_identity_changed"
    assert closer.closed == []
    assert opener.targets == []
    assert windows.calls == 2


def test_instance_is_rechecked_inside_close_delivery_boundary(tmp_path):
    original = _window()
    replacement = _window(
        process_id=999,
        thread_id=999,
        process_lifecycle_token=9999,
        window_class="ReplacementFlash",
    )
    windows = _Windows([original])
    closer = _Closer(
        before_close_boundary=lambda: setattr(
            windows,
            "windows",
            [replacement],
        )
    )
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        windows,
        closer,
        opener,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_identity_changed"
    assert closer.closed == []
    assert opener.targets == []


def test_close_timeout_never_opens_a_duplicate_window(tmp_path):
    window = _window()
    closer = _Closer(remains=100)
    opener = _Opener()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    restarter = WindowsBattleWindowRestarter(
        _Windows([window]),
        closer,
        opener,
        close_timeout_seconds=0.2,
        poll_seconds=0.1,
        monotonic_clock=lambda: now[0],
        sleeper=sleep,
    )

    result = restarter.restart(window, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_close_timeout"
    assert closer.closed == [window.handle]
    assert opener.targets == []


def test_self_reopened_role_is_rechecked_before_shortcut_open(tmp_path):
    original = _window()
    reopened = _window(
        handle=22,
        process_id=202,
        thread_id=502,
        process_lifecycle_token=2002,
    )
    windows = _Windows([original])
    opener = _Opener()

    class _CloseThenSelfReopen:
        def __init__(self):
            self.closed = []

        def is_window(self, _handle):
            if self.closed:
                windows.windows = [reopened]
                return False
            return True

        def close_window_if_instance_matches(
            self,
            handle,
            expected_identity,
            current_identity,
        ):
            if current_identity(handle) != expected_identity:
                return False, "battle_window_identity_changed"
            self.closed.append(handle)
            return True, None

    closer = _CloseThenSelfReopen()
    restarter = WindowsBattleWindowRestarter(
        windows,
        closer,
        opener,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_already_exists"
    assert result.window_closed is True
    assert closer.closed == [original.handle]
    assert opener.targets == []
    assert windows.calls == 4


def test_delayed_self_reopen_during_stable_absence_never_opens_shortcut(
    tmp_path,
):
    original = _window()
    reopened = _window(
        handle=22,
        process_id=202,
        thread_id=502,
        process_lifecycle_token=2002,
    )
    windows = _Windows([original])
    closer = _Closer()
    opener = _Opener()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        windows.windows = [reopened]

    restarter = WindowsBattleWindowRestarter(
        windows,
        closer,
        opener,
        absence_stability_seconds=0.2,
        monotonic_clock=lambda: now[0],
        sleeper=sleep,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_already_exists"
    assert result.window_closed is True
    assert closer.closed == [original.handle]
    assert opener.targets == []


def test_self_reopen_inside_shortcut_boundary_never_opens_duplicate(
    tmp_path,
):
    original = _window()
    reopened = _window(
        handle=22,
        process_id=202,
        thread_id=502,
        process_lifecycle_token=2002,
    )
    windows = _Windows([original])
    closer = _Closer()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    opener = _Opener(
        before_open_boundary=lambda: setattr(
            windows,
            "windows",
            [reopened],
        )
    )
    restarter = WindowsBattleWindowRestarter(
        windows,
        closer,
        opener,
        absence_stability_seconds=0.2,
        monotonic_clock=lambda: now[0],
        sleeper=sleep,
    )

    result = restarter.restart(original, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_already_exists"
    assert result.window_closed is True
    assert closer.closed == [original.handle]
    assert opener.targets == []


def test_open_failure_is_reported_without_touching_other_targets(tmp_path):
    window = _window()
    closer = _Closer()
    opener = _Opener(succeeds=False)
    restarter = WindowsBattleWindowRestarter(
        _Windows([window]),
        closer,
        opener,
    )

    result = restarter.restart(window, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_shortcut_open_failed"
    assert result.window_closed is True
    assert closer.closed == [window.handle]
    assert opener.targets == [_target(tmp_path)]


def test_missing_retry_refuses_to_open_when_target_window_exists(tmp_path):
    target = _target(tmp_path)
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([_window(fingerprint=target.fingerprint)]),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(
        target,
        [_window(fingerprint=target.fingerprint)],
    )

    assert result.success is False
    assert result.failure_code == "battle_window_already_exists"
    assert opener.targets == []


def test_missing_retry_refuses_unknown_existing_window(tmp_path):
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([]),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(
        _target(tmp_path),
        [_window(fingerprint=None)],
    )

    assert result.success is False
    assert result.failure_code == "battle_window_existing_state_unknown"
    assert opener.targets == []


def test_missing_retry_rechecks_live_windows_before_opening(tmp_path):
    target = _target(tmp_path)
    windows = _Windows(
        [_window(fingerprint=target.fingerprint)],
    )
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        windows,
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(target, [])

    assert result.success is False
    assert result.failure_code == "battle_window_already_exists"
    assert windows.calls == 1
    assert opener.targets == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("handle", 0),
        ("process_id", 0),
        ("thread_id", 0),
        ("window_class", ""),
        ("process_lifecycle_token", False),
        ("fingerprint", None),
    ),
)
def test_missing_retry_refuses_incomplete_live_window_instance(
    tmp_path,
    field,
    value,
):
    invalid_window = _window(**{field: value})
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([invalid_window]),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(_target(tmp_path), [])

    assert result.success is False
    assert result.failure_code == "battle_window_existing_state_unknown"
    assert opener.targets == []


def test_missing_retry_ignores_duplicate_identity_of_unrelated_target(tmp_path):
    duplicate = "b" * 64
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows(
            [
                _window(
                    handle=21,
                    process_id=201,
                    fingerprint=duplicate,
                ),
                _window(
                    handle=22,
                    process_id=202,
                    fingerprint=duplicate,
                ),
            ]
        ),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(_target(tmp_path), [])

    assert result.success is True
    assert result.failure_code is None
    assert len(opener.targets) == 1


def test_missing_retry_refuses_when_live_enumeration_fails(tmp_path):
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _FailingWindows(),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(_target(tmp_path), [])

    assert result.success is False
    assert result.failure_code == "battle_window_enumeration_failed"
    assert opener.targets == []


def test_missing_retry_ignores_unknown_non_game_window(tmp_path):
    unrelated = _window(fingerprint=None)
    unrelated = WindowInfo(
        handle=unrelated.handle,
        title="記事本",
        visible=unrelated.visible,
        minimized=unrelated.minimized,
        rect=unrelated.rect,
        process_id=unrelated.process_id,
        window_class="Notepad",
        launch_fingerprint=None,
        thread_id=unrelated.thread_id,
        process_lifecycle_token=unrelated.process_lifecycle_token,
    )
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([unrelated]),
        _Closer(),
        opener,
    )
    target = _target(tmp_path)

    result = restarter.reopen_missing(target, [])

    assert result.success is True
    assert opener.targets == [target]


def test_missing_retry_opens_only_exact_absent_target(tmp_path):
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([]),
        _Closer(),
        opener,
    )
    target = _target(tmp_path)

    result = restarter.reopen_missing(target, [])

    assert result.success is True
    assert result.shortcut_open_requested is True
    assert opener.targets == [target]


@pytest.mark.parametrize("shared_field", ("handle", "process_id"))
def test_direct_restart_refuses_cross_role_live_instance_collision(
    tmp_path,
    shared_field,
):
    target_window = _window()
    conflicting = _window(
        handle=22,
        process_id=202,
        fingerprint="b" * 64,
    )
    conflicting = WindowInfo(
        handle=(
            target_window.handle
            if shared_field == "handle"
            else conflicting.handle
        ),
        title=conflicting.title,
        visible=conflicting.visible,
        minimized=conflicting.minimized,
        rect=conflicting.rect,
        process_id=(
            target_window.process_id
            if shared_field == "process_id"
            else conflicting.process_id
        ),
        window_class=conflicting.window_class,
        launch_fingerprint=conflicting.launch_fingerprint,
        thread_id=conflicting.thread_id,
        process_lifecycle_token=conflicting.process_lifecycle_token,
    )
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([target_window, conflicting]),
        closer,
        opener,
    )

    result = restarter.restart(target_window, _target(tmp_path))

    assert result.success is False
    assert result.failure_code == "battle_window_identity_duplicate"
    assert closer.closed == []
    assert opener.targets == []


def test_direct_reopen_ignores_cross_role_handle_collision(tmp_path):
    first = _window(handle=21, process_id=201, fingerprint="b" * 64)
    second = _window(
        handle=first.handle,
        process_id=202,
        fingerprint="c" * 64,
    )
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([first, second]),
        _Closer(),
        opener,
    )

    result = restarter.reopen_missing(_target(tmp_path), [])

    assert result.success is True
    assert result.failure_code is None
    assert len(opener.targets) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("thread_id", 0),
        ("window_class", ""),
        ("process_lifecycle_token", False),
    ),
)
def test_direct_restart_ignores_incomplete_unrelated_live_candidate(
    tmp_path,
    field,
    value,
):
    target_window = _window()
    invalid = _window(
        handle=22,
        process_id=202,
        fingerprint="b" * 64,
        **{field: value},
    )
    closer = _Closer()
    opener = _Opener()
    restarter = WindowsBattleWindowRestarter(
        _Windows([target_window, invalid]),
        closer,
        opener,
    )

    result = restarter.restart(target_window, _target(tmp_path))

    assert result.success is True
    assert result.failure_code is None
    assert closer.closed == [target_window.handle]
    assert len(opener.targets) == 1


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


def test_final_shortcut_delivery_refuses_resolver_failure(tmp_path, monkeypatch):
    target = _target(tmp_path)
    target.shortcut_path.write_bytes(b"shortcut")
    resolver = _ShortcutResolver(lambda _paths: {})
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
    assert failure_code == "battle_shortcut_identity_unresolved"
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

    resolver = _ShortcutResolver(resolve)
    backend = WindowsShortcutOpenBackend(resolver)
    starts = []
    monkeypatch.setattr(
        "adapters.windows_battle_restart.os.startfile",
        lambda path: starts.append(path),
        raising=False,
    )

    def absence_check():
        calls.append("absence")
        if target_present[0]:
            return "battle_window_already_exists"
        return None

    opened, failure_code = backend.open_shortcut_if_target_absent(
        target,
        absence_check,
    )

    assert opened is False
    assert failure_code == "battle_window_already_exists"
    assert calls == ["absence", "resolve", "absence"]
    assert resolver.calls == [(target.shortcut_path,)]
    assert starts == []
