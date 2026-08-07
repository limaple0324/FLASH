from __future__ import annotations

import copy
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, TypeVar


_RESULT = TypeVar("_RESULT")

class IdentityDataResource(str, Enum):
    GROUP_SETTINGS = "group_settings"
    CHARACTER_DATA = "character_data"
    WINDOW_REGISTRY = "window_registry"
    RECONNECT_IDENTITY = "reconnect_identity"
    CHARACTER_VIEW_CACHE = "character_view_cache"
    CURRENT_GROUP = "current_group"


_RESOURCE_ORDER = (
    IdentityDataResource.GROUP_SETTINGS,
    IdentityDataResource.CHARACTER_DATA,
    IdentityDataResource.WINDOW_REGISTRY,
    IdentityDataResource.RECONNECT_IDENTITY,
    IdentityDataResource.CHARACTER_VIEW_CACHE,
    IdentityDataResource.CURRENT_GROUP,
)
_RESOURCE_INDEX = {resource: index for index, resource in enumerate(_RESOURCE_ORDER)}


class IdentityTransactionError(RuntimeError):
    """Base error for identity-data transaction coordination."""


class IdentityTransactionClosedError(IdentityTransactionError):
    """Raised when a new transaction is requested during or after shutdown."""


class IdentityTransactionBlockedError(IdentityTransactionError):
    """Raised after a rollback failure permanently blocks further writes."""

    def __init__(self, rollback_failure: IdentityTransactionRollbackError) -> None:
        super().__init__("identity transactions are blocked after rollback failure")
        self.rollback_failure = rollback_failure


class IdentityTransactionReentryError(IdentityTransactionError):
    """Raised instead of deadlocking when an active callback re-enters the coordinator."""


class IdentityTransactionStageError(IdentityTransactionError):
    """Raised when a transaction contains an invalid or duplicate stage."""


class IdentityTransactionValidationError(IdentityTransactionError):
    """Raised when a candidate or whole-transaction validator rejects the plan."""


class IdentityTransactionRollbackError(IdentityTransactionError):
    """Preserves the original failure and every failure encountered during rollback."""

    def __init__(
        self,
        original_error: BaseException,
        rollback_errors: tuple[BaseException, ...],
    ) -> None:
        super().__init__(
            f"transaction failed ({original_error!r}) and rollback failed "
            f"({rollback_errors!r})"
        )
        self.original_error = original_error
        self.rollback_errors = rollback_errors


@dataclass(frozen=True)
class _FileStage:
    resource: IdentityDataResource
    path: Path
    normalized_path: str
    candidate: bytes
    validator: Callable[[bytes], object]
    original_exists: bool
    original: bytes | None
    sequence: int


@dataclass(frozen=True)
class _MemoryStage:
    resource: IdentityDataResource
    apply: Callable[[], object]
    restore: Callable[[object], object]
    original: object
    sequence: int


@dataclass
class _CandidateFile:
    stage: _FileStage
    temporary_path: Path


