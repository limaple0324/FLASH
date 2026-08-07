import json
import threading

import pytest

from config.config_manager import ConfigManager
from core.window_registry import WindowHealth, WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import CharacterImportance
from domain.character_store import CharacterStore
from services.character_view_service import CharacterViewService
from services.character_note_service import CharacterNoteService
from services.current_group_publication_service import (
    CurrentGroupPublicationNotificationError,
    CurrentGroupPublicationPlan,
    CurrentGroupPublicationService,
)
from services.group_character_registration_service import (
    GroupCharacterRegistrationService,
)
from services.group_configuration_service import (
    GroupConfigurationService,
)
from services.group_selection_service import (
    GroupSelectionService,
    PlayerGroupChoice,
    PlayerGroupMember,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


def _configuration(tmp_path, coordinator):
    names = (
        "120古",
        "120靈",
        "120射",
        "120福",
        "120獵",
        "100古",
        "100靈",
        "100福",
        "100獵",
        "160福",
        "160帥",
        "大排",
        "和尚",
        "餐廳",
    )
    shortcuts = []
    for name in names:
        path = tmp_path / f"{name}.lnk"
        path.write_bytes(b"shortcut")
        shortcuts.append(path)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "14支",
                        "launch_entries": [
                            {"path": str(path)}
                            for path in shortcuts
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return GroupConfigurationService(
        tmp_path / "groups.json",
        coordinator,
        legacy_config_path=legacy,
    )


def _two_group_configuration(tmp_path, coordinator):
    paths = []
    for name in ("角色甲", "角色乙"):
        path = tmp_path / f"{name}.lnk"
        path.write_bytes(b"shortcut")
        paths.append(path)
    legacy = tmp_path / "two-groups-legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {"name": "甲組", "launch_entries": [{"path": str(paths[0])}]},
                    {"name": "乙組", "launch_entries": [{"path": str(paths[1])}]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return GroupConfigurationService(
        tmp_path / "two-groups.json",
        coordinator,
        legacy_config_path=legacy,
    )


def test_selected_group_populates_character_page_data_without_guessing_windows(
    tmp_path,
):
    registry = WindowRegistry()
    coordinator = IdentityDataTransactionCoordinator()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        _configuration(tmp_path, coordinator),
        coordinator,
    )

    profiles = service.ensure_group("14支", ())
    views = CharacterViewService(registry, profiles, coordinator).all("14支")

    assert len(views) == 14
    assert {item.display_name for item in views} >= {
        "100古",
        "120古",
        "160帥",
        "亞洛",
    }
    assert {
        (item.level, item.importance)
        for item in views
    } == {
        (100, CharacterImportance.SECONDARY.value),
        (120, CharacterImportance.PRIMARY.value),
        (160, CharacterImportance.PRIMARY.value),
    }
    assert registry_store.load().all()
    assert character_store.load() == profiles


def test_registration_rejects_dependencies_using_another_coordinator(tmp_path):
    owner = IdentityDataTransactionCoordinator()
    other = IdentityDataTransactionCoordinator()
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", owner)
    character_store = CharacterStore(tmp_path / "characters.json", owner)
    configuration = _configuration(tmp_path, owner)

    with pytest.raises(ValueError, match="injected coordinator"):
        GroupCharacterRegistrationService(
            registry,
            registry_store,
            character_store,
            configuration,
            other,
        )


def test_group_registration_is_idempotent_and_preserves_saved_note(tmp_path):
    registry = WindowRegistry()
    coordinator = IdentityDataTransactionCoordinator()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        _configuration(tmp_path, coordinator),
        coordinator,
    )
    profiles = service.ensure_group("14支", ())
    character_id = next(
        record.character_id
        for record in registry.all()
        if record.display_name == "120古"
    )
    registry.set_note(character_id, "保留備註")
    registry_store.save(registry)

    repeated = service.ensure_group("14支", profiles)

    assert repeated == profiles
    assert len(registry.all()) == 14
    assert registry.get(character_id).note == "保留備註"


def test_shared_shortcut_never_moves_character_identity_to_another_group(
    tmp_path,
):
    shared = tmp_path / "共用角色.lnk"
    shared.write_bytes(b"shortcut")
    legacy = tmp_path / "shared-legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "name": "甲組",
                        "launch_entries": [{"path": str(shared)}],
                    },
                    {
                        "name": "乙組",
                        "launch_entries": [{"path": str(shared)}],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    coordinator = IdentityDataTransactionCoordinator()
    configuration = GroupConfigurationService(
        tmp_path / "shared-groups.json",
        coordinator,
        legacy_config_path=legacy,
    )
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )

    service.ensure_group("甲組", ())
    entry_id = configuration.group("甲組").entries[0].entry_id
    registry.set_note(entry_id, "甲組資料")
    registry_store.save(registry)
    service.ensure_group("乙組", ())

    assert registry.get(entry_id).group == "甲組"
    assert registry.get(entry_id).note == "甲組資料"
    choice = GroupSelectionService(
        registry,
        legacy_config_path=configuration.path,
        configuration=configuration,
    ).find("乙組")
    assert choice.members[0].character_id is None


