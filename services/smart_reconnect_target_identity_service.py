"""Build one complete reconnect-identity batch from one identity generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from core.smart_reconnect_authorization import (
    identity_aliases_conflict,
    normalize_identity_alias,
    observed_alias_matches,
)
from core.window_registry import CharacterWindowRecord, WindowRegistry
from domain.character import Character, CharacterImportance
from services.character_view_service import CharacterViewService
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationEntry,
    GroupConfigurationService,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
    IdentityTransactionError,
    IdentityTransactionRollbackError,
)


def normalize_reconnect_role_alias(value: object) -> str | None:
    """Compatibility export for the controller's observed-name checks."""

    return normalize_identity_alias(value)


@dataclass(frozen=True, slots=True)
class SmartReconnectTargetIdentity:
    fingerprint: str
    character_id: str
    role_aliases: tuple[str, ...]
    importance: CharacterImportance
    original_slot_index: int | None = None
    original_line_number: int | None = None

    def __post_init__(self) -> None:
        fingerprint = normalize_launch_fingerprint(self.fingerprint)
        if fingerprint is None:
            raise ValueError("fingerprint must be a complete SHA-256 digest")
        character_id = self.character_id.strip()
        if not character_id:
            raise ValueError("character_id must not be empty")
        aliases = tuple(
            dict.fromkeys(
                normalized
                for normalized in (
                    normalize_identity_alias(value) for value in self.role_aliases
                )
                if normalized is not None
            )
        )
        if not aliases:
            raise ValueError("role_aliases must contain a valid identity")
        if not isinstance(self.importance, CharacterImportance):
            raise TypeError("importance must be CharacterImportance")
        if self.original_slot_index is not None and (
            isinstance(self.original_slot_index, bool)
            or not isinstance(self.original_slot_index, int)
            or self.original_slot_index not in (0, 1, 2)
        ):
            raise ValueError("original_slot_index must be 0, 1, 2, or None")
        if self.original_line_number is not None and (
            isinstance(self.original_line_number, bool)
            or not isinstance(self.original_line_number, int)
            or not 1 <= self.original_line_number <= 8
        ):
            raise ValueError("original_line_number must be 1 through 8 or None")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "role_aliases", aliases)

    def matches_observed_identity(self, value: object) -> bool:
        return observed_alias_matches(self.role_aliases, value)


@dataclass(frozen=True, slots=True)
class _SavedTargetState:
    character_id: str
    slot_index: int | None
    line_number: int | None


class SmartReconnectTargetIdentityError(RuntimeError):
    """The complete identity source cannot form one unambiguous batch."""