class IdentityDataTransaction:
    """A transaction plan that is valid only inside its coordinator callback."""

    def __init__(self, coordinator: IdentityDataTransactionCoordinator) -> None:
        self._coordinator = coordinator
        self._accepting = True
        self._files: list[_FileStage] = []
        self._memories: list[_MemoryStage] = []
        self._whole_validator: Callable[[], object] | None = None
        self._file_targets: set[str] = set()
        self._memory_resources: set[IdentityDataResource] = set()
        self._sequence = 0

    def stage_file(
        self,
        resource: IdentityDataResource,
        path: str | os.PathLike[str],
        candidate_bytes: bytes,
        validator: Callable[[bytes], object],
    ) -> None:
        self._assert_accepting()
        self._require_resource(resource)
        if not callable(validator):
            raise IdentityTransactionStageError("file validator must be callable")
        if not isinstance(candidate_bytes, bytes):
            raise IdentityTransactionStageError("candidate_bytes must be immutable bytes")

        target = Path(path).absolute()
        normalized = os.path.normcase(os.path.abspath(os.fspath(target)))
        if normalized in self._file_targets:
            raise IdentityTransactionStageError(f"duplicate file target: {target}")

        original_exists = target.exists()
        original = target.read_bytes() if original_exists else None
        self._file_targets.add(normalized)
        self._files.append(
            _FileStage(
                resource=resource,
                path=target,
                normalized_path=normalized,
                candidate=candidate_bytes,
                validator=validator,
                original_exists=original_exists,
                original=original,
                sequence=self._next_sequence(),
            )
        )

    def stage_memory(
        self,
        resource: IdentityDataResource,
        snapshot_reader: Callable[[], object],
        apply: Callable[[], object],
        restore: Callable[[object], object],
    ) -> None:
        self._assert_accepting()
        self._require_resource(resource)
        if resource in self._memory_resources:
            raise IdentityTransactionStageError(f"duplicate memory resource: {resource}")
        if not callable(snapshot_reader) or not callable(apply) or not callable(restore):
            raise IdentityTransactionStageError(
                "memory snapshot_reader, apply, and restore must be callable"
            )

        # The coordinator-owned transaction captures the live preimage here, while
        # it exclusively owns the transaction slot. Callers never supply a backup.
        original = copy.deepcopy(snapshot_reader())
        self._memory_resources.add(resource)
        self._memories.append(
            _MemoryStage(
                resource=resource,
                apply=apply,
                restore=restore,
                original=original,
                sequence=self._next_sequence(),
            )
        )

    def validate_all(self, validator: Callable[[], object]) -> None:
        self._assert_accepting()
        if self._whole_validator is not None:
            raise IdentityTransactionStageError("duplicate whole-transaction validator")
        if not callable(validator):
            raise IdentityTransactionStageError("whole-transaction validator must be callable")
        self._whole_validator = validator

    def _finish_preparing(self) -> None:
        self._accepting = False

    def _assert_accepting(self) -> None:
        if not self._accepting or not self._coordinator._is_current_owner():
            raise IdentityTransactionStageError(
                "transaction stages may only be added by the active prepare callback"
            )

    @staticmethod
    def _require_resource(resource: IdentityDataResource) -> None:
        if not isinstance(resource, IdentityDataResource):
            raise IdentityTransactionStageError(f"unknown resource: {resource}")

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def _ordered_files(self) -> list[_FileStage]:
        return sorted(
            self._files,
            key=lambda stage: (
                _RESOURCE_INDEX[stage.resource],
                stage.normalized_path,
                stage.sequence,
            ),
        )

    def _ordered_memories(self) -> list[_MemoryStage]:
        return sorted(
            self._memories,
            key=lambda stage: (_RESOURCE_INDEX[stage.resource], stage.sequence),
        )


