import json
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from adapters.windows_window import (
    WindowInfo,
    monitored_window_instance_fingerprint,
)
from config.config_manager import ConfigManager
from core.reconnect_policy import ReconnectScreenState
from core.sp1_boundaries import SmartReconnectBoundary
from core.window_registry import CharacterWindowRecord
from domain.character import Character, CharacterImportance
from main import (
    ConnectedSyncTargetContractProvider,
    GAME_TIME_AUTO_UPDATE_KEY,
    GAME_TIME_OFFSET_MS_KEY,
    INPUT_POLICY_KEY,
    SMART_RECONNECT_ENABLED_KEY,
    SMART_RECONNECT_CONSENT_KEY,
    SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY,
    SMART_RECONNECT_INTERVAL_MS_KEY,
    SMART_RECONNECT_INTERVAL_MIGRATION_KEY,
    SMART_RECONNECT_MODE_KEY,
    SYNC_KEYS_COLLAPSED_KEY,
    TIMED_CLICK_SETTINGS_KEY,
    UI_THEME_CLASSIC_GOLD_MIGRATION_KEY,
    UI_THEME_KEY,
    _connected_sync_fingerprints,
    build_configured_reconnect_plan,
    build_services,
    apply_auto_battle_after_game_launch,
    apply_smart_reconnect_auto_battle_setting,
    apply_smart_reconnect_snapshot_transition,
    normalize_smart_reconnect_auto_battle_enabled,
    resolve_connected_sync_target_contract,
    resolve_complete_sync_instance_windows,
    resolve_registered_reconnect_roles,
    group_role_action_started_game,
    group_window_launch_started_game,
    stop_input_sync_pair,
)
from services.app_context import AppContext
from services.deferred_sync_operation_service import (
    DeferredSyncOperationService,
)
from services.target_window_contract_service import (
    ResolvedTargetWindows,
    TargetWindowContractService,
)
from services.smart_reconnect_monitor import (
    SMART_RECONNECT_MODE_HIGH_PERFORMANCE,
    SmartReconnectMonitor,
)
from services.ungrouped_window_service import UngroupedWindowService
from services.smart_reconnect_capture_settings_service import (
    SMART_RECONNECT_CAPTURE_MODES_KEY,
    SmartReconnectCaptureSettings,
    SmartReconnectCaptureSettingsService,
)
from services.group_role_status_service import (
    GroupRoleActionResult,
    GroupRoleStatusService,
)
from services.group_window_launch_service import GroupWindowLaunchResult
from services.game_operation_gate import GameOperationGate


class _SyncWindow:
    def __init__(self, fingerprint: str) -> None:
        self.launch_fingerprint = fingerprint


