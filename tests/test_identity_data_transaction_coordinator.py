from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import services.identity_data_transaction_coordinator as transaction_module
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
    IdentityDataResource,
    IdentityTransactionBlockedError,
    IdentityTransactionClosedError,
    IdentityTransactionReentryError,
    IdentityTransactionRollbackError,
    IdentityTransactionStageError,
    IdentityTransactionValidationError,
)


def _memory_snapshot(state: dict[str, object]):
    return lambda: dict(state)


def _memory_restore(state: dict[str, object]):
    def restore(value: object) -> None:
        state.clear()
        state.update(value)  # type: ignore[arg-type]

    return restore


def _stage_file(
    transaction, resource: IdentityDataResource, path: Path, value: bytes
) -> None:
    transaction.stage_file(resource, path, value, lambda candidate: candidate == value)


def test_concurrent_transactions_are_serial_and_do_not_lose_updates() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    state: dict[str, object] = {"count": 0}
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def worker() -> None:
        start.wait()
        try:
            coordinator.execute(
                lambda transaction: transaction.stage_memory(
                    IdentityDataResource.CHARACTER_DATA,
                    _memory_snapshot(state),
                    lambda candidate=int(state["count"]) + 1: state.update(count=candidate),
                    _memory_restore(state),
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert state == {"count": 2}


def test_second_file_replace_failure_restores_every_file(tmp_path: Path, monkeypatch) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    group_path = tmp_path / "group.bin"
    character_path = tmp_path / "character.bin"
    group_path.write_bytes(b"group-old")
    character_path.write_bytes(b"character-old")
    real_replace = transaction_module.os.replace
    formal_replaces = 0

    def fail_second_formal_replace(source, destination) -> None:
        nonlocal formal_replaces
        if ".candidate-" in Path(source).name:
            formal_replaces += 1
            if formal_replaces == 2:
                raise OSError("second formal replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_second_formal_replace)

    def prepare(transaction) -> None:
        _stage_file(
            transaction,
            IdentityDataResource.CHARACTER_DATA,
            character_path,
            b"character-new",
        )
        _stage_file(
            transaction,
            IdentityDataResource.GROUP_SETTINGS,
            group_path,
            b"group-new",
        )

    with pytest.raises(OSError, match="second formal replace failed"):
        coordinator.execute(prepare)

    assert group_path.read_bytes() == b"group-old"
    assert character_path.read_bytes() == b"character-old"
    assert list(tmp_path.glob("*.tmp")) == []
    assert coordinator.is_blocked is False


def test_memory_apply_failure_restores_current_and_previous_memory_and_files(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "group.bin"
    path.write_bytes(b"old-file")
    first = {"value": "first-old"}
    second = {"value": "second-old"}

    def failing_apply() -> None:
        second["value"] = "second-partial"
        raise RuntimeError("memory publish failed")

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"new-file")
        transaction.stage_memory(
            IdentityDataResource.GROUP_SETTINGS,
            _memory_snapshot(first),
            lambda: first.update(value="first-new"),
            _memory_restore(first),
        )
        transaction.stage_memory(
            IdentityDataResource.CHARACTER_DATA,
            _memory_snapshot(second),
            failing_apply,
            _memory_restore(second),
        )

    with pytest.raises(RuntimeError, match="memory publish failed"):
        coordinator.execute(prepare)

    assert path.read_bytes() == b"old-file"
    assert first == {"value": "first-old"}
    assert second == {"value": "second-old"}


def test_close_waits_for_active_transaction_and_rejects_new_transactions() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    entered = threading.Event()
    release = threading.Event()
    close_result: list[bool] = []

    def active_prepare(transaction) -> None:
        entered.set()
        assert release.wait(2)

    active = threading.Thread(target=lambda: coordinator.execute(active_prepare))
    active.start()
    assert entered.wait(1)

    closer = threading.Thread(target=lambda: close_result.append(coordinator.close_and_wait(2)))
    closer.start()
    deadline = time.monotonic() + 1
    while not coordinator.is_closing and time.monotonic() < deadline:
        time.sleep(0.005)

    with pytest.raises(IdentityTransactionClosedError):
        coordinator.execute(lambda transaction: None)
    with pytest.raises(IdentityTransactionClosedError):
        coordinator.snapshot(lambda: "must not run")

    release.set()
    active.join(timeout=2)
    closer.join(timeout=2)
    assert close_result == [True]
    assert coordinator.is_closed is True


def test_close_timeout_keeps_rejecting_then_second_close_succeeds() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    entered = threading.Event()
    release = threading.Event()

    def active_prepare(transaction) -> None:
        entered.set()
        assert release.wait(2)

    active = threading.Thread(target=lambda: coordinator.execute(active_prepare))
    active.start()
    assert entered.wait(1)
    assert coordinator.close_and_wait(0.01) is False
    with pytest.raises(IdentityTransactionClosedError):
        coordinator.execute(lambda transaction: None)
    with pytest.raises(IdentityTransactionClosedError):
        coordinator.snapshot(lambda: "must not run")
    release.set()
    active.join(timeout=2)
    assert coordinator.close_and_wait(1) is True


def test_files_and_memory_publish_the_same_candidate(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "character.bin"
    path.write_bytes(b"old")
    memory: dict[str, object] = {"value": b"old"}

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.CHARACTER_DATA, path, b"candidate")
        transaction.stage_memory(
            IdentityDataResource.CHARACTER_DATA,
            _memory_snapshot(memory),
            lambda: memory.update(value=b"candidate"),
            _memory_restore(memory),
        )
        transaction.validate_all(
            lambda: path.read_bytes() == b"old" and memory["value"] == b"old"
        )

    coordinator.execute(prepare)
    assert path.read_bytes() == memory["value"] == b"candidate"


def test_existing_file_is_restored_byte_for_byte(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "legacy.bin"
    original = b"\x00\xfflegacy\r\n\x80"
    path.write_bytes(original)
    memory = {"value": "old"}

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.CHARACTER_DATA, path, b"new")
        transaction.stage_memory(
            IdentityDataResource.CHARACTER_DATA,
            _memory_snapshot(memory),
            lambda: (_ for _ in ()).throw(RuntimeError("stop")),
            _memory_restore(memory),
        )

    with pytest.raises(RuntimeError, match="stop"):
        coordinator.execute(prepare)
    assert path.read_bytes() == original


def test_file_created_by_failed_transaction_is_removed_on_restore(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "new.bin"
    memory = {"value": "old"}

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.CHARACTER_DATA, path, b"new")
        transaction.stage_memory(
            IdentityDataResource.CHARACTER_DATA,
            _memory_snapshot(memory),
            lambda: (_ for _ in ()).throw(RuntimeError("stop")),
            _memory_restore(memory),
        )

    with pytest.raises(RuntimeError, match="stop"):
        coordinator.execute(prepare)
    assert path.exists() is False


def test_rollback_failure_preserves_both_errors_and_permanently_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first-old")
    second.write_bytes(b"second-old")
    real_replace = transaction_module.os.replace
    calls = 0

    def fail_commit_and_rollback(source, destination) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("original commit error")
        if calls == 4:
            raise PermissionError("rollback error")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_commit_and_rollback)

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, first, b"first-new")
        _stage_file(
            transaction, IdentityDataResource.CHARACTER_DATA, second, b"second-new"
        )

    with pytest.raises(IdentityTransactionRollbackError) as captured:
        coordinator.execute(prepare)

    assert str(captured.value.original_error) == "original commit error"
    assert [str(error) for error in captured.value.rollback_errors] == ["rollback error"]
    assert coordinator.is_blocked is True
    assert coordinator.snapshot(lambda: first.read_bytes()) == b"first-new"
    with pytest.raises(IdentityTransactionBlockedError) as blocked:
        coordinator.execute(lambda transaction: None)
    assert blocked.value.rollback_failure is captured.value
    assert coordinator.close_and_wait(0) is True


def test_unknown_duplicate_targets_and_duplicate_memory_are_rejected(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "one.bin"
    state = {"value": 1}

    with pytest.raises(IdentityTransactionStageError, match="unknown resource"):
        coordinator.execute(
            lambda transaction: _stage_file(transaction, "unknown", path, b"one")
        )

    def duplicate_file(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"one")
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"two")

    with pytest.raises(IdentityTransactionStageError, match="duplicate file"):
        coordinator.execute(duplicate_file)

    def duplicate_memory(transaction) -> None:
        transaction.stage_memory(
            IdentityDataResource.GROUP_SETTINGS,
            _memory_snapshot(state),
            lambda: None,
            _memory_restore(state),
        )
        transaction.stage_memory(
            IdentityDataResource.GROUP_SETTINGS,
            _memory_snapshot(state),
            lambda: None,
            _memory_restore(state),
        )

    with pytest.raises(IdentityTransactionStageError, match="duplicate memory"):
        coordinator.execute(duplicate_memory)


def test_formal_file_replacements_use_fixed_resource_order(tmp_path: Path, monkeypatch) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    paths = {
        IdentityDataResource.GROUP_SETTINGS: tmp_path / "group.bin",
        IdentityDataResource.CHARACTER_DATA: tmp_path / "character.bin",
        IdentityDataResource.WINDOW_REGISTRY: tmp_path / "window.bin",
        IdentityDataResource.RECONNECT_IDENTITY: tmp_path / "reconnect.bin",
        IdentityDataResource.CHARACTER_VIEW_CACHE: tmp_path / "view.bin",
        IdentityDataResource.CURRENT_GROUP: tmp_path / "current-group.bin",
    }
    for path in paths.values():
        path.write_bytes(b"old")
    real_replace = transaction_module.os.replace
    destinations: list[Path] = []

    def record_replace(source, destination) -> None:
        if ".candidate-" in Path(source).name:
            destinations.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", record_replace)

    def prepare(transaction) -> None:
        for resource in (
            IdentityDataResource.CURRENT_GROUP,
            IdentityDataResource.CHARACTER_VIEW_CACHE,
            IdentityDataResource.GROUP_SETTINGS,
            IdentityDataResource.RECONNECT_IDENTITY,
            IdentityDataResource.WINDOW_REGISTRY,
            IdentityDataResource.CHARACTER_DATA,
        ):
            _stage_file(transaction, resource, paths[resource], resource.value.encode())

    coordinator.execute(prepare)
    assert destinations == [
        paths[IdentityDataResource.GROUP_SETTINGS],
        paths[IdentityDataResource.CHARACTER_DATA],
        paths[IdentityDataResource.WINDOW_REGISTRY],
        paths[IdentityDataResource.RECONNECT_IDENTITY],
        paths[IdentityDataResource.CHARACTER_VIEW_CACHE],
        paths[IdentityDataResource.CURRENT_GROUP],
    ]


def test_same_thread_reentry_is_rejected_without_deadlock() -> None:
    coordinator = IdentityDataTransactionCoordinator()

    def prepare(transaction) -> None:
        coordinator.snapshot(lambda: None)

    with pytest.raises(IdentityTransactionReentryError):
        coordinator.execute(prepare)


def test_all_validators_run_before_any_formal_replace(tmp_path: Path, monkeypatch) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"old")
    second.write_bytes(b"old")
    events: list[str] = []
    real_replace = transaction_module.os.replace

    def record_replace(source, destination) -> None:
        if ".candidate-" in Path(source).name:
            events.append(f"replace:{Path(destination).name}")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", record_replace)

    def prepare(transaction) -> None:
        transaction.stage_file(
            IdentityDataResource.CHARACTER_DATA,
            second,
            b"second",
            lambda candidate: events.append("validate:second") or True,
        )
        transaction.stage_file(
            IdentityDataResource.GROUP_SETTINGS,
            first,
            b"first",
            lambda candidate: events.append("validate:first") or True,
        )
        transaction.validate_all(lambda: events.append("validate:all") or True)

    coordinator.execute(prepare)
    assert events == [
        "validate:first",
        "validate:second",
        "validate:all",
        "replace:first.bin",
        "replace:second.bin",
    ]