class IdentityDataTransactionCoordinator:
    """Serializes atomic file-and-memory identity-data transactions.

    One condition protects lifecycle and ownership. The active-owner slot remains
    held logically through prepare, validation, staging, replacement, publication,
    and any rollback, while the condition can still wake shutdown waiters.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = False
        self._owner_thread_id: int | None = None
        self._closing = False
        self._closed = False
        self._rollback_failure: IdentityTransactionRollbackError | None = None

    def execute(
        self,
        prepare: Callable[[IdentityDataTransaction], _RESULT],
    ) -> _RESULT:
        if not callable(prepare):
            raise TypeError("prepare must be callable")
        self._begin_operation(write=True)
        try:
            transaction = IdentityDataTransaction(self)
            try:
                result = prepare(transaction)
            finally:
                # A transaction object is never reusable, including when prepare
                # escapes it and then raises before commit begins.
                transaction._finish_preparing()
            self._commit(transaction)
            return result
        except IdentityTransactionRollbackError as failure:
            with self._condition:
                self._rollback_failure = failure
            raise
        finally:
            self._end_operation()

    def snapshot(self, reader: Callable[[], _RESULT]) -> _RESULT:
        if not callable(reader):
            raise TypeError("reader must be callable")
        self._begin_operation(write=False)
        try:
            return reader()
        finally:
            self._end_operation()

    def close_and_wait(self, timeout: float = 5.0) -> bool:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        thread_id = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._active and self._owner_thread_id == thread_id:
                raise IdentityTransactionReentryError(
                    "an active transaction cannot close its own coordinator"
                )
            self._closing = True
            self._condition.notify_all()
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._closed = True
            return True

    @property
    def is_blocked(self) -> bool:
        with self._condition:
            return self._rollback_failure is not None

    @property
    def is_closing(self) -> bool:
        with self._condition:
            return self._closing

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def rollback_failure(self) -> IdentityTransactionRollbackError | None:
        with self._condition:
            return self._rollback_failure

    def _begin_operation(self, *, write: bool) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if self._active and self._owner_thread_id == thread_id:
                raise IdentityTransactionReentryError(
                    "same-thread coordinator re-entry is not allowed"
                )
            self._raise_if_operation_unavailable(write=write)
            while self._active:
                self._condition.wait()
                self._raise_if_operation_unavailable(write=write)
            self._raise_if_operation_unavailable(write=write)
            self._active = True
            self._owner_thread_id = thread_id

    def _end_operation(self) -> None:
        with self._condition:
            self._active = False
            self._owner_thread_id = None
            self._condition.notify_all()

    def _raise_if_operation_unavailable(self, *, write: bool) -> None:
        if self._closing or self._closed:
            raise IdentityTransactionClosedError("identity transactions are closed")
        if write and self._rollback_failure is not None:
            raise IdentityTransactionBlockedError(self._rollback_failure)

    def _is_current_owner(self) -> bool:
        with self._condition:
            return self._active and self._owner_thread_id == threading.get_ident()

    def _commit(self, transaction: IdentityDataTransaction) -> None:
        files = transaction._ordered_files()
        memories = transaction._ordered_memories()

        for stage in files:
            if stage.validator(stage.candidate) is False:
                raise IdentityTransactionValidationError(
                    f"candidate validator rejected {stage.path}"
                )
        if (
            transaction._whole_validator is not None
            and transaction._whole_validator() is False
        ):
            raise IdentityTransactionValidationError(
                "whole-transaction validator rejected the candidates"
            )

        candidates: list[_CandidateFile] = []
        try:
            for stage in files:
                candidates.append(
                    _CandidateFile(
                        stage=stage,
                        temporary_path=self._write_temporary(
                            stage.path, stage.candidate, marker="candidate"
                        ),
                    )
                )
        except BaseException:
            self._cleanup_candidates(candidates)
            raise

        touched_files: list[_FileStage] = []
        touched_memories: list[_MemoryStage] = []
        try:
            for candidate in candidates:
                # Record before replace because an operating-system failure can be
                # reported after the destination has already changed.
                touched_files.append(candidate.stage)
                os.replace(candidate.temporary_path, candidate.stage.path)
            for stage in memories:
                # The current memory stage is included so a partial mutation that
                # raises is restored along with every earlier publication.
                touched_memories.append(stage)
                stage.apply()
        except BaseException as original_error:
            rollback_errors = self._rollback(touched_files, touched_memories)
            if rollback_errors:
                failure = IdentityTransactionRollbackError(
                    original_error, tuple(rollback_errors)
                )
                raise failure from original_error
            raise
        finally:
            self._cleanup_candidates(candidates)

    def _rollback(
        self,
        touched_files: list[_FileStage],
        touched_memories: list[_MemoryStage],
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for stage in reversed(touched_memories):
            try:
                stage.restore(copy.deepcopy(stage.original))
            except BaseException as error:
                errors.append(error)
        for stage in reversed(touched_files):
            try:
                if stage.original_exists:
                    assert stage.original is not None
                    temporary = self._write_temporary(
                        stage.path, stage.original, marker="rollback"
                    )
                    try:
                        os.replace(temporary, stage.path)
                    finally:
                        self._unlink_if_present(temporary)
                else:
                    self._unlink_if_present(stage.path)
            except BaseException as error:
                errors.append(error)
        return errors

    @staticmethod
    def _write_temporary(path: Path, content: bytes, *, marker: str) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.{marker}-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            IdentityDataTransactionCoordinator._unlink_if_present(temporary_path)
            raise
        return temporary_path

    @staticmethod
    def _cleanup_candidates(candidates: list[_CandidateFile]) -> None:
        for candidate in candidates:
            IdentityDataTransactionCoordinator._unlink_if_present(
                candidate.temporary_path
            )

    @staticmethod
    def _unlink_if_present(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "IdentityDataTransaction",
    "IdentityDataTransactionCoordinator",
    "IdentityDataResource",
    "IdentityTransactionBlockedError",
    "IdentityTransactionClosedError",
    "IdentityTransactionError",
    "IdentityTransactionReentryError",
    "IdentityTransactionRollbackError",
    "IdentityTransactionStageError",
    "IdentityTransactionValidationError",
]
