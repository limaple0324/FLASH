from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Callable

import pytest

import services.identity_data_transaction_coordinator as transaction_module
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore
from services.group_configuration_service import GroupConfigurationService
from services.group_launch_service import SavedWindowPlacement
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
    IdentityTransactionStageError,
    IdentityTransactionValidationError,
)


def _characters(*identities: str) -> tuple[Character, ...]:
    return tuple(
        Character(
            character_id=identity,
            display_name=f"角色-{identity}",
            level=100 + index,
            importance=CharacterImportance.PRIMARY,
        )
        for index, identity in enumerate(identities)
    )


def _registry(*identities: str) -> WindowRegistry:
    registry = WindowRegistry()
    for identity in identities:
        registry.register_character(identity, f"視窗-{identity}")
    return registry


def _file_state(*paths: Path) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _shortcut(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.lnk"
    path.write_bytes(b"shortcut")
    return path


def _group_fixture(
    tmp_path: Path,
    coordinator: IdentityDataTransactionCoordinator,
) -> tuple[GroupConfigurationService, tuple[Path, ...]]:
    shortcuts = tuple(
        _shortcut(tmp_path, name)
        for name in ("one", "two", "three", "four")
    )
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        coordinator,
    )
    service.add_shortcuts("alpha", shortcuts[:2])
    service.add_shortcuts("beta", shortcuts[2:3])
    return service, shortcuts