class _InstanceWindowBackend:
    def __init__(self, windows, foreground=1) -> None:
        self.windows = list(windows)
        self.foreground = foreground

    def list_windows(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.foreground


class _InstanceMessages:
    def __init__(self) -> None:
        self.keys = []
        self.pointers = []

    def is_window(self, _handle):
        return True

    def probe_responsive(self, _handle, _timeout):
        return True

    def send_virtual_key(self, handle, key):
        self.keys.append((handle, key))
        return True

    def send_key_chord(self, handle, keys):
        self.keys.append((handle, keys))
        return True

    def send_pointer(self, handle, x_ratio, y_ratio, event):
        self.pointers.append((handle, x_ratio, y_ratio, event))
        return True


def _shared_launcher_sync_windows(count: int):
    source = "a" * 64
    raw = tuple(
        WindowInfo(
            handle=index,
            title="Adobe Flash Player 11",
            visible=True,
            minimized=False,
            rect=((index - 1) * 100, 0, index * 100, 100),
            process_id=777,
            window_class="Flash",
            launch_fingerprint=source,
            thread_id=1700 + index,
            process_lifecycle_token=900001,
        )
        for index in range(1, count + 1)
    )
    synced = tuple(
        replace(
            window,
            launch_fingerprint=monitored_window_instance_fingerprint(window),
        )
        for window in raw
    )
    assert all(window.launch_fingerprint is not None for window in synced)
    return raw, synced


def test_registered_reconnect_roles_cross_check_all_confirmed_primary_records():
    names = (
        "120古",
        "120靈",
        "120射",
        "120福",
        "120獵",
        "亞洛",
        "160帥",
        "大排",
        "和尚",
        "餐廳",
    )
    characters = tuple(
        Character(
            f"id-{index}",
            name,
            120 if name.startswith("120") else 160,
            CharacterImportance.PRIMARY,
        )
        for index, name in enumerate(names)
    )
    registry = tuple(
        CharacterWindowRecord(
            character.character_id,
            character.display_name,
            role="主號",
        )
        for character in characters
    )

    result = resolve_registered_reconnect_roles(
        characters,
        registry,
        (),
    )

    assert {item.role_id for item in result} == set(names)
    assert all(
        item.importance is CharacterImportance.PRIMARY
        for item in result
    )


def test_registered_primary_requires_character_and_registry_identity_agreement():
    characters = (
        Character(
            "id-fu",
            "120福",
            120,
            CharacterImportance.PRIMARY,
        ),
    )
    mismatched = (
        CharacterWindowRecord("id-fu", "120古", role="主號"),
    )

    assert resolve_registered_reconnect_roles(
        characters,
        mismatched,
        (),
    ) == ()


def test_partial_connected_sync_requires_controller_and_keeps_scope_order():
    first = "a" * 64
    second = "b" * 64
    third = "c" * 64
    scope = (first, second, third)

    assert _connected_sync_fingerprints(
        scope,
        (_SyncWindow(third), _SyncWindow(first)),
    ) == (first, third)
    assert _connected_sync_fingerprints(
        scope,
        (_SyncWindow(second),),
    ) == ()
    assert _connected_sync_fingerprints(scope, ()) == ()
    assert _connected_sync_fingerprints(
        scope,
        (_SyncWindow(first), _SyncWindow(first), _SyncWindow(third)),
    ) == ()


def test_complete_instance_scope_preserves_shared_launcher_windows_and_rejects_conflicts():
    _raw, synced = _shared_launcher_sync_windows(15)
    entry_ids = tuple(f"entry-{index}" for index in range(15))

    resolved = resolve_complete_sync_instance_windows(
        entry_ids,
        entry_ids,
        synced,
        controller_entry_id=entry_ids[0],
    )

    assert tuple(window.handle for window in resolved) == tuple(range(1, 16))
    assert len({window.launch_fingerprint for window in resolved}) == 15
    assert resolve_complete_sync_instance_windows(
        entry_ids,
        entry_ids,
        (synced[0], synced[0], *synced[2:]),
        controller_entry_id=entry_ids[0],
    ) == ()
    assert resolve_complete_sync_instance_windows(
        entry_ids,
        (entry_ids[0], entry_ids[0], *entry_ids[2:]),
        synced,
        controller_entry_id=entry_ids[0],
    ) == ()


def test_partial_instance_scope_requires_controller_and_restores_slot_order():
    _raw, synced = _shared_launcher_sync_windows(3)
    entry_ids = ("controller", "follower-a", "follower-b")

    isolated = resolve_complete_sync_instance_windows(
        entry_ids,
        (entry_ids[2], entry_ids[0]),
        (synced[2], synced[0]),
        controller_entry_id=entry_ids[0],
    )
    assert tuple(window.handle for window in isolated) == (1, 3)

    assert resolve_complete_sync_instance_windows(
        entry_ids,
        entry_ids[1:],
        synced[1:],
        controller_entry_id=entry_ids[0],
    ) == ()

    restored = resolve_complete_sync_instance_windows(
        entry_ids,
        (entry_ids[2], entry_ids[0], entry_ids[1]),
        (synced[2], synced[0], synced[1]),
        controller_entry_id=entry_ids[0],
    )
    assert tuple(window.handle for window in restored) == (1, 2, 3)


def test_complete_instance_scope_rejects_stale_controller_contract():
    _raw, synced = _shared_launcher_sync_windows(2)
    old_scope = ("old-controller", "new-controller")
    new_scope = ("new-controller", "old-controller")
    new_resolution = (synced[1], synced[0])

    assert resolve_complete_sync_instance_windows(
        old_scope,
        new_scope,
        new_resolution,
        controller_entry_id="new-controller",
    ) == ()
    assert tuple(
        window.handle
        for window in resolve_complete_sync_instance_windows(
            new_scope,
            new_scope,
            new_resolution,
            controller_entry_id="new-controller",
        )
    ) == (2, 1)


def test_group_identity_uses_one_resolved_scope_contract():
    source = Path("main.py").read_text(encoding="utf-8")
    apply_group_source = source[
        source.index("    def apply_group_identity("):
        source.index("    sync_connected_fingerprints:")
    ]
    apply_connected_source = source[
        source.index("    def apply_connected_sync_identity("):
        source.index("    def group_identity_failure_message(")
    ]

    for function_source in (apply_group_source, apply_connected_source):
        assert "sync_scope_service.scope(choice.name)" not in function_source
        assert "resolved_targets.sync_scope_entry_ids" in function_source
        assert "resolved_targets.sync_controller_entry_id" in function_source
    assert "current_sync_target_windows()" not in apply_connected_source
    assert "target_window_contract_service.reconnect_targets(" not in (
        apply_connected_source
    )
    assert apply_connected_source.count(
        "resolve_connected_sync_target_contract("
    ) == 1


def test_connected_sync_contract_switches_controller_between_operations_only():
    raw, synced = _shared_launcher_sync_windows(2)
    old_contract = ResolvedTargetWindows(
        windows=raw,
        sync_windows=synced,
        sync_entry_ids=("controller-a", "controller-b"),
        sync_scope_entry_ids=("controller-a", "controller-b"),
        sync_controller_entry_id="controller-a",
    )
    new_contract = ResolvedTargetWindows(
        windows=(raw[1], raw[0]),
        sync_windows=(synced[1], synced[0]),
        sync_entry_ids=("controller-b", "controller-a"),
        sync_scope_entry_ids=("controller-b", "controller-a"),
        sync_controller_entry_id="controller-b",
    )

    class ContractService:
        def __init__(self):
            self.calls = 0

        def reconnect_targets(self, _group_name):
            result = (old_contract, new_contract)[min(self.calls, 1)]
            self.calls += 1
            return result

    service = ContractService()

    first_contract, first_connected = resolve_connected_sync_target_contract(
        service,
        "group",
    )
    assert service.calls == 1
    assert first_contract.sync_controller_entry_id == "controller-a"
    assert tuple(window.handle for window in first_connected) == (1, 2)
    assert tuple(
        window.handle
        for window in resolve_complete_sync_instance_windows(
            first_contract.sync_scope_entry_ids,
            first_contract.sync_entry_ids,
            first_contract.sync_windows,
            controller_entry_id=first_contract.sync_controller_entry_id,
        )
    ) == (1, 2)

    second_contract, second_connected = resolve_connected_sync_target_contract(
        service,
        "group",
    )
    assert service.calls == 2
    assert second_contract.sync_controller_entry_id == "controller-b"
    assert tuple(window.handle for window in second_connected) == (2, 1)
    assert tuple(
        window.handle
        for window in resolve_complete_sync_instance_windows(
            second_contract.sync_scope_entry_ids,
            second_contract.sync_entry_ids,
            second_contract.sync_windows,
            controller_entry_id=second_contract.sync_controller_entry_id,
        )
    ) == (2, 1)


def test_keyboard_and_pointer_reject_a_controller_switch_on_next_operation():
    raw, synced = _shared_launcher_sync_windows(2)
    old_contract = ResolvedTargetWindows(
        windows=raw,
        sync_windows=synced,
        sync_entry_ids=("controller-a", "controller-b"),
        sync_scope_entry_ids=("controller-a", "controller-b"),
        sync_controller_entry_id="controller-a",
    )
    new_contract = ResolvedTargetWindows(
        windows=(raw[1], raw[0]),
        sync_windows=(synced[1], synced[0]),
        sync_entry_ids=("controller-b", "controller-a"),
        sync_scope_entry_ids=("controller-b", "controller-a"),
        sync_controller_entry_id="controller-b",
    )

    class ContractService:
        def __init__(self):
            self.calls = 0

        def reconnect_targets(self, _group_name):
            result = (old_contract, new_contract)[min(self.calls, 1)]
            self.calls += 1
            return result

    for controller_type in (
        WindowsInputSyncController,
        WindowsPointerSyncController,
    ):
        service = ContractService()
        provider = ConnectedSyncTargetContractProvider(
            service,
            lambda: "group",
        )
        backend = _InstanceWindowBackend(synced)
        messages = _InstanceMessages()
        controller = controller_type(
            expected_windows=2,
            title_keywords=("Adobe Flash Player",),
            window_backend=backend,
            message_backend=messages,
            target_windows_provider=provider.windows,
            require_expected_window_count=False,
        )
        try:
            controller.set_allowed_window_instances(synced)
            controller.set_controller_fingerprint(
                synced[0].launch_fingerprint
            )
            if controller_type is WindowsInputSyncController:
                first = controller.send_approved_key(
                    "B",
                    policy=WindowInputPolicy.ALL,
                    execute=True,
                    source_handle=1,
                )
                messages.keys.clear()
                second = controller.send_approved_key(
                    "B",
                    policy=WindowInputPolicy.ALL,
                    execute=True,
                    source_handle=1,
                )
                assert first.sent_windows == 2
                assert second.sent_windows == 0
                assert messages.keys == []
            else:
                first = controller.send_click(
                    source_handle=1,
                    x_ratio=0.5,
                    y_ratio=0.5,
                    policy=WindowInputPolicy.ALL,
                    execute=True,
                    include_source=False,
                )
                messages.pointers.clear()
                second = controller.send_click(
                    source_handle=1,
                    x_ratio=0.5,
                    y_ratio=0.5,
                    policy=WindowInputPolicy.ALL,
                    execute=True,
                    include_source=False,
                )
                assert first.sent_windows == 1
                assert second.sent_windows == 0
                assert messages.pointers == []
            assert service.calls == 2
        finally:
            assert controller.close() is True


def test_unknown_reconnect_screen_does_not_block_basic_keyboard_and_pointer_delivery():
    raw, synced = _shared_launcher_sync_windows(3)
    contract = ResolvedTargetWindows(
        windows=raw,
        sync_windows=synced,
        sync_entry_ids=("controller", "follower-a", "follower-b"),
        sync_scope_entry_ids=("controller", "follower-a", "follower-b"),
        sync_controller_entry_id="controller",
    )

    class ContractService:
        def reconnect_targets(self, _group_name):
            return contract

    class UnknownScreenObserver:
        def __init__(self):
            self.calls = 0

        def observe_window_instance_states(self, _windows):
            self.calls += 1
            return {
                window.launch_fingerprint: ReconnectScreenState.UNKNOWN
                for window in synced
            }

    observer = UnknownScreenObserver()
    provider = ConnectedSyncTargetContractProvider(
        ContractService(),
        lambda: "group",
    )
    backend = _InstanceWindowBackend(synced)
    messages = _InstanceMessages()
    keyboard = WindowsInputSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        target_windows_provider=provider.windows,
        require_expected_window_count=False,
    )
    pointer = WindowsPointerSyncController(
        expected_windows=3,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        target_windows_provider=provider.windows,
        require_expected_window_count=False,
    )
    try:
        for controller in (keyboard, pointer):
            controller.set_allowed_window_instances(synced)
            controller.set_controller_fingerprint(
                synced[0].launch_fingerprint
            )

        assert observer.observe_window_instance_states(raw) == {
            window.launch_fingerprint: ReconnectScreenState.UNKNOWN
            for window in synced
        }
        assert observer.calls == 1
        key_result = keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=1,
        )
        pointer_result = pointer.send_click(
            source_handle=1,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        )

        assert key_result.sent_windows == 3
        assert [handle for handle, _key in messages.keys] == [1, 2, 3]
        assert pointer_result.sent_windows == 2
        assert [item[0] for item in messages.pointers] == [2, 2, 3, 3]
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_deferred_keyboard_and_pointer_use_separate_connected_screen_gate():
    raw, synced = _shared_launcher_sync_windows(2)
    contract = ResolvedTargetWindows(
        windows=raw,
        sync_windows=synced,
        sync_entry_ids=("controller", "follower"),
        sync_scope_entry_ids=("controller", "follower"),
        sync_controller_entry_id="controller",
    )

    class ContractService:
        def reconnect_targets(self, _group_name):
            return contract

    provider = ConnectedSyncTargetContractProvider(
        ContractService(),
        lambda: "group",
    )
    states = {
        window.launch_fingerprint: ReconnectScreenState.UNKNOWN
        for window in synced
    }
    reconnecting: set[str] = set()
    deferred_failures = []
    deferred = DeferredSyncOperationService(
        on_failure=deferred_failures.append,
    )
    backend = _InstanceWindowBackend(synced)
    messages = _InstanceMessages()
    keyboard = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: tuple(reconnecting),
        deferred_screen_state_provider=states.get,
        target_windows_provider=provider.windows,
        require_expected_window_count=False,
    )
    pointer = WindowsPointerSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        deferred_service=deferred,
        reconnecting_provider=lambda: tuple(reconnecting),
        deferred_screen_state_provider=states.get,
        target_windows_provider=provider.windows,
        require_expected_window_count=False,
    )

    def send_pair():
        key_result = keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=1,
        )
        pointer_result = pointer.send_click(
            source_handle=1,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        )
        return key_result, pointer_result

    def wait_until_drained() -> None:
        deadline = monotonic() + 2.0
        while deferred.pending() and monotonic() < deadline:
            sleep(0.01)
        assert deferred.pending() == 0

    try:
        for controller in (keyboard, pointer):
            controller.set_allowed_window_instances(synced)
            controller.set_controller_fingerprint(
                synced[0].launch_fingerprint
            )

        immediate_key, immediate_pointer = send_pair()
        assert immediate_key.sent_windows == 2
        assert immediate_pointer.sent_windows == 1
        assert sorted(handle for handle, _key in messages.keys) == [1, 2]
        assert [item[0] for item in messages.pointers] == [2, 2]
        assert deferred.pending() == 0

        messages.keys.clear()
        messages.pointers.clear()
        reconnecting.add(synced[1].launch_fingerprint)
        deferred_key, deferred_pointer = send_pair()
        assert deferred_key.sent_windows == 0
        assert deferred_pointer.sent_windows == 0
        assert messages.keys == []
        assert messages.pointers == []
        assert deferred.pending() == 3

        reconnecting.clear()
        states.update(
            {
                fingerprint: ReconnectScreenState.CONNECTED
                for fingerprint in states
            }
        )
        deferred.process_ready(
            reconnecting_targets=(),
            failed_targets=(),
            ready_targets=tuple(states),
        )
        wait_until_drained()
        assert sorted(handle for handle, _key in messages.keys) == [1, 2]
        assert [item[0] for item in messages.pointers] == [2, 2]
        assert deferred_failures == []

        messages.keys.clear()
        messages.pointers.clear()
        states.update(
            {
                fingerprint: ReconnectScreenState.UNKNOWN
                for fingerprint in states
            }
        )
        reconnecting.add(synced[1].launch_fingerprint)
        send_pair()
        assert deferred.pending() == 3
        reconnecting.clear()
        deferred.process_ready(
            reconnecting_targets=(),
            failed_targets=(),
            ready_targets=tuple(states),
        )
        wait_until_drained()
        assert messages.keys == []
        assert messages.pointers == []
        assert {
            failure.failure_code for failure in deferred_failures
        } >= {"operation_screen_not_safe"}
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_missing_controller_contract_stops_keyboard_and_pointer_without_promotion():
    raw, synced = _shared_launcher_sync_windows(2)
    missing_controller = ResolvedTargetWindows(
        windows=(raw[1],),
        sync_windows=(),
        sync_entry_ids=(),
        sync_scope_entry_ids=("controller", "follower"),
        sync_controller_entry_id="controller",
    )

    class ContractService:
        def reconnect_targets(self, _group_name):
            return missing_controller

    provider = ConnectedSyncTargetContractProvider(
        ContractService(),
        lambda: "group",
    )
    backend = _InstanceWindowBackend((synced[1],), foreground=2)
    messages = _InstanceMessages()

    for controller_type in (
        WindowsInputSyncController,
        WindowsPointerSyncController,
    ):
        controller = controller_type(
            expected_windows=2,
            title_keywords=("Adobe Flash Player",),
            window_backend=backend,
            message_backend=messages,
            target_windows_provider=provider.windows,
            require_expected_window_count=False,
        )
        try:
            controller.set_allowed_window_instances(synced)
            controller.set_controller_fingerprint(
                synced[0].launch_fingerprint
            )
            if controller_type is WindowsInputSyncController:
                result = controller.send_approved_key(
                    "B",
                    policy=WindowInputPolicy.ALL,
                    execute=True,
                    source_handle=2,
                )
                assert result.sent_windows == 0
                assert messages.keys == []
            else:
                result = controller.send_click(
                    source_handle=2,
                    x_ratio=0.5,
                    y_ratio=0.5,
                    policy=WindowInputPolicy.ALL,
                    execute=True,
                    include_source=False,
                )
                assert result.sent_windows == 0
                assert messages.pointers == []
            assert provider.windows() == ()
        finally:
            assert controller.close() is True


