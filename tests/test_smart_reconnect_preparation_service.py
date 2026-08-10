import hashlib
import json
import multiprocessing
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.config_manager import ConfigManager
from core.smart_reconnect_authorization import (
    ReconnectLaunchMode,
    ReconnectRevocationReason,
    ShortcutFileIdentity,
    ShortcutSeal,
)
from core.target_window_contract import (
    ActualWindowContract,
    ActualWindowSnapshot,
    ObservationFreshness,
)
from core.window_instance import WindowInstanceToken
from core.window_registry import CharacterWindowRecord, WindowRegistry
from domain.character import Character, CharacterImportance
from services.character_view_service import CharacterViewService
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationEntry,
)
from services.group_launch_service import SavedWindowPlacement
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransactionCoordinator,
)
from services.smart_reconnect_authorization_coordinator import (
    ReconnectAuthorizationMismatchError,
    ReconnectAuthorizationState,
    ReconnectAuthorizationUnavailableError,
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_preparation_service import (
    SmartReconnectPreparationError,
    SmartReconnectPreparationService,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentityService,
)
from adapters.windows_window import Win32WindowBackend
from adapters.game_screen_recognizer import ScreenRecognition
from adapters.windows_background_capture import CaptureSample
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectEnumerationResult,
    SmartReconnectObservationRequest,
    SmartReconnectObservationSnapshot,
    SmartReconnectShortcutObservation,
    SmartReconnectWindowObservation,
    WindowsSmartReconnectObservationBroker,
)
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState


class StaticGroupService:
    def __init__(self, coordinator, group):
        self.coordinator = coordinator
        self.group_value = group

    def groups(self):
        return (self.group_value,)

    def group(self, name):
        return self.group_value if name == self.group_value.name else None


class CoordinatorBackedGroupService:
    def __init__(self, coordinator, groups=()):
        self.coordinator = coordinator
        self._groups = tuple(groups)

    def groups(self):
        return self.coordinator.read_consistent(lambda: self._groups)


class StaticRegistry(WindowRegistry):
    def __init__(self, records):
        super().__init__()
        self._records = {record.character_id: record for record in records}


class StaticFingerprintResolver:
    def __init__(self, fingerprints):
        self.fingerprints = fingerprints

    def resolve(self, paths):
        return {
            Path(path).resolve(strict=False): self.fingerprints.get(
                Path(path).resolve(strict=False)
            )
            for path in paths
        }


class StaticSealResolver:
    def __init__(self, seals):
        self.seals = seals
        self.revalidate_calls = 0

    def resolve(self, paths):
        return {
            Path(path).resolve(strict=False): self.seals.get(
                Path(path).resolve(strict=False)
            )
            for path in paths
        }

    def revalidate(self, expected):
        self.revalidate_calls += 1
        path = Path(expected.file_identity.normalized_path).resolve(strict=False)
        return self.seals.get(path) == expected


class StaticTargetWindowService:
    def __init__(self, snapshot):
        self.snapshot_value = snapshot
        self.entered = None
        self.release = None

    def actual_snapshot(self):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(2)
        return self.snapshot_value


class StaticObservationBroker:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.refresh_calls = 0
        self.entered = None
        self.release = None
        self.gate_entered = None
        self.gate_release = None

    def refresh(self, _paths=()):
        self.refresh_calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(2)
        return self.snapshot

    def latest_snapshot(self):
        return self.snapshot

    def current_snapshot(self):
        return self.snapshot

    def is_generation_current(self, generation):
        return generation == self.snapshot.generation

    def run_if_generation_current(self, generation, callback):
        initially_current = self.is_generation_current(generation)
        if self.gate_entered is not None:
            self.gate_entered.set()
        if self.gate_release is not None:
            assert self.gate_release.wait(2)
        if (
            not initially_current
            or not self.is_generation_current(generation)
        ):
            return False, None
        return True, callback()


class BrokerTargetWindowService:
    def __init__(self, broker):
        self.broker = broker

    def actual_snapshot(self):
        observed = self.broker.refresh()
        action_reader = getattr(self.broker, "action_snapshot", None)
        action = action_reader() if callable(action_reader) else None
        action_lease = (
            action[1] if action is not None and action[0] is observed else None
        )
        return ActualWindowSnapshot(
            ActualWindowSnapshot.SCHEMA_VERSION,
            targets=tuple(
                ActualWindowContract(
                    item.window.launch_fingerprint,
                    item.instance,
                    item.window.visible,
                )
                for item in observed.windows
                if item.instance is not None
            ),
            blocked_fingerprints=observed.blocked_fingerprints,
            isolated_window_count=observed.isolated_window_count,
            anonymous_isolated_window_count=(
                observed.anonymous_isolated_window_count
            ),
            failure_codes=observed.failure_codes,
            observation_generation=observed.generation,
            observation_request_serial=observed.request_serial,
            observation_static_generation=observed.static_generation,
            changed_fingerprints=observed.changed_fingerprints,
            action_lease=action_lease,
        )


class ForbiddenSealResolver:
    def resolve(self, _paths):
        raise AssertionError("formal preparation used direct seal resolution")

    def revalidate(self, _seal):
        raise AssertionError("formal preparation used direct seal revalidation")


def build_fixture(tmp_path, *, include_saved_state=True):
    identity = IdentityDataTransactionCoordinator()
    authorization = SmartReconnectAuthorizationCoordinator()
    names = ("100古", "100靈", "安全角")
    characters = tuple(
        Character(
            f"character-{index}",
            name,
            121 - index,
            (
                CharacterImportance.PRIMARY
                if index == 1
                else CharacterImportance.SECONDARY
            ),
        )
        for index, name in enumerate(names, start=1)
    )
    records = tuple(
        CharacterWindowRecord(
            character.character_id,
            character.display_name,
            aliases=(f"{character.display_name}別名",),
            group="僅供顯示的舊組別",
            role=character.importance.value,
        )
        for character in characters
    )
    registry = StaticRegistry(records)
    character_view = CharacterViewService(registry, characters, identity)
    fingerprints = ("a" * 64, "b" * 64, "c" * 64)
    entries = []
    seals = {}
    windows = []
    fingerprint_paths = {}
    for index, (character, fingerprint) in enumerate(
        zip(characters, fingerprints),
        start=1,
    ):
        shortcut = tmp_path / f"role-{index}.lnk"
        shortcut.write_bytes(f"shortcut-{index}".encode())
        entry = GroupConfigurationEntry(
            entry_id=character.character_id,
            display_name=character.display_name,
            shortcut_path=shortcut,
            role="主要" if index == 1 else "次要",
            order=index,
            placement=SavedWindowPlacement(0, 0, 800, 600),
            role_id=f"{character.display_name}登記",
        )
        entries.append(entry)
        resolved_path = shortcut.resolve(strict=False)
        fingerprint_paths[resolved_path] = fingerprint
        seals[resolved_path] = ShortcutSeal(
            ShortcutFileIdentity(str(resolved_path), 50, index),
            f"{index}" * 64,
            fingerprint,
        )
        windows.append(
            ActualWindowContract(
                fingerprint=fingerprint,
                instance=WindowInstanceToken(
                    handle=10 + index,
                    process_id=100 + index,
                    thread_id=200 + index,
                    window_class="FlashWindow",
                    rect=(0, 0, 800, 600),
                    minimized=False,
                    process_lifecycle_token=300 + index,
                ),
                visible=True,
            )
        )
    group = GroupConfiguration("group-1", "顯示組別", tuple(entries))
    group_service = StaticGroupService(identity, group)
    state_path = tmp_path / "smart_reconnect_target_identity.json"
    if include_saved_state:
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": {
                        fingerprint: {
                            "character_id": character.character_id,
                            "slot_index": index - 1,
                            "line_number": index,
                        }
                        for index, (character, fingerprint) in enumerate(
                            zip(characters, fingerprints),
                            start=1,
                        )
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    fingerprint_resolver = StaticFingerprintResolver(fingerprint_paths)
    target_identity = SmartReconnectTargetIdentityService(
        identity,
        group_service,
        character_view,
        registry,
        fingerprint_resolver,
        state_path,
    )
    target_windows = StaticTargetWindowService(
        ActualWindowSnapshot(
            ActualWindowSnapshot.SCHEMA_VERSION,
            tuple(windows),
        )
    )
    seal_resolver = StaticSealResolver(seals)
    config = ConfigManager(tmp_path / "config" / "settings.json")
    preparation = SmartReconnectPreparationService(
        target_identity_service=target_identity,
        target_window_contract_service=target_windows,
        shortcut_seal_resolver=seal_resolver,
        authorization_coordinator=authorization,
        identity_coordinator=identity,
        config=config,
        product_launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
    )
    return SimpleNamespace(
        identity=identity,
        authorization=authorization,
        characters=characters,
        group=group,
        group_service=group_service,
        target_identity=target_identity,
        target_windows=target_windows,
        seals=seal_resolver,
        fingerprint_resolver=fingerprint_resolver,
        fingerprints=fingerprints,
        preparation=preparation,
        config=config,
        state_path=state_path,
    )


