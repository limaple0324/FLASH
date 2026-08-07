import json
import threading
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
    TargetWindowContract,
    TargetWindowPhase,
    TargetWindowSnapshot,
)
from core.window_registry import CharacterWindowRecord, WindowRegistry
from domain.character import Character, CharacterImportance
from domain.group import CharacterGroup
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
    ReconnectAuthorizationState,
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_preparation_service import (
    SmartReconnectPreparationError,
    SmartReconnectPreparationService,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentityService,
)
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


CURRENT_GROUP = "current_group_name"


class StaticGroupService:
    def __init__(self, coordinator, group):
        self.coordinator = coordinator
        self.group_value = group

    def groups(self):
        return (self.group_value,)

    def group(self, name):
        return self.group_value if name == self.group_value.name else None


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
        return False


class StaticTargetWindowService:
    def __init__(self, snapshot):
        self.snapshot_value = snapshot
        self.entered = None
        self.release = None

    def snapshot(self, group_name, *, expanded_sync_scope):
        assert expanded_sync_scope is False
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(2)
        return self.snapshot_value


def build_fixture(tmp_path, *, include_saved_state=True):
    identity = IdentityDataTransactionCoordinator()
    authorization = SmartReconnectAuthorizationCoordinator()
    characters = (
        Character("character-1", "青龍主號", 120, CharacterImportance.PRIMARY),
        Character("character-2", "白虎副號", 110, CharacterImportance.SECONDARY),
    )
    records = tuple(
        CharacterWindowRecord(
            character.character_id,
            character.display_name,
            aliases=(f"{character.display_name}舊稱",),
            group="第一組",
            role=character.importance.value,
        )
        for character in characters
    )
    registry = StaticRegistry(records)
    character_view = CharacterViewService(registry, characters, identity)
    fingerprints = ("a" * 64, "b" * 64)
    entries = []
    seals = {}
    windows = []
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
            role="主視窗" if index == 1 else "同步視窗",
            order=index,
            placement=SavedWindowPlacement(0, 0, 800, 600),
            role_id=f"{character.display_name}別名",
        )
        entries.append(entry)
        seals[shortcut.resolve()] = ShortcutSeal(
            ShortcutFileIdentity(str(shortcut.resolve()), 50, index),
            f"{index}" * 64,
            fingerprint,
        )
        windows.append(
            TargetWindowContract(
                schema_version=TargetWindowContract.SCHEMA_VERSION,
                group_name="第一組",
                window_code=f"W{index}",
                display_name=character.display_name,
                binding_role=entry.role,
                role_id=entry.role_id,
                character_id=character.character_id,
                process_id=100 + index,
                fingerprint=fingerprint,
                phase=TargetWindowPhase.BACKGROUND,
                safe=True,
                handle=10 + index,
                rect=(0, 0, 800, 600),
                visible=True,
                thread_id=200 + index,
                window_class="FlashWindow",
                process_lifecycle_token=300 + index,
            )
        )
    group = GroupConfiguration("group-1", "第一組", tuple(entries))
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
    target_identity = SmartReconnectTargetIdentityService(
        identity,
        group_service,
        character_view,
        registry,
        StaticFingerprintResolver(
            {
                entry.shortcut_path.resolve(): fingerprint
                for entry, fingerprint in zip(entries, fingerprints)
            }
        ),
        state_path,
    )
    target_windows = StaticTargetWindowService(
        TargetWindowSnapshot(
            TargetWindowSnapshot.SCHEMA_VERSION,
            group.name,
            tuple(windows),
        )
    )
    seal_resolver = StaticSealResolver(seals)
    workspace = WorkspaceService(
        identity,
        WorkspaceState(
            current_group=CharacterGroup(
                group.group_id,
                group.name,
                characters,
            )
        ),
    )
    config = ConfigManager(tmp_path / "config" / "settings.json")
    config.set(CURRENT_GROUP, group.name)
    preparation = SmartReconnectPreparationService(
        target_identity_service=target_identity,
        target_window_contract_service=target_windows,
        shortcut_seal_resolver=seal_resolver,
        authorization_coordinator=authorization,
        identity_coordinator=identity,
        configuration=group_service,
        character_view=character_view,
        registry=registry,
        workspace=workspace,
        config=config,
        current_group_name_key=CURRENT_GROUP,
        product_launch_mode=ReconnectLaunchMode.IDENTITY_BOUND,
    )
    return SimpleNamespace(
        identity=identity,
        authorization=authorization,
        characters=characters,
        records=records,
        registry=registry,
        character_view=character_view,
        group=group,
        group_service=group_service,
        target_identity=target_identity,
        target_windows=target_windows,
        seals=seal_resolver,
        workspace=workspace,
        config=config,
        preparation=preparation,
        state_path=state_path,
    )


