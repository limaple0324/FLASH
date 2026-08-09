"""Safely save the player-confirmed character note field."""

from __future__ import annotations

from core.window_registry import CharacterWindowRecord, WindowRegistry
from core.window_registry_store import WindowRegistryStore
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)


class CharacterNoteService:
    """Save a cloned registry before updating the live read-only source."""

    def __init__(
        self,
        registry: WindowRegistry,
        store: WindowRegistryStore,
        coordinator: IdentityDataTransactionCoordinator,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        if not isinstance(store, WindowRegistryStore):
            raise TypeError("store must be WindowRegistryStore.")
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator.")
        if store.coordinator is not coordinator:
            raise ValueError("store must use the injected coordinator.")
        self._registry = registry
        self._store = store
        self._coordinator = coordinator

    def set_note(
        self,
        character_id: str,
        note: str,
    ) -> CharacterWindowRecord:
        return self._coordinator.execute(
            lambda transaction: self.stage_set_note(
                transaction,
                character_id,
                note,
            )
        )

    def stage_set_note(
        self,
        transaction: IdentityDataTransaction,
        character_id: str,
        note: str,
    ) -> CharacterWindowRecord:
        self._coordinator.require_transaction(transaction)
        if not isinstance(note, str):
            raise TypeError("note must be str.")
        normalized = note.strip()
        if not normalized:
            raise ValueError("note must not be empty; use clear_note instead.")
        return self._stage_persist(transaction, character_id, normalized)

    def clear_note(self, character_id: str) -> CharacterWindowRecord:
        return self._coordinator.execute(
            lambda transaction: self.stage_clear_note(transaction, character_id)
        )

    def stage_clear_note(
        self,
        transaction: IdentityDataTransaction,
        character_id: str,
    ) -> CharacterWindowRecord:
        self._coordinator.require_transaction(transaction)
        return self._stage_persist(transaction, character_id, None)

    def _stage_persist(
        self,
        transaction: IdentityDataTransaction,
        character_id: str,
        note: str | None,
    ) -> CharacterWindowRecord:
        candidate = self._registry.clone_runtime()
        candidate_record = candidate.set_note(character_id, note)
        self._store.stage_save(transaction, candidate)
        transaction.stage_memory(
            IdentityDataResource.WINDOW_REGISTRY,
            self._registry.clone_runtime,
            lambda: self._registry.replace_runtime(candidate),
            self._restore_registry,
        )
        return candidate_record

    def _restore_registry(self, snapshot: object) -> None:
        if not isinstance(snapshot, WindowRegistry):
            raise TypeError("invalid runtime registry snapshot")
        self._registry.replace_runtime(snapshot)
