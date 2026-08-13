import hashlib
import json
import os
from dataclasses import replace

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_smart_reconnect import (
    ReconnectRuntimeStateStore,
    WindowsSmartReconnectController,
)
from adapters.windows_window import WindowInfo
from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardService
from config.config_manager import ConfigManager
from core.target_window_contract import TargetWindowPhase
from core.window_registry import WindowHealth, WindowRegistry
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.data_contract_migration_service import (
    DataContractMigrationService,
)
from services.event_bus import EventBus
from services.group_configuration_service import GroupConfigurationService
from services.game_operation_gate import GameOperationGate
from services.group_selection_service import GroupSelectionService
from services.keyboard_sync_monitor import Win32KeyboardStateBackend
from services.lifecycle_contract import (
    start_service,
    stop_service,
)
from services.sync_scope_service import SyncScopeService
from services.target_window_contract_service import (
    TargetWindowContractService,
)


class _Resolver:
    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
        }


class _SharedFingerprintResolver:
    def resolve(self, paths):
        return {path: "a" * 64 for path in paths}


class _ShortcutContentResolver:
    def __init__(self):
        self.calls = 0

    def resolve(self, paths):
        self.calls += 1
        resolved = {}
        for path in paths:
            try:
                content = path.read_bytes()
            except OSError:
                continue
            resolved[path] = hashlib.sha256(content).hexdigest()
        return resolved


class _WindowBackend:
    def __init__(
        self,
        windows=(),
        foreground=None,
        *,
        fail=False,
        complete_instances=True,
    ):
        self.windows = tuple(
            replace(
                window,
                thread_id=(
                    window.thread_id
                    if window.thread_id is not None
                    else window.handle + 1000
                ),
                process_lifecycle_token=(
                    window.process_lifecycle_token
                    if window.process_lifecycle_token is not None
                    else (window.process_id or 0) + 100000
                ),
            )
            if complete_instances
            else window
            for window in windows
        )
        self.foreground = foreground
        self.fail = fail

    def list_windows(self):
        if self.fail:
            raise OSError("fault injection")
        return self.windows

    def foreground_handle(self):
        return self.foreground

    def top_window_at(self, _x, _y):
        return None


class _MutatingWindowBackend(_WindowBackend):
    def __init__(self, windows, foreground, mutate):
        super().__init__(windows, foreground)
        self._mutate = mutate
        self._mutated = False

    def list_windows(self):
        windows = super().list_windows()
        if not self._mutated:
            self._mutated = True
            self._mutate()
        return windows


class _MessageBackend:
    def __init__(self):
        self.sent = []
        self.pointers = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout):
        return True

    def send_virtual_key(self, handle, key):
        self.sent.append((handle, key))
        return True

    def send_key_chord(self, handle, keys):
        self.sent.append((handle, keys))
        return True

    def send_pointer(self, handle, x_ratio, y_ratio, event):
        self.pointers.append((handle, x_ratio, y_ratio, event))
        return True