def install_broker_preparation_path(fixture):
    legacy = fixture.target_windows.snapshot_value
    windows = tuple(
        SmartReconnectWindowObservation(
            window=WindowInfo(
                handle=target.instance.handle,
                title="Adobe Flash Player",
                visible=target.visible,
                minimized=target.instance.minimized,
                rect=target.instance.rect,
                process_id=target.instance.process_id,
                window_class=target.instance.window_class,
                launch_fingerprint=target.fingerprint,
                thread_id=target.instance.thread_id,
                process_lifecycle_token=(
                    target.instance.process_lifecycle_token
                ),
            ),
            instance=target.instance,
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
        for target in legacy.targets
    )
    shortcuts = tuple(
        SmartReconnectShortcutObservation(
            str(path),
            seal.launch_fingerprint,
            seal,
        )
        for path, seal in fixture.seals.seals.items()
    )
    broker = StaticObservationBroker(
        SmartReconnectObservationSnapshot(
            generation=17,
            windows=windows,
            shortcuts=shortcuts,
        )
    )
    fixture.target_identity._observation_broker = broker
    fixture.target_identity._resolver = type(
        "ForbiddenFingerprintResolver",
        (),
        {
            "resolve": lambda _self, _paths: (_ for _ in ()).throw(
                AssertionError(
                    "formal preparation used direct fingerprint resolution"
                )
            )
        },
    )()
    fixture.preparation._observation_broker = broker
    fixture.preparation._shortcut_seals = ForbiddenSealResolver()
    fixture.preparation._target_windows = BrokerTargetWindowService(broker)
    return broker


def _run_tk_preparation_lock_regression(work_dir, result_queue):
    root = None
    try:
        import tkinter as tk

        root = tk.Tk()
        title = f"smart-reconnect-lock-{os.getpid()}"
        root.title(title)
        root.geometry("320x180+20+20")
        root.update_idletasks()
        root.update()

        work_path = Path(work_dir)
        shortcut = work_path / "tk-role.lnk"
        shortcut.write_bytes(b"tk-shortcut")
        resolved_shortcut = shortcut.resolve(strict=False)
        fingerprint = "d" * 64
        identity = IdentityDataTransactionCoordinator()
        authorization = SmartReconnectAuthorizationCoordinator()
        group_service = CoordinatorBackedGroupService(identity)
        character = Character(
            "tk-character",
            "tk-role",
            100,
            CharacterImportance.PRIMARY,
        )
        record = CharacterWindowRecord(
            character.character_id,
            character.display_name,
            aliases=("tk-role",),
            role=character.importance.value,
        )
        registry = StaticRegistry((record,))
        character_view = CharacterViewService(registry, (character,), identity)
        backend = Win32WindowBackend()
        provider_entered = threading.Event()
        ui_read_started = threading.Event()
        ui_read_done = threading.Event()
        own_window_seen = threading.Event()
        catalog_calls = {"value": 0}

        def catalog():
            catalog_calls["value"] += 1
            if catalog_calls["value"] == 2:
                provider_entered.set()
                ui_read_started.wait()
            windows = backend.list_windows()
            if any(
                window.process_id == os.getpid() and window.title == title
                for window in windows
            ):
                own_window_seen.set()
            return (shortcut,)

        target_identity = SmartReconnectTargetIdentityService(
            identity,
            group_service,
            character_view,
            registry,
            StaticFingerprintResolver({resolved_shortcut: fingerprint}),
            work_path / "tk-target-state.json",
            ungrouped_shortcut_provider=lambda _fingerprint: shortcut,
            ungrouped_shortcut_catalog_provider=catalog,
        )
        target_windows = StaticTargetWindowService(
            ActualWindowSnapshot(
                ActualWindowSnapshot.SCHEMA_VERSION,
                (
                    ActualWindowContract(
                        fingerprint=fingerprint,
                        instance=WindowInstanceToken(
                            handle=41,
                            process_id=os.getpid(),
                            thread_id=threading.get_native_id(),
                            window_class="TkTopLevel",
                            rect=(20, 20, 340, 200),
                            minimized=False,
                            process_lifecycle_token=1,
                        ),
                        visible=True,
                    ),
                ),
            )
        )
        seal = ShortcutSeal(
            ShortcutFileIdentity(
                str(resolved_shortcut),
                1,
                1,
            ),
            "1" * 64,
            fingerprint,
        )
        preparation = SmartReconnectPreparationService(
            target_identity_service=target_identity,
            target_window_contract_service=target_windows,
            shortcut_seal_resolver=StaticSealResolver(
                {resolved_shortcut: seal}
            ),
            authorization_coordinator=authorization,
            identity_coordinator=identity,
            config=ConfigManager(work_path / "tk-config" / "settings.json"),
            product_launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
        )
        initial = preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        old_grant = initial.targets[0]
        prepared = []
        failures = []
        old_grant_rejected = {"value": False}
        heartbeats = {"value": 0}

        def prepare_in_background():
            try:
                prepared.append(
                    preparation.prepare(
                        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
                    )
                )
            except Exception as error:
                failures.append(repr(error))

        worker = threading.Thread(target=prepare_in_background, daemon=True)
        button = tk.Button(root, text="enable", command=worker.start)
        button.pack()

        def heartbeat():
            heartbeats["value"] += 1
            root.after(10, heartbeat)

        def read_identity_when_external_started():
            if not provider_entered.is_set():
                root.after(5, read_identity_when_external_started)
                return
            old_grant_rejected["value"] = (
                authorization.current_authorization() is None
            )
            try:
                authorization.run_authorized(
                    epoch=old_grant.authorization_epoch,
                    batch_id=old_grant.authorization_id,
                    source_generation=old_grant.source_generation,
                    fingerprint=old_grant.fingerprint,
                    character_id=old_grant.character_id,
                    instance=old_grant.instance,
                    callback=lambda current: current,
                )
            except ReconnectAuthorizationUnavailableError:
                old_grant_rejected["value"] = (
                    old_grant_rejected["value"] and True
                )
            else:
                old_grant_rejected["value"] = False
            ui_read_started.set()
            group_service.groups()
            ui_read_done.set()

        root.after(0, heartbeat)
        root.after(0, read_identity_when_external_started)
        button.invoke()
        deadline = time.monotonic() + 3.0
        heartbeat_until = time.monotonic() + 1.0
        while time.monotonic() < deadline and (
            worker.is_alive() or time.monotonic() < heartbeat_until
        ):
            root.update()
            time.sleep(0.002)
        worker.join(0.2)
        result_queue.put(
            {
                "worker_alive": worker.is_alive(),
                "prepared": len(prepared),
                "failures": failures,
                "ui_read_done": ui_read_done.is_set(),
                "heartbeats": heartbeats["value"],
                "own_window_seen": own_window_seen.is_set(),
                "old_grant_rejected": old_grant_rejected["value"],
                "catalog_calls": catalog_calls["value"],
            }
        )
    except BaseException as error:
        result_queue.put({"error": repr(error)})
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows Tk window")
def test_tk_ui_identity_read_keeps_heartbeating_during_ungrouped_preparation(
    tmp_path,
):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_run_tk_preparation_lock_regression,
        args=(str(tmp_path), result_queue),
    )
    process.start()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(2)
        pytest.fail("Tk UI and ungrouped preparation deadlocked")

    assert process.exitcode == 0
    result = result_queue.get(timeout=1)
    assert "error" not in result
    assert result["worker_alive"] is False
    assert result["prepared"] == 1
    assert result["failures"] == []
    assert result["ui_read_done"] is True
    assert result["heartbeats"] >= 20
    assert result["own_window_seen"] is True
    assert result["old_grant_rejected"] is True
    assert result["catalog_calls"] == 2