class SmartReconnectTargetIdentityService:
    """Resolve all target identities while one shared coordinator owns the source."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        coordinator: IdentityDataTransactionCoordinator,
        configuration: GroupConfigurationService,
        character_view: CharacterViewService,
        registry: WindowRegistry,
        shortcut_fingerprint_resolver: ShortcutFingerprintResolver,
        state_path: Path,
    ) -> None:
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator")
        if (
            not callable(getattr(configuration, "groups", None))
            or getattr(configuration, "coordinator", None) is not coordinator
        ):
            raise ValueError("configuration must share the identity coordinator")
        if (
            not callable(getattr(character_view, "all_with_identities", None))
            or not callable(getattr(character_view, "character_profiles", None))
            or getattr(character_view, "coordinator", None) is not coordinator
        ):
            raise ValueError("character_view must share the identity coordinator")
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry")
        if not callable(getattr(shortcut_fingerprint_resolver, "resolve", None)):
            raise TypeError("shortcut_fingerprint_resolver must provide resolve")
        self._coordinator = coordinator
        self._configuration = configuration
        self._character_view = character_view
        self._registry = registry
        self._resolver = shortcut_fingerprint_resolver
        self._state_path = Path(state_path)
        self._saved = self._coordinator.read_consistent(self._load_state_unlocked)

    @property
    def coordinator(self) -> IdentityDataTransactionCoordinator:
        return self._coordinator

    @property
    def state_path(self) -> Path:
        return self._state_path

    def targets_for_group(
        self,
        group_name: object,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        try:
            return self._coordinator.read_consistent(
                lambda: self._build_targets_unlocked(group_name=group_name)
            )
        except SmartReconnectTargetIdentityError:
            return ()

    def targets_for_group_in_snapshot(
        self,
        group_name: object,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        self._coordinator.require_consistent_snapshot_owner()
        return self._build_targets_unlocked(group_name=group_name)

    def target_for(self, fingerprint: object) -> SmartReconnectTargetIdentity | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None
        try:
            targets = self._coordinator.read_consistent(
                lambda: self._build_targets_unlocked(group_name=None)
            )
        except SmartReconnectTargetIdentityError:
            return None
        return next(
            (target for target in targets if target.fingerprint == normalized),
            None,
        )

    def _build_targets_unlocked(
        self,
        *,
        group_name: object | None,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        self._coordinator.require_consistent_snapshot_owner()
        groups = self._selected_groups(group_name)
        entries = tuple(
            (group, entry)
            for group in groups
            for entry in group.entries
        )
        if not entries:
            raise SmartReconnectTargetIdentityError(
                "target identity group has no entries"
            )
        paths = tuple(
            dict.fromkeys(entry.shortcut_path.resolve(strict=False) for _, entry in entries)
        )
        try:
            raw_fingerprints = self._resolver.resolve(paths)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise SmartReconnectTargetIdentityError(
                "shortcut fingerprint resolution failed"
            ) from error
        if not isinstance(raw_fingerprints, Mapping):
            raise SmartReconnectTargetIdentityError(
                "shortcut fingerprint result is invalid"
            )
        fingerprints = {
            path.resolve(strict=False): normalize_launch_fingerprint(value)
            for path, value in raw_fingerprints.items()
            if isinstance(path, Path)
        }
        if any(fingerprints.get(path) is None for path in paths):
            raise SmartReconnectTargetIdentityError(
                "shortcut fingerprint batch is incomplete"
            )

        characters = self._character_view.character_profiles()
        records = self._registry.all()
        characters_by_id = self._unique_characters(characters)
        records_by_id = self._unique_records(records)
        targets: list[SmartReconnectTargetIdentity] = []
        exact_memberships: set[tuple[str, str, Path]] = set()
        for group, entry in entries:
            path = entry.shortcut_path.resolve(strict=False)
            fingerprint = fingerprints.get(path)
            if fingerprint is None:
                raise SmartReconnectTargetIdentityError(
                    "shortcut fingerprint is missing"
                )
            character = characters_by_id.get(entry.entry_id)
            record = records_by_id.get(entry.entry_id)
            if character is None or record is None:
                raise SmartReconnectTargetIdentityError(
                    "group character identity is incomplete"
                )
            if (
                character.display_name != record.display_name
                or record.group not in (None, group.name)
            ):
                raise SmartReconnectTargetIdentityError(
                    "group, character, and registry identities disagree"
                )
            aliases = tuple(
                dict.fromkeys(
                    normalized
                    for normalized in (
                        normalize_identity_alias(character.display_name),
                        normalize_identity_alias(record.display_name),
                        normalize_identity_alias(entry.role_id),
                        *(
                            normalize_identity_alias(value)
                            for value in record.aliases
                        ),
                    )
                    if normalized is not None
                )
            )
            if not aliases:
                raise SmartReconnectTargetIdentityError(
                    "character aliases are unavailable"
                )
            membership = (fingerprint, entry.entry_id, path)
            if membership in exact_memberships and group_name is None:
                continue
            exact_memberships.add(membership)
            saved = self._saved.get(fingerprint)
            slot_index = None
            line_number = None
            if saved is not None and saved.character_id == entry.entry_id:
                slot_index = saved.slot_index
                line_number = saved.line_number
            targets.append(
                SmartReconnectTargetIdentity(
                    fingerprint=fingerprint,
                    character_id=entry.entry_id,
                    role_aliases=aliases,
                    importance=character.importance,
                    original_slot_index=slot_index,
                    original_line_number=line_number,
                )
            )
        self._validate_complete_batch(tuple(targets))
        return tuple(targets)

    def _selected_groups(
        self,
        group_name: object | None,
    ) -> tuple[GroupConfiguration, ...]:
        groups = self._configuration.groups()
        if group_name is None:
            selected = groups
        elif isinstance(group_name, str) and group_name.strip():
            selected = tuple(
                group for group in groups if group.name == group_name.strip()
            )
        else:
            selected = ()
        if not selected:
            raise SmartReconnectTargetIdentityError(
                "target identity group is unavailable"
            )
        return selected

    @staticmethod
    def _unique_characters(
        characters: Iterable[Character],
    ) -> dict[str, Character]:
        result: dict[str, Character] = {}
        for character in characters:
            if (
                not isinstance(character, Character)
                or character.character_id in result
            ):
                raise SmartReconnectTargetIdentityError(
                    "character identities are invalid or duplicated"
                )
            result[character.character_id] = character
        return result

    @staticmethod
    def _unique_records(
        records: Iterable[CharacterWindowRecord],
    ) -> dict[str, CharacterWindowRecord]:
        result: dict[str, CharacterWindowRecord] = {}
        for record in records:
            if (
                not isinstance(record, CharacterWindowRecord)
                or record.character_id in result
            ):
                raise SmartReconnectTargetIdentityError(
                    "registry identities are invalid or duplicated"
                )
            result[record.character_id] = record
        return result

    @staticmethod
    def _validate_complete_batch(
        targets: tuple[SmartReconnectTargetIdentity, ...],
    ) -> None:
        if not targets:
            raise SmartReconnectTargetIdentityError("target batch is empty")
        for values, label in (
            ([target.fingerprint for target in targets], "fingerprint"),
            ([target.character_id for target in targets], "character"),
        ):
            if len(values) != len(set(values)):
                raise SmartReconnectTargetIdentityError(
                    f"target batch contains duplicate {label} identity"
                )
        for index, left in enumerate(targets):
            for right in targets[index + 1 :]:
                if any(
                    identity_aliases_conflict(left_alias, right_alias)
                    for left_alias in left.role_aliases
                    for right_alias in right.role_aliases
                ):
                    raise SmartReconnectTargetIdentityError(
                        "target batch contains ambiguous role aliases"
                    )

    def remember_verified_slot(
        self,
        fingerprint: object,
        character_id: object,
        slot_index: object,
    ) -> bool:
        if (
            isinstance(slot_index, bool)
            or not isinstance(slot_index, int)
            or slot_index not in (0, 1, 2)
        ):
            return False
        return self._remember(
            fingerprint,
            character_id,
            slot_index=slot_index,
        )

    def remember_verified_line(
        self,
        fingerprint: object,
        character_id: object,
        line_number: object,
    ) -> bool:
        if (
            isinstance(line_number, bool)
            or not isinstance(line_number, int)
            or not 1 <= line_number <= 8
        ):
            return False
        return self._remember(
            fingerprint,
            character_id,
            line_number=line_number,
        )

    def _remember(
        self,
        fingerprint: object,
        character_id: object,
        *,
        slot_index: int | None = None,
        line_number: int | None = None,
    ) -> bool:
        normalized = normalize_launch_fingerprint(fingerprint)
        if (
            normalized is None
            or not isinstance(character_id, str)
            or not character_id.strip()
        ):
            return False

        def prepare(transaction: IdentityDataTransaction) -> bool:
            targets = self._build_targets_unlocked(group_name=None)
            target = next(
                (item for item in targets if item.fingerprint == normalized),
                None,
            )
            if target is None or target.character_id != character_id.strip():
                return False
            current = self._saved.get(
                target.fingerprint,
                _SavedTargetState(target.character_id, None, None),
            )
            updated = _SavedTargetState(
                target.character_id,
                current.slot_index if slot_index is None else slot_index,
                current.line_number if line_number is None else line_number,
            )
            if updated == current:
                return True
            candidate = dict(self._saved)
            candidate[target.fingerprint] = updated
            content = self._serialize_state(candidate)
            transaction.stage_file(
                IdentityDataResource.RECONNECT_IDENTITY,
                self._state_path,
                content,
                lambda serialized: self._validate_serialized_state(
                    serialized,
                    candidate,
                ),
            )
            transaction.stage_memory(
                IdentityDataResource.RECONNECT_IDENTITY,
                lambda: dict(self._saved),
                lambda: self._install_saved(candidate),
                self._restore_saved,
            )
            return True

        try:
            return self._coordinator.execute(prepare)
        except IdentityTransactionRollbackError:
            raise
        except (
            IdentityTransactionError,
            OSError,
            SmartReconnectTargetIdentityError,
            TypeError,
            ValueError,
        ):
            return False

    def _load_state_unlocked(self) -> dict[str, _SavedTargetState]:
        try:
            content = self._state_path.read_bytes()
        except OSError:
            return {}
        try:
            return self._parse_state(content)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return {}

    @classmethod
    def _parse_state(cls, content: bytes) -> dict[str, _SavedTargetState]:
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, Mapping) or payload.get("version") != cls.SCHEMA_VERSION:
            raise ValueError("unsupported target identity state")
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, Mapping):
            raise ValueError("target identity state has no target mapping")
        result: dict[str, _SavedTargetState] = {}
        for raw_fingerprint, raw_value in raw_targets.items():
            fingerprint = normalize_launch_fingerprint(raw_fingerprint)
            if fingerprint is None or not isinstance(raw_value, Mapping):
                raise ValueError("target identity state entry is invalid")
            character_id = raw_value.get("character_id")
            slot_index = raw_value.get("slot_index")
            line_number = raw_value.get("line_number")
            if (
                not isinstance(character_id, str)
                or not character_id.strip()
                or (
                    slot_index is not None
                    and (
                        isinstance(slot_index, bool)
                        or not isinstance(slot_index, int)
                        or slot_index not in (0, 1, 2)
                    )
                )
                or (
                    line_number is not None
                    and (
                        isinstance(line_number, bool)
                        or not isinstance(line_number, int)
                        or not 1 <= line_number <= 8
                    )
                )
                or fingerprint in result
            ):
                raise ValueError("target identity state entry is invalid")
            result[fingerprint] = _SavedTargetState(
                character_id.strip(),
                slot_index,
                line_number,
            )
        return result

    @classmethod
    def _serialize_state(
        cls,
        saved: Mapping[str, _SavedTargetState],
    ) -> bytes:
        payload = {
            "version": cls.SCHEMA_VERSION,
            "targets": {
                fingerprint: {
                    "character_id": value.character_id,
                    "slot_index": value.slot_index,
                    "line_number": value.line_number,
                }
                for fingerprint, value in sorted(saved.items())
            },
        }
        return (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    @classmethod
    def _validate_serialized_state(
        cls,
        content: bytes,
        expected: Mapping[str, _SavedTargetState],
    ) -> bool:
        try:
            return cls._parse_state(content) == dict(expected)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return False

    def _install_saved(self, candidate: Mapping[str, _SavedTargetState]) -> None:
        self._coordinator.require_active_transaction_owner()
        self._saved = dict(candidate)

    def _restore_saved(self, snapshot: object) -> None:
        self._coordinator.require_active_transaction_owner()
        if not isinstance(snapshot, dict) or any(
            not isinstance(key, str) or not isinstance(value, _SavedTargetState)
            for key, value in snapshot.items()
        ):
            raise TypeError("invalid reconnect identity memory snapshot")
        self._saved = dict(snapshot)


__all__ = [
    "SmartReconnectTargetIdentity",
    "SmartReconnectTargetIdentityError",
    "SmartReconnectTargetIdentityService",
    "normalize_reconnect_role_alias",
]
