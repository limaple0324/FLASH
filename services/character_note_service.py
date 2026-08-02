"""Safely save the player-confirmed character note field."""

from __future__ import annotations

from core.window_registry import CharacterWindowRecord, WindowRegistry
from core.window_registry_store import WindowRegistryStore


class CharacterNoteService:
    """Save a cloned registry before updating the live read-only source."""

    def __init__(
        self,
        registry: WindowRegistry,
        store: WindowRegistryStore,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        if not isinstance(store, WindowRegistryStore):
            raise TypeError("store must be WindowRegistryStore.")
        self._registry = registry
        self._store = store

    def set_note(
        self,
        character_id: str,
        note: str,
    ) -> CharacterWindowRecord:
        if not isinstance(note, str):
            raise TypeError("note must be str.")
        normalized = note.strip()
        if not normalized:
            raise ValueError("note must not be empty; use clear_note instead.")
        return self._persist(character_id, normalized)

    def clear_note(self, character_id: str) -> CharacterWindowRecord:
        return self._persist(character_id, None)

    def _persist(
        self,
        character_id: str,
        note: str | None,
    ) -> CharacterWindowRecord:
        candidate = WindowRegistry.from_dict(self._registry.to_dict())
        candidate_record = candidate.set_note(character_id, note)
        self._store.save(candidate)
        self._registry.set_note(character_id, note)
        return candidate_record