def test_existing_ungrouped_records_are_backfilled_without_new_identities(
    tmp_path,
):
    coordinator = IdentityDataTransactionCoordinator()
    configuration = _configuration(tmp_path, coordinator)
    group = configuration.group("14支")
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    for entry in group.entries:
        registry.register_character(
            entry.entry_id,
            (
                "亞洛"
                if entry.display_name == "160福"
                else entry.display_name
            ),
            note=(
                "原備註"
                if entry.display_name == "120古"
                else None
            ),
        )
    registry_store.save(registry)
    identities_before = {
        record.character_id for record in registry.all()
    }
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )

    profiles = service.ensure_group("14支", ())
    restored = registry_store.load()

    assert {
        record.character_id for record in restored.all()
    } == identities_before
    assert {record.group for record in restored.all()} == {"14支"}
    assert len(CharacterViewService(registry, profiles, coordinator).all("14支")) == 14
    ancient = next(
        record
        for record in restored.all()
        if record.display_name == "120古"
    )
    assert ancient.note == "原備註"
    assert ancient.role == CharacterImportance.PRIMARY.value


def test_detaching_removed_entries_preserves_identity_and_note_without_ghost_group(
    tmp_path,
):
    coordinator = IdentityDataTransactionCoordinator()
    configuration = _configuration(tmp_path, coordinator)
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )
    service.ensure_group("14支", ())
    identities = tuple(record.character_id for record in registry.all())
    noted_id = identities[0]
    registry.set_note(noted_id, "永久保留")
    registry_store.save(registry)

    detached = service.detach_entries("14支", identities)
    configuration.delete_group("14支")
    restored = registry_store.load()
    choices = GroupSelectionService(
        registry,
        legacy_config_path=tmp_path / "legacy.json",
        configuration=configuration,
    ).choices()

    assert detached == identities
    assert {record.character_id for record in restored.all()} == set(
        identities
    )
    assert all(record.group is None for record in restored.all())
    assert all(record.role is None for record in restored.all())
    assert restored.get(noted_id).note == "永久保留"
    assert choices == ()