def test_single_follower_isolated_then_restored_in_original_delivery_order():
    raw, synced = _shared_launcher_sync_windows(3)
    scope = ("controller", "follower-a", "follower-b")
    partial = ResolvedTargetWindows(
        windows=(raw[2], raw[0]),
        sync_windows=(synced[2], synced[0]),
        sync_entry_ids=("follower-b", "controller"),
        sync_scope_entry_ids=scope,
        sync_controller_entry_id="controller",
    )
    restored = ResolvedTargetWindows(
        windows=(raw[2], raw[0], raw[1]),
        sync_windows=(synced[2], synced[0], synced[1]),
        sync_entry_ids=("follower-b", "controller", "follower-a"),
        sync_scope_entry_ids=scope,
        sync_controller_entry_id="controller",
    )

    class ContractService:
        contract = partial

        def reconnect_targets(self, _group_name):
            return self.contract

    service = ContractService()
    provider = ConnectedSyncTargetContractProvider(service, lambda: "group")
    backend = _InstanceWindowBackend(synced)
    messages = _InstanceMessages()
    keyboard = WindowsInputSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        target_windows_provider=provider.windows,
        require_expected_window_count=False,
    )
    pointer = WindowsPointerSyncController(
        expected_windows=2,
        title_keywords=("Adobe Flash Player",),
        window_backend=backend,
        message_backend=messages,
        target_windows_provider=provider.windows,
        require_expected_window_count=False,
    )
    try:
        partial_windows = provider.windows()
        assert tuple(window.handle for window in partial_windows) == (1, 3)
        for controller in (keyboard, pointer):
            controller.set_allowed_window_instances(partial_windows)
            controller.set_controller_fingerprint(
                partial_windows[0].launch_fingerprint
            )

        partial_key = keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=1,
        )
        partial_pointer = pointer.send_click(
            source_handle=1,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        )
        assert partial_key.sent_windows == 2
        assert [handle for handle, _key in messages.keys] == [1, 3]
        assert partial_pointer.sent_windows == 1
        assert [item[0] for item in messages.pointers] == [3, 3]

        service.contract = restored
        restored_windows = provider.windows()
        assert tuple(window.handle for window in restored_windows) == (1, 2, 3)
        for controller in (keyboard, pointer):
            controller.set_expected_windows(3)
            controller.set_allowed_window_instances(restored_windows)
            controller.set_controller_fingerprint(
                restored_windows[0].launch_fingerprint
            )
        messages.keys.clear()
        messages.pointers.clear()

        restored_key = keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=1,
        )
        restored_pointer = pointer.send_click(
            source_handle=1,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        )
        assert restored_key.sent_windows == 3
        assert [handle for handle, _key in messages.keys] == [1, 2, 3]
        assert restored_pointer.sent_windows == 2
        assert [item[0] for item in messages.pointers] == [2, 2, 3, 3]
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_keyboard_and_pointer_sync_use_complete_instance_scope_for_nine_and_fifteen_shared_launchers():
    for count in (9, 15):
        _raw, synced = _shared_launcher_sync_windows(count)
        backend = _InstanceWindowBackend(synced)
        messages = _InstanceMessages()
        keyboard = WindowsInputSyncController(
            expected_windows=count,
            title_keywords=("Adobe Flash Player",),
            window_backend=backend,
            message_backend=messages,
            target_windows_provider=lambda: tuple(backend.windows),
            require_expected_window_count=False,
        )
        pointer = WindowsPointerSyncController(
            expected_windows=count,
            title_keywords=("Adobe Flash Player",),
            window_backend=backend,
            message_backend=messages,
            target_windows_provider=lambda: tuple(backend.windows),
            require_expected_window_count=False,
        )
        try:
            for controller in (keyboard, pointer):
                controller.set_allowed_window_instances(synced)
                controller.set_controller_fingerprint(
                    synced[0].launch_fingerprint
                )

            key_result = keyboard.send_approved_key(
                "B",
                policy=WindowInputPolicy.ALL,
                execute=True,
                source_handle=1,
            )
            pointer_result = pointer.send_click(
                source_handle=1,
                x_ratio=0.5,
                y_ratio=0.5,
                policy=WindowInputPolicy.ALL,
                execute=True,
                include_source=False,
            )

            assert key_result.sent_windows == count
            assert {handle for handle, _key in messages.keys} == set(
                range(1, count + 1)
            )
            assert pointer_result.sent_windows == count - 1
            assert {item[0] for item in messages.pointers} == set(
                range(2, count + 1)
            )

            foreign_raw = WindowInfo(
                handle=count + 1,
                title="Adobe Flash Player 11",
                visible=True,
                minimized=False,
                rect=(count * 100, 0, (count + 1) * 100, 100),
                process_id=777,
                window_class="Flash",
                launch_fingerprint="a" * 64,
                thread_id=1800 + count,
                process_lifecycle_token=900001,
            )
            foreign = replace(
                foreign_raw,
                launch_fingerprint=monitored_window_instance_fingerprint(
                    foreign_raw
                ),
            )
            backend.windows.append(foreign)
            messages.keys.clear()
            result_after_foreign = keyboard.send_approved_key(
                "B",
                policy=WindowInputPolicy.ALL,
                execute=True,
                source_handle=1,
            )
            assert result_after_foreign.sent_windows == count
            assert all(handle <= count for handle, _key in messages.keys)

            backend.windows.append(synced[0])
            messages.keys.clear()
            conflict_result = keyboard.send_approved_key(
                "B",
                policy=WindowInputPolicy.ALL,
                execute=True,
                source_handle=1,
            )
            assert conflict_result.sent_windows == 0
            assert messages.keys == []

            keyboard.set_allowed_window_instances(None)
            pointer.set_allowed_window_instances(None)
            assert keyboard._allowed_instance_identities is None
            assert pointer._allowed_instance_identities is None
        finally:
            assert keyboard.close() is True
            assert pointer.close() is True