def _configured_group(
    tmp_path,
    *,
    names=("主號", "分號"),
    resolver=None,
):
    shortcuts = []
    for name in names:
        shortcut = tmp_path / f"{name}.lnk"
        shortcut.write_bytes(f"shortcut:{name}".encode("utf-8"))
        shortcuts.append(shortcut)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "測試組",
                        "launch_entries": [
                            {
                                "path": str(shortcut),
                                "role": "主控" if index == 0 else "同步",
                            }
                            for index, shortcut in enumerate(shortcuts)
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    configuration = GroupConfigurationService(
        tmp_path / "groups.json",
        legacy_config_path=legacy,
    )
    scope_service = SyncScopeService(
        configuration,
        resolver or _Resolver(),
    )
    scope = scope_service.scope("測試組")
    return configuration, scope_service, scope


def _complete_windows_for_scope(scope):
    return tuple(
        WindowInfo(
            11 + index,
            "Adobe Flash Player 11",
            True,
            False,
            (index * 900, 0, (index + 1) * 900, 600),
            101 + index,
            "Flash",
            fingerprint,
            1001 + index,
            100001 + index,
        )
        for index, fingerprint in enumerate(scope.fingerprints)
    )


def test_event_bus_deduplicates_unsubscribes_and_isolates_handlers():
    bus = EventBus()
    calls = []

    def broken(_payload):
        raise RuntimeError("fault injection")

    def working(payload):
        calls.append(payload)

    assert bus.subscribe("changed", broken) is True
    assert bus.subscribe("changed", broken) is False
    assert bus.subscribe("changed", working) is True
    bus.publish("changed", 1)
    assert calls == [1]
    assert bus.unsubscribe("changed", broken) is True
    assert bus.unsubscribe("changed", broken) is False
    bus.publish("changed", 2)
    assert calls == [1, 2]


def test_target_window_contract_is_shared_versioned_and_player_safe(tmp_path):
    configuration, scope_service, scope = _configured_group(tmp_path)
    registry = WindowRegistry()
    for entry in configuration.group("測試組").entries:
        registry.register_character(
            entry.entry_id,
            entry.display_name,
            group="測試組",
            role=entry.role,
        )
    windows = (
        WindowInfo(
            11,
            "Adobe Flash Player 11",
            True,
            False,
            (0, 0, 900, 600),
            101,
            "Flash",
            scope.fingerprints[0],
        ),
        WindowInfo(
            12,
            "Adobe Flash Player 11",
            True,
            True,
            (900, 0, 1800, 600),
            102,
            "Flash",
            scope.fingerprints[1],
        ),
    )
    backend = _WindowBackend(windows, foreground=11)
    service = TargetWindowContractService(
        configuration,
        scope_service,
        registry,
        backend,
    )

    snapshot = service.snapshot("測試組")

    assert snapshot.schema_version == 1
    assert tuple(item.phase for item in snapshot.targets) == (
        TargetWindowPhase.FOREGROUND,
        TargetWindowPhase.MINIMIZED,
    )
    assert tuple(item.handle for item in snapshot.targets) == (11, 12)
    public = snapshot.to_public_dict()
    assert all("handle" not in item for item in public["targets"])
    assert all("title" not in item for item in public["targets"])
    assert tuple(window.handle for window in service.windows("測試組")) == (
        11,
        12,
    )


def test_scope_cache_tracks_group_version_and_controller_shortcut_evidence(
    tmp_path,
):
    resolver = _ShortcutContentResolver()
    configuration, scope_service, scope = _configured_group(
        tmp_path,
        names=("主控", "跟隨甲", "跟隨乙"),
        resolver=resolver,
    )
    group_name = configuration.groups()[0].name
    entries = configuration.group(group_name).entries
    entry_ids = tuple(entry.entry_id for entry in entries)
    backend = _WindowBackend(
        _complete_windows_for_scope(scope),
        foreground=11,
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        backend,
    )

    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids
    initial = service.reconnect_targets(group_name)
    assert initial.sync_scope_entry_ids == entry_ids
    assert initial.sync_controller_entry_id == entry_ids[0]
    cached_calls = resolver.calls
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids
    assert resolver.calls == cached_calls

    configuration_content = configuration.path.read_bytes()
    configuration_stat = configuration.path.stat()
    changed_configuration = bytearray(configuration_content)
    whitespace_index = changed_configuration.index(ord("\n"))
    changed_configuration[whitespace_index] = ord(" ")
    configuration.path.write_bytes(changed_configuration)
    os.utime(
        configuration.path,
        ns=(configuration_stat.st_atime_ns, configuration_stat.st_mtime_ns),
    )
    assert configuration.path.stat().st_size == configuration_stat.st_size
    assert configuration.path.stat().st_mtime_ns == configuration_stat.st_mtime_ns
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids
    assert resolver.calls > cached_calls

    controller_path = entries[0].shortcut_path
    original_content = controller_path.read_bytes()
    replacement = tmp_path / "controller-replacement.tmp"
    replacement.write_bytes(original_content)
    cached_calls = resolver.calls
    replacement.replace(controller_path)
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids
    assert resolver.calls > cached_calls

    controller_stat = controller_path.stat()
    changed_controller_content = bytearray(original_content)
    changed_controller_content[-1] = (
        changed_controller_content[-1] + 1
    ) % 256
    controller_path.write_bytes(changed_controller_content)
    os.utime(
        controller_path,
        ns=(controller_stat.st_atime_ns, controller_stat.st_mtime_ns),
    )
    assert controller_path.stat().st_size == controller_stat.st_size
    assert controller_path.stat().st_mtime_ns == controller_stat.st_mtime_ns
    identity_changed = service.reconnect_targets(group_name)
    assert identity_changed.sync_entry_ids == entry_ids[1:]
    assert tuple(window.handle for window in identity_changed.windows) == (12, 13)

    controller_path.write_bytes(original_content)
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids

    case_renamed_path = controller_path.with_suffix(".LNK")
    cached_calls = resolver.calls
    controller_path.rename(case_renamed_path)
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids
    assert resolver.calls > cached_calls
    case_renamed_path.rename(controller_path)
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids

    renamed_path = controller_path.with_name("renamed-controller.lnk")
    controller_path.rename(renamed_path)
    assert service.reconnect_targets(group_name).sync_entry_ids == ()
    renamed_path.rename(controller_path)
    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids

    controller_path.unlink()
    assert service.reconnect_targets(group_name).sync_entry_ids == ()
    fresh_service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        backend,
    )
    assert fresh_service.reconnect_targets(group_name).sync_entry_ids == ()


