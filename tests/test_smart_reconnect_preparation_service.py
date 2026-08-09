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
