from dataclasses import replace
from pathlib import Path

from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from services.group_configuration_service import GroupConfigurationService
from services.ungrouped_window_service import UngroupedWindowService


def _shortcut(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.lnk"
    path.write_bytes(b"shortcut")
    return path.resolve()


def _window(handle: int, fingerprint: str) -> WindowInfo:
    return WindowInfo(
        handle=handle,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 900, 600),
        process_id=handle + 100,
        window_class="ShockwaveFlash",
        launch_fingerprint=fingerprint,
        thread_id=handle + 1_000,
        process_lifecycle_token=handle + 10_000,
    )


class FakeShortcutFingerprintResolver:
    def __init__(self, values: dict[Path, str]) -> None:
        self.values = values
        self.calls: list[tuple[Path, ...]] = []

    def resolve(self, paths):
        paths = tuple(paths)
        self.calls.append(paths)
        return {
            Path(path).resolve(): self.values[Path(path).resolve()]
            for path in paths
            if Path(path).resolve() in self.values
        }


class FakeWindowBackend:
    def __init__(self, windows) -> None:
        self.windows = tuple(windows)

    def list_windows(self):
        return self.windows


def test_lists_only_open_unique_windows_outside_all_groups(tmp_path):
    desktop = tmp_path / "Desktop"
    nested = desktop / "123"
    grouped = _shortcut(desktop, "已分組")
    ungrouped = _shortcut(nested, "未分組")
    group_fingerprint = "a" * 64
    free_fingerprint = "b" * 64
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    configuration.add_shortcuts("第一組", (grouped,))
    observed: list[tuple[tuple[str, ...], tuple[WindowInfo, ...]]] = []

    resolver = FakeShortcutFingerprintResolver(
        {
            grouped: group_fingerprint,
            ungrouped: free_fingerprint,
        }
    )
    service = UngroupedWindowService(
        configuration,
        resolver,
        FakeWindowBackend(
            (
                _window(1, group_fingerprint),
                _window(2, free_fingerprint),
            )
        ),
        screen_states_provider=lambda fingerprints, windows: (
            observed.append((fingerprints, windows))
            or {free_fingerprint: ReconnectScreenState.CONNECTED}
        ),
        shortcut_roots=(desktop, nested),
    )

    candidates = service.safe_candidates()

    assert candidates == ((free_fingerprint, ungrouped, _window(2, free_fingerprint)),)
    assert observed == []

    snapshot = service.snapshot()

    assert [(item.shortcut_name, item.status) for item in snapshot] == [
        ("未分組.lnk", "online"),
    ]
    assert snapshot[0].fingerprint == free_fingerprint
    assert service.shortcut_for(free_fingerprint) == ungrouped
    assert len(resolver.calls) == 1
    assert observed[0][0] == (free_fingerprint,)
    assert observed[0][1][0].handle == 2


def test_hides_duplicate_shortcut_identity_and_keeps_unknown_state(tmp_path):
    desktop = tmp_path / "Desktop"
    nested = desktop / "123"
    first = _shortcut(desktop, "第一個")
    second = _shortcut(nested, "第二個")
    third = _shortcut(nested, "未知狀態")
    duplicate_fingerprint = "c" * 64
    unknown_fingerprint = "d" * 64
    configuration = GroupConfigurationService(tmp_path / "groups.json")

    service = UngroupedWindowService(
        configuration,
        FakeShortcutFingerprintResolver(
            {
                first: duplicate_fingerprint,
                second: duplicate_fingerprint,
                third: unknown_fingerprint,
            }
        ),
        FakeWindowBackend(
            (
                _window(1, duplicate_fingerprint),
                _window(2, unknown_fingerprint),
            )
        ),
        screen_states_provider=lambda _fingerprints, _windows: {
            unknown_fingerprint: ReconnectScreenState.LOGIN_START,
        },
        shortcut_roots=(desktop, nested),
    )

    snapshot = service.snapshot()

    assert [(item.shortcut_name, item.status) for item in snapshot] == [
        ("未知狀態.lnk", "unknown"),
    ]
    assert service.shortcut_for(duplicate_fingerprint) is None


def test_shortcut_lookup_is_unique_and_does_not_reenter_screen_observation(
    tmp_path,
):
    desktop = tmp_path / "Desktop"
    shortcut = _shortcut(desktop, "唯一未分組")
    fingerprint = "e" * 64
    screen_calls = []
    configuration = GroupConfigurationService(tmp_path / "groups.json")
    backend = FakeWindowBackend((_window(1, fingerprint),))
    service = UngroupedWindowService(
        configuration,
        FakeShortcutFingerprintResolver({shortcut: fingerprint}),
        backend,
        screen_states_provider=lambda *_args: screen_calls.append(True),
        shortcut_roots=(desktop,),
    )

    assert service.shortcut_for(fingerprint) == shortcut
    assert screen_calls == []

    backend.windows = (
        _window(1, fingerprint),
        _window(2, fingerprint),
    )
    assert service.shortcut_for(fingerprint) is None
    assert screen_calls == []


def test_safe_candidates_excludes_duplicate_and_incomplete_live_instances(
    tmp_path,
):
    desktop = tmp_path / "Desktop"
    duplicate = _shortcut(desktop, "duplicate")
    incomplete = _shortcut(desktop, "incomplete")
    duplicate_fingerprint = "f" * 64
    incomplete_fingerprint = "1" * 64
    service = UngroupedWindowService(
        GroupConfigurationService(tmp_path / "groups.json"),
        FakeShortcutFingerprintResolver(
            {
                duplicate: duplicate_fingerprint,
                incomplete: incomplete_fingerprint,
            }
        ),
        FakeWindowBackend(
            (
                _window(1, duplicate_fingerprint),
                _window(2, duplicate_fingerprint),
                replace(
                    _window(3, incomplete_fingerprint),
                    process_lifecycle_token=None,
                ),
            )
        ),
        shortcut_roots=(desktop,),
    )

    assert service.safe_candidates() == ()


def test_safe_candidate_without_shortcut_has_detection_identity_only(tmp_path):
    fingerprint = "2" * 64
    window = _window(1, fingerprint)
    service = UngroupedWindowService(
        GroupConfigurationService(tmp_path / "groups.json"),
        FakeShortcutFingerprintResolver({}),
        FakeWindowBackend((window,)),
        shortcut_roots=(tmp_path / "empty",),
    )

    assert service.safe_candidates() == ((fingerprint, None, window),)
    assert service.snapshot() == ()
    assert service.shortcut_for(fingerprint) is None