def test_scope_cache_isolates_one_follower_and_restores_original_slot_order(
    tmp_path,
):
    resolver = _ShortcutContentResolver()
    configuration, scope_service, scope = _configured_group(
        tmp_path,
        names=("主控", "跟隨甲", "跟隨乙"),
        resolver=resolver,
    )
    group_name = configuration.groups()[0].name
    entries = configuration.group(group_name).entries
    entry_ids = tuple(entry.entry_id for entry in entries)
    backend = _WindowBackend(
        _complete_windows_for_scope(scope),
        foreground=11,
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        backend,
    )

    assert service.reconnect_targets(group_name).sync_entry_ids == entry_ids
    follower_path = entries[1].shortcut_path
    follower_content = follower_path.read_bytes()
    follower_path.unlink()

    isolated = service.reconnect_targets(group_name)
    assert isolated.sync_entry_ids == (entry_ids[0], entry_ids[2])
    assert tuple(window.handle for window in isolated.sync_windows) == (11, 13)
    assert isolated.target_failure_evidence == ()
    assert isolated.global_failure_codes == (
        "shortcut_identity_unresolved",
        "unattributed_candidate_window",
    )

    follower_path.write_bytes(follower_content)
    restored = service.reconnect_targets(group_name)
    assert restored.sync_entry_ids == entry_ids
    assert tuple(window.handle for window in restored.sync_windows) == (
        11,
        12,
        13,
    )


def test_reconnect_targets_never_mix_snapshot_with_changed_scope_evidence(
    tmp_path,
):
    resolver = _ShortcutContentResolver()
    configuration, scope_service, scope = _configured_group(
        tmp_path,
        names=("主控", "跟隨甲", "跟隨乙"),
        resolver=resolver,
    )
    group_name = configuration.groups()[0].name
    controller_path = configuration.group(group_name).entries[0].shortcut_path
    original_content = controller_path.read_bytes()
    changed_content = bytearray(original_content)
    changed_content[-1] = (changed_content[-1] + 1) % 256
    backend = _MutatingWindowBackend(
        _complete_windows_for_scope(scope),
        11,
        lambda: controller_path.write_bytes(changed_content),
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        backend,
    )

    resolved = service.reconnect_targets(group_name)

    assert resolved.windows == ()
    assert resolved.sync_windows == ()
    assert resolved.sync_entry_ids == ()
    assert "scope_evidence_changed_during_snapshot" in (
        resolved.global_failure_codes
    )


