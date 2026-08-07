"""角色等級與重要度的交易式 JSON 儲存。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from domain.character import Character, CharacterImportance
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)


class CharacterStore:
    SCHEMA_VERSION = 1
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
    def _character_from_dict(payload: Mapping[str, object]) -> Character:
        character_id = payload.get("character_id")
        display_name = payload.get("display_name")
        level = payload.get("level")
        importance = payload.get("importance")
        if not isinstance(character_id, str) or not isinstance(display_name, str):
            raise ValueError("Character identity fields must be strings.")
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("Character level must be an integer.")
        if not isinstance(importance, str):
            raise ValueError("Character importance must be a string.")
        return Character(
            character_id=character_id,
            display_name=display_name,
            level=level,
            importance=CharacterImportance(importance),
        )

    @classmethod
    def _deserialize(cls, content: bytes) -> tuple[Character, ...]:
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Character root must be an object.")
        if payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("Unsupported character schema version.")
        raw_characters = payload.get("characters", [])
        if not isinstance(raw_characters, list):
            raise ValueError("characters must be a list.")
        if any(not isinstance(item, Mapping) for item in raw_characters):
            raise ValueError("Each character must be an object.")
        characters = tuple(cls._character_from_dict(item) for item in raw_characters)
        cls._validate_unique_identities(characters)
        return characters

    @classmethod
    def _serialize(cls, characters: tuple[Character, ...]) -> bytes:
        payload = {
            "schema_version": cls.SCHEMA_VERSION,
            "characters": [character.to_dict() for character in characters],
        }
        return (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    @staticmethod
    def _validate_unique_identities(characters: tuple[Character, ...]) -> None:
        identities = [character.character_id for character in characters]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate stable character identity.")

    def load(self) -> tuple[Character, ...]:
        return self._coordinator.execute(self._prepare_load)

    def _prepare_load(
        self,
        transaction: IdentityDataTransaction,
    ) -> tuple[Character, ...]:
        self._coordinator.require_transaction(transaction)
        try:
            primary = self.path.read_bytes()
        except FileNotFoundError:
            characters, from_backup = self._load_backup_or_empty()
            self._stage_recovery_state(
                transaction,
                recovered_from_corruption=False,
                recovered_from_backup=from_backup,
                corrupt_backup=None,
            )
            return characters

        try:
            characters = self._deserialize(primary)
        except self._PARSE_ERRORS:
            corrupt_path = self._next_corrupt_path()
            transaction.stage_file(
                IdentityDataResource.CHARACTER_DATA,
                corrupt_path,
                primary,
                lambda candidate, expected=primary: candidate == expected,
            )
            transaction.stage_delete(
                IdentityDataResource.CHARACTER_DATA,
                self.path,
                lambda original, expected=primary: original == expected,
            )
            characters, from_backup = self._load_backup_or_empty()
            self._stage_recovery_state(
                transaction,
                recovered_from_corruption=True,
                recovered_from_backup=from_backup,
                corrupt_backup=corrupt_path,
            )
            return characters

        self._stage_recovery_state(
            transaction,
            recovered_from_corruption=False,
            recovered_from_backup=False,
            corrupt_backup=None,
        )
        return characters

    def _load_backup_or_empty(self) -> tuple[tuple[Character, ...], bool]:
        try:
            content = self.backup_path.read_bytes()
            return self._deserialize(content), True
        except (OSError, *self._PARSE_ERRORS):
            return (), False

    def save(self, characters: Iterable[Character]) -> None:
        self._coordinator.execute(
            lambda transaction: self.stage_save(transaction, characters)
        )

    def stage_save(
        self,
        transaction: IdentityDataTransaction,
        characters: Iterable[Character],
    ) -> None:
        self._coordinator.require_transaction(transaction)
        items = tuple(characters)
        if any(not isinstance(item, Character) for item in items):
            raise TypeError("characters must contain only Character values.")
        self._validate_unique_identities(items)
        candidate = self._serialize(items)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            previous = self.path.read_bytes()
        except FileNotFoundError:
            previous = None
        if previous is not None:
            transaction.stage_file(
                IdentityDataResource.CHARACTER_DATA,
                self.backup_path,
                previous,
                lambda content, expected=previous: content == expected,
            )
        transaction.stage_file(
            IdentityDataResource.CHARACTER_DATA,
            self.path,
            candidate,
            lambda content, expected=items: self._deserialize(content) == expected,
        )

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
            IdentityDataResource.CHARACTER_DATA,
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