def test_prepare_publishes_complete_identity_bound_actual_window_batch(tmp_path):
    fixture = build_fixture(tmp_path)

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert batch.source.identity_generation == fixture.identity.generation
    assert batch.source.character_ids == (
        "character-1",
        "character-2",
        "character-3",
    )
    assert tuple(target.original_slot_index for target in batch.targets) == (0, 1, 2)
    assert tuple(target.original_line_number for target in batch.targets) == (1, 2, 3)
    assert fixture.authorization.current_authorization() is batch
    assert fixture.seals.revalidate_calls == 0


def test_broker_preparation_uses_only_published_process_observation(tmp_path):
    fixture = build_fixture(tmp_path)
    broker = install_broker_preparation_path(fixture)

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert tuple(target.fingerprint for target in batch.targets) == (
        fixture.fingerprints
    )
    assert broker.refresh_calls == 1
    assert fixture.authorization.current_authorization() is batch


def test_local_broker_shortcut_isolation_preserves_sibling_grant_objects(
    tmp_path,
):
    fixture = build_fixture(tmp_path)
    broker = install_broker_preparation_path(fixture)
    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    first_by_fingerprint = {
        target.fingerprint: target for target in first.targets
    }
    changed = fixture.fingerprints[0]
    broker.snapshot = SmartReconnectObservationSnapshot(
        generation=broker.snapshot.generation + 1,
        windows=tuple(
            item
            for item in broker.snapshot.windows
            if item.window.launch_fingerprint != changed
        ),
        shortcuts=broker.snapshot.shortcuts,
        blocked_fingerprints=frozenset((changed,)),
        isolated_window_count=1,
    )

    rebound = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert rebound.target_for(changed) is None
    for fingerprint in fixture.fingerprints[1:]:
        sibling = rebound.target_for(fingerprint)
        previous = first_by_fingerprint[fingerprint]
        assert sibling == previous
        assert sibling.authorization_id == previous.authorization_id
        assert sibling.authorization_epoch == previous.authorization_epoch
        assert sibling.source_generation == previous.source_generation


def test_broker_wait_holds_no_identity_or_config_lock_and_rejects_stale_result(
    tmp_path,
):
    fixture = build_fixture(tmp_path)
    broker = install_broker_preparation_path(fixture)
    entered = threading.Event()
    release = threading.Event()
    broker.entered = entered
    broker.release = release
    prepared = []
    failures = []

    def prepare_in_background():
        try:
            prepared.append(
                fixture.preparation.prepare(
                    launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
                )
            )
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=prepare_in_background)
    worker.start()
    assert entered.wait(2)
    assert fixture.authorization.current_authorization() is None

    identity_read = threading.Event()
    identity_reader = threading.Thread(
        target=lambda: (
            fixture.identity.read_consistent(lambda: identity_read.set())
        )
    )
    identity_reader.start()
    assert identity_read.wait(0.5)
    fixture.config.set("changed_during_broker_observation", True)

    release.set()
    worker.join(2)
    identity_reader.join(2)

    assert worker.is_alive() is False
    assert identity_reader.is_alive() is False
    assert prepared == []
    assert len(failures) == 1
    assert isinstance(failures[0], SmartReconnectPreparationError)
    assert "changed during preparation" in str(failures[0])
    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.EMPTY


def test_corrupt_identity_state_blocks_group_actual_window_authorization(
    tmp_path,
):
    state_path = tmp_path / "smart_reconnect_target_identity.json"
    original = b"{broken"
    state_path.write_bytes(original)
    fixture = build_fixture(tmp_path, include_saved_state=False)

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert batch.targets == ()
    assert batch.source.character_ids == ()
    assert batch.isolated_fingerprints == frozenset(fixture.fingerprints)
    assert all(
        batch.target_for(fingerprint) is None
        for fingerprint in fixture.fingerprints
    )
    assert fixture.authorization.current_authorization() is batch
    assert state_path.read_bytes() == original


def test_closed_window_rebinds_only_actual_remaining_windows(tmp_path):
    fixture = build_fixture(tmp_path)
    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    fixture.target_windows.snapshot_value = replace(
        fixture.target_windows.snapshot_value,
        targets=fixture.target_windows.snapshot_value.targets[:2],
    )

    rebound = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert tuple(target.character_id for target in rebound.targets) == (
        "character-1",
        "character-2",
    )
    assert tuple(
        target.authorization_id for target in rebound.targets
    ) == tuple(target.authorization_id for target in first.targets[:2])
    assert fixture.authorization.state is ReconnectAuthorizationState.ACTIVE