def test_controller_shortcut_loss_stops_keyboard_and_pointer_target_providers(
    tmp_path,
):
    resolver = _ShortcutContentResolver()
    configuration, scope_service, scope = _configured_group(
        tmp_path,
        names=("主控", "跟隨甲", "跟隨乙"),
        resolver=resolver,
    )
    group_name = configuration.groups()[0].name
    window_backend = _WindowBackend(
        _complete_windows_for_scope(scope),
        foreground=11,
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        window_backend,
    )
    provider = lambda: service.reconnect_targets(group_name).sync_windows
    initial_windows = provider()
    messages = _MessageBackend()
    keyboard = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=window_backend,
        message_backend=messages,
        target_windows_provider=provider,
        require_expected_window_count=False,
    )
    pointer = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=window_backend,
        message_backend=messages,
        target_windows_provider=provider,
        require_expected_window_count=False,
    )
    for controller in (keyboard, pointer):
        controller.set_allowed_window_instances(initial_windows)
        controller.set_controller_fingerprint(
            initial_windows[0].launch_fingerprint
        )

    try:
        assert keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            exclude_foreground=True,
            source_handle=11,
        ).sent_windows == 2
        assert pointer.send_click(
            source_handle=11,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        ).sent_windows == 2

        configuration.group(group_name).entries[0].shortcut_path.unlink()
        window_backend.foreground = 12
        messages.sent.clear()
        messages.pointers.clear()
        assert keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=12,
        ).sent_windows == 0
        assert pointer.send_click(
            source_handle=12,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        ).sent_windows == 0
        assert messages.sent == []
        assert messages.pointers == []
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_target_window_enumeration_failure_fails_closed(tmp_path):
    configuration, scope_service, _scope = _configured_group(tmp_path)
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend(fail=True),
    )

    snapshot = service.snapshot("測試組")

    assert snapshot.safe_targets == ()
    assert "window_enumeration_failed" in snapshot.failure_codes