def test_prepare_publishes_one_complete_identity_bound_batch_from_old_v1_state(
    tmp_path,
):
    fixture = build_fixture(tmp_path)

    batch = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
    )

    assert batch.source.identity_generation == fixture.identity.generation
    assert batch.source.character_ids == ("character-1", "character-2")
    assert tuple(target.original_slot_index for target in batch.targets) == (0, 1)
    assert tuple(target.original_line_number for target in batch.targets) == (1, 2)
    assert fixture.authorization.current_authorization() is batch
    assert fixture.seals.revalidate_calls == 0
    assert json.loads(fixture.state_path.read_text(encoding="utf-8"))["version"] == 1


def test_missing_one_role_leaves_zero_authorization(tmp_path):
    fixture = build_fixture(tmp_path)
    fixture.preparation.prepare(launch_mode=ReconnectLaunchMode.IDENTITY_BOUND)
    fixture.target_windows.snapshot_value = replace(
        fixture.target_windows.snapshot_value,
        targets=fixture.target_windows.snapshot_value.targets[:1],
    )

    with pytest.raises(SmartReconnectPreparationError):
        fixture.preparation.prepare(
            launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
        )

    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.EMPTY


@pytest.mark.parametrize(
    "launch_mode",
    (ReconnectLaunchMode.IDENTITY_BOUND, ReconnectLaunchMode.COMPATIBILITY),
)
def test_every_launch_mode_rejects_missing_slot_or_line_without_fallback(
    tmp_path,
    launch_mode,
):
    fixture = build_fixture(tmp_path, include_saved_state=False)

    with pytest.raises(SmartReconnectPreparationError):
        fixture.preparation.prepare(launch_mode=launch_mode)

    assert fixture.authorization.current_authorization() is None


def test_launch_mode_must_be_explicit_and_complete_compatibility_can_start(tmp_path):
    fixture = build_fixture(tmp_path)

    with pytest.raises(TypeError, match="explicitly"):
        fixture.preparation.prepare(launch_mode=None)
    compatibility = fixture.preparation.prepare(
        launch_mode=ReconnectLaunchMode.COMPATIBILITY
    )

    assert compatibility.launch_mode is ReconnectLaunchMode.COMPATIBILITY


def test_identity_write_waits_for_snapshot_publish_then_revokes_new_batch(tmp_path):
    fixture = build_fixture(tmp_path)
    fixture.identity.register_before_write_listener(
        lambda _generation: fixture.authorization.revoke(
            ReconnectRevocationReason.IDENTITY_WRITE
        )
    )
    entered = threading.Event()
    release = threading.Event()
    write_done = threading.Event()
    fixture.target_windows.entered = entered
    fixture.target_windows.release = release
    prepared = []
    prepare_thread = threading.Thread(
        target=lambda: prepared.append(
            fixture.preparation.prepare(
                launch_mode=ReconnectLaunchMode.IDENTITY_BOUND
            )
        )
    )
    prepare_thread.start()
    assert entered.wait(2)
    state = {"value": "before"}

    def write_identity():
        def prepare(transaction):
            transaction.stage_memory(
                IdentityDataResource.CHARACTER_DATA,
                lambda: dict(state),
                lambda: state.update(value="after"),
                lambda original: state.update(original),
            )

        fixture.identity.execute(prepare)
        write_done.set()

    writer = threading.Thread(target=write_identity)
    writer.start()

    assert fixture.authorization.current_authorization() is None
    assert fixture.authorization.state is ReconnectAuthorizationState.REBINDING
    assert write_done.wait(0.05) is False
    release.set()
    prepare_thread.join(2)
    writer.join(2)

    assert len(prepared) == 1
    assert state == {"value": "after"}
    assert fixture.identity.generation == 1
    assert fixture.authorization.current_authorization() is None
    assert (
        fixture.authorization.last_revocation_reason
        is ReconnectRevocationReason.IDENTITY_WRITE
    )