def test_real_preparation_replaces_only_changed_window_grant_generation(
    tmp_path,
):
    fixture = build_fixture(tmp_path)
    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    old_target = first.targets[0]
    old_sibling = first.targets[1]
    original_window = fixture.target_windows.snapshot_value.targets[0]
    replacement_instance = replace(
        original_window.instance,
        process_lifecycle_token=(
            original_window.instance.process_lifecycle_token + 1000
        ),
    )
    fixture.target_windows.snapshot_value = replace(
        fixture.target_windows.snapshot_value,
        targets=(
            replace(original_window, instance=replacement_instance),
            *fixture.target_windows.snapshot_value.targets[1:],
        ),
    )

    rebound = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    replacement_target = rebound.target_for(old_target.fingerprint)
    current_sibling = rebound.target_for(old_sibling.fingerprint)
    assert rebound.source.source_generation == first.source.source_generation == 0
    assert replacement_target is not None
    assert replacement_target.instance == replacement_instance
    assert replacement_target.authorization_id != old_target.authorization_id
    assert replacement_target.authorization_epoch > old_target.authorization_epoch
    assert replacement_target.source_generation > old_target.source_generation
    assert current_sibling == old_sibling
    assert fixture.authorization.validate(
        epoch=replacement_target.authorization_epoch,
        batch_id=replacement_target.authorization_id,
        source_generation=replacement_target.source_generation,
        fingerprint=replacement_target.fingerprint,
        character_id=replacement_target.character_id,
        instance=replacement_target.instance,
        callback=lambda current: current,
    ) == replacement_target
    with pytest.raises(ReconnectAuthorizationMismatchError):
        fixture.authorization.validate(
            epoch=old_target.authorization_epoch,
            batch_id=old_target.authorization_id,
            source_generation=old_target.source_generation,
            fingerprint=old_target.fingerprint,
            character_id=old_target.character_id,
            instance=old_target.instance,
            callback=lambda current: current,
        )
    with pytest.raises(ReconnectAuthorizationMismatchError):
        fixture.authorization.validate(
            epoch=replacement_target.authorization_epoch,
            batch_id=replacement_target.authorization_id,
            source_generation=replacement_target.source_generation,
            fingerprint=replacement_target.fingerprint,
            character_id=replacement_target.character_id,
            instance=old_target.instance,
            callback=lambda current: current,
        )


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
def test_monitoring_authorization_allows_unknown_slot_and_line(
    tmp_path,
    launch_mode,
):
    fixture = build_fixture(tmp_path, include_saved_state=False)

    batch = fixture.preparation.prepare(launch_mode=launch_mode)

    assert len(batch.targets) == 3
    assert all(target.original_slot_index is None for target in batch.targets)
    assert all(target.original_line_number is None for target in batch.targets)
    assert fixture.authorization.current_authorization() is batch


def test_one_missing_seal_isolates_only_that_window(tmp_path):
    fixture = build_fixture(tmp_path)
    missing_path = fixture.group.entries[1].shortcut_path.resolve(strict=False)
    fixture.seals.seals.pop(missing_path)

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert tuple(target.fingerprint for target in batch.targets) == (
        fixture.fingerprints[0],
        fixture.fingerprints[2],
    )
    assert batch.isolated_fingerprints == frozenset((fixture.fingerprints[1],))
    assert batch.isolated_window_count == 1


def test_duplicate_shortcut_file_identity_isolates_conflicts_only(tmp_path):
    fixture = build_fixture(tmp_path)
    first_path = fixture.group.entries[0].shortcut_path.resolve(strict=False)
    second_path = fixture.group.entries[1].shortcut_path.resolve(strict=False)
    first_seal = fixture.seals.seals[first_path]
    second_seal = fixture.seals.seals[second_path]
    fixture.seals.seals[second_path] = replace(
        second_seal,
        file_identity=ShortcutFileIdentity(
            str(second_path),
            first_seal.file_identity.volume_serial_number,
            first_seal.file_identity.file_index,
        ),
    )

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert tuple(target.fingerprint for target in batch.targets) == (
        fixture.fingerprints[2],
    )
    assert batch.isolated_fingerprints == frozenset(fixture.fingerprints[:2])


def test_empty_actual_snapshot_remains_active_for_future_windows(tmp_path):
    fixture = build_fixture(tmp_path)
    fixture.target_windows.snapshot_value = ActualWindowSnapshot(
        ActualWindowSnapshot.SCHEMA_VERSION
    )

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert batch.targets == ()
    assert fixture.authorization.current_authorization() is batch
    assert fixture.authorization.state is ReconnectAuthorizationState.ACTIVE


def test_retained_absent_target_requires_unique_current_source_binding(tmp_path):
    fixture = build_fixture(tmp_path)
    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    fixture.target_windows.snapshot_value = replace(
        fixture.target_windows.snapshot_value,
        targets=fixture.target_windows.snapshot_value.targets[1:],
    )
    conflict_path = tmp_path / "conflicting-role.lnk"
    conflict_path.write_bytes(b"conflict")
    conflict_entry = replace(
        fixture.group.entries[1],
        shortcut_path=conflict_path,
        order=99,
        role_id="衝突角色",
    )
    fixture.group_service.group_value = replace(
        fixture.group,
        entries=(*fixture.group.entries, conflict_entry),
    )
    fixture.fingerprint_resolver.fingerprints[
        conflict_path.resolve(strict=False)
    ] = fixture.fingerprints[0]

    rebound = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
        retained_targets=(first.targets[0],),
    )

    assert tuple(target.fingerprint for target in rebound.targets) == (
        fixture.fingerprints[1],
        fixture.fingerprints[2],
    )
    assert tuple(
        target.authorization_id for target in rebound.targets
    ) == tuple(target.authorization_id for target in first.targets[1:])


def test_launch_mode_must_be_explicit_and_compatibility_can_be_requested(tmp_path):
    fixture = build_fixture(tmp_path)

    with pytest.raises(TypeError, match="explicitly"):
        fixture.preparation.prepare(launch_mode=None)
    compatibility = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.COMPATIBILITY
    )

    assert compatibility.launch_mode is ReconnectLaunchMode.COMPATIBILITY