def test_target_contract_prefers_current_group_for_shared_shortcut(tmp_path):
    shared = tmp_path / "共用角色.lnk"
    shared.write_bytes(b"shortcut")
    legacy = tmp_path / "legacy-shared.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "甲組",
                        "launch_entries": [
                            {
                                "path": str(shared),
                                "role": "主窗口",
                                "role_id": "甲角色",
                            }
                        ],
                    },
                    {
                        "name": "乙組",
                        "launch_entries": [
                            {
                                "path": str(shared),
                                "role": "主窗口",
                                "role_id": "乙角色",
                            }
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    configuration = GroupConfigurationService(
        tmp_path / "shared-groups.json",
        legacy_config_path=legacy,
    )
    scope_service = SyncScopeService(configuration, _Resolver())
    fingerprint = scope_service.scope("甲組").fingerprints[0]
    registry = WindowRegistry()
    entry = configuration.group("甲組").entries[0]
    registry.register_character(
        entry.entry_id,
        "甲角色",
        group="甲組",
        role="主號",
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        registry,
        _WindowBackend(
            (
                WindowInfo(
                    11,
                    "Adobe Flash Player 11",
                    True,
                    False,
                    (0, 0, 900, 600),
                    101,
                    "Flash",
                    fingerprint,
                ),
            ),
            foreground=11,
        ),
    )

    first = service.snapshot("甲組").targets[0]
    second = service.snapshot("乙組").targets[0]

    assert first.group_name == "甲組"
    assert first.role_id == "甲角色"
    assert first.character_id is None
    assert second.group_name == "乙組"
    assert second.role_id == "乙角色"
    assert second.character_id is None
    assert first.window_code != second.window_code


def test_unidentified_flash_window_blocks_every_mutating_target_set(tmp_path):
    configuration, scope_service, scope = _configured_group(tmp_path)
    known = WindowInfo(
        11,
        "Adobe Flash Player 11",
        True,
        False,
        (0, 0, 900, 600),
        101,
        "Flash",
        scope.fingerprints[0],
    )
    unknown = WindowInfo(
        12,
        "Adobe Flash Player 11",
        True,
        False,
        (900, 0, 1800, 600),
        102,
        "Flash",
        None,
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend((known, unknown), foreground=11),
    )

    snapshot = service.snapshot("測試組")

    assert "unidentified_candidate_window" in snapshot.failure_codes
    assert service.windows("測試組") == ()


def test_reconnect_targets_keep_safe_role_when_other_window_is_unidentified(
    tmp_path,
):
    configuration, scope_service, scope = _configured_group(tmp_path)
    group_name = configuration.groups()[0].name
    known = WindowInfo(
        11,
        "Adobe Flash Player 11",
        True,
        False,
        (0, 0, 900, 600),
        101,
        "Flash",
        scope.fingerprints[0],
    )
    unknown = WindowInfo(
        12,
        "Adobe Flash Player 11",
        True,
        False,
        (900, 0, 1800, 600),
        102,
        "Flash",
        None,
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend((known, unknown), foreground=11),
    )

    reconnect_targets = service.reconnect_targets(group_name)

    assert tuple(window.handle for window in reconnect_targets.windows) == (11,)
    assert reconnect_targets.global_failure_codes == (
        "unidentified_candidate_window",
    )
    assert tuple(
        (item.entry_id, item.fingerprint, item.failure_codes)
        for item in reconnect_targets.target_failure_evidence
    ) == ((
        scope.entry_ids[1],
        scope.fingerprints[1],
        ("window_offline",),
    ),)


def test_reconnect_targets_keep_offline_evidence_without_blocking_safe_sibling(
    tmp_path,
):
    configuration, scope_service, scope = _configured_group(tmp_path)
    group_name = configuration.groups()[0].name
    known = WindowInfo(
        11,
        "Adobe Flash Player 11",
        True,
        False,
        (0, 0, 900, 600),
        101,
        "Flash",
        scope.fingerprints[0],
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend((known,), foreground=11),
    )

    reconnect_targets = service.reconnect_targets(group_name)

    assert tuple(window.handle for window in reconnect_targets.windows) == (11,)
    assert tuple(
        (item.entry_id, item.fingerprint, item.failure_codes)
        for item in reconnect_targets.target_failure_evidence
    ) == ((
        scope.entry_ids[1],
        scope.fingerprints[1],
        ("window_offline",),
    ),)
    assert reconnect_targets.global_failure_codes == ()


def test_reconnect_targets_reject_incomplete_instance_before_capture(tmp_path):
    configuration, scope_service, scope = _configured_group(tmp_path)
    group_name = configuration.groups()[0].name
    complete = WindowInfo(
        11,
        "Adobe Flash Player 11",
        True,
        False,
        (0, 0, 900, 600),
        101,
        "Flash",
        scope.fingerprints[0],
        1001,
        100001,
    )
    incomplete_windows = (
        replace(complete, thread_id=None),
        replace(complete, window_class=None),
        replace(complete, process_lifecycle_token=None),
    )

    for incomplete in incomplete_windows:
        service = TargetWindowContractService(
            configuration,
            scope_service,
            WindowRegistry(),
            _WindowBackend(
                (incomplete,),
                foreground=11,
                complete_instances=False,
            ),
        )

        reconnect_targets = service.reconnect_targets(group_name)

        assert reconnect_targets.windows == ()
        assert "window_instance_incomplete" in (
            reconnect_targets.global_failure_codes
        )


def test_reconnect_targets_isolate_duplicate_role_without_hiding_safe_sibling(
    tmp_path,
):
    configuration, scope_service, scope = _configured_group(tmp_path)
    group_name = configuration.groups()[0].name
    windows = (
        WindowInfo(
            11,
            "Adobe Flash Player 11",
            True,
            False,
            (0, 0, 900, 600),
            101,
            "Flash",
            scope.fingerprints[0],
        ),
        WindowInfo(
            12,
            "Adobe Flash Player 11",
            True,
            False,
            (900, 0, 1800, 600),
            102,
            "Flash",
            scope.fingerprints[1],
        ),
        WindowInfo(
            13,
            "Adobe Flash Player 11",
            True,
            False,
            (1800, 0, 2700, 600),
            103,
            "Flash",
            scope.fingerprints[1],
        ),
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend(windows, foreground=11),
    )

    reconnect_targets = service.reconnect_targets(group_name)

    assert tuple(window.handle for window in reconnect_targets.windows) == (11,)
    assert tuple(
        (item.entry_id, item.fingerprint, item.failure_codes)
        for item in reconnect_targets.target_failure_evidence
    ) == ((
        scope.entry_ids[1],
        scope.fingerprints[1],
        ("window_identity_duplicate",),
    ),)


def test_reconnect_targets_bind_shared_launcher_digest_to_confirmed_instances(
    tmp_path,
):
    configuration, _scope_service, _scope = _configured_group(tmp_path)
    group_name = configuration.groups()[0].name
    scope_service = SyncScopeService(
        configuration,
        _SharedFingerprintResolver(),
    )
    entries = configuration.group(group_name).entries
    source_fingerprint = "a" * 64
    windows = (
        WindowInfo(
            11,
            "Adobe Flash Player 11",
            True,
            False,
            (0, 0, 900, 600),
            701,
            "Flash",
            source_fingerprint,
            1701,
            900001,
        ),
        WindowInfo(
            12,
            "Adobe Flash Player 11",
            True,
            False,
            (900, 0, 1800, 600),
            701,
            "Flash",
            source_fingerprint,
            1702,
            900001,
        ),
        WindowInfo(
            13,
            "Adobe Flash Player 11",
            True,
            False,
            (1800, 0, 2700, 600),
            701,
            "Flash",
            source_fingerprint,
            1703,
            900001,
        ),
    )
    registry = WindowRegistry()
    for entry, window in zip(entries, windows[:2]):
        registry.register_character(
            entry.entry_id,
            entry.display_name,
            group=group_name,
            role=entry.role,
        )
        registry.confirm_window(
            entry.entry_id,
            handle=window.handle,
            process_id=window.process_id,
            window_class=window.window_class,
            rect=window.rect,
            health=WindowHealth.READY,
        )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        registry,
        _WindowBackend(windows, foreground=11),
    )

    resolved = service.reconnect_targets(group_name)

    assert tuple(window.handle for window in resolved.windows) == (11, 12)
    assert resolved.sync_entry_ids == tuple(entry.entry_id for entry in entries)
    assert tuple(window.handle for window in resolved.sync_windows) == (11, 12)
    assert len(
        {
            window.launch_fingerprint for window in resolved.sync_windows
        }
    ) == 2
    assert all(
        window.launch_fingerprint != source_fingerprint
        for window in resolved.sync_windows
    )


def test_shared_launcher_incomplete_or_conflicting_instance_isolated(tmp_path):
    configuration, _scope_service, _scope = _configured_group(tmp_path)
    group_name = configuration.groups()[0].name
    scope_service = SyncScopeService(
        configuration,
        _SharedFingerprintResolver(),
    )
    entries = configuration.group(group_name).entries
    source_fingerprint = "a" * 64
    windows = (
        WindowInfo(
            11,
            "Adobe Flash Player 11",
            True,
            False,
            (0, 0, 900, 600),
            701,
            "Flash",
            source_fingerprint,
            1701,
            900001,
        ),
        WindowInfo(
            12,
            "Adobe Flash Player 11",
            True,
            False,
            (900, 0, 1800, 600),
            701,
            "Flash",
            source_fingerprint,
            1702,
            900001,
        ),
    )
    registry = WindowRegistry()
    first = entries[0]
    registry.register_character(
        first.entry_id,
        first.display_name,
        group=group_name,
        role=first.role,
    )
    registry.confirm_window(
        first.entry_id,
        handle=11,
        process_id=701,
        window_class="Flash",
        rect=windows[0].rect,
        health=WindowHealth.READY,
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        registry,
        _WindowBackend(windows, foreground=11),
    )

    isolated = service.reconnect_targets(group_name)

    assert tuple(window.handle for window in isolated.windows) == (11,)
    assert isolated.sync_entry_ids == (first.entry_id,)
    assert isolated.target_failure_evidence == ()
    assert isolated.global_failure_codes == (
        "window_identity_duplicate",
        "unattributed_candidate_window",
    )

    second = entries[1]
    registry.register_character(
        second.entry_id,
        second.display_name,
        group=group_name,
        role=second.role,
    )
    registry.confirm_window(
        second.entry_id,
        handle=11,
        process_id=701,
        window_class="Flash",
        rect=windows[0].rect,
        health=WindowHealth.READY,
    )

    conflicted = service.reconnect_targets(group_name)

    assert conflicted.windows == ()
    assert conflicted.sync_windows == ()
    assert "window_identity_duplicate" in conflicted.global_failure_codes


def test_sync_controller_accepts_only_the_shared_target_provider():
    windows = (
        WindowInfo(
            11,
            "",
            True,
            False,
            (0, 0, 900, 600),
            101,
            None,
            "1" * 64,
        ),
        WindowInfo(
            12,
            "",
            True,
            False,
            (900, 0, 1800, 600),
            102,
            None,
            "2" * 64,
        ),
    )
    messages = _MessageBackend()
    controller = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=_WindowBackend((), foreground=11),
        message_backend=messages,
        allowed_fingerprints=("1" * 64, "2" * 64),
        target_windows_provider=lambda: windows,
    )
    controller.set_controller_fingerprint("1" * 64)

    try:
        result = controller.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            exclude_foreground=True,
            source_handle=11,
        )

        assert result.sent_windows == 1
        assert messages.sent[0][0] == 12
    finally:
        assert controller.close() is True


