"""Persistent transaction-backed storage for the character window registry."""

from __future__ import annotations

import json
from pathlib import Path

from core.window_registry import WindowRegistry
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)


class WindowRegistryStore:
    """Load and save registry data through one injected shared coordinator."""

    _PARSE_ERRORS = (UnicodeError, json.JSONDecodeError, ValueError, TypeError)

    def __init__(
        self,
        path: Path,
        coordinator: IdentityDataTransactionCoordinator,
    ) -> None:
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be an IdentityDataTransactionCoordinator")
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self._coordinator = coordinator
        self.recovered_from_corruption = False
        self.recovered_from_backup = False
        self.corrupt_backup: Path | None = None

    @property
    def coordinator(self) -> IdentityDataTransactionCoordinator:
        return self._coordinator

    @staticmethod
    def _deserialize(content: bytes) -> WindowRegistry:
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Registry root must be an object.")
        return WindowRegistry.from_dict(payload)

    @staticmethod
    def _serialize(registry: WindowRegistry) -> bytes:
        return (
            json.dumps(registry.to_dict(), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    def load(self) -> WindowRegistry:
        return self._coordinator.execute(self._prepare_load)

    def _prepare_load(
        self,
        transaction: IdentityDataTransaction,
    ) -> WindowRegistry:
        self._coordinator.require_transaction(transaction)
        try:
            primary = self.path.read_bytes()
        except FileNotFoundError:
            registry, from_backup = self._load_backup_or_empty()
            self._stage_recovery_state(
                transaction,
                recovered_from_corruption=False,
                recovered_from_backup=from_backup,
                corrupt_backup=None,
            )
            return registry

        try:
            registry = self._deserialize(primary)
        except self._PARSE_ERRORS:
            corrupt_path = self._next_corrupt_path()
            transaction.stage_file(
                IdentityDataResource.WINDOW_REGISTRY,
                corrupt_path,
                primary,
                lambda candidate, expected=primary: candidate == expected,
            )
            transaction.stage_delete(
                IdentityDataResource.WINDOW_REGISTRY,
                self.path,
                lambda original, expected=primary: original == expected,
            )
            registry, from_backup = self._load_backup_or_empty()
            self._stage_recovery_state(
                transaction,
                recovered_from_corruption=True,
                recovered_from_backup=from_backup,
                corrupt_backup=corrupt_path,
            )
            return registry

        self._stage_recovery_state(
            transaction,
            recovered_from_corruption=False,
            recovered_from_backup=False,
            corrupt_backup=None,
        )
        return registry

    def _load_backup_or_empty(self) -> tuple[WindowRegistry, bool]:
        try:
            content = self.backup_path.read_bytes()
            return self._deserialize(content), True
        except (OSError, *self._PARSE_ERRORS):
            return WindowRegistry(), False

    def save(self, registry: WindowRegistry) -> None:
        self._coordinator.execute(
            lambda transaction: self.stage_save(transaction, registry)
        )

    def stage_save(
        self,
        transaction: IdentityDataTransaction,
        registry: WindowRegistry,
    ) -> None:
        self._coordinator.require_transaction(transaction)
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be a WindowRegistry")
        candidate = self._serialize(registry)
        expected_payload = json.loads(candidate.decode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            previous = self.path.read_bytes()
        except FileNotFoundError:
            previous = None
        if previous is not None:
            transaction.stage_file(
                IdentityDataResource.WINDOW_REGISTRY,
                self.backup_path,
                previous,
                lambda content, expected=previous: content == expected,
            )
        transaction.stage_file(
            IdentityDataResource.WINDOW_REGISTRY,
            self.path,
            candidate,
            lambda content, expected=expected_payload: (
                self._validate_serialized_candidate(content, expected)
            ),
        )

    @classmethod
    def _validate_serialized_candidate(
        cls,
        content: bytes,
        expected_payload: object,
    ) -> bool:
        cls._deserialize(content)
        return json.loads(content.decode("utf-8")) == expected_payload

    def _stage_recovery_state(
        self,
        transaction: IdentityDataTransaction,
        *,
        recovered_from_corruption: bool,
        recovered_from_backup: bool,
        corrupt_backup: Path | None,
    ) -> None:
        state = (
            recovered_from_corruption,
            recovered_from_backup,
            corrupt_backup,
        )
        transaction.stage_memory(
            IdentityDataResource.WINDOW_REGISTRY,
            self._recovery_state,
            lambda state=state: self._apply_recovery_state(state),
            self._restore_recovery_state,
        )

    def _recovery_state(self) -> tuple[bool, bool, Path | None]:
        return (
            self.recovered_from_corruption,
            self.recovered_from_backup,
            self.corrupt_backup,
        )

    def _apply_recovery_state(self, state: tuple[bool, bool, Path | None]) -> None:
        self.recovered_from_corruption = state[0]
        self.recovered_from_backup = state[1]
        self.corrupt_backup = state[2]

    def _restore_recovery_state(self, state: object) -> None:
        if not isinstance(state, tuple) or len(state) != 3:
            raise TypeError("invalid recovery-state snapshot")
        self.recovered_from_corruption = state[0]
        self.recovered_from_backup = state[1]
        self.corrupt_backup = state[2]

    def _next_corrupt_path(self) -> Path:
        candidate = self.path.with_suffix(self.path.suffix + ".corrupt")
        index = 1
        while candidate.exists():
            candidate = self.path.with_suffix(self.path.suffix + f".corrupt.{index}")
            index += 1
        return candidate