@pytest.mark.parametrize("changed_source", ("identity", "config"))
def test_source_write_does_not_wait_for_external_preparation_and_fails_stale(
    tmp_path,
    monkeypatch,
    changed_source,
):
    fixture = build_fixture(tmp_path)
    initial = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    old_grant = initial.targets[0]
    entered = threading.Event()
    release = threading.Event()
    write_done = threading.Event()
    fixture.target_windows.entered = entered
    fixture.target_windows.release = release
    prepared = []
    failures = []
    publish_calls = []
    original_publish = fixture.authorization.publish

    def counted_publish(*args, **kwargs):
        publish_calls.append((args, kwargs))
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(fixture.authorization, "publish", counted_publish)

    def prepare_in_background():
        try:
            prepared.append(
                fixture.preparation.prepare(
                    launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
                )
            )
        except Exception as error:
            failures.append(error)

    prepare_thread = threading.Thread(target=prepare_in_background)
    prepare_thread.start()
    assert entered.wait(2)
    assert fixture.authorization.current_authorization() is None
    with pytest.raises(ReconnectAuthorizationUnavailableError):
        fixture.authorization.run_authorized(
            epoch=old_grant.authorization_epoch,
            batch_id=old_grant.authorization_id,
            source_generation=old_grant.source_generation,
            fingerprint=old_grant.fingerprint,
            character_id=old_grant.character_id,
            instance=old_grant.instance,
            callback=lambda current: current,
        )
    state = {"value": "before"}

    def write_source():
        if changed_source == "identity":
            def prepare(transaction):
                transaction.stage_memory(
                    IdentityDataResource.CHARACTER_DATA,
                    lambda: dict(state),
                    lambda: state.update(value="after"),
                    lambda original: state.update(original),
                )

            fixture.identity.execute(prepare)
        else:
            fixture.config.set("changed_during_preparation", True)
        write_done.set()

    writer = threading.Thread(target=write_source)
    writer.start()

    assert fixture.authorization.state is ReconnectAuthorizationState.REBINDING
    assert write_done.wait(0.5) is True
    release.set()
    prepare_thread.join(2)
    writer.join(2)

    assert prepare_thread.is_alive() is False
    assert writer.is_alive() is False
    assert prepared == []
    assert len(failures) == 1
    assert isinstance(failures[0], SmartReconnectPreparationError)
    assert "changed during preparation" in str(failures[0])
    assert publish_calls == []
    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.EMPTY
    assert state == (
        {"value": "after"}
        if changed_source == "identity"
        else {"value": "before"}
    )
    assert fixture.identity.generation == (1 if changed_source == "identity" else 0)
    assert fixture.config.revision == (1 if changed_source == "config" else 0)


@pytest.mark.parametrize(
    ("blocked_stage", "invalidation", "expected_reason"),
    (
        (
            "source_capture",
            "revoke",
            ReconnectRevocationReason.EXPLICIT,
        ),
        (
            "external_window_read",
            "revoke_target",
            ReconnectRevocationReason.SOURCE_CHANGED,
        ),
    ),
)
def test_revocation_invalidates_inflight_preparation_ticket(
    tmp_path,
    monkeypatch,
    blocked_stage,
    invalidation,
    expected_reason,
):
    fixture = build_fixture(tmp_path)
    initial = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    entered = threading.Event()
    release = threading.Event()
    failures = []

    if blocked_stage == "source_capture":
        original_capture = (
            fixture.target_identity.capture_source_snapshot_in_current
        )

        def blocked_capture():
            entered.set()
            assert release.wait(2)
            return original_capture()

        monkeypatch.setattr(
            fixture.target_identity,
            "capture_source_snapshot_in_current",
            blocked_capture,
        )
    else:
        fixture.target_windows.entered = entered
        fixture.target_windows.release = release

    def prepare_in_background():
        try:
            fixture.preparation.prepare(
                launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
            )
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=prepare_in_background)
    worker.start()
    assert entered.wait(2)
    assert fixture.authorization.state is ReconnectAuthorizationState.REBINDING
    assert fixture.authorization.current_authorization() is None

    if invalidation == "revoke":
        fixture.authorization.revoke(ReconnectRevocationReason.EXPLICIT)
    else:
        assert fixture.authorization.revoke_target(
            initial.targets[0].fingerprint,
            ReconnectRevocationReason.SOURCE_CHANGED,
        ) is False
    release.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert len(failures) == 1
    assert isinstance(failures[0], SmartReconnectPreparationError)
    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.EMPTY
    assert fixture.authorization.last_revocation_reason is expected_reason


def build_pending_preparation_fixture(
    tmp_path,
    *,
    role_aliases=("100古",),
):
    identity = IdentityDataTransactionCoordinator()
    authorization = SmartReconnectAuthorizationCoordinator()
    group_service = CoordinatorBackedGroupService(identity, ())
    registry = StaticRegistry((
        CharacterWindowRecord(
            "registered-other",
            "已登記其他角色",
            aliases=("其他角色",),
            role=CharacterImportance.SECONDARY.value,
        ),
    ))
    character_view = CharacterViewService(
        registry,
        (
            Character(
                "registered-other",
                "已登記其他角色",
                80,
                CharacterImportance.SECONDARY,
            ),
        ),
        identity,
    )
    fingerprints = [f"{13 + index:x}" * 64 for index in range(len(role_aliases))]
    paths = [
        tmp_path / f"unknown-{index + 1}.lnk"
        for index in range(len(role_aliases))
    ]
    for index, path in enumerate(paths, start=1):
        path.write_bytes(f"unknown-{index}".encode("ascii"))
    instances = [
        WindowInstanceToken(
            501 + index,
            601 + index,
            701 + index,
            "FlashWindow",
            (0, 0, 800, 600),
            False,
            801 + index,
        )
        for index in range(len(role_aliases))
    ]
    aliases = {
        instance.handle: alias
        for instance, alias in zip(instances, role_aliases)
    }
    fingerprint_paths = {
        path.resolve(): fingerprint
        for path, fingerprint in zip(paths, fingerprints)
    }
    seals = {
        path.resolve(): ShortcutSeal(
            ShortcutFileIdentity(path, 90, 91 + index),
            f"{index + 1}" * 64,
            fingerprint,
        )
        for index, (path, fingerprint) in enumerate(zip(paths, fingerprints))
    }
    target_identity = SmartReconnectTargetIdentityService(
        identity,
        group_service,
        character_view,
        registry,
        StaticFingerprintResolver(fingerprint_paths),
        tmp_path / "smart_reconnect_target_identity.json",
        ungrouped_shortcut_provider=lambda fingerprint: (
            paths[fingerprints.index(fingerprint)]
            if fingerprint in fingerprints
            else None
        ),
        ungrouped_shortcut_catalog_provider=lambda: tuple(paths),
    )
    target_windows = StaticTargetWindowService(
        ActualWindowSnapshot(
            ActualWindowSnapshot.SCHEMA_VERSION,
            tuple(
                ActualWindowContract(
                    fingerprint,
                    instance,
                    True,
                )
                for fingerprint, instance in zip(fingerprints, instances)
            ),
        )
    )
    seal_resolver = StaticSealResolver(seals)
    config = ConfigManager(tmp_path / "config" / "settings.json")
    preparation = SmartReconnectPreparationService(
        target_identity_service=target_identity,
        target_window_contract_service=target_windows,
        shortcut_seal_resolver=seal_resolver,
        authorization_coordinator=authorization,
        identity_coordinator=identity,
        config=config,
        product_launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
        role_identity_reader=lambda handle: SimpleNamespace(
            success=aliases.get(handle) is not None,
            role_id=aliases.get(handle),
        ),
    )
    return SimpleNamespace(
        identity=identity,
        authorization=authorization,
        target_identity=target_identity,
        target_windows=target_windows,
        seals=seal_resolver,
        fingerprint_resolver=target_identity._resolver,
        preparation=preparation,
        config=config,
        fingerprints=fingerprints,
        paths=paths,
        instances=instances,
        aliases=aliases,
    )