def test_sync_pointer_uses_its_candidate_set_without_reconnect_test_helper():
    windows = (
        WindowInfo(
            11,
            "",
            True,
            False,
            (0, 0, 900, 600),
            101,
            None,
            "1" * 64,
        ),
        WindowInfo(
            12,
            "",
            True,
            False,
            (900, 0, 1800, 600),
            102,
            None,
            "2" * 64,
        ),
    )
    provider = lambda: windows
    backend = _WindowBackend((), foreground=11)
    keyboard = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=_MessageBackend(),
        target_windows_provider=provider,
    )
    pointer = WindowsPointerSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=object(),
        target_windows_provider=provider,
    )
    try:
        assert keyboard._candidate_windows() == windows
        assert pointer._candidate_windows() == windows
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_shared_operation_gate_blocks_sync_pointer_and_reconnect_mutations():
    fingerprint = "1" * 64
    windows = (
        WindowInfo(
            11,
            "",
            True,
            False,
            (0, 0, 900, 600),
            101,
            None,
            fingerprint,
        ),
    )
    gate = GameOperationGate()
    assert gate.close_and_wait() is True
    messages = _MessageBackend()
    keyboard = WindowsInputSyncController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=_WindowBackend(windows, foreground=11),
        message_backend=messages,
        allowed_fingerprints=(fingerprint,),
        target_windows_provider=lambda: windows,
        operation_gate=gate,
    )
    keyboard.set_controller_fingerprint(fingerprint)
    pointer = WindowsPointerSyncController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=_WindowBackend(windows, foreground=11),
        message_backend=object(),
        target_windows_provider=lambda: windows,
        operation_gate=gate,
    )
    reconnect = WindowsSmartReconnectController(
        expected_windows=1,
        title_keywords=("Adobe Flash Player",),
        window_backend=_WindowBackend(windows, foreground=11),
        capture_provider=object(),
        recognizer=object(),
        mouse_backend=object(),
        target_windows_provider=lambda: windows,
        operation_gate=gate,
        execution_enabled=True,
    )
    try:
        key_result = keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=11,
        )
        pointer_result = pointer.send_click(
            source_handle=11,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
        )
        reconnect_result = reconnect.reconnect()

        assert key_result.failure_codes == ("operation_gate_closed",)
        assert pointer_result.failure_codes == ("operation_gate_closed",)
        assert reconnect_result.code == "reconnect.operation_paused"
        assert messages.sent == []
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_foreground_monitor_accepts_only_shared_group_targets():
    current = {"handle": 12}
    backend = Win32KeyboardStateBackend(
        foreground_handle_provider=lambda: current["handle"],
        target_handles_provider=lambda: (11,),
    )

    assert backend.foreground_game_handle() is None
    current["handle"] = 11
    assert backend.foreground_game_handle() == 11


