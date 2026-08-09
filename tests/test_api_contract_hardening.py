import hashlib
import json
from dataclasses import replace
from pathlib import Path

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
from core.window_registry import WindowRegistry
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.data_contract_migration_service import (
    DataContractMigrationService,
)
from services.event_bus import EventBus
from services.group_configuration_service import (
    GroupConfigurationService as _GroupConfigurationService,
)
from services.game_operation_gate import GameOperationGate
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)
from services.group_selection_service import GroupSelectionService
from services.keyboard_sync_monitor import Win32KeyboardStateBackend
from services.lifecycle_contract import (
    cancel_service,
    join_service,
    start_service,
    stop_service,
)
from services.smart_reconnect_authorization_coordinator import (
    ReconnectAuthorizationState,
    SmartReconnectAuthorizationCoordinator,
)
from services.sync_scope_service import SyncScopeService
from services.target_window_contract_service import (
    TargetWindowContractService,
)


class GroupConfigurationService(_GroupConfigurationService):
    def __init__(self, path, *, legacy_config_path=None):
        super().__init__(
            path,
            IdentityDataTransactionCoordinator(),
            legacy_config_path=legacy_config_path,
        )


class _Resolver:
    def resolve(self, paths):
        return {
            path: hashlib.sha256(str(path).encode()).hexdigest()
            for path in paths
        }


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


class _MessageBackend:
    def __init__(self):
        self.sent = []

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


def _configured_group(tmp_path):
    shortcuts = []
    for name in ("主號", "分號"):
        shortcut = tmp_path / f"{name}.lnk"
        shortcut.write_bytes(b"shortcut")
        shortcuts.append(shortcut)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "測試組",
                        "launch_entries": [
                            {"path": str(shortcuts[0]), "role": "主控"},
                            {"path": str(shortcuts[1]), "role": "同步"},
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
    resolver = _Resolver()
    scope_service = SyncScopeService(configuration, resolver)
    scope = scope_service.scope("測試組")
    return configuration, scope_service, scope


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
    assert reconnect_targets.failure_codes == (
        "unidentified_candidate_window",
        "window_offline",
    )


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
    assert reconnect_targets.failure_codes == ("window_offline",)
    assert reconnect_targets.blocked_fingerprints == frozenset()


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
            reconnect_targets.failure_codes
        )
        assert reconnect_targets.blocked_fingerprints == frozenset(
            {scope.fingerprints[0]}
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
    assert reconnect_targets.failure_codes == ("window_identity_duplicate",)
    assert reconnect_targets.blocked_fingerprints == frozenset(
        {scope.fingerprints[1]}
    )


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


def test_sync_pointer_and_reconnect_receive_the_same_target_set():
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
    reconnect = WindowsSmartReconnectController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        capture_provider=object(),
        recognizer=object(),
        mouse_backend=object(),
        target_windows_provider=provider,
    )
    try:
        assert keyboard._candidate_windows() == windows
        assert pointer._candidate_windows() == windows
        assert reconnect._candidate_windows() == windows
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
        legacy_config_path=configuration.path,
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


def test_lifecycle_contract_keeps_cancel_and_join_failures_explicit():
    class Service:
        running = True

        def cancel(self):
            return False

        def join(self):
            return False

    service = Service()

    assert cancel_service(service).code == "lifecycle.cancel_failed"
    joined = join_service(service)
    assert joined.success is False
    assert joined.running is True
    assert joined.code == "lifecycle.join_failed"


def test_data_contract_migration_is_sequential_and_rejects_future(tmp_path):
    config = ConfigManager(tmp_path / "settings.json")
    service = DataContractMigrationService(config)
    assert service.state.component_versions["reconnect"] == (
        ReconnectRuntimeStateStore.VERSION
    )
    migrated = service.migrate_component(
        "reconnect",
        {"version": 1, "value": "kept"},
        migrations={
            1: lambda payload: {**payload, "version": 2},
            2: lambda payload: {**payload, "version": 3},
            3: lambda payload: {**payload, "version": 4},
            4: lambda payload: {**payload, "version": 5},
            5: lambda payload: {**payload, "version": 6},
            6: lambda payload: {**payload, "version": 7},
        },
        version_key="version",
    )
    assert migrated == {
        "version": ReconnectRuntimeStateStore.VERSION,
        "value": "kept",
    }
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
    try:
        service.migrate_component(
            "cards",
            {"schema_version": 2},
            migrations={},
        )
    except ValueError as error:
        assert "newer" in str(error)
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


def _many_group_configuration(tmp_path, group_sizes):
    groups = []
    for group_index, size in enumerate(group_sizes, start=1):
        entries = []
        for item_index in range(1, size + 1):
            shortcut = (
                tmp_path
                / f"group-{group_index}-role-{item_index}.lnk"
            )
            shortcut.write_bytes(
                f"shortcut-{group_index}-{item_index}".encode()
            )
            entries.append(
                {
                    "path": str(shortcut),
                    "role": "主要" if item_index == 1 else "次要",
                    "role_id": f"角色-{group_index}-{item_index}",
                }
            )
        groups.append(
            {
                "name": f"組別-{group_index}",
                "launch_entries": entries,
            }
        )
    legacy = tmp_path / "many-groups-legacy.json"
    legacy.write_text(
        json.dumps({"groups": groups}, ensure_ascii=False),
        encoding="utf-8",
    )
    configuration = GroupConfigurationService(
        tmp_path / "many-groups.json",
        legacy_config_path=legacy,
    )
    return configuration, SyncScopeService(configuration, _Resolver())