def test_group_identity_failure_explains_cross_group_ambiguity():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def group_identity_failure_message("):
        source.index("    def clear_group_identity(")
    ]

    assert "共用捷徑延伸到其他組別" in function_source
    assert "無法唯一對應遊戲視窗" in function_source
    assert "維持安全停止" in function_source


def test_smart_reconnect_snapshot_transition_has_no_group_dependency() -> None:
    class Controller:
        def __init__(self):
            self.prepared = 0
            self.execution = []

        def prepare_execution_snapshot(self):
            self.prepared += 1
            return SimpleNamespace(
                success=True,
                message="快照完成",
            )

        def set_execution_enabled(self, value):
            self.execution.append(value)

    controller = Controller()
    started = apply_smart_reconnect_snapshot_transition(
        True,
        controller,
        object(),
        start_monitor=lambda _monitor: SimpleNamespace(success=True),
        stop_monitor=lambda _monitor: SimpleNamespace(success=True),
    )

    assert started.success is True
    assert controller.prepared == 1
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("def apply_smart_reconnect_snapshot_transition("):
        source.index("def resolve_group_role_progress_subject_id(")
    ]
    assert "group_selection_service" not in function_source
    assert "set_group_launch_plan" not in function_source
    assert "reopen_missing" not in function_source


def test_smart_reconnect_snapshot_failure_is_returned_without_starting() -> None:
    class Controller:
        def __init__(self):
            self.execution = []

        def prepare_execution_snapshot(self):
            return SimpleNamespace(
                success=False,
                message="目前遊戲視窗身分有衝突，沒有啟用智慧重連。",
            )

        def set_execution_enabled(self, value):
            self.execution.append(value)

    controller = Controller()
    starts = []

    result = apply_smart_reconnect_snapshot_transition(
        True,
        controller,
        object(),
        start_monitor=lambda monitor: starts.append(monitor),
        stop_monitor=lambda _monitor: SimpleNamespace(success=True),
    )

    assert result.success is False
    assert "身分有衝突" in result.message
    assert starts == []
    assert controller.execution == [False]


def test_smart_reconnect_stop_always_revokes_snapshot_authority() -> None:
    class Controller:
        def __init__(self):
            self.execution = []

        def prepare_execution_snapshot(self):
            return SimpleNamespace(success=True, message="")

        def set_execution_enabled(self, value):
            self.execution.append(value)

    controller = Controller()

    result = apply_smart_reconnect_snapshot_transition(
        False,
        controller,
        object(),
        start_monitor=lambda _monitor: SimpleNamespace(success=True),
        stop_monitor=lambda _monitor: SimpleNamespace(success=True),
    )

    assert result.success is True
    assert controller.execution == [False]


def test_smart_reconnect_auto_battle_setting_can_be_enabled_and_disabled():
    class Controller:
        def __init__(self):
            self.values = []

        def set_auto_battle_enabled(self, value):
            self.values.append(value)

    class Config:
        def __init__(self):
            self.values = {}

        def set(self, key, value):
            self.values[key] = value

    controller = Controller()
    config = Config()

    assert apply_smart_reconnect_auto_battle_setting(
        True,
        controller,
        config,
    )
    assert apply_smart_reconnect_auto_battle_setting(
        False,
        controller,
        config,
    )
    assert controller.values == [True, False]
    assert config.values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is False


