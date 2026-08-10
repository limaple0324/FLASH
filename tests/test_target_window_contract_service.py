import os
from pathlib import Path

from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectObservationSnapshot,
    SmartReconnectShortcutObservation,
    SmartReconnectWindowObservation,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from core.smart_reconnect_authorization import ShortcutFileIdentity, ShortcutSeal
from core.target_window_contract import TargetWindowPhase
from core.window_instance import WindowInstanceToken
from core.window_registry import WindowRegistry
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationEntry,
)
from services.target_window_contract_service import TargetWindowContractService


FIRST = "a" * 64
SECOND = "b" * 64
BLOCKED = "c" * 64


def _window(fingerprint: str, offset: int) -> WindowInfo:
    return WindowInfo(
        handle=100 + offset,
        title="Adobe Flash Player",
        visible=True,
        minimized=False,
        rect=(0, 0, 800, 600),
        process_id=200 + offset,
        window_class="FlashWindow",
        launch_fingerprint=fingerprint,
        thread_id=300 + offset,
        process_lifecycle_token=400 + offset,
    )


def _observed(window: WindowInfo) -> SmartReconnectWindowObservation:
    return SmartReconnectWindowObservation(
        window=window,
        instance=WindowInstanceToken.from_window(window),
        sample=None,
        recognition=ScreenRecognition(
            ReconnectScreenState.UNKNOWN,
            None,
            None,
            None,
        ),
        fresh_capture=False,
        capture_route="visible",
        role_id=None,
    )


class StaticConfiguration:
    def __init__(self, paths):
        entries = tuple(
            GroupConfigurationEntry(
                entry_id=f"entry-{index}",
                display_name=f"role-{index}",
                shortcut_path=path,
                role="member",
                order=index,
            )
            for index, path in enumerate(paths, start=1)
        )
        self._groups = (GroupConfiguration("group-1", "one", entries),)

    def groups(self):
        return self._groups

    def group(self, name):
        return next((group for group in self._groups if group.name == name), None)

    def expanded_sync_members(self, _entry_id):
        return ()


class ForbiddenWindowBackend:
    def list_windows(self):
        raise AssertionError("formal actual snapshot used direct enumeration")

    def foreground_handle(self):
        raise AssertionError("formal snapshot queried foreground directly")


class StaticBroker:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.current = True
        self.paths = None
        self.latest_calls = 0

    def refresh(self, paths=()):
        self.paths = tuple(paths)
        return self.snapshot

    def latest_snapshot(self):
        self.latest_calls += 1
        return self.snapshot

    def current_snapshot(self):
        self.latest_calls += 1
        return self.snapshot if self.current else None

    def stable_snapshot(self):
        self.latest_calls += 1
        return self.snapshot

    def is_generation_current(self, generation):
        return self.current and generation == self.snapshot.generation


def _shortcut(path: Path, fingerprint: str, index: int):
    normalized = os.path.normcase(os.path.abspath(path))
    return SmartReconnectShortcutObservation(
        normalized,
        fingerprint,
        ShortcutSeal(
            ShortcutFileIdentity(normalized, 10, index),
            f"{index:064x}",
            fingerprint,
        ),
    )


def _service(tmp_path: Path, snapshot):
    paths = (tmp_path / "one.lnk", tmp_path / "two.lnk")
    broker = StaticBroker(snapshot)
    service = TargetWindowContractService(
        StaticConfiguration(paths),
        object(),
        WindowRegistry(),
        ForbiddenWindowBackend(),
        observation_broker=broker,
    )
    return service, broker, paths


def test_actual_snapshot_uses_broker_generation_and_local_isolation(tmp_path):
    snapshot = SmartReconnectObservationSnapshot(
        generation=7,
        windows=(
            _observed(_window(FIRST, 1)),
            _observed(_window(SECOND, 2)),
        ),
        blocked_fingerprints=frozenset((BLOCKED,)),
        isolated_window_count=2,
        anonymous_isolated_window_count=1,
    )
    service, broker, paths = _service(tmp_path, snapshot)

    actual = service.actual_snapshot()

    assert tuple(target.fingerprint for target in actual.targets) == (
        FIRST,
        SECOND,
    )
    assert actual.blocked_fingerprints == frozenset((BLOCKED,))
    assert actual.isolated_window_count == 2
    assert actual.anonymous_isolated_window_count == 1
    assert actual.observation_generation == 7
    assert broker.paths == paths

    resolved = service.actual_reconnect_targets()
    assert tuple(window.launch_fingerprint for window in resolved.windows) == (
        FIRST,
        SECOND,
    )
    assert resolved.actual_window_snapshot is True
    assert resolved.observation_generation == 7