def _actual_window(index, fingerprint, **changes):
    values = {
        "handle": 100 + index,
        "title": f"Adobe Flash Player {index}",
        "visible": True,
        "minimized": False,
        "rect": (index * 10, 0, index * 10 + 900, 600),
        "process_id": 1000 + index,
        "window_class": "Flash",
        "launch_fingerprint": fingerprint,
        "thread_id": 2000 + index,
        "process_lifecycle_token": 3000 + index,
    }
    values.update(changes)
    return WindowInfo(**values)


def test_actual_window_source_ignores_nine_closed_members_of_group_fourteen(
    tmp_path,
):
    configuration, scope_service = _many_group_configuration(tmp_path, (14,))
    group = configuration.groups()[0]
    scope = scope_service.scope(group.name)
    windows = tuple(
        _actual_window(index, fingerprint)
        for index, fingerprint in enumerate(scope.fingerprints[:5], start=1)
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend(windows, complete_instances=False),
    )

    snapshot = service.actual_snapshot()
    reconnect_targets = service.actual_reconnect_targets()

    assert len(group.entries) == 14
    assert tuple(target.fingerprint for target in snapshot.targets) == (
        scope.fingerprints[:5]
    )
    assert tuple(window.launch_fingerprint for window in reconnect_targets.windows) == (
        scope.fingerprints[:5]
    )
    assert snapshot.failure_codes == ()
    assert reconnect_targets.actual_window_snapshot is True


def test_actual_window_source_monitors_twelve_windows_across_three_groups(
    tmp_path,
):
    configuration, scope_service = _many_group_configuration(
        tmp_path,
        (4, 4, 4),
    )
    fingerprints = tuple(
        fingerprint
        for group in configuration.groups()
        for fingerprint in scope_service.scope(group.name).fingerprints
    )
    windows = tuple(
        _actual_window(index, fingerprint)
        for index, fingerprint in enumerate(fingerprints, start=1)
    )
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend(windows, complete_instances=False),
    )

    snapshot = service.actual_snapshot()

    assert len(configuration.groups()) == 3
    assert len(snapshot.targets) == 12
    assert frozenset(target.fingerprint for target in snapshot.targets) == (
        frozenset(fingerprints)
    )
    assert snapshot.blocked_fingerprints == frozenset()


def test_actual_window_source_locally_isolates_anonymous_incomplete_and_duplicate(
    tmp_path,
):
    configuration, scope_service = _many_group_configuration(tmp_path, (6,))
    fingerprints = scope_service.scope(configuration.groups()[0].name).fingerprints
    safe_first = _actual_window(1, fingerprints[0])
    collision_complete = _actual_window(2, fingerprints[1])
    collision_incomplete = _actual_window(
        3,
        fingerprints[2],
        handle=collision_complete.handle,
        thread_id=None,
    )
    duplicate_first = _actual_window(4, fingerprints[3])
    duplicate_second = _actual_window(5, fingerprints[3])
    anonymous = _actual_window(6, None)
    safe_second = _actual_window(7, fingerprints[4])
    service = TargetWindowContractService(
        configuration,
        scope_service,
        WindowRegistry(),
        _WindowBackend(
            (
                safe_first,
                collision_complete,
                collision_incomplete,
                duplicate_first,
                duplicate_second,
                anonymous,
                safe_second,
            ),
            complete_instances=False,
        ),
    )

    snapshot = service.actual_snapshot()
    reconnect_targets = service.actual_reconnect_targets()

    assert tuple(target.fingerprint for target in snapshot.targets) == (
        fingerprints[0],
        fingerprints[4],
    )
    assert snapshot.blocked_fingerprints == frozenset(
        (fingerprints[1], fingerprints[2], fingerprints[3])
    )
    assert snapshot.anonymous_isolated_window_count == 1
    assert snapshot.isolated_window_count == 5
    assert tuple(window.handle for window in reconnect_targets.windows) == (
        safe_first.handle,
        safe_second.handle,
    )
    assert reconnect_targets.failure_codes == ()


def test_main_wires_one_shared_actual_window_authority_and_final_stop(
    monkeypatch,
):
    import main

    source = (Path(__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert source.count("SmartReconnectAuthorizationCoordinator()") == 1
    assert "register_before_write_listener" not in source
    assert "target_window_contract_service.actual_reconnect_targets" in source
    assert "identity_data_transaction_coordinator.snapshot_with_generation" in source
    assert "observed_identity_alias_catalog" in source

    coordinator = SmartReconnectAuthorizationCoordinator()

    class Monitor:
        def stop(self):
            return True

    def registered(service_type):
        if service_type is main.SmartReconnectMonitor:
            return Monitor()
        if service_type is main.SmartReconnectAuthorizationCoordinator:
            return coordinator
        return None

    monkeypatch.setattr(main.AppContext, "get", registered)

    main.shutdown_smart_reconnect_monitor()

    assert coordinator.current_authorization() is None
    assert coordinator.state is ReconnectAuthorizationState.STOPPED