def test_real_game_launch_enables_saves_and_reflects_auto_battle():
    class Controller:
        def __init__(self):
            self.values = []

        def set_auto_battle_enabled(self, value):
            self.values.append(value)

    class Config:
        def __init__(self):
            self.values = {}

        def set(self, key, value):
            self.values[key] = value

    class View:
        def __init__(self):
            self.values = []

        def set_smart_reconnect_auto_battle_enabled(self, value):
            self.values.append(value)

    controller = Controller()
    config = Config()
    view = View()

    assert not apply_auto_battle_after_game_launch(
        False,
        controller,
        config,
        view,
    )
    assert apply_auto_battle_after_game_launch(
        True,
        controller,
        config,
        view,
    )

    assert controller.values == [True]
    assert config.values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is True
    assert view.values == [True]


def _launch_auto_battle_outputs(result, predicate):
    controller = SimpleNamespace(values=[])
    controller.set_auto_battle_enabled = controller.values.append
    config = SimpleNamespace(values={})
    config.set = config.values.__setitem__
    view = SimpleNamespace(values=[])
    view.set_smart_reconnect_auto_battle_enabled = view.values.append

    applied = apply_auto_battle_after_game_launch(
        predicate(result),
        controller,
        config,
        view,
    )
    return applied, controller.values, config.values, view.values


def test_successful_group_launch_enables_auto_battle() -> None:
    result = GroupWindowLaunchResult(
        True,
        "14支",
        total_count=14,
        launched_count=2,
        restored_count=12,
        action="launch",
    )

    applied, controller_values, config_values, view_values = (
        _launch_auto_battle_outputs(
            result,
            group_window_launch_started_game,
        )
    )

    assert applied is True
    assert controller_values == [True]
    assert config_values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is True
    assert view_values == [True]


def test_partial_failed_group_launch_does_not_enable_auto_battle() -> None:
    result = GroupWindowLaunchResult(
        False,
        "14支",
        total_count=14,
        launched_count=2,
        restored_count=11,
        failure_code="group_window_place_failed",
        action="launch",
    )

    outputs = _launch_auto_battle_outputs(
        result,
        group_window_launch_started_game,
    )

    assert outputs == (False, [], {}, [])


def test_restore_only_group_operation_does_not_enable_auto_battle() -> None:
    result = GroupWindowLaunchResult(
        True,
        "14支",
        total_count=14,
        launched_count=0,
        restored_count=14,
        action="restore",
    )

    outputs = _launch_auto_battle_outputs(
        result,
        group_window_launch_started_game,
    )

    assert outputs == (False, [], {}, [])


def test_single_role_launch_result_requires_successful_real_launch() -> None:
    launched = GroupRoleActionResult(True, action="launched")
    activated = GroupRoleActionResult(True, action="activated")
    failed = GroupRoleActionResult(
        False,
        action="launched",
        failure_code="role_launch_failed",
    )

    applied, controller_values, config_values, view_values = (
        _launch_auto_battle_outputs(
            launched,
            group_role_action_started_game,
        )
    )
    assert applied is True
    assert controller_values == [True]
    assert config_values[SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY] is True
    assert view_values == [True]
    assert _launch_auto_battle_outputs(
        activated,
        group_role_action_started_game,
    ) == (False, [], {}, [])
    assert _launch_auto_battle_outputs(
        failed,
        group_role_action_started_game,
    ) == (False, [], {}, [])


def test_only_two_real_launch_results_feed_the_shared_auto_battle_helper():
    source = Path("main.py").read_text(encoding="utf-8")
    group_completion = source[
        source.index("    def complete_group_window_launch("):
        source.index("    def start_group_window_operation(")
    ]
    single_role = source[
        source.index("    def activate_or_launch_group_role("):
        source.index("    home_view = HomeView(")
    ]

    assert "group_window_launch_started_game(result)" in group_completion
    assert "apply_auto_battle_after_game_launch" in group_completion
    assert "group_role_action_started_game(result)" in single_role
    assert "apply_auto_battle_after_game_launch" in single_role


def test_smart_reconnect_enable_binds_configured_plan_before_opening_gate():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def change_smart_reconnect("):
        source.index("    def change_smart_reconnect_auto_battle(")
    ]
    configured_source = source[
        source.index("    def configured_reconnect_plan("):
        source.index("    def write_clipboard(")
    ]
    plan_builder_source = source[
        source.index("def build_configured_reconnect_plan("):
        source.index("def apply_auto_battle_after_game_launch(")
    ]
    group_identity_source = source[
        source.index("    def apply_group_identity("):
        source.index("    sync_connected_fingerprints:")
    ]
    input_sync_source = source[
        source.index("    def change_keyboard_sync("):
        source.index("    def change_smart_reconnect(")
    ]

    close_index = function_source.index("close_group_operation_gate()")
    plan_index = function_source.index("configured_reconnect_plan()")
    bind_index = function_source.index(
        "smart_reconnect_controller.set_group_launch_plan(plan)"
    )
    transition_index = function_source.index(
        "apply_smart_reconnect_snapshot_transition("
    )
    reopen_index = function_source.index("reopen_group_operation_gate()")
    save_index = function_source.index("config.update_values(")
    assert (
        close_index
        < plan_index
        < bind_index
        < transition_index
        < reopen_index
        < save_index
    )
    assert "smart_reconnect_controller.set_group_launch_plan(None)" in (
        function_source
    )
    assert "CURRENT_GROUP_NAME_KEY" not in function_source
    assert "workspace_service.snapshot()" not in function_source
    assert "group_selection_service.find(" not in function_source
    assert "apply_group_identity(choice)" not in function_source
    assert "CURRENT_GROUP_NAME_KEY" not in configured_source
    assert "workspace_service.snapshot()" not in configured_source
    assert "group_selection_service.choices()" in configured_source
    assert "not entry.role_id.strip()" not in plan_builder_source
    assert "recovery_role_id" in plan_builder_source
    assert "smart_reconnect_controller.set_group_launch_plan" not in (
        group_identity_source
    )
    assert "input_controller.set_allowed_window_instances" in (
        group_identity_source
    )
    assert "pointer_sync_controller.set_allowed_window_instances" in (
        group_identity_source
    )
    assert "workspace_service.snapshot()" in input_sync_source
    assert "group_selection_service.find(group_name)" in input_sync_source