def test_failed_validator_prevents_every_formal_replace(tmp_path: Path, monkeypatch) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "value.bin"
    path.write_bytes(b"old")
    replacements: list[Path] = []
    real_replace = transaction_module.os.replace

    def record_replace(source, destination) -> None:
        replacements.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", record_replace)

    with pytest.raises(IdentityTransactionValidationError):
        coordinator.execute(
            lambda transaction: transaction.stage_file(
                IdentityDataResource.GROUP_SETTINGS,
                path,
                b"new",
                lambda candidate: False,
            )
        )

    assert replacements == []
    assert path.read_bytes() == b"old"


def test_same_file_path_cannot_be_staged_under_different_resources(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "shared.bin"

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"one")
        _stage_file(transaction, IdentityDataResource.CHARACTER_DATA, path, b"two")

    with pytest.raises(IdentityTransactionStageError, match="duplicate file"):
        coordinator.execute(prepare)


def test_execute_reentry_is_rejected_without_deadlock() -> None:
    coordinator = IdentityDataTransactionCoordinator()

    with pytest.raises(IdentityTransactionReentryError):
        coordinator.execute(
            lambda transaction: coordinator.execute(lambda nested: None)
        )


@pytest.mark.parametrize("validator_result", [False, RuntimeError("validator raised")])
def test_whole_validator_failure_has_no_side_effects_and_coordinator_remains_usable(
    tmp_path: Path,
    monkeypatch,
    validator_result: object,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "value.bin"
    path.write_bytes(b"old")
    memory = {"value": "old"}
    temporary_writes: list[object] = []
    real_mkstemp = transaction_module.tempfile.mkstemp

    def record_mkstemp(*args, **kwargs):
        temporary_writes.append((args, kwargs))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(transaction_module.tempfile, "mkstemp", record_mkstemp)

    def validate() -> bool:
        if isinstance(validator_result, BaseException):
            raise validator_result
        return bool(validator_result)

    def prepare(transaction) -> None:
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"new")
        transaction.stage_memory(
            IdentityDataResource.GROUP_SETTINGS,
            _memory_snapshot(memory),
            lambda: memory.update(value="new"),
            _memory_restore(memory),
        )
        transaction.validate_all(validate)

    expected = RuntimeError if isinstance(validator_result, BaseException) else IdentityTransactionValidationError
    with pytest.raises(expected):
        coordinator.execute(prepare)

    assert path.read_bytes() == b"old"
    assert memory == {"value": "old"}
    assert temporary_writes == []
    coordinator.execute(lambda transaction: None)


