from adapters.windows_window import WindowInfo
from core.window_registry import WindowHealth, WindowRegistry
from main import register_server_time_services
from services.app_context import AppContext
from services.server_clock import ServerTimeSourceIdentity
from services.server_time_bridge import (
    ProcessMemoryServerTimeReader,
    ServerTimeBridge,
    ServerTimeBridgeServer,
)
from services.managed_game_process_service import ManagedGameWindow


FINGERPRINT = "1f383b186a886c54a70800bdabfdc5c7a986fee50d63a089e7bfd17557b5b8d0"


class Backend:
    def __init__(self, window):
        self.window = window

    def list_windows(self):
        return (self.window,)


class Managed:
    def __init__(self):
        self._records = {
            FINGERPRINT: ManagedGameWindow(
                "測試組",
                "測試角色",
                20,
                10,
                FINGERPRINT,
            )
        }


class Group:
    def __init__(self, name):
        self.name = name


class Target:
    def __init__(self, entry_id, display_name, fingerprint=FINGERPRINT):
        self.entry_id = entry_id
        self.display_name = display_name
        self.fingerprint = fingerprint


class Plan:
    ready = True

    def __init__(self, target):
        self._target = target

    def target_for_fingerprint(self, fingerprint):
        return self._target if fingerprint == self._target.fingerprint else None


class GroupConfiguration:
    def __init__(self, *names):
        self._groups = tuple(Group(name) for name in names)

    def groups(self):
        return self._groups


class GroupLaunch:
    def __init__(self, plans):
        self._plans = plans

    def plan(self, group_name):
        return self._plans[group_name]


class StaleManaged:
    def __init__(self, group_name, role_name):
        self._records = {
            FINGERPRINT: ManagedGameWindow(
                group_name,
                role_name,
                999,
                998,
                FINGERPRINT,
            )
        }


def test_main_wires_server_clock_and_bridge_with_identity_gate():
    registry = WindowRegistry()
    record = registry.register_character("role-100", "角色")
    window = WindowInfo(
        handle=10,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 100, 100),
        process_id=20,
        window_class="ShockwaveFlash",
        launch_fingerprint=FINGERPRINT,
        thread_id=30,
        process_lifecycle_token=40,
    )
    registry.confirm_window(
        record.character_id,
        handle=10,
        process_id=20,
        window_class="ShockwaveFlash",
        rect=(0, 0, 100, 100),
        health=WindowHealth.READY,
    )
    AppContext.clear()
    clock, bridge = register_server_time_services(
        Backend(window),
        registry,
        managed_process_service=Managed(),
        start_listener=False,
    )
    assert AppContext.get(type(clock)) is clock
    assert AppContext.get(ServerTimeBridge) is bridge
    assert AppContext.get(ServerTimeBridgeServer).running is False
    memory_reader = AppContext.get(ProcessMemoryServerTimeReader)
    assert memory_reader.running is True
    memory_reader.stop()
    identity = ServerTimeSourceIdentity(10, 20, 30, 40, FINGERPRINT)
    assert bridge.ingest(
        {
            "protocol_version": 1,
            "source_instance_identity": {
                "handle": identity.handle,
                "process_id": identity.process_id,
                "thread_id": identity.thread_id,
                "lifecycle": identity.lifecycle,
                "fingerprint": identity.fingerprint,
            },
            "server_now_ms": 123456,
            "sample_local_flash_timer": 12,
            "sample_sequence": 1,
        }
    ) is True
    assert clock.calibration_count == 1


def test_main_resolves_transport_bound_source_only_from_confirmed_managed_window():
    registry = WindowRegistry()
    record = registry.register_character("role-100", "角色")
    window = WindowInfo(
        handle=10,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 100, 100),
        process_id=20,
        window_class="ShockwaveFlash",
        launch_fingerprint=FINGERPRINT,
        thread_id=30,
        process_lifecycle_token=40,
    )
    registry.confirm_window(
        record.character_id,
        handle=10,
        process_id=20,
        window_class="ShockwaveFlash",
        rect=(0, 0, 100, 100),
        health=WindowHealth.READY,
    )
    AppContext.clear()
    clock, bridge = register_server_time_services(
        Backend(window),
        registry,
        managed_process_service=Managed(),
        start_listener=False,
        start_memory_reader=False,
    )
    assert AppContext.get(ProcessMemoryServerTimeReader)._windows() == (window,)
    assert bridge.ingest(
        {
            "protocol_version": 1,
            "source_instance_identity": "transport-bound",
            "server_now_ms": 123456,
            "sample_local_flash_timer": 12,
            "sample_sequence": 1,
        },
        transport_process_id=20,
    ) is True
    assert clock.calibration_count == 1


def test_main_revalidates_one_current_window_from_registered_target():
    registry = WindowRegistry()
    registry.register_character("entry-1", "角色")
    window = WindowInfo(
        handle=10,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 100, 100),
        process_id=20,
        window_class="ShockwaveFlash",
        launch_fingerprint=FINGERPRINT,
        thread_id=30,
        process_lifecycle_token=40,
    )
    target = Target("entry-1", "角色")
    AppContext.clear()
    clock, bridge = register_server_time_services(
        Backend(window),
        registry,
        managed_process_service=StaleManaged("測試組", "角色"),
        group_configuration_service=GroupConfiguration("測試組", "另一組"),
        group_launch_service=GroupLaunch(
            {"測試組": Plan(target), "另一組": Plan(target)}
        ),
        start_listener=False,
        start_memory_reader=False,
    )
    assert AppContext.get(ProcessMemoryServerTimeReader)._windows() == (window,)
    assert bridge.ingest(
        {
            "protocol_version": 1,
            "source_instance_identity": "transport-bound",
            "server_now_ms": 123456,
            "sample_local_flash_timer": 12,
            "sample_sequence": 1,
        },
        transport_process_id=20,
    ) is True
    assert clock.calibration_count == 1


def test_main_revalidation_rejects_same_fingerprint_for_two_characters():
    registry = WindowRegistry()
    registry.register_character("entry-1", "角色一")
    registry.register_character("entry-2", "角色二")
    window = WindowInfo(
        handle=10,
        title="Adobe Flash Player 11",
        visible=True,
        minimized=False,
        rect=(0, 0, 100, 100),
        process_id=20,
        window_class="ShockwaveFlash",
        launch_fingerprint=FINGERPRINT,
        thread_id=30,
        process_lifecycle_token=40,
    )
    AppContext.clear()
    _clock, bridge = register_server_time_services(
        Backend(window),
        registry,
        managed_process_service=StaleManaged("測試組", "角色一"),
        group_configuration_service=GroupConfiguration("測試組", "另一組"),
        group_launch_service=GroupLaunch(
            {
                "測試組": Plan(Target("entry-1", "角色一")),
                "另一組": Plan(Target("entry-2", "角色二")),
            }
        ),
        start_listener=False,
        start_memory_reader=False,
    )
    assert AppContext.get(ProcessMemoryServerTimeReader)._windows() == ()
    assert bridge.ingest(
        {
            "protocol_version": 1,
            "source_instance_identity": "transport-bound",
            "server_now_ms": 123456,
            "sample_local_flash_timer": 12,
            "sample_sequence": 1,
        },
        transport_process_id=20,
    ) is False