def test_configured_reconnect_plan_keeps_recovery_conflicts_detection_only(
    tmp_path,
):
    shortcut = tmp_path / "same.lnk"
    other_shortcut = tmp_path / "other.lnk"
    scope = SimpleNamespace(
        ready=True,
        entry_ids=("shared-entry",),
        entry_fingerprints=("a" * 64,),
    )

    def entry(
        role_id,
        placement=None,
        *,
        display_name="same",
        shortcut_path=shortcut,
        role_name_prefix="",
    ):
        return SimpleNamespace(
            entry_id="shared-entry",
            display_name=display_name,
            shortcut_path=shortcut_path,
            role_id=role_id,
            role_name_prefix=role_name_prefix,
            placement=placement,
        )

    def plan_for(first, second, *, choices=()):
        return build_configured_reconnect_plan(
            scope,
            (
                SimpleNamespace(entries=(first,)),
                SimpleNamespace(entries=(second,)),
            ),
            (),
            choices,
        )

    blank_and_a = plan_for(entry("A"), entry(""))
    same_role = plan_for(entry("A"), entry("A"))
    conflicting_roles = plan_for(entry("A"), entry("B"))
    saved_prefix = plan_for(
        entry("A", role_name_prefix="敖"),
        entry("", role_name_prefix=""),
    )
    conflicting_prefixes = plan_for(
        entry("A", role_name_prefix="敖"),
        entry("A", role_name_prefix="嘻"),
    )
    placement_difference = plan_for(
        entry("A"),
        entry("A", SimpleNamespace(x=1)),
    )
    display_difference = plan_for(
        entry("A"),
        entry("A", display_name="另一顯示名稱"),
    )
    profile_conflict = plan_for(
        entry("A"),
        entry("A"),
        choices=(
            SimpleNamespace(
                members=(
                    SimpleNamespace(
                        entry_id="shared-entry",
                        character_id="profile-a",
                    ),
                )
            ),
            SimpleNamespace(
                members=(
                    SimpleNamespace(
                        entry_id="shared-entry",
                        character_id="profile-b",
                    ),
                )
            ),
        ),
    )
    path_conflict = plan_for(
        entry("A"),
        entry("A", shortcut_path=other_shortcut),
    )

    assert blank_and_a is not None
    assert blank_and_a.targets[0].role_id == "A"
    assert same_role is not None
    assert same_role.targets[0].role_id == "A"
    assert conflicting_roles is not None
    assert len(conflicting_roles.targets) == 1
    assert conflicting_roles.targets[0].role_id == ""
    assert saved_prefix is not None
    assert saved_prefix.targets[0].role_name_prefix == "敖"
    assert conflicting_prefixes is not None
    assert conflicting_prefixes.targets[0].role_name_prefix == ""
    assert placement_difference is not None
    assert placement_difference.targets[0].role_id == "A"
    assert display_difference is not None
    assert display_difference.targets[0].role_id == "A"
    assert profile_conflict is not None
    assert len(profile_conflict.targets) == 1
    assert profile_conflict.targets[0].role_id == ""
    assert path_conflict is None


def test_build_services_registers_input_controller_and_safe_default(tmp_path):
    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    controller = AppContext.get(WindowsInputSyncController)
    pointer = AppContext.get(WindowsPointerSyncController)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    reconnect_boundary = AppContext.get(SmartReconnectBoundary)
    reconnect_monitor = AppContext.get(SmartReconnectMonitor)
    capture_settings_service = AppContext.get(
        SmartReconnectCaptureSettingsService
    )

    assert config.get(INPUT_POLICY_KEY) == WindowInputPolicy.ALL.value
    assert config.get(SMART_RECONNECT_ENABLED_KEY) is False
    assert config.get(SMART_RECONNECT_CONSENT_KEY) is False
    assert config.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY) is True
    assert config.get(SMART_RECONNECT_INTERVAL_MS_KEY) == 2000
    assert config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is True
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == {
        "visible": True,
        "obscured": True,
        "minimized": True,
    }
    assert config.get(GAME_TIME_OFFSET_MS_KEY) == 0
    assert config.get(GAME_TIME_AUTO_UPDATE_KEY) is True
    assert config.get(SYNC_KEYS_COLLAPSED_KEY) is True
    assert config.get(UI_THEME_KEY) == "classic_gold"
    assert config.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is True
    assert config.get(TIMED_CLICK_SETTINGS_KEY) == {
        "target_time": "08:00:00.000",
        "lead_ms": 0,
        "repeat_count": 3,
        "repeat_interval_ms": 0,
    }
    assert isinstance(controller, WindowsInputSyncController)
    assert isinstance(pointer, WindowsPointerSyncController)
    assert controller._screen_state_provider is None
    assert pointer._screen_state_provider is None
    assert controller._deferred_screen_state_provider is not None
    assert (
        controller._deferred_screen_state_provider
        is pointer._deferred_screen_state_provider
    )
    assert isinstance(reconnect, WindowsSmartReconnectController)
    assert reconnect_boundary is reconnect
    assert reconnect.auto_battle_enabled is True
    assert isinstance(reconnect_monitor, SmartReconnectMonitor)
    assert reconnect_monitor.monitor_interval_ms == 2000
    assert (
        capture_settings_service.snapshot()
        == SmartReconnectCaptureSettings()
    )
    assert reconnect.capture_settings == SmartReconnectCaptureSettings()


def test_formal_deferred_gate_reobserves_connected_before_each_delivery(
    tmp_path,
):
    build_services(root=tmp_path)
    keyboard = AppContext.get(WindowsInputSyncController)
    pointer = AppContext.get(WindowsPointerSyncController)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    target_service = AppContext.get(TargetWindowContractService)
    deferred = AppContext.get(DeferredSyncOperationService)
    deferred_failures = []
    deferred._on_failure = deferred_failures.append
    raw, synced = _shared_launcher_sync_windows(2)
    contract = ResolvedTargetWindows(
        windows=raw,
        sync_windows=synced,
        sync_entry_ids=("controller", "follower"),
        sync_scope_entry_ids=("controller", "follower"),
        sync_controller_entry_id="controller",
    )
    screen_state = {"value": ReconnectScreenState.UNKNOWN}
    observed: list[ReconnectScreenState] = []

    def observe_window_instance_states(windows):
        value = screen_state["value"]
        observed.append(value)
        return {
            monitored_window_instance_fingerprint(window): value
            for window in windows
        }

    target_service.reconnect_targets = (
        lambda _group_name, **_kwargs: contract
    )
    reconnect.observe_window_instance_states = observe_window_instance_states
    backend = _InstanceWindowBackend(synced)
    messages = _InstanceMessages()
    for controller in (keyboard, pointer):
        controller._window_backend = backend
        controller._message_backend = messages
        controller.set_expected_windows(2)
        controller.set_allowed_window_instances(synced)
        controller.set_controller_fingerprint(
            synced[0].launch_fingerprint
        )

    def enqueue_pair() -> None:
        deferred.enqueue(
            synced[0].launch_fingerprint,
            "key:B",
            kind="keyboard",
            payload={"key": "B"},
        )
        deferred.enqueue(
            synced[1].launch_fingerprint,
            "pointer:click:0.5000:0.5000",
            kind="pointer",
            payload={
                "x_ratio": 0.5,
                "y_ratio": 0.5,
                "event": "click",
            },
        )

    def process_ready_and_wait() -> None:
        deferred.process_ready(
            reconnecting_targets=(),
            failed_targets=(),
            ready_targets=tuple(
                window.launch_fingerprint for window in synced
            ),
        )
        deadline = monotonic() + 2.0
        while deferred.pending() and monotonic() < deadline:
            sleep(0.01)
        assert deferred.pending() == 0

    try:
        assert keyboard._screen_state_provider is None
        assert pointer._screen_state_provider is None
        immediate_key = keyboard.send_approved_key(
            "B",
            policy=WindowInputPolicy.ALL,
            execute=True,
            source_handle=1,
        )
        immediate_pointer = pointer.send_click(
            source_handle=1,
            x_ratio=0.5,
            y_ratio=0.5,
            policy=WindowInputPolicy.ALL,
            execute=True,
            include_source=False,
        )
        assert immediate_key.sent_windows == 2
        assert immediate_pointer.sent_windows == 1
        assert observed == []

        messages.keys.clear()
        messages.pointers.clear()
        screen_state["value"] = ReconnectScreenState.CONNECTED
        assert (
            keyboard._deferred_screen_state_provider(
                synced[0].launch_fingerprint
            )
            is ReconnectScreenState.CONNECTED
        )
        screen_state["value"] = ReconnectScreenState.UNKNOWN
        failure_count = len(deferred_failures)
        enqueue_pair()
        process_ready_and_wait()
        assert messages.keys == []
        assert messages.pointers == []
        assert {
            failure.failure_code
            for failure in deferred_failures[failure_count:]
        } == {"operation_screen_not_safe"}
        assert observed[-2:] == [
            ReconnectScreenState.UNKNOWN,
            ReconnectScreenState.UNKNOWN,
        ]

        screen_state["value"] = ReconnectScreenState.CONNECTED
        enqueue_pair()
        process_ready_and_wait()
        assert messages.keys == [(1, 0x42)]
        assert [item[0] for item in messages.pointers] == [2, 2]
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True