def test_both_stores_require_an_explicit_coordinator(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        CharacterStore(tmp_path / "characters.json")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        WindowRegistryStore(tmp_path / "registry.json")  # type: ignore[call-arg]


def test_character_and_registry_stage_save_commit_in_one_shared_transaction(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    characters = CharacterStore(tmp_path / "characters.json", coordinator)
    registry = WindowRegistryStore(tmp_path / "registry.json", coordinator)

    def prepare(transaction) -> None:
        characters.stage_save(transaction, _characters("one", "two"))
        registry.stage_save(transaction, _registry("one", "two"))

    coordinator.execute(prepare)

    assert [item.character_id for item in characters.load()] == ["one", "two"]
    assert [item.character_id for item in registry.load().all()] == ["one", "two"]


@pytest.mark.parametrize("failed_replace", [1, 2, 3, 4])
def test_combined_store_transaction_restores_every_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch,
    failed_replace: int,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    characters = CharacterStore(tmp_path / "characters.json", coordinator)
    registry = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    coordinator.execute(
        lambda transaction: (
            characters.stage_save(transaction, _characters("first")),
            registry.stage_save(transaction, _registry("first")),
        )
    )
    coordinator.execute(
        lambda transaction: (
            characters.stage_save(transaction, _characters("second")),
            registry.stage_save(transaction, _registry("second")),
        )
    )
    observed_paths = (
        characters.backup_path,
        characters.path,
        registry.backup_path,
        registry.path,
    )
    before = _file_state(*observed_paths)
    real_replace = transaction_module.os.replace
    replacements = 0

    def fail_selected_replace(source, destination) -> None:
        nonlocal replacements
        if ".candidate-" in Path(source).name:
            replacements += 1
            if replacements == failed_replace:
                raise OSError(f"replace {failed_replace} failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_selected_replace)

    with pytest.raises(OSError, match=f"replace {failed_replace} failed"):
        coordinator.execute(
            lambda transaction: (
                characters.stage_save(transaction, _characters("new")),
                registry.stage_save(transaction, _registry("new")),
            )
        )

    assert _file_state(*observed_paths) == before
    assert coordinator.is_blocked is False


@pytest.mark.parametrize("store_kind", ["character", "registry"])
def test_primary_read_permission_error_propagates_without_recovery_side_effects(
    tmp_path: Path,
    monkeypatch,
    store_kind: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    if store_kind == "character":
        store = CharacterStore(tmp_path / "characters.json", coordinator)
        store.save(_characters("one"))
    else:
        store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
        store.save(_registry("one"))
    marker = tmp_path / "previous-marker"
    previous_flags = (True, True, marker)
    store.recovered_from_corruption = previous_flags[0]
    store.recovered_from_backup = previous_flags[1]
    store.corrupt_backup = previous_flags[2]
    primary = store.path.read_bytes()
    real_read_bytes = Path.read_bytes

    def fail_primary_read(path: Path) -> bytes:
        if path == store.path:
            raise PermissionError("primary read denied")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_primary_read)

    with pytest.raises(PermissionError, match="primary read denied"):
        store.load()

    assert real_read_bytes(store.path) == primary
    assert list(tmp_path.glob(f"{store.path.name}.corrupt*")) == []
    assert (
        store.recovered_from_corruption,
        store.recovered_from_backup,
        store.corrupt_backup,
    ) == previous_flags


@pytest.mark.parametrize("store_kind", ["character", "registry"])
def test_corrupt_recovery_stages_sidecar_before_deleting_primary(
    tmp_path: Path,
    monkeypatch,
    store_kind: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    if store_kind == "character":
        store = CharacterStore(tmp_path / "characters.json", coordinator)
    else:
        store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    store.path.write_bytes(b'{"broken": [')
    events: list[str] = []
    real_replace = transaction_module.os.replace
    real_unlink = Path.unlink

    def record_replace(source, destination) -> None:
        if ".candidate-" in Path(source).name and ".corrupt" in Path(destination).name:
            events.append("sidecar")
        real_replace(source, destination)

    def record_unlink(path: Path, *args, **kwargs) -> None:
        if path == store.path:
            events.append("primary-delete")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(transaction_module.os, "replace", record_replace)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    store.load()
    assert events == ["sidecar", "primary-delete"]


@pytest.mark.parametrize("store_kind", ["character", "registry"])
def test_corrupt_recovery_delete_failure_leaves_files_and_flags_unchanged(
    tmp_path: Path,
    monkeypatch,
    store_kind: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    if store_kind == "character":
        store = CharacterStore(tmp_path / "characters.json", coordinator)
        store.save(_characters("old"))
        store.save(_characters("new"))
    else:
        store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
        store.save(_registry("old"))
        store.save(_registry("new"))
    corrupt = b'{"broken": ['
    store.path.write_bytes(corrupt)
    old_marker = tmp_path / "old-marker"
    store.recovered_from_corruption = False
    store.recovered_from_backup = True
    store.corrupt_backup = old_marker
    real_unlink = Path.unlink

    def fail_primary_delete(path: Path, *args, **kwargs) -> None:
        if path == store.path:
            raise PermissionError("primary delete failed")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_primary_delete)

    with pytest.raises(PermissionError, match="primary delete failed"):
        store.load()

    assert store.path.read_bytes() == corrupt
    assert list(tmp_path.glob(f"{store.path.name}.corrupt*")) == []
    assert store.recovered_from_corruption is False
    assert store.recovered_from_backup is True
    assert store.corrupt_backup == old_marker


@pytest.mark.parametrize("store_kind", ["character", "registry"])
def test_corrupt_recovery_sidecar_failure_leaves_primary_and_flags_unchanged(
    tmp_path: Path,
    monkeypatch,
    store_kind: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    if store_kind == "character":
        store = CharacterStore(tmp_path / "characters.json", coordinator)
        store.save(_characters("old"))
        store.save(_characters("new"))
    else:
        store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
        store.save(_registry("old"))
        store.save(_registry("new"))
    corrupt = b'{"broken": ['
    store.path.write_bytes(corrupt)
    old_marker = tmp_path / "old-marker"
    store.recovered_from_corruption = False
    store.recovered_from_backup = True
    store.corrupt_backup = old_marker
    real_replace = transaction_module.os.replace

    def fail_sidecar_replace(source, destination) -> None:
        if ".candidate-" in Path(source).name and ".corrupt" in Path(destination).name:
            raise PermissionError("sidecar replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_sidecar_replace)

    with pytest.raises(PermissionError, match="sidecar replace failed"):
        store.load()

    assert store.path.read_bytes() == corrupt
    assert list(tmp_path.glob(f"{store.path.name}.corrupt*")) == []
    assert store.recovered_from_corruption is False
    assert store.recovered_from_backup is True
    assert store.corrupt_backup == old_marker


@pytest.mark.parametrize("store_kind", ["character", "registry"])
def test_save_stages_backup_before_primary(
    tmp_path: Path,
    monkeypatch,
    store_kind: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    if store_kind == "character":
        store = CharacterStore(tmp_path / "characters.json", coordinator)
        store.save(_characters("old"))
        candidate = _characters("new")
    else:
        store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
        store.save(_registry("old"))
        candidate = _registry("new")
    real_replace = transaction_module.os.replace
    destinations: list[Path] = []

    def record_replace(source, destination) -> None:
        if ".candidate-" in Path(source).name:
            destinations.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", record_replace)
    store.save(candidate)
    assert destinations == [store.backup_path.absolute(), store.path.absolute()]


@pytest.mark.parametrize("store_kind", ["character", "registry"])
@pytest.mark.parametrize("load_route", ["normal", "backup", "corrupt"])
def test_load_memory_publish_failure_restores_files_and_previous_flags(
    tmp_path: Path,
    monkeypatch,
    store_kind: str,
    load_route: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    if store_kind == "character":
        store = CharacterStore(tmp_path / "characters.json", coordinator)
        first, second = _characters("old"), _characters("new")
    else:
        store = WindowRegistryStore(tmp_path / "registry.json", coordinator)
        first, second = _registry("old"), _registry("new")
    store.save(first)
    if load_route in {"backup", "corrupt"}:
        store.save(second)
    if load_route == "backup":
        store.path.unlink()
    elif load_route == "corrupt":
        store.path.write_bytes(b'{"broken": [')

    marker = tmp_path / "previous-marker"
    previous_flags = (False, True, marker)
    store.recovered_from_corruption = previous_flags[0]
    store.recovered_from_backup = previous_flags[1]
    store.corrupt_backup = previous_flags[2]
    before = _file_state(store.path, store.backup_path)

    def fail_memory_publish(state) -> None:
        store.recovered_from_corruption = True
        store.recovered_from_backup = False
        store.corrupt_backup = tmp_path / "partial-marker"
        raise RuntimeError("recovery flags publish failed")

    monkeypatch.setattr(store, "_apply_recovery_state", fail_memory_publish)

    with pytest.raises(RuntimeError, match="recovery flags publish failed"):
        store.load()

    assert _file_state(store.path, store.backup_path) == before
    assert list(tmp_path.glob(f"{store.path.name}.corrupt*")) == []
    assert (
        store.recovered_from_corruption,
        store.recovered_from_backup,
        store.corrupt_backup,
    ) == previous_flags


def test_character_store_preserves_legacy_json_and_two_save_backup(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "characters.json"
    legacy = {
        "schema_version": 1,
        "characters": [
            {
                "character_id": "legacy",
                "display_name": "舊角色",
                "level": 88,
                "importance": "主號",
            }
        ],
    }
    path.write_bytes((json.dumps(legacy, ensure_ascii=False) + "\n").encode("utf-8"))
    store = CharacterStore(path, coordinator)

    assert [item.character_id for item in store.load()] == ["legacy"]
    first_bytes = path.read_bytes()
    store.save(_characters("new"))
    assert store.backup_path.read_bytes() == first_bytes
    second_bytes = path.read_bytes()
    store.save(_characters("latest"))
    assert store.backup_path.read_bytes() == second_bytes


def test_registry_store_preserves_v1_json_and_two_save_backup(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "registry.json"
    legacy = {
        "schema_version": 1,
        "characters": [
            {
                "character_id": "legacy",
                "display_name": "舊視窗",
                "handle": 999,
                "process_id": 12,
                "window_class": "Flash",
                "rect": [0, 0, 800, 600],
                "health": "ready",
                "confirmed": True,
            }
        ],
    }
    path.write_bytes((json.dumps(legacy, ensure_ascii=False) + "\n").encode("utf-8"))
    store = WindowRegistryStore(path, coordinator)

    assert [item.character_id for item in store.load().all()] == ["legacy"]
    first_bytes = path.read_bytes()
    store.save(_registry("new"))
    assert store.backup_path.read_bytes() == first_bytes
    second_bytes = path.read_bytes()
    store.save(_registry("latest"))
    assert store.backup_path.read_bytes() == second_bytes


@pytest.mark.parametrize("store_kind", ["character", "registry"])
def test_stage_save_rejects_wrong_coordinator_before_reading_store_path(
    tmp_path: Path,
    store_kind: str,
) -> None:
    owner = IdentityDataTransactionCoordinator()
    wrong = IdentityDataTransactionCoordinator()
    unreadable_path = tmp_path / "characters.json"
    unreadable_path.mkdir()
    if store_kind == "character":
        store = CharacterStore(unreadable_path, owner)
        candidate = _characters("one")
    else:
        store = WindowRegistryStore(unreadable_path, owner)
        candidate = _registry("one")

    with pytest.raises(IdentityTransactionStageError, match="coordinator"):
        wrong.execute(
            lambda transaction: store.stage_save(transaction, candidate)
        )


def test_parallel_saves_through_shared_coordinator_preserve_both_stores(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    characters = CharacterStore(tmp_path / "characters.json", coordinator)
    registry = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def save_characters() -> None:
        start.wait()
        try:
            characters.save(_characters("character"))
        except BaseException as error:  # pragma: no cover - assertion reports it
            errors.append(error)

    def save_registry() -> None:
        start.wait()
        try:
            registry.save(_registry("registry"))
        except BaseException as error:  # pragma: no cover - assertion reports it
            errors.append(error)

    threads = [
        threading.Thread(target=save_characters),
        threading.Thread(target=save_registry),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert [item.character_id for item in characters.load()] == ["character"]
    assert [item.character_id for item in registry.load().all()] == ["registry"]


def test_identity_stores_and_group_service_expose_exact_shared_coordinator(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    characters = CharacterStore(tmp_path / "characters.json", coordinator)
    registry = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    groups = GroupConfigurationService(tmp_path / "groups.json", coordinator)

    assert characters.coordinator is coordinator
    assert registry.coordinator is coordinator
    assert groups.coordinator is coordinator


def test_group_service_requires_an_explicit_shared_coordinator(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        GroupConfigurationService(tmp_path / "groups.json")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="coordinator"):
        GroupConfigurationService(
            tmp_path / "groups.json",
            object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("_groups", []),
        ("_sync_edges", {}),
        ("_root_extras", {}),
        ("migration_backup_path", None),
        ("corrupt_backup_path", None),
        ("recovered_from_backup", True),
    ],
)
def test_live_group_state_component_setters_cannot_bypass_coordinator(
    tmp_path: Path,
    attribute: str,
    value: object,
) -> None:
    service = GroupConfigurationService(
        tmp_path / "groups.json",
        IdentityDataTransactionCoordinator(),
    )

    with pytest.raises(RuntimeError, match="transaction"):
        setattr(service, attribute, value)


def test_group_candidate_validation_rejects_malformed_identity_before_writes(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service, _shortcuts = _group_fixture(tmp_path, coordinator)
    before = _file_state(service.path, service.backup_path)
    before_state = service._state

    def make_invalid(candidate) -> None:
        candidate._groups[0]["launch_entries"].append(
            {
                "entry_id": "forged",
                "path": str(tmp_path / "missing.lnk"),
                "role": "member",
            }
        )
        candidate._save()

    with pytest.raises(
        IdentityTransactionValidationError,
        match="invalid group configuration candidate",
    ):
        coordinator.execute(
            lambda transaction: service.stage_candidate(
                transaction,
                make_invalid,
            )
        )

    assert _file_state(service.path, service.backup_path) == before
    assert service._state is before_state


def _prepare_group_mutation(
    case: str,
    service: GroupConfigurationService,
    shortcuts: tuple[Path, ...],
    tmp_path: Path,
) -> Callable[[], object]:
    alpha = service.group("alpha")
    beta = service.group("beta")
    assert alpha is not None and beta is not None
    first, second = alpha.entries
    beta_main = beta.entries[0]

    if case == "create_group":
        return lambda: service.create_group("gamma")
    if case == "set_master_locked":
        return lambda: service.set_master_locked("alpha", True)
    if case == "set_launch_hotkey":
        return lambda: service.set_launch_hotkey("alpha", "F4")
    if case == "rename_group":
        return lambda: service.rename_group("alpha", "renamed")
    if case == "delete_group":
        return lambda: service.delete_group("beta")
    if case == "move_group":
        return lambda: service.move_group("beta", -1)
    if case == "reorder_group_entries":
        return lambda: service.reorder_group_entries(
            "alpha",
            (second.entry_id, first.entry_id),
        )
    if case == "update_saved_placements":
        return lambda: service.update_saved_placements(
            "alpha",
            {
                first.shortcut_path: SavedWindowPlacement(1, 2, 800, 600, 0),
                second.shortcut_path: SavedWindowPlacement(3, 4, 800, 600, 100),
            },
        )
    if case == "set_sync_target_settings":
        return lambda: service.set_sync_target_settings(
            "alpha",
            second.entry_id,
            offset_enabled=True,
            offset_x=10,
            offset_y=-10,
            delay_ms=250,
        )
    if case == "clear_sync_target_settings":
        assert service.set_sync_target_settings(
            "alpha",
            second.entry_id,
            offset_enabled=True,
            offset_x=10,
            offset_y=-10,
            delay_ms=250,
        ) is True
        return lambda: service.clear_sync_target_settings(
            "alpha",
            second.entry_id,
        )
    if case == "set_sync_base_point":
        return lambda: service.set_sync_base_point("alpha", (10, 20))
    if case == "set_role_id":
        return lambda: service.set_role_id(
            "alpha",
            first.entry_id,
            "role-one",
        )
    if case == "import_configuration":
        source = tmp_path / "import.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "groups": [
                        {
                            "name": "imported",
                            "launch_entries": [
                                {"path": str(shortcuts[3])}
                            ],
                        }
                    ],
                    "sync_edges": {},
                }
            ),
            encoding="utf-8",
        )
        return lambda: service.import_configuration(source)
    if case == "add_shortcuts":
        return lambda: service.add_shortcuts("alpha", (shortcuts[3],))
    if case == "remove_shortcut":
        return lambda: service.remove_shortcut("alpha", second.entry_id)
    if case == "set_main_entry":
        return lambda: service.set_main_entry("alpha", second.entry_id)
    if case == "clear_group":
        return lambda: service.clear_group("alpha")
    if case == "add_sync_relation":
        return lambda: service.add_sync_relation(
            first.entry_id,
            beta_main.entry_id,
        )
    if case == "remove_sync_relation":
        assert service.add_sync_relation(
            first.entry_id,
            beta_main.entry_id,
        ) is True
        return lambda: service.remove_sync_relation(
            first.entry_id,
            beta_main.entry_id,
        )
    raise AssertionError(f"unknown group mutation case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "create_group",
        "set_master_locked",
        "set_launch_hotkey",
        "rename_group",
        "delete_group",
        "move_group",
        "reorder_group_entries",
        "update_saved_placements",
        "set_sync_target_settings",
        "clear_sync_target_settings",
        "set_sync_base_point",
        "set_role_id",
        "import_configuration",
        "add_shortcuts",
        "remove_shortcut",
        "set_main_entry",
        "clear_group",
        "add_sync_relation",
        "remove_sync_relation",
    ],
)
def test_every_group_mutator_keeps_files_and_live_state_exact_on_primary_failure(
    tmp_path: Path,
    monkeypatch,
    case: str,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service, shortcuts = _group_fixture(tmp_path, coordinator)
    mutation = _prepare_group_mutation(case, service, shortcuts, tmp_path)
    before_files = _file_state(service.path, service.backup_path)
    before_state = deepcopy(service._state)
    before_state_object = service._state
    real_replace = transaction_module.os.replace

    def fail_primary_replace(source, destination) -> None:
        if (
            ".candidate-" in Path(source).name
            and Path(destination) == service.path.absolute()
        ):
            raise OSError("group primary replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_primary_replace)

    with pytest.raises(OSError, match="group primary replace failed"):
        mutation()

    assert _file_state(service.path, service.backup_path) == before_files
    assert service._state is before_state_object
    assert service._state == before_state


def test_group_stage_candidate_no_op_adds_zero_file_and_memory_stages(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService(tmp_path / "groups.json", coordinator)
    observed: list[tuple[int, int]] = []

    def prepare(transaction) -> None:
        assert service.stage_candidate(
            transaction,
            lambda candidate: candidate.add_shortcuts("empty", ()),
        ) == ()
        observed.append(
            (len(transaction._file_operations), len(transaction._memories))
        )

    coordinator.execute(prepare)

    assert observed == [(0, 0)]
    assert service.path.exists() is False
    assert service.group("empty") is None


@pytest.mark.parametrize("transaction_kind", ["wrong", "sealed"])
def test_group_stage_candidate_rejects_invalid_transaction_before_file_reads(
    tmp_path: Path,
    monkeypatch,
    transaction_kind: str,
) -> None:
    owner = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService(tmp_path / "groups.json", owner)
    real_read_bytes = Path.read_bytes

    def unexpected_read(path: Path) -> bytes:
        raise AssertionError(f"unexpected file read: {path}")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    if transaction_kind == "wrong":
        wrong = IdentityDataTransactionCoordinator()
        with pytest.raises(IdentityTransactionStageError, match="coordinator"):
            wrong.execute(
                lambda transaction: service.stage_candidate(
                    transaction,
                    lambda candidate: candidate.create_group("blocked"),
                )
            )
    else:
        captured = []
        owner.execute(lambda transaction: captured.append(transaction))
        with pytest.raises(IdentityTransactionStageError, match="active"):
            service.stage_candidate(
                captured[0],
                lambda candidate: candidate.create_group("blocked"),
            )
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)


def test_group_character_and_registry_commit_in_one_shared_transaction(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    groups, _shortcuts = _group_fixture(tmp_path, coordinator)
    characters = CharacterStore(tmp_path / "characters.json", coordinator)
    registry = WindowRegistryStore(tmp_path / "registry.json", coordinator)

    coordinator.execute(
        lambda transaction: (
            groups.stage_candidate(
                transaction,
                lambda candidate: candidate.create_group("gamma"),
            ),
            characters.stage_save(transaction, _characters("one")),
            registry.stage_save(transaction, _registry("one")),
        )
    )

    assert groups.group("gamma") is not None
    assert [item.character_id for item in characters.load()] == ["one"]
    assert [item.character_id for item in registry.load().all()] == ["one"]


@pytest.mark.parametrize("failed_replace", [1, 2, 3, 4, 5, 6])
def test_three_store_transaction_rolls_back_on_every_replace_failure(
    tmp_path: Path,
    monkeypatch,
    failed_replace: int,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    groups, _shortcuts = _group_fixture(tmp_path, coordinator)
    characters = CharacterStore(tmp_path / "characters.json", coordinator)
    registry = WindowRegistryStore(tmp_path / "registry.json", coordinator)
    characters.save(_characters("old"))
    registry.save(_registry("old"))
    observed_paths = (
        groups.backup_path,
        groups.path,
        characters.backup_path,
        characters.path,
        registry.backup_path,
        registry.path,
    )
    before_files = _file_state(*observed_paths)
    before_group_state = groups._state
    real_replace = transaction_module.os.replace
    replacements = 0

    def fail_selected_replace(source, destination) -> None:
        nonlocal replacements
        if ".candidate-" in Path(source).name:
            replacements += 1
            if replacements == failed_replace:
                raise OSError(f"replace {failed_replace} failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_selected_replace)

    with pytest.raises(OSError, match=f"replace {failed_replace} failed"):
        coordinator.execute(
            lambda transaction: (
                groups.stage_candidate(
                    transaction,
                    lambda candidate: candidate.create_group("gamma"),
                ),
                characters.stage_save(transaction, _characters("new")),
                registry.stage_save(transaction, _registry("new")),
            )
        )

    assert _file_state(*observed_paths) == before_files
    assert groups._state is before_group_state
    assert groups.group("gamma") is None
    assert [item.character_id for item in characters.load()] == ["old"]
    assert [item.character_id for item in registry.load().all()] == ["old"]


def test_each_public_group_read_uses_exactly_one_shared_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service, _shortcuts = _group_fixture(tmp_path, coordinator)
    alpha = service.group("alpha")
    beta = service.group("beta")
    assert alpha is not None and alpha.main_entry is not None
    assert beta is not None and beta.main_entry is not None
    service.add_sync_relation(
        alpha.main_entry.entry_id,
        beta.main_entry.entry_id,
    )
    real_snapshot = coordinator.snapshot
    calls = 0

    def count_snapshot(reader):
        nonlocal calls
        calls += 1
        return real_snapshot(reader)

    monkeypatch.setattr(coordinator, "snapshot", count_snapshot)
    reads = (
        lambda: service.groups(),
        lambda: service.group("alpha"),
        lambda: service.launch_hotkeys(),
        lambda: service.available_sync_members("alpha"),
        lambda: service.explicit_sync_members("alpha"),
        lambda: service.expanded_sync_members(alpha.main_entry.entry_id),
        lambda: service.migration_backup_path,
        lambda: service.corrupt_backup_path,
        lambda: service.recovered_from_backup,
    )
    for read in reads:
        calls = 0
        read()
        assert calls == 1


def test_group_read_waits_for_active_transaction_and_sees_committed_state(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service, _shortcuts = _group_fixture(tmp_path, coordinator)
    entered = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    reader_done = threading.Event()
    result: list[tuple[str, ...]] = []

    def write() -> None:
        def prepare(transaction) -> None:
            entered.set()
            assert release.wait(timeout=2)
            service.stage_candidate(
                transaction,
                lambda candidate: candidate.create_group("gamma"),
            )

        coordinator.execute(prepare)

    def read() -> None:
        reader_started.set()
        result.append(tuple(group.name for group in service.groups()))
        reader_done.set()

    writer = threading.Thread(target=write)
    reader = threading.Thread(target=read)
    writer.start()
    assert entered.wait(timeout=2)
    reader.start()
    assert reader_started.wait(timeout=2)
    assert reader_done.wait(timeout=0.05) is False
    release.set()
    writer.join(timeout=2)
    reader.join(timeout=2)

    assert writer.is_alive() is False
    assert reader.is_alive() is False
    assert result == [("alpha", "beta", "gamma")]


@pytest.mark.parametrize("managed_file", ["primary", "backup"])
def test_group_managed_read_permission_error_propagates_without_recovery(
    tmp_path: Path,
    monkeypatch,
    managed_file: str,
) -> None:
    path = tmp_path / "groups.json"
    backup = tmp_path / "groups.json.bak"
    payload = b'{"schema_version": 2, "groups": [], "sync_edges": {}}\n'
    target = path if managed_file == "primary" else backup
    target.write_bytes(payload)
    if managed_file == "backup":
        assert path.exists() is False
        assert backup.is_file()
    real_read_bytes = Path.read_bytes

    def fail_managed_read(candidate: Path) -> bytes:
        if candidate == target:
            raise PermissionError(f"{managed_file} read denied")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", fail_managed_read)

    with pytest.raises(PermissionError, match=f"{managed_file} read denied"):
        GroupConfigurationService(path, IdentityDataTransactionCoordinator())

    assert real_read_bytes(target) == payload
    assert list(tmp_path.glob("groups.json.corrupt*")) == []
    assert list(tmp_path.glob("groups.json.pre-migration*")) == []


def test_schema_migration_evidence_failure_prevents_all_migration_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "groups.json"
    original = b'{"schema_version": 1, "groups": [], "sync_edges": {}}\n'
    path.write_bytes(original)
    real_replace = transaction_module.os.replace

    def fail_evidence_replace(source, destination) -> None:
        if (
            ".candidate-" in Path(source).name
            and ".pre-migration" in Path(destination).name
        ):
            raise OSError("migration evidence failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_evidence_replace)

    with pytest.raises(OSError, match="migration evidence failed"):
        GroupConfigurationService(path, IdentityDataTransactionCoordinator())

    assert path.read_bytes() == original
    assert list(tmp_path.glob("groups.json.pre-migration*")) == []
    assert path.with_name(path.name + ".bak").exists() is False


def test_schema_migration_activity_failure_keeps_evidence_and_unpublished_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "groups.json"
    original = b'{"schema_version": 1, "groups": [], "sync_edges": {}}\n'
    path.write_bytes(original)
    coordinator = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService.__new__(GroupConfigurationService)
    real_replace = transaction_module.os.replace

    def fail_primary_replace(source, destination) -> None:
        if (
            ".candidate-" in Path(source).name
            and Path(destination) == path.absolute()
        ):
            raise OSError("migration activity failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_primary_replace)

    with pytest.raises(OSError, match="migration activity failed"):
        GroupConfigurationService.__init__(service, path, coordinator)

    evidence = tuple(tmp_path.glob("groups.json.pre-migration*"))
    assert path.read_bytes() == original
    assert len(evidence) == 1
    assert evidence[0].read_bytes() == original
    assert service._state.groups == []
    assert service._state.sync_edges == {}
    assert service._state.migration_backup_path is None


def test_corrupt_recovery_primary_failure_restores_corrupt_file_and_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "groups.json"
    backup = tmp_path / "groups.json.bak"
    corrupt = b'{"broken": ['
    valid = b'{"schema_version": 2, "groups": [], "sync_edges": {}}\n'
    path.write_bytes(corrupt)
    backup.write_bytes(valid)
    coordinator = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService.__new__(GroupConfigurationService)
    real_replace = transaction_module.os.replace

    def fail_primary_replace(source, destination) -> None:
        if (
            ".candidate-" in Path(source).name
            and Path(destination) == path.absolute()
        ):
            raise OSError("corrupt recovery primary failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_primary_replace)

    with pytest.raises(OSError, match="corrupt recovery primary failed"):
        GroupConfigurationService.__init__(service, path, coordinator)

    assert path.read_bytes() == corrupt
    assert backup.read_bytes() == valid
    assert list(tmp_path.glob("groups.json.corrupt*")) == []
    assert service._state.groups == []
    assert service._state.corrupt_backup_path is None
    assert service._state.recovered_from_backup is False


def test_group_memory_publish_failure_restores_files_and_live_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service, _shortcuts = _group_fixture(tmp_path, coordinator)
    before_files = _file_state(service.path, service.backup_path)
    before_state = deepcopy(service._state)

    def fail_memory_publish(state) -> None:
        service._state = deepcopy(state)
        raise RuntimeError("group memory publish failed")

    monkeypatch.setattr(service, "_install_state", fail_memory_publish)

    with pytest.raises(RuntimeError, match="group memory publish failed"):
        service.create_group("gamma")

    assert _file_state(service.path, service.backup_path) == before_files
    assert service._state == before_state
    assert service.group("gamma") is None


def test_corrupt_recovery_sidecar_failure_keeps_group_primary_and_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "groups.json"
    backup = tmp_path / "groups.json.bak"
    corrupt = b'{"broken": ['
    valid = b'{"schema_version": 2, "groups": [], "sync_edges": {}}\n'
    path.write_bytes(corrupt)
    backup.write_bytes(valid)
    coordinator = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService.__new__(GroupConfigurationService)
    real_replace = transaction_module.os.replace

    def fail_sidecar_replace(source, destination) -> None:
        if (
            ".candidate-" in Path(source).name
            and ".corrupt" in Path(destination).name
        ):
            raise OSError("group sidecar replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_sidecar_replace)

    with pytest.raises(OSError, match="group sidecar replace failed"):
        GroupConfigurationService.__init__(service, path, coordinator)

    assert path.read_bytes() == corrupt
    assert backup.read_bytes() == valid
    assert list(tmp_path.glob("groups.json.corrupt*")) == []
    assert service._state.groups == []
    assert service._state.corrupt_backup_path is None
    assert service._state.recovered_from_backup is False


def test_corrupt_without_backup_delete_failure_keeps_group_primary_and_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "groups.json"
    corrupt = b'{"broken": ['
    path.write_bytes(corrupt)
    coordinator = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService.__new__(GroupConfigurationService)
    real_unlink = Path.unlink

    def fail_primary_delete(candidate: Path, *args, **kwargs) -> None:
        if candidate == path.absolute():
            raise OSError("group primary delete failed")
        real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_primary_delete)

    with pytest.raises(OSError, match="group primary delete failed"):
        GroupConfigurationService.__init__(service, path, coordinator)

    assert path.read_bytes() == corrupt
    assert list(tmp_path.glob("groups.json.corrupt*")) == []
    assert service._state.groups == []
    assert service._state.corrupt_backup_path is None
    assert service._state.recovered_from_backup is False


def test_version_two_group_normalization_rewrites_disk_to_match_memory(
    tmp_path: Path,
) -> None:
    shortcut = _shortcut(tmp_path, "valid")
    path = tmp_path / "groups.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "groups": [
                {
                    "name": "alpha",
                    "launch_hotkey": "f4",
                    "master_locked": 1,
                    "launch_entries": [
                        {"path": str(shortcut)},
                        {"path": str(tmp_path / "missing.lnk")},
                    ],
                }
            ],
            "sync_edges": {"unknown": ["missing"]},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    path.write_bytes(original)
    coordinator = IdentityDataTransactionCoordinator()

    service = GroupConfigurationService(path, coordinator)

    disk_payload = json.loads(path.read_text(encoding="utf-8"))
    memory_payload = coordinator.snapshot(service._payload)
    group = service.group("alpha")
    assert disk_payload == memory_payload
    assert service.backup_path.read_bytes() == original
    assert group is not None
    assert len(group.entries) == 1
    assert group.launch_hotkey == "F4"
    assert group.master_locked is True


def test_version_two_group_normalization_failure_restores_file_and_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shortcut = _shortcut(tmp_path, "valid")
    path = tmp_path / "groups.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "groups": [
                {
                    "name": "alpha",
                    "launch_entries": [
                        {"path": str(shortcut)},
                        {"path": str(tmp_path / "missing.lnk")},
                    ],
                }
            ],
            "sync_edges": {},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    path.write_bytes(original)
    coordinator = IdentityDataTransactionCoordinator()
    service = GroupConfigurationService.__new__(GroupConfigurationService)
    real_replace = transaction_module.os.replace

    def fail_primary_replace(source, destination) -> None:
        if (
            ".candidate-" in Path(source).name
            and Path(destination) == path.absolute()
        ):
            raise OSError("normalization rewrite failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_primary_replace)

    with pytest.raises(OSError, match="normalization rewrite failed"):
        GroupConfigurationService.__init__(service, path, coordinator)

    assert path.read_bytes() == original
    assert path.with_name(path.name + ".bak").exists() is False
    assert service._state.groups == []
    assert service._state.sync_edges == {}


def test_version_two_integer_boolean_is_rewritten_as_real_boolean(
    tmp_path: Path,
) -> None:
    path = tmp_path / "groups.json"
    original = json.dumps(
        {
            "schema_version": 2,
            "groups": [
                {
                    "group_id": GroupConfigurationService.group_id_for_name(
                        "alpha"
                    ),
                    "name": "alpha",
                    "launch_entries": [],
                    "launch_hotkey": "",
                    "master_locked": 1,
                    "entry_order_customized": False,
                }
            ],
            "sync_edges": {},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    path.write_bytes(original)

    service = GroupConfigurationService(
        path,
        IdentityDataTransactionCoordinator(),
    )

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["groups"][0]["master_locked"] is True
    assert type(rewritten["groups"][0]["master_locked"]) is bool
    assert service.group("alpha").master_locked is True
    assert service.backup_path.read_bytes() == original


def test_version_two_formatting_and_key_order_difference_does_not_rewrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "groups.json"
    original = (
        '{\n  "sync_edges": {},\n  "groups": [],\n'
        '  "schema_version": 2\n}\n'
    ).encode("utf-8")
    path.write_bytes(original)

    service = GroupConfigurationService(
        path,
        IdentityDataTransactionCoordinator(),
    )

    assert service.groups() == ()
    assert path.read_bytes() == original
    assert service.backup_path.exists() is False