def test_broker_global_failure_never_falls_back_to_direct_backend(tmp_path):
    snapshot = SmartReconnectObservationSnapshot(
        generation=9,
        failure_codes=("window_enumeration_failed",),
    )
    service, _broker, _paths = _service(tmp_path, snapshot)

    actual = service.actual_snapshot()

    assert actual.targets == ()
    assert actual.failure_codes == ("window_enumeration_failed",)
    assert actual.observation_generation == 9


def test_group_snapshot_reads_latest_broker_snapshot_without_direct_io(tmp_path):
    paths = (tmp_path / "one.lnk", tmp_path / "two.lnk")
    first = _window(FIRST, 1)
    second = _window(SECOND, 2)
    snapshot = SmartReconnectObservationSnapshot(
        generation=12,
        windows=(_observed(first), _observed(second)),
        shortcuts=(
            _shortcut(paths[0], FIRST, 1),
            _shortcut(paths[1], SECOND, 2),
        ),
        foreground_handle=first.handle,
    )
    broker = StaticBroker(snapshot)
    service = TargetWindowContractService(
        StaticConfiguration(paths),
        object(),
        WindowRegistry(),
        ForbiddenWindowBackend(),
        observation_broker=broker,
    )

    grouped = service.snapshot("one", expanded_sync_scope=False)

    assert grouped.failure_codes == ()
    assert tuple(target.fingerprint for target in grouped.targets) == (
        FIRST,
        SECOND,
    )
    assert tuple(target.phase for target in grouped.targets) == (
        TargetWindowPhase.FOREGROUND,
        TargetWindowPhase.BACKGROUND,
    )
    assert all(target.safe for target in grouped.targets)
    assert broker.latest_calls == 1
    assert broker.paths is None


def test_group_snapshot_keeps_the_once_captured_stable_publication(tmp_path):
    paths = (tmp_path / "one.lnk", tmp_path / "two.lnk")
    safe = SmartReconnectObservationSnapshot(
        generation=12,
        windows=(
            _observed(_window(FIRST, 1)),
            _observed(_window(SECOND, 2)),
        ),
        shortcuts=(
            _shortcut(paths[0], FIRST, 1),
            _shortcut(paths[1], SECOND, 2),
        ),
    )
    replacement = SmartReconnectObservationSnapshot(
        generation=13,
        failure_codes=("window_enumeration_failed",),
    )

    class InterleavingBroker(StaticBroker):
        def stable_snapshot(self):
            self.latest_calls += 1
            captured = self.snapshot
            self.snapshot = replacement
            return captured

    broker = InterleavingBroker(safe)
    service = TargetWindowContractService(
        StaticConfiguration(paths),
        object(),
        WindowRegistry(),
        ForbiddenWindowBackend(),
        observation_broker=broker,
    )

    grouped = service.snapshot("one", expanded_sync_scope=False)

    assert grouped.failure_codes == ()
    assert tuple(target.fingerprint for target in grouped.safe_targets) == (
        FIRST,
        SECOND,
    )
    assert broker.latest_calls == 1


def test_group_snapshot_keeps_stable_sync_targets_during_action_refresh(tmp_path):
    paths = (tmp_path / "one.lnk", tmp_path / "two.lnk")
    snapshot = SmartReconnectObservationSnapshot(
        generation=14,
        windows=(
            _observed(_window(FIRST, 1)),
            _observed(_window(SECOND, 2)),
        ),
        shortcuts=(
            _shortcut(paths[0], FIRST, 1),
            _shortcut(paths[1], SECOND, 2),
        ),
    )
    service, broker, _paths = _service(tmp_path, snapshot)
    broker.current = False

    grouped = service.snapshot("one", expanded_sync_scope=False)

    assert grouped.failure_codes == ()
    assert tuple(target.fingerprint for target in grouped.safe_targets) == (
        FIRST,
        SECOND,
    )