def test_build_services_wires_ungrouped_detection_into_reconnect_contract(
    tmp_path,
):
    build_services(root=tmp_path)

    reconnect = AppContext.get(WindowsSmartReconnectController)
    ungrouped = AppContext.get(UngroupedWindowService)
    contract = AppContext.get(TargetWindowContractService)

    assert callable(reconnect._registered_role_provider)
    assert contract._ungrouped_window_service is ungrouped


def test_build_services_preserves_explicit_saved_auto_battle_off(tmp_path):
    build_services(root=tmp_path)
    config = AppContext.get(ConfigManager)
    config.set(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY, False)

    build_services(root=tmp_path)
    reconnect = AppContext.get(WindowsSmartReconnectController)

    assert reconnect.auto_battle_enabled is False


def test_invalid_legacy_auto_battle_value_self_heals_to_on(tmp_path):
    build_services(root=tmp_path)
    config = AppContext.get(ConfigManager)
    config.set(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY, "invalid")

    build_services(root=tmp_path)
    reloaded = AppContext.get(ConfigManager)
    reconnect = AppContext.get(WindowsSmartReconnectController)

    assert normalize_smart_reconnect_auto_battle_enabled(None) is True
    assert normalize_smart_reconnect_auto_battle_enabled(False) is False
    assert reloaded.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY) is True
    assert reconnect.auto_battle_enabled is True