def test_prepare_failure_has_no_side_effects_and_seals_transaction(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "value.bin"
    path.write_bytes(b"old")
    escaped: list[object] = []

    def prepare(transaction) -> None:
        escaped.append(transaction)
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"new")
        raise RuntimeError("prepare failed")

    with pytest.raises(RuntimeError, match="prepare failed"):
        coordinator.execute(prepare)

    assert path.read_bytes() == b"old"
    with pytest.raises(IdentityTransactionStageError):
        escaped[0].validate_all(lambda: True)  # type: ignore[union-attr]
    coordinator.execute(lambda transaction: None)


def test_failed_prepare_transaction_stays_sealed_inside_later_execute_and_snapshot(
    tmp_path: Path,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    escaped: list[object] = []
    path = tmp_path / "escaped.bin"

    def fail_prepare(transaction) -> None:
        escaped.append(transaction)
        raise RuntimeError("prepare failed")

    with pytest.raises(RuntimeError, match="prepare failed"):
        coordinator.execute(fail_prepare)

    def misuse_escaped_transaction() -> None:
        escaped[0].stage_file(  # type: ignore[union-attr]
            IdentityDataResource.GROUP_SETTINGS,
            path,
            b"forbidden",
            lambda candidate: True,
        )

    with pytest.raises(IdentityTransactionStageError):
        coordinator.execute(lambda current: misuse_escaped_transaction())
    with pytest.raises(IdentityTransactionStageError):
        coordinator.snapshot(misuse_escaped_transaction)
    assert path.exists() is False


def test_execute_returns_prepare_result_only_after_successful_commit(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "value.bin"
    path.write_bytes(b"old")
    expected = object()

    def successful_prepare(transaction):
        _stage_file(transaction, IdentityDataResource.GROUP_SETTINGS, path, b"new")
        return expected

    assert coordinator.execute(successful_prepare) is expected
    assert path.read_bytes() == b"new"

    def rejected_prepare(transaction):
        transaction.validate_all(lambda: False)
        return object()

    with pytest.raises(IdentityTransactionValidationError):
        coordinator.execute(rejected_prepare)


def test_successful_transaction_object_is_sealed_after_execute(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    escaped: list[object] = []
    coordinator.execute(lambda transaction: escaped.append(transaction))

    with pytest.raises(IdentityTransactionStageError):
        escaped[0].stage_memory(  # type: ignore[union-attr]
            IdentityDataResource.CURRENT_GROUP,
            lambda: None,
            lambda: None,
            lambda value: None,
        )


def test_memory_restore_failure_preserves_apply_and_restore_errors_and_blocks() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    memory = {"value": "old"}
    calls = 0

    def restore(value: object) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("memory restore failed")

    def apply() -> None:
        memory["value"] = "partial"
        raise RuntimeError("memory apply failed")

    with pytest.raises(IdentityTransactionRollbackError) as captured:
        coordinator.execute(
            lambda transaction: transaction.stage_memory(
                IdentityDataResource.CURRENT_GROUP,
                _memory_snapshot(memory),
                apply,
                restore,
            )
        )

    assert str(captured.value.original_error) == "memory apply failed"
    assert [str(error) for error in captured.value.rollback_errors] == [
        "memory restore failed"
    ]
    assert calls == 1
    assert coordinator.is_blocked is True


def test_memory_rollback_runs_in_reverse_resource_order() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    first = {"value": "old"}
    second = {"value": "old"}
    restore_order: list[str] = []

    def restore(name: str, state: dict[str, str]):
        def callback(value: object) -> None:
            restore_order.append(name)
            state.clear()
            state.update(value)  # type: ignore[arg-type]

        return callback

    def fail_after_mutation() -> None:
        second["value"] = "partial"
        raise RuntimeError("stop")

    def prepare(transaction) -> None:
        transaction.stage_memory(
            IdentityDataResource.GROUP_SETTINGS,
            lambda: dict(first),
            lambda: first.update(value="new"),
            restore("group", first),
        )
        transaction.stage_memory(
            IdentityDataResource.CHARACTER_DATA,
            lambda: dict(second),
            fail_after_mutation,
            restore("character", second),
        )

    with pytest.raises(RuntimeError, match="stop"):
        coordinator.execute(prepare)
    assert restore_order == ["character", "group"]
    assert first == second == {"value": "old"}


def test_stage_delete_removes_existing_file_after_validation(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "obsolete.bin"
    path.write_bytes(b"legacy")

    coordinator.execute(
        lambda transaction: transaction.stage_delete(
            IdentityDataResource.CHARACTER_DATA,
            path,
            lambda original: original == b"legacy",
        )
    )

    assert path.exists() is False


def test_stage_delete_is_reversed_when_a_later_publish_fails(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "primary.bin"
    path.write_bytes(b"original")
    state = {"value": "old"}

    def prepare(transaction) -> None:
        transaction.stage_delete(
            IdentityDataResource.CHARACTER_DATA,
            path,
            lambda original: original == b"original",
        )
        transaction.stage_memory(
            IdentityDataResource.CURRENT_GROUP,
            _memory_snapshot(state),
            lambda: (_ for _ in ()).throw(RuntimeError("publish failed")),
            _memory_restore(state),
        )

    with pytest.raises(RuntimeError, match="publish failed"):
        coordinator.execute(prepare)
    assert path.read_bytes() == b"original"


def test_stage_delete_of_missing_file_remains_missing_after_rollback(tmp_path: Path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "never-created.bin"
    state = {"value": "old"}

    def prepare(transaction) -> None:
        transaction.stage_delete(
            IdentityDataResource.CHARACTER_DATA,
            path,
            lambda original: original is None,
        )
        transaction.stage_memory(
            IdentityDataResource.CURRENT_GROUP,
            _memory_snapshot(state),
            lambda: (_ for _ in ()).throw(RuntimeError("publish failed")),
            _memory_restore(state),
        )

    with pytest.raises(RuntimeError, match="publish failed"):
        coordinator.execute(prepare)
    assert path.exists() is False


def test_require_transaction_rejects_a_transaction_from_another_coordinator() -> None:
    owner = IdentityDataTransactionCoordinator()
    other = IdentityDataTransactionCoordinator()

    with pytest.raises(IdentityTransactionStageError, match="coordinator"):
        owner.execute(lambda transaction: other.require_transaction(transaction))


def test_same_resource_file_stages_commit_in_stage_order_and_rollback_in_reverse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    staged_first = tmp_path / "z-backup.bin"
    staged_second = tmp_path / "a-primary.bin"
    staged_first.write_bytes(b"z-old")
    staged_second.write_bytes(b"a-old")
    state = {"value": "old"}
    events: list[str] = []
    real_replace = transaction_module.os.replace

    def record_replace(source, destination) -> None:
        source_name = Path(source).name
        if ".candidate-" in source_name:
            events.append(f"commit:{Path(destination).name}")
        elif ".rollback-" in source_name:
            events.append(f"rollback:{Path(destination).name}")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", record_replace)

    def prepare(transaction) -> None:
        _stage_file(
            transaction,
            IdentityDataResource.CHARACTER_DATA,
            staged_first,
            b"z-new",
        )
        _stage_file(
            transaction,
            IdentityDataResource.CHARACTER_DATA,
            staged_second,
            b"a-new",
        )
        transaction.stage_memory(
            IdentityDataResource.CURRENT_GROUP,
            _memory_snapshot(state),
            lambda: (_ for _ in ()).throw(RuntimeError("publish failed")),
            _memory_restore(state),
        )

    with pytest.raises(RuntimeError, match="publish failed"):
        coordinator.execute(prepare)

    assert events == [
        "commit:z-backup.bin",
        "commit:a-primary.bin",
        "rollback:a-primary.bin",
        "rollback:z-backup.bin",
    ]


@pytest.mark.parametrize("validation", [False, RuntimeError("delete validator failed")])
def test_stage_delete_validation_failure_has_no_side_effects_and_is_reusable(
    tmp_path: Path,
    monkeypatch,
    validation: object,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "primary.bin"
    path.write_bytes(b"original")
    temporary_writes: list[object] = []
    real_mkstemp = transaction_module.tempfile.mkstemp

    def validator(original: bytes | None) -> bool:
        if isinstance(validation, BaseException):
            raise validation
        return bool(validation)

    def record_mkstemp(*args, **kwargs):
        temporary_writes.append((args, kwargs))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(transaction_module.tempfile, "mkstemp", record_mkstemp)
    expected = RuntimeError if isinstance(validation, BaseException) else IdentityTransactionValidationError

    with pytest.raises(expected):
        coordinator.execute(
            lambda transaction: transaction.stage_delete(
                IdentityDataResource.CHARACTER_DATA, path, validator
            )
        )

    assert path.read_bytes() == b"original"
    assert temporary_writes == []
    coordinator.execute(lambda transaction: None)


@pytest.mark.parametrize("delete_first", [False, True])
def test_stage_file_and_delete_cannot_target_the_same_path(
    tmp_path: Path,
    delete_first: bool,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "shared.bin"
    path.write_bytes(b"old")

    def prepare(transaction) -> None:
        stage_file = lambda: _stage_file(
            transaction, IdentityDataResource.CHARACTER_DATA, path, b"new"
        )
        stage_delete = lambda: transaction.stage_delete(
            IdentityDataResource.CHARACTER_DATA, path, lambda original: True
        )
        first, second = (stage_delete, stage_file) if delete_first else (stage_file, stage_delete)
        first()
        second()

    with pytest.raises(IdentityTransactionStageError, match="duplicate file"):
        coordinator.execute(prepare)
    assert path.read_bytes() == b"old"


def test_require_transaction_rejects_sealed_transaction_from_same_coordinator() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    escaped: list[object] = []
    coordinator.execute(lambda transaction: escaped.append(transaction))

    with pytest.raises(IdentityTransactionStageError, match="active transaction"):
        coordinator.execute(
            lambda current: coordinator.require_transaction(escaped[0])  # type: ignore[arg-type]
        )


def test_stage_delete_rollback_failure_blocks_and_preserves_both_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    path = tmp_path / "primary.bin"
    path.write_bytes(b"original")
    state = {"value": "old"}
    real_replace = transaction_module.os.replace

    def fail_delete_restore(source, destination) -> None:
        if ".rollback-" in Path(source).name and Path(destination) == path:
            raise PermissionError("delete restore failed")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_delete_restore)

    def prepare(transaction) -> None:
        transaction.stage_delete(
            IdentityDataResource.CHARACTER_DATA,
            path,
            lambda original: original == b"original",
        )
        transaction.stage_memory(
            IdentityDataResource.CURRENT_GROUP,
            _memory_snapshot(state),
            lambda: (_ for _ in ()).throw(RuntimeError("publish failed")),
            _memory_restore(state),
        )

    with pytest.raises(IdentityTransactionRollbackError) as captured:
        coordinator.execute(prepare)
    assert str(captured.value.original_error) == "publish failed"
    assert [str(error) for error in captured.value.rollback_errors] == [
        "delete restore failed"
    ]
    assert coordinator.is_blocked is True