def test_group_rename_publishes_one_candidate_across_every_identity_view(
    tmp_path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    configuration = _configuration(tmp_path, coordinator)
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    registration = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )
    profiles = registration.ensure_group("14支", ())
    character_view = CharacterViewService(registry, profiles, coordinator)
    original_group = configuration.group("14支")
    original_entry_ids = tuple(
        entry.entry_id for entry in original_group.entries
    )
    selection = GroupSelectionService(
        registry,
        configuration=configuration,
    )
    original_choice = selection.find("14支")
    workspace = WorkspaceService(
        coordinator,
        WorkspaceState(
            current_group=selection.workspace_group(
                original_choice,
                profiles,
            )
        ),
    )
    config = ConfigManager(tmp_path / "settings.json")
    config.set("current_group_name", "14支")
    publication = CurrentGroupPublicationService(
        config,
        workspace,
        coordinator,
        current_group_name_key="current_group_name",
    )

    def fail_after_commit() -> None:
        raise RuntimeError("listener observed committed rename")

    workspace.subscribe(fail_after_commit)

    def prepare(transaction):
        def rename(candidate):
            previous = candidate.group("14支")
            assert candidate.rename_group("14支", "新14支") is True
            return previous, candidate.group("新14支")

        previous, renamed = configuration.stage_candidate(
            transaction,
            rename,
        )
        reconciled = registration.stage_reconcile(
            transaction,
            profiles=character_view.profiles_in_transaction(transaction),
            group_names=(renamed.name,),
            detachments=(
                (
                    previous.name,
                    tuple(entry.entry_id for entry in previous.entries),
                ),
            ),
            group_overrides=(renamed,),
        )
        assert reconciled.groups == (renamed,)
        character_view.stage_replace(transaction, reconciled.profiles)
        choice = PlayerGroupChoice(
            group_id=renamed.group_id,
            name=renamed.name,
            character_count=len(renamed.entries),
            members=tuple(
                PlayerGroupMember(
                    entry_id=entry.entry_id,
                    display_name=entry.display_name,
                    role=entry.role,
                    role_id=entry.role_id or None,
                    character_id=entry.entry_id,
                )
                for entry in renamed.entries
            ),
        )
        return CurrentGroupPublicationPlan(
            renamed.name,
            selection.workspace_group(choice, reconciled.profiles),
            renamed,
        )

    with pytest.raises(
        CurrentGroupPublicationNotificationError,
        match="listener observed committed rename",
    ) as notification:
        publication.execute(prepare)

    renamed = notification.value.result.result
    group_payload = json.loads(configuration.path.read_text(encoding="utf-8"))
    config_payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    persisted_registry = registry_store.load()
    persisted_profiles = character_store.load()
    visible = character_view.all("新14支")

    assert renamed.name == "新14支"
    assert configuration.group("14支") is None
    assert configuration.group("新14支") == renamed
    assert tuple(group["name"] for group in group_payload["groups"]) == (
        "新14支",
    )
    assert {record.group for record in registry.all()} == {"新14支"}
    assert {record.group for record in persisted_registry.all()} == {"新14支"}
    assert {record.character_id for record in registry.all()} == set(
        original_entry_ids
    )
    assert character_store.load() == persisted_profiles == profiles
    assert len(visible) == len(original_entry_ids)
    assert {item.group for item in visible} == {"新14支"}
    assert workspace.state.current_group.name == "新14支"
    assert workspace.state.current_group.character_ids == tuple(
        entry.entry_id for entry in renamed.entries
    )
    assert config.get("current_group_name") == "新14支"
    assert config_payload["current_group_name"] == "新14支"


@pytest.mark.parametrize("group_order", [("甲組", "乙組"), ("乙組", "甲組")])
def test_bulk_reconcile_publishes_multiple_groups_once_without_lost_updates(
    tmp_path,
    monkeypatch,
    group_order,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    configuration = _two_group_configuration(tmp_path, coordinator)
    registry = WindowRegistry()
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )
    original_stage_save = registry_store.stage_save
    registry_stage_calls = 0

    def count_stage_save(transaction, candidate):
        nonlocal registry_stage_calls
        registry_stage_calls += 1
        return original_stage_save(transaction, candidate)

    monkeypatch.setattr(registry_store, "stage_save", count_stage_save)

    result = coordinator.execute(
        lambda transaction: service.stage_reconcile(
            transaction,
            profiles=(),
            group_names=group_order,
        )
    )

    assert tuple(group.name for group in result.groups if group is not None) == group_order
    assert {record.group for record in registry.all()} == {"甲組", "乙組"}
    assert {record.group for record in registry_store.load().all()} == {"甲組", "乙組"}
    assert registry_stage_calls == 1