def test_group_choice_exposes_real_members_without_paths(tmp_path):
    configuration, _scope_service, _scope = _configured_group(tmp_path)
    registry = WindowRegistry()
    for entry in configuration.group("測試組").entries:
        registry.register_character(
            entry.entry_id,
            entry.display_name,
            group="測試組",
            role=entry.role,
        )
    service = GroupSelectionService(
        registry,
        configuration=configuration,
    )

    choice = service.find("測試組")

    assert choice.character_count == 2
    assert tuple(item.display_name for item in choice.members) == (
        "主號",
        "分號",
    )
    assert all(item.character_id for item in choice.members)
    assert "shortcut_path" not in json.dumps(
        choice.to_public_dict(),
        ensure_ascii=False,
    )


def test_card_listener_failure_is_recoverable_by_full_resync():
    failures = []
    service = CardService(listener_error_callback=failures.append)
    working_calls = []

    def broken():
        raise RuntimeError("fault injection")

    service.subscribe(broken)
    service.subscribe(lambda: working_calls.append(service.snapshot().revision))
    service.upsert(
        GroupCard(
            card_id="one",
            group=CharacterGroup("group", "測試組"),
            activity=ActivityDefinition(
                "activity",
                "測試",
                ActivityType.DAILY,
                ResetRule.DAILY_MIDNIGHT,
            ),
            current_progress="測試",
            priority_reason=CardPriorityReason.PREFERENCE,
        )
    )

    assert failures
    assert working_calls == [1]
    assert service.snapshot().schema_version == 1
    assert service.resync() == 1
    assert working_calls == [1, 1]