def test_existing_theme_migrates_to_gold_once_without_overriding_later_choice(
    tmp_path,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = config_dir / "settings.json"
    settings.write_text(
        json.dumps({UI_THEME_KEY: "clear_blue"}, ensure_ascii=False),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    assert config.get(UI_THEME_KEY) == "classic_gold"
    assert config.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is True

    config.set(UI_THEME_KEY, "forest_green")
    build_services(root=tmp_path)

    reloaded = AppContext.get(ConfigManager)
    assert reloaded.get(UI_THEME_KEY) == "forest_green"
    assert reloaded.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is True


def test_stop_sync_pair_reports_actual_partial_cleanup_without_false_success():
    class Monitor:
        def __init__(self, *, fail_stop=False):
            self.enabled = True
            self.fail_stop = fail_stop

        def start(self):
            self.enabled = True
            return True

        def stop(self):
            if self.fail_stop:
                return False
            self.enabled = False
            return True

    keyboard = Monitor(fail_stop=True)
    mouse = Monitor()

    assert stop_input_sync_pair(keyboard, mouse) is False
    assert keyboard.enabled is True
    assert mouse.enabled is False

    keyboard.fail_stop = False
    mouse.start()
    assert stop_input_sync_pair(keyboard, mouse) is True
    assert keyboard.enabled is False
    assert mouse.enabled is False


def test_smart_reconnect_monitor_restores_saved_interval(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {SMART_RECONNECT_INTERVAL_MS_KEY: 2750},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    monitor = AppContext.get(SmartReconnectMonitor)
    assert config.get(SMART_RECONNECT_INTERVAL_MS_KEY) == 2750
    assert config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is True
    assert monitor.monitor_interval_ms == 2750


def test_smart_reconnect_monitor_restores_saved_service_mode(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                SMART_RECONNECT_MODE_KEY:
                    SMART_RECONNECT_MODE_HIGH_PERFORMANCE,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    monitor = AppContext.get(SmartReconnectMonitor)
    assert monitor.monitor_mode == SMART_RECONNECT_MODE_HIGH_PERFORMANCE


def test_smart_reconnect_controller_restores_saved_capture_modes(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    expected = {
        "visible": True,
        "obscured": False,
        "minimized": True,
    }
    (config_dir / "settings.json").write_text(
        json.dumps(
            {SMART_RECONNECT_CAPTURE_MODES_KEY: expected},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    service = AppContext.get(SmartReconnectCaptureSettingsService)
    controller = AppContext.get(WindowsSmartReconnectController)
    assert config.get(SMART_RECONNECT_CAPTURE_MODES_KEY) == expected
    assert service.snapshot().to_dict() == expected
    assert controller.capture_settings.to_dict() == expected


def test_old_default_reconnect_interval_migrates_once_to_balanced_default(
    tmp_path,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(
            {SMART_RECONNECT_INTERVAL_MS_KEY: 1000},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    build_services(root=tmp_path)

    config = AppContext.get(ConfigManager)
    monitor = AppContext.get(SmartReconnectMonitor)
    assert config.get(SMART_RECONNECT_INTERVAL_MS_KEY) == 2000
    assert config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is True
    assert monitor.monitor_interval_ms == 2000


def test_sync_services_share_lifecycle_backend_with_separate_target_contracts(
    tmp_path,
):
    build_services(root=tmp_path)

    keyboard = AppContext.get(WindowsInputSyncController)
    pointer = AppContext.get(WindowsPointerSyncController)
    statuses = AppContext.get(GroupRoleStatusService)
    reconnect = AppContext.get(WindowsSmartReconnectController)
    gate = AppContext.get(GameOperationGate)

    assert keyboard._window_backend is pointer._window_backend
    assert keyboard._window_backend is statuses._window_backend
    assert keyboard._target_windows_provider is pointer._target_windows_provider
    assert keyboard._target_windows_provider is not reconnect._target_windows_provider
    assert statuses._target_snapshot_provider is not None
    assert keyboard._operation_gate is gate
    assert pointer._operation_gate is gate
    assert reconnect._operation_gate is gate
    assert statuses._operation_gate is gate


def test_main_window_polling_uses_a_throttled_current_group_handle_cache():
    source = Path("main.py").read_text(encoding="utf-8")

    assert source.count(
        "target_handles_provider=current_target_handles"
    ) == 2
    assert source.count(
        'execution_enabled_provider=lambda: bool('
    ) == 2
    assert 'sync_session_state["enabled"] = False' in source
    handle_source = source[
        source.index("    def current_target_handles("):
        source.index("    def log_keyboard_sync_result(")
    ]
    assert "if choice is None or not apply_connected_sync_identity(" in handle_source
    assert handle_source.count("resolve_connected_sync_target_contract(") == 1
    assert "resolved_targets=resolved_targets" in handle_source
    assert 'sync_session_state["enabled"] = False' in handle_source
    assert "sync_connected_fingerprints = None" in handle_source
    assert "sync_connected_instance_signature = None" in handle_source
    assert "stop_input_sync_pair(" in handle_source
    assert "handles = ()" in handle_source
    assert 'sync_source_handle_cache: dict[str, object]' in source
    assert '"expires_at": now + 0.25' in source
    assert "target_windows_provider=current_sync_target_windows" in source
    assert source.count("operation_record_store.append_deferred(") >= 3


def test_home_exposes_three_policies_and_complete_confirmed_shortcuts():
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "僅允許前台" in source
    assert "允許前台與背景" in source
    assert "全部允許（含最小化）" in source
    assert "開始同步視窗" in source
    assert "停止同步視窗" in source
    assert "CONFIRMED_GAME_SHORTCUTS" in source
    assert "測試 B" not in source


def test_sync_toggle_returns_direct_card_feedback_for_every_branch():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index("    def change_keyboard_sync("):
        source.index("    def change_smart_reconnect(")
    ]

    assert "-> SyncToggleViewResult:" in function_source
    assert function_source.count("SyncToggleViewResult(") >= 7
    assert "同步中｜同步左鍵、拖曳與已確認快捷鍵" in function_source
    assert "同步已停止；背景清理仍在完成中。" in function_source
    assert "未能啟動；同步沒有啟用。" in function_source
    assert "messagebox.showerror" not in function_source


def test_sync_key_collapsed_state_is_loaded_saved_and_wired_to_home():
    source = Path("main.py").read_text(encoding="utf-8")

    assert 'SYNC_KEYS_COLLAPSED_KEY = "sync_keys_collapsed"' in source
    assert "def change_sync_keys_collapsed(" in source
    assert "config.set(SYNC_KEYS_COLLAPSED_KEY, bool(collapsed))" in source
    assert "sync_keys_collapsed=configured_sync_keys_collapsed" in source
    assert (
        "on_sync_keys_collapsed_change=change_sync_keys_collapsed"
        in source
    )


def test_smart_reconnect_interval_uses_legacy_key_and_is_saved():
    source = Path("main.py").read_text(encoding="utf-8")

    assert (
        'SMART_RECONNECT_INTERVAL_MS_KEY = "disconnect_detect_interval_ms"'
        in source
    )
    assert "def change_smart_reconnect_interval(" in source
    assert (
        "config.set(SMART_RECONNECT_INTERVAL_MS_KEY, normalized)"
        in source
    )
    assert "smart_reconnect_interval_ms=(" in source
    assert (
        "on_smart_reconnect_interval_change=("
        in source
    )


def test_smart_reconnect_capture_modes_use_one_formal_persistent_entrypoint():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "SmartReconnectCaptureSettingsService(config)" in source
    assert "def change_smart_reconnect_capture_modes(" in source
    assert (
        "smart_reconnect_capture_settings_service.update(modes)"
        in source
    )
    assert "smart_reconnect_controller.set_capture_settings(settings)" in source
    assert "smart_reconnect_capture_modes=(" in source
    assert "on_smart_reconnect_capture_modes_change=(" in source


def test_group_member_continuous_click_never_falls_back_to_one_window():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "block_physical_fallback=lambda source:" in source
    assert (
        "pointer_sync_controller.source_must_block_physical_fallback("
        in source
    )


def test_group_change_and_group_edit_stop_continuous_click_immediately():
    source = Path("main.py").read_text(encoding="utf-8")

    assert source.count("auto_click_service.stop()") >= 2


def test_group_configuration_change_does_not_stop_or_rebind_reconnect_snapshot():
    source = Path("main.py").read_text(encoding="utf-8")
    function_source = source[
        source.index(
            "    def stop_group_automation_for_configuration_change("
        ):
        source.index("    def finish_group_management(")
    ]

    assert "smart_reconnect_monitor" not in function_source
    assert "set_group_launch_plan" not in function_source
    assert "SMART_RECONNECT_ENABLED_KEY" not in function_source


def test_group_change_stops_all_automation_before_publishing_new_group():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]

    close_index = change_group.index(
        "close_group_operation_gate()"
    )
    stop_index = change_group.index(
        "stop_group_automation_for_configuration_change()"
    )
    apply_index = change_group.index("apply_group_identity(choice)")
    workspace_index = change_group.index(
        "workspace_service.set_current_group("
    )
    config_index = change_group.index(
        "config.set(CURRENT_GROUP_NAME_KEY"
    )
    reopen_index = change_group.rindex(
        "reopen_group_operation_gate()"
    )

    assert (
        close_index
        < stop_index
        < apply_index
        < config_index
        < workspace_index
        < reopen_index
    )


def test_group_switch_revokes_old_keyboard_mouse_and_timed_authority_first():
    source = Path("main.py").read_text(encoding="utf-8")
    stop_source = source[
        source.index(
            "    def stop_group_automation_for_configuration_change("
        ):
        source.index("    def finish_group_management(")
    ]

    assert 'sync_session_state["enabled"] = False' in stop_source
    assert "input_controller.invalidate_scheduled()" in stop_source
    assert "pointer_sync_controller.invalidate_scheduled()" in stop_source
    assert "game_time_timed_click_service.clear_target(" in stop_source
    assert "stop_service(keyboard_sync_monitor)" in stop_source
    assert "stop_service(mouse_sync_monitor)" in stop_source


def test_group_change_allows_selection_but_clears_identity_when_unresolved():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]

    apply_index = change_group.index("apply_group_identity(choice)")
    clear_index = change_group.index("clear_group_identity()")
    config_index = change_group.index("config.set(CURRENT_GROUP_NAME_KEY")
    workspace_index = change_group.index(
        "workspace_service.set_current_group("
    )

    assert "identity_ready = apply_group_identity(choice) is not None" in change_group
    assert "if not identity_ready:" in change_group
    assert apply_index < clear_index < config_index < workspace_index
    assert "同步與智慧重連已保持停用" in change_group


def test_failed_group_change_clears_unbound_identity_before_reopening_gate():
    source = Path("main.py").read_text(encoding="utf-8")
    change_group = source[
        source.index("    def change_group("):
        source.index(
            "    def stop_group_automation_for_configuration_change(",
        )
    ]
    rollback_start = change_group.index(
        "        except Exception:",
        change_group.index("selected_workspace_group = "),
    )
    rollback_end = change_group.index(
        "        reopen_group_operation_gate()",
        rollback_start,
    ) + len("        reopen_group_operation_gate()")
    rollback = change_group[
        rollback_start:
        rollback_end
    ]

    restore_index = rollback.index("restore_group_identity(old_choice)")
    clear_index = rollback.index("clear_group_identity()")
    publish_index = rollback.index("restore_published_group(")
    reopen_index = rollback.index("reopen_group_operation_gate()")

    assert restore_index < clear_index < publish_index < reopen_index
    assert "if rollback_ready and publication_restored:" in rollback


def test_role_identity_refresh_only_rebinds_current_group_and_reopens_gate():
    source = Path("main.py").read_text(encoding="utf-8")
    refresh = source[
        source.index("    def refresh_group_sync_identity("):
        source.index("    def capture_sync_base_point(")
    ]

    assert "config.get(CURRENT_GROUP_NAME_KEY" in refresh
    assert "!= group_name" in refresh
    assert "close_group_operation_gate()" in refresh
    assert "finally:" not in refresh
    assert "applied = apply_group_identity(choice) is not None" in refresh
    assert "clear_group_identity()" in refresh
    assert "reopen_group_operation_gate()" in refresh


def test_input_verifier_has_a_bounded_delay_for_real_foreground_testing():
    source = Path("scripts/verify_input_sync_sp1.py").read_text(
        encoding="utf-8"
    )

    assert "--delay-seconds" in source
    assert "between 0 and 30" in source
    assert "--activate-one-for-foreground-test" in source
    assert 'window.window_class == "ShockwaveFlash"' in source
    assert "_SnapshotWindowBackend(validated_windows)" in source
    assert "resolve_fingerprints=True" in source


def test_input_verifier_restores_its_temporary_minimized_subset():
    source = Path("scripts/verify_input_sync_sp1.py").read_text(
        encoding="utf-8"
    )

    assert "--minimize-count-for-test" in source
    assert "resolve_fingerprints=True" in source
    assert "finally:" in source
    assert "_restore_flash_windows(restore_handles)" in source