def test_bulk_reconcile_failure_restores_character_file_registry_file_and_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    configuration = _configuration(tmp_path, coordinator)
    configuration.create_group("空組")
    registry = WindowRegistry()
    registry.register_character("sentinel", "既有角色", note="原備註")
    registry.confirm_window(
        "sentinel",
        handle=456,
        process_id=789,
        window_class="GameWindow",
        rect=(0, 0, 800, 600),
        health=WindowHealth.READY,
    )
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    registry_store.save(registry)
    character_store.save(())
    before_registry_file = registry_store.path.read_bytes()
    before_character_file = character_store.path.read_bytes()
    before_runtime = registry.all()
    service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )
    original_replace = registry.replace_runtime
    replace_calls = 0

    def fail_first_publish(candidate: WindowRegistry) -> None:
        nonlocal replace_calls
        replace_calls += 1
        original_replace(candidate)
        if replace_calls == 1:
            raise OSError("bulk runtime publish interrupted")

    monkeypatch.setattr(registry, "replace_runtime", fail_first_publish)

    with pytest.raises(OSError, match="bulk runtime publish interrupted"):
        coordinator.execute(
            lambda transaction: service.stage_reconcile(
                transaction,
                profiles=(),
                group_names=("14支", "空組"),
            )
        )

    assert character_store.path.read_bytes() == before_character_file
    assert registry_store.path.read_bytes() == before_registry_file
    assert registry.all() == before_runtime
    assert registry.get("sentinel").handle == 456
    assert registry.get("sentinel").health is WindowHealth.READY


@pytest.mark.parametrize("first_operation", ["registration", "note"])
def test_concurrent_note_and_registration_keep_both_updates_and_live_runtime(
    tmp_path,
    monkeypatch,
    first_operation,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    configuration = _two_group_configuration(tmp_path, coordinator)
    entry_id = configuration.group("甲組").entries[0].entry_id
    registry = WindowRegistry()
    registry.register_character(entry_id, "角色甲")
    registry.confirm_window(
        entry_id,
        handle=321,
        process_id=654,
        window_class="GameWindow",
        rect=(0, 0, 800, 600),
        health=WindowHealth.WARNING,
    )
    registry_store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    character_store = CharacterStore(tmp_path / "characters.json", coordinator)
    registry_store.save(registry)
    registration = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        configuration,
        coordinator,
    )
    notes = CharacterNoteService(registry, registry_store, coordinator)
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    if first_operation == "registration":
        original_stage = registration.stage_ensure_group

        def held_stage(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original_stage(*args, **kwargs)

        monkeypatch.setattr(registration, "stage_ensure_group", held_stage)
        first = lambda: registration.ensure_group("甲組", ())
        second = lambda: notes.set_note(entry_id, "並發備註")
    else:
        original_stage = notes.stage_set_note

        def held_stage(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original_stage(*args, **kwargs)

        monkeypatch.setattr(notes, "stage_set_note", held_stage)
        first = lambda: notes.set_note(entry_id, "並發備註")
        second = lambda: registration.ensure_group("甲組", ())

    def run(action) -> None:
        try:
            action()
        except BaseException as error:
            errors.append(error)

    first_thread = threading.Thread(target=run, args=(first,))
    second_thread = threading.Thread(target=run, args=(second,))
    first_thread.start()
    assert entered.wait(5)
    second_thread.start()
    release.set()
    first_thread.join(5)
    second_thread.join(5)

    assert errors == []
    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    live = registry.get(entry_id)
    persisted = registry_store.load().get(entry_id)
    assert live.note == "並發備註"
    assert live.group == "甲組"
    assert live.handle == 321
    assert live.health is WindowHealth.WARNING
    assert persisted.note == "並發備註"
    assert persisted.group == "甲組"
