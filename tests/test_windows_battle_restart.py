from adapters.windows_battle_restart import WindowsBattleWindowRestarter
from adapters.windows_window import WindowInfo
from services.group_launch_service import GroupLaunchTarget


def _window(handle=11, process_id=101, fingerprint="a" * 64):
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 900, 600),
        process_id=process_id,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
    )


class _Windows:
    def __init__(self, windows):
        self.windows = list(windows)

    def list_windows(self):
        return list(self.windows)


class _Closer:
    def __init__(self, *, close=True, remains=0):
        self.close = close
        self.remains = remains
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

    def close_window(self, handle):
        self.closed.append(handle)
        return self.close


class _Opener:
    def __init__(self, *, succeeds=True):
        self.succeeds = succeeds
        self.targets = []

    def open_shortcut(self, target):
        self.targets.append(target)
        return self.succeeds


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