def test_lifecycle_contract_never_reports_a_failed_stop_as_stopped():
    class Service:
        running = True

        def start(self):
            return False

        def stop(self):
            return False

    service = Service()
    assert start_service(service).success is True
    stopped = stop_service(service)
    assert stopped.success is False
    assert stopped.running is True
    assert stopped.code == "lifecycle.stop_failed"


def test_lifecycle_contract_treats_an_already_stopped_service_as_stopped():
    class Service:
        running = False

        def stop(self):
            return False

    stopped = stop_service(Service())

    assert stopped.success is True
    assert stopped.running is False
    assert stopped.code == "lifecycle.stopped"


def test_data_contract_versions_are_normalized_and_reject_future(tmp_path):
    config = ConfigManager(tmp_path / "settings.json")
    service = DataContractMigrationService(config)
    assert config.get(service.SETTINGS_KEY) == service.CURRENT_VERSIONS
    assert service.CURRENT_VERSIONS["reconnect"] == (
        ReconnectRuntimeStateStore.VERSION
    )
    try:
        service.verify_supported_versions(
            {
                **service.CURRENT_VERSIONS,
                "reconnect": 3,
            }
        )
    except RuntimeError as error:
        assert "reconnect" in str(error)
    else:
        raise AssertionError("version drift must be rejected")

    future_config = ConfigManager(tmp_path / "future-settings.json")
    future_config.set(
        service.SETTINGS_KEY,
        {
            **service.CURRENT_VERSIONS,
            "cards": service.CURRENT_VERSIONS["cards"] + 1,
        },
    )
    try:
        DataContractMigrationService(future_config)
    except ValueError as error:
        assert "cards" in str(error)
    else:
        raise AssertionError("future data must be rejected")


def test_oldest_reconnect_contract_migrates_to_safe_current_state(tmp_path):
    state_path = tmp_path / "reconnect.json"
    fingerprint = "1" * 64
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "pending_fingerprints": [fingerprint],
                "active_fingerprints": [fingerprint],
            }
        ),
        encoding="utf-8",
    )

    state = ReconnectRuntimeStateStore(state_path).load()

    assert state.pending_fingerprints == set()
    assert state.active_fingerprints == set()
    assert json.loads(
        state_path.read_text(encoding="utf-8")
    )["version"] == ReconnectRuntimeStateStore.VERSION