def install_pending_broker_path(fixture, *, generation=31):
    instance = fixture.instances[0]
    fingerprint = fixture.fingerprints[0]
    shortcut = fixture.paths[0].resolve()
    seal = fixture.seals.seals[shortcut]
    broker = StaticObservationBroker(
        SmartReconnectObservationSnapshot(
            generation=generation,
            windows=(
                SmartReconnectWindowObservation(
                    window=WindowInfo(
                        handle=instance.handle,
                        title="Adobe Flash Player",
                        visible=True,
                        minimized=instance.minimized,
                        rect=instance.rect,
                        process_id=instance.process_id,
                        window_class=instance.window_class,
                        launch_fingerprint=fingerprint,
                        thread_id=instance.thread_id,
                        process_lifecycle_token=(
                            instance.process_lifecycle_token
                        ),
                    ),
                    instance=instance,
                    sample=CaptureSample(1, 1, b"\1\0\0\0", True),
                    recognition=ScreenRecognition(
                        ReconnectScreenState.CONNECTED,
                        1.0,
                        None,
                        "connected",
                    ),
                    fresh_capture=True,
                    capture_route="visible",
                    role_id=fixture.aliases[instance.handle],
                    role_region_sha256=hashlib.sha256(
                        fixture.aliases[instance.handle].encode("utf-8")
                    ).hexdigest(),
                ),
            ),
            shortcuts=(
                SmartReconnectShortcutObservation(
                    os.fspath(shortcut),
                    fingerprint,
                    seal,
                ),
            ),
        )
    )
    fixture.target_identity._observation_broker = broker
    fixture.preparation._observation_broker = broker
    fixture.preparation._target_windows = BrokerTargetWindowService(broker)
    fixture.preparation._shortcut_seals = ForbiddenSealResolver()
    return broker


def _formal_pending_observation_worker(
    request: SmartReconnectObservationRequest,
):
    payload = json.loads(
        (Path(request.reference_dir) / "formal-observation.json").read_text(
            encoding="utf-8"
        )
    )
    windows = tuple(
        WindowInfo(
            handle=item["handle"],
            title="Adobe Flash Player",
            visible=True,
            minimized=False,
            rect=(0, 0, 800, 600),
            process_id=item["process_id"],
            window_class="FlashWindow",
            launch_fingerprint=item["fingerprint"],
            thread_id=item["thread_id"],
            process_lifecycle_token=item["process_lifecycle_token"],
        )
        for item in payload["windows"]
    )
    shortcuts = tuple(
        SmartReconnectShortcutObservation(
            item["path"],
            item["fingerprint"],
            ShortcutSeal(
                ShortcutFileIdentity(
                    item["path"],
                    item["volume_serial_number"],
                    item["file_index"],
                ),
                item["content_sha256"],
                item["fingerprint"],
            ),
        )
        for item in payload["shortcuts"]
    )
    if request.stage == "enumerate":
        return SmartReconnectEnumerationResult(
            windows=windows,
            shortcuts=shortcuts,
            foreground_handle=windows[0].handle if windows else None,
        )
    if request.stage == "window":
        window = request.window
        assert window is not None
        role_id = next(
            item["role_id"]
            for item in payload["windows"]
            if item["handle"] == window.handle
        )
        sample = CaptureSample(1, 1, b"\1\0\0\0", True)
        return SmartReconnectWindowObservation(
            window=window,
            instance=WindowInstanceToken.from_window(window),
            sample=sample,
            recognition=ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            ),
            fresh_capture=True,
            capture_route="visible",
            role_id=role_id,
            role_region_sha256=hashlib.sha256(
                role_id.encode("utf-8")
            ).hexdigest(),
        )
    if request.stage == "seal":
        return request.expected_seal
    raise AssertionError(request.stage)


def install_formal_pending_broker_path(fixture):
    payload = {
        "windows": [
            {
                "handle": instance.handle,
                "process_id": instance.process_id,
                "thread_id": instance.thread_id,
                "process_lifecycle_token": (
                    instance.process_lifecycle_token
                ),
                "fingerprint": fingerprint,
                "role_id": fixture.aliases[instance.handle],
            }
            for fingerprint, instance in zip(
                fixture.fingerprints,
                fixture.instances,
            )
        ],
        "shortcuts": [
            {
                "path": seal.file_identity.normalized_path,
                "volume_serial_number": (
                    seal.file_identity.volume_serial_number
                ),
                "file_index": seal.file_identity.file_index,
                "content_sha256": seal.content_sha256,
                "fingerprint": seal.launch_fingerprint,
            }
            for seal in fixture.seals.seals.values()
        ],
    }
    (fixture.target_identity.state_path.parent / "formal-observation.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    broker = WindowsSmartReconnectObservationBroker(
        reference_dir=fixture.target_identity.state_path.parent,
        _worker_operation=_formal_pending_observation_worker,
    )
    fixture.target_identity._observation_broker = broker
    fixture.preparation._observation_broker = broker
    fixture.preparation._target_windows = BrokerTargetWindowService(broker)
    fixture.preparation._shortcut_seals = ForbiddenSealResolver()
    fixture.preparation._role_identity_reader = None
    return broker


def test_pending_window_has_zero_grant_until_second_independent_observation(
    tmp_path,
):
    fixture = build_pending_preparation_fixture(tmp_path)

    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    first_state = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][fixture.fingerprints[0]]

    assert first.targets == ()
    assert first.target_for(fixture.fingerprints[0]) is None
    assert first.isolated_fingerprints == frozenset(fixture.fingerprints)
    assert first_state["status"] == "pending"
    assert first_state["verified_aliases"] == []

    second = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    second_generation = fixture.identity.generation
    second_state = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][fixture.fingerprints[0]]

    assert len(second.targets) == 1
    assert second_state["status"] == "confirmed"
    assert second_state["character_id"] == first_state["character_id"]
    assert second_state["verified_aliases"] == ["100古"]

    third = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert third.targets[0].authorization_id == second.targets[0].authorization_id
    assert third.targets[0].authorization_epoch == second.targets[0].authorization_epoch
    assert third.targets[0].source_generation == second.targets[0].source_generation
    assert fixture.identity.generation == second_generation


def test_unproven_background_role_never_persists_or_authorizes(tmp_path):
    fixture = build_pending_preparation_fixture(tmp_path)
    broker = install_pending_broker_path(fixture)
    observed = broker.snapshot.windows[0]
    broker.snapshot = replace(
        broker.snapshot,
        windows=(replace(
            observed,
            sample=CaptureSample(1, 1, b"\1\0\0\0", True),
            recognition=ScreenRecognition(
                ReconnectScreenState.CONNECTED,
                1.0,
                None,
                "connected",
            ),
            fresh_capture=False,
            freshness=ObservationFreshness.UNPROVEN,
            capture_route="obscured",
            role_id="100古",
            role_cache_key=None,
        ),),
    )
    state_before = (
        fixture.target_identity.state_path.read_bytes()
        if fixture.target_identity.state_path.exists()
        else None
    )
    generation_before = fixture.identity.generation

    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    second = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert first.targets == ()
    assert second.targets == ()
    assert fixture.authorization.current_authorization().targets == ()
    state_after = (
        fixture.target_identity.state_path.read_bytes()
        if fixture.target_identity.state_path.exists()
        else None
    )
    assert state_after == state_before
    assert fixture.identity.generation == generation_before


def test_bare_level_100_remains_pending_and_never_receives_a_grant(tmp_path):
    fixture = build_pending_preparation_fixture(tmp_path)
    fixture.aliases[501] = "100"

    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    second = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    saved = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][fixture.fingerprints[0]]

    assert first.targets == ()
    assert second.targets == ()
    assert saved["status"] == "pending"
    assert saved["verified_aliases"] == []
    assert saved["evidence_alias"] is None


def test_unproven_print_window_role_writes_nothing_and_has_zero_grant(tmp_path):
    fixture = build_pending_preparation_fixture(tmp_path)
    broker = install_pending_broker_path(fixture)
    observed = broker.snapshot.windows[0]
    broker.snapshot = replace(
        broker.snapshot,
        windows=(
            replace(
                observed,
                sample=CaptureSample(1, 1, b"\3\0\0\0", True),
                recognition=ScreenRecognition(
                    ReconnectScreenState.UNKNOWN,
                    None,
                    None,
                    None,
                ),
                fresh_capture=False,
                freshness=ObservationFreshness.UNPROVEN,
                capture_route="print_window",
            ),
        ),
    )
    generation_before = fixture.identity.generation

    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    second = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert first.targets == ()
    assert second.targets == ()
    assert fixture.target_identity.state_path.exists() is False
    assert fixture.identity.generation == generation_before


@pytest.mark.skipif(os.name != "nt", reason="requires spawn process broker")
def test_formal_spawn_broker_keeps_100_pending_and_confirms_full_names(
    tmp_path,
):
    bare_root = tmp_path / "bare"
    bare_root.mkdir()
    bare = build_pending_preparation_fixture(
        bare_root,
        role_aliases=("100",),
    )
    bare_broker = install_formal_pending_broker_path(bare)
    try:
        bare_first = bare.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        bare_second = bare.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        bare_state = json.loads(
            bare.target_identity.state_path.read_text(encoding="utf-8")
        )["targets"][bare.fingerprints[0]]

        assert bare_first.targets == ()
        assert bare_second.targets == ()
        assert bare_state["status"] == "pending"
        assert bare_state["verified_aliases"] == []
        assert bare_broker.current_snapshot().generation >= 2
    finally:
        assert bare_broker.close() is True

    full_root = tmp_path / "full"
    full_root.mkdir()
    full = build_pending_preparation_fixture(
        full_root,
        role_aliases=("100古", "100靈"),
    )
    full_broker = install_formal_pending_broker_path(full)
    try:
        full_first = full.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        full_second = full.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        confirmed_generation = full.identity.generation
        full_third = full.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        full_state = json.loads(
            full.target_identity.state_path.read_text(encoding="utf-8")
        )["targets"]

        assert full_first.targets == ()
        assert len(full_second.targets) == 2
        assert full_third is full_second
        assert full.identity.generation == confirmed_generation
        assert {
            target.role_aliases for target in full_second.targets
        } == {("100古",), ("100靈",)}
        assert {
            full_state[fingerprint]["status"]
            for fingerprint in full.fingerprints
        } == {"confirmed"}
        assert full_broker.current_snapshot().generation >= 2
    finally:
        assert full_broker.close() is True


@pytest.mark.skipif(os.name != "nt", reason="requires spawn process broker")
def test_formal_broker_role_region_change_requires_two_new_observations(
    tmp_path,
):
    fixture = build_pending_preparation_fixture(
        tmp_path,
        role_aliases=("100古",),
    )
    broker = install_formal_pending_broker_path(fixture)
    payload_path = tmp_path / "formal-observation.json"
    try:
        first = fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        confirmed_old = fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        old_state = json.loads(
            fixture.target_identity.state_path.read_text(encoding="utf-8")
        )["targets"][fixture.fingerprints[0]]

        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        payload["windows"][0]["role_id"] = "100靈"
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        switched_first = fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        pending_state = json.loads(
            fixture.target_identity.state_path.read_text(encoding="utf-8")
        )["targets"][fixture.fingerprints[0]]
        switched_second = fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        confirmed_generation = fixture.identity.generation
        switched_third = fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )
        confirmed_state = json.loads(
            fixture.target_identity.state_path.read_text(encoding="utf-8")
        )["targets"][fixture.fingerprints[0]]

        assert first.targets == ()
        assert len(confirmed_old.targets) == 1
        assert old_state["status"] == "confirmed"
        assert old_state["verified_aliases"] == ["100古"]
        assert switched_first.targets == ()
        assert pending_state["status"] == "pending"
        assert pending_state["verified_aliases"] == []
        assert pending_state["character_id"] == old_state["character_id"]
        assert len(switched_second.targets) == 1
        assert switched_second.targets[0].role_aliases == ("100靈",)
        assert confirmed_state["status"] == "confirmed"
        assert confirmed_state["verified_aliases"] == ["100靈"]
        assert switched_third is switched_second
        assert fixture.identity.generation == confirmed_generation
    finally:
        assert broker.close() is True


def test_observation_generation_changes_before_persistence_write_nothing(
    tmp_path,
):
    fixture = build_pending_preparation_fixture(tmp_path)
    broker = install_pending_broker_path(fixture)
    broker.gate_entered = threading.Event()
    broker.gate_release = threading.Event()
    failures = []

    def prepare_and_capture_failure():
        try:
            fixture.preparation.prepare(
                launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
            )
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=prepare_and_capture_failure)
    worker.start()
    assert broker.gate_entered.wait(2)
    broker.snapshot = replace(
        broker.snapshot,
        generation=broker.snapshot.generation + 1,
    )
    broker.gate_release.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert len(failures) == 1
    assert isinstance(failures[0], SmartReconnectPreparationError)
    assert "observation changed before identity enrollment" in str(failures[0])
    assert fixture.target_identity.state_path.exists() is False
    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.EMPTY


def test_observation_generation_changes_before_authorization_publish_nothing(
    tmp_path,
):
    fixture = build_fixture(tmp_path)
    broker = install_broker_preparation_path(fixture)
    broker.gate_entered = threading.Event()
    broker.gate_release = threading.Event()
    failures = []

    def prepare_and_capture_failure():
        try:
            fixture.preparation.prepare(
                launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
            )
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=prepare_and_capture_failure)
    worker.start()
    assert broker.gate_entered.wait(2)
    broker.snapshot = replace(
        broker.snapshot,
        generation=broker.snapshot.generation + 1,
    )
    broker.gate_release.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert len(failures) == 1
    assert isinstance(failures[0], SmartReconnectPreparationError)
    assert "observation changed during authorization publish" in str(
        failures[0]
    )
    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.EMPTY


def test_old_preparation_failure_cannot_revoke_new_preparation_ticket(
    tmp_path,
):
    fixture = build_fixture(tmp_path)
    broker = install_broker_preparation_path(fixture)
    entered = threading.Event()
    release = threading.Event()
    broker.entered = entered
    broker.release = release
    failures = []

    def prepare_once():
        return (
            fixture.preparation.prepare(
                launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
            )
        )

    def run_and_capture():
        try:
            prepare_once()
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=run_and_capture)
    worker.start()
    assert entered.wait(2)
    newer = fixture.authorization.begin_reprepare()

    release.set()
    worker.join(2)

    assert worker.is_alive() is False
    assert len(failures) == 1
    assert isinstance(failures[0], SmartReconnectPreparationError)
    assert fixture.authorization.state is ReconnectAuthorizationState.REBINDING
    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.fail_preparation(newer) is True


def test_pending_window_write_keeps_confirmed_sibling_grant_object(tmp_path):
    fixture = build_pending_preparation_fixture(tmp_path)
    fixture.preparation.prepare(launch_mode=ReconnectLaunchMode.IDENTITY_BOUND)
    confirmed = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    sibling_grant = confirmed.targets[0]

    new_path = tmp_path / "unknown-2.lnk"
    new_path.write_bytes(b"unknown-2")
    new_fingerprint = "e" * 64
    new_instance = WindowInstanceToken(
        502,
        602,
        702,
        "FlashWindow",
        (0, 0, 800, 600),
        False,
        802,
    )
    fixture.paths.append(new_path)
    fixture.fingerprints.append(new_fingerprint)
    fixture.instances.append(new_instance)
    fixture.fingerprint_resolver.fingerprints[new_path.resolve()] = (
        new_fingerprint
    )
    fixture.seals.seals[new_path.resolve()] = ShortcutSeal(
        ShortcutFileIdentity(new_path, 90, 92),
        "2" * 64,
        new_fingerprint,
    )
    fixture.aliases[502] = None
    fixture.target_windows.snapshot_value = ActualWindowSnapshot(
        ActualWindowSnapshot.SCHEMA_VERSION,
        (
            ActualWindowContract(
                fixture.fingerprints[0],
                fixture.instances[0],
                True,
            ),
            ActualWindowContract(new_fingerprint, new_instance, True),
        ),
    )

    rebound = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    pending_state = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][new_fingerprint]

    rebound_sibling = rebound.target_for(fixture.fingerprints[0])
    assert rebound_sibling is not None
    assert rebound_sibling.authorization_id == sibling_grant.authorization_id
    assert rebound_sibling.authorization_epoch == sibling_grant.authorization_epoch
    assert rebound_sibling.source_generation == sibling_grant.source_generation
    assert rebound.target_for(new_fingerprint) is None
    assert pending_state["status"] == "pending"

    next_cycle = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    next_sibling = next_cycle.target_for(fixture.fingerprints[0])
    next_pending_state = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][new_fingerprint]

    assert next_sibling == sibling_grant
    assert next_cycle.target_for(new_fingerprint) is None
    assert next_pending_state["status"] == "pending"


def test_remembered_slot_keeps_confirmed_identity_authorized_next_cycle(
    tmp_path,
):
    fixture = build_pending_preparation_fixture(tmp_path)
    fixture.preparation.prepare(launch_mode=ReconnectLaunchMode.IDENTITY_BOUND)
    confirmed = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    fingerprint = fixture.fingerprints[0]
    saved = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][fingerprint]
    before_generation = fixture.identity.generation

    assert fixture.target_identity.remember_verified_slot(
        fingerprint,
        saved["character_id"],
        2,
    ) is True
    assert fixture.identity.generation == before_generation + 1

    next_cycle = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    next_target = next_cycle.target_for(fingerprint)
    next_saved = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][fingerprint]

    assert confirmed.target_for(fingerprint) is not None
    assert next_target is not None
    assert next_target.original_slot_index == 2
    assert next_saved["status"] == "confirmed"


def test_stale_absent_sibling_generation_is_not_advanced_by_internal_write(
    tmp_path,
):
    fixture = build_pending_preparation_fixture(tmp_path)
    fixture.aliases[501] = "100古"
    second_path = tmp_path / "unknown-2.lnk"
    second_path.write_bytes(b"unknown-2")
    second_fingerprint = "e" * 64
    second_instance = WindowInstanceToken(
        502,
        602,
        702,
        "FlashWindow",
        (0, 0, 800, 600),
        False,
        802,
    )
    fixture.paths.append(second_path)
    fixture.fingerprints.append(second_fingerprint)
    fixture.instances.append(second_instance)
    fixture.aliases[502] = "100靈"
    fixture.fingerprint_resolver.fingerprints[second_path.resolve()] = (
        second_fingerprint
    )
    fixture.seals.seals[second_path.resolve()] = ShortcutSeal(
        ShortcutFileIdentity(second_path, 90, 92),
        "2" * 64,
        second_fingerprint,
    )
    both_windows = ActualWindowSnapshot(
        ActualWindowSnapshot.SCHEMA_VERSION,
        (
            ActualWindowContract(
                fixture.fingerprints[0],
                fixture.instances[0],
                True,
            ),
            ActualWindowContract(second_fingerprint, second_instance, True),
        ),
    )
    fixture.target_windows.snapshot_value = both_windows

    first = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    confirmed = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    second_grant = confirmed.target_for(second_fingerprint)
    before = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"]
    old_second_generation = before[second_fingerprint][
        "identity_generation"
    ]

    assert first.targets == ()
    assert second_grant is not None

    def stage_external_write(transaction):
        transaction.stage_memory(
            IdentityDataResource.CURRENT_GROUP,
            lambda: None,
            lambda: None,
            lambda _snapshot: None,
        )
        return True

    fixture.identity.execute(stage_external_write)
    fixture.target_windows.snapshot_value = ActualWindowSnapshot(
        ActualWindowSnapshot.SCHEMA_VERSION,
        (
            ActualWindowContract(
                fixture.fingerprints[0],
                fixture.instances[0],
                True,
            ),
        ),
    )

    stale_batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
        retained_targets=(second_grant,),
    )
    after_internal_write = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"]

    assert stale_batch.target_for(second_fingerprint) is None
    assert after_internal_write[second_fingerprint]["status"] == "confirmed"
    assert (
        after_internal_write[second_fingerprint]["identity_generation"]
        == old_second_generation
    )

    fixture.target_windows.snapshot_value = both_windows
    first_reopen_cycle = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    first_reopen_state = json.loads(
        fixture.target_identity.state_path.read_text(encoding="utf-8")
    )["targets"][second_fingerprint]

    assert first_reopen_cycle.target_for(second_fingerprint) is None
    assert first_reopen_state["status"] == "pending"

    second_reopen_cycle = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )
    replacement = second_reopen_cycle.target_for(second_fingerprint)

    assert replacement is not None
    assert replacement.authorization_id != second_grant.authorization_id
    assert replacement.source_generation > second_grant.source_generation


def test_stop_and_closed_window_preserve_persisted_pending_identity(tmp_path):
    fixture = build_pending_preparation_fixture(tmp_path)
    fixture.preparation.prepare(launch_mode=ReconnectLaunchMode.IDENTITY_BOUND)
    pending_bytes = fixture.target_identity.state_path.read_bytes()

    fixture.authorization.revoke(ReconnectRevocationReason.STOPPED)
    fixture.target_windows.snapshot_value = ActualWindowSnapshot(
        ActualWindowSnapshot.SCHEMA_VERSION,
        (),
    )
    closed = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert closed.targets == ()
    assert fixture.target_identity.state_path.read_bytes() == pending_bytes
