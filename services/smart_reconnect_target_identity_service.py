"""Build one complete reconnect-identity batch from one identity generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationTarget,
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
    shortcut_path: Path | None = None
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
        shortcut_path = (
            Path(self.shortcut_path).resolve(strict=False)
            if self.shortcut_path is not None
            else None
        )
        if shortcut_path is not None and shortcut_path.suffix.casefold() != ".lnk":
            raise ValueError("shortcut_path must identify a Windows shortcut")
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
        object.__setattr__(self, "shortcut_path", shortcut_path)

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
        *,
        ungrouped_shortcut_provider: Callable[[str], Path | None] | None = None,
        ungrouped_shortcut_catalog_provider: (
            Callable[[], Iterable[Path]] | None
        ) = None,
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
        if ungrouped_shortcut_provider is not None and not callable(
            ungrouped_shortcut_provider
        ):
            raise TypeError("ungrouped_shortcut_provider must be callable or None")
        if ungrouped_shortcut_catalog_provider is not None and not callable(
            ungrouped_shortcut_catalog_provider
        ):
            raise TypeError(
                "ungrouped_shortcut_catalog_provider must be callable or None"
            )
        self._coordinator = coordinator
        self._configuration = configuration
        self._character_view = character_view
        self._registry = registry
        self._resolver = shortcut_fingerprint_resolver
        self._ungrouped_shortcut_provider = ungrouped_shortcut_provider
        self._ungrouped_shortcut_catalog_provider = (
            ungrouped_shortcut_catalog_provider
        )
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
                lambda: self._build_requested_targets_unlocked((normalized,))
            )
        except SmartReconnectTargetIdentityError:
            return None
        return next(
            (target for target in targets if target.fingerprint == normalized),
            None,
        )

    def targets_for_fingerprints_in_snapshot(
        self,
        fingerprints: Iterable[object],
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        """Resolve each actual fingerprint independently in one source snapshot."""

        self._coordinator.require_consistent_snapshot_owner()
        normalized = tuple(
            dict.fromkeys(
                value
                for value in (
                    normalize_launch_fingerprint(item) for item in fingerprints
                )
                if value is not None
            )
        )
        return self._build_requested_targets_unlocked(normalized)

    def observed_identity_alias_catalog(self) -> tuple[tuple[str, str], ...]:
        """Return reliable screen aliases without making them target gates."""

        try:
            return self._coordinator.read_consistent(
                self._observed_identity_alias_catalog_unlocked
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return ()

    def _observed_identity_alias_catalog_unlocked(
        self,
    ) -> tuple[tuple[str, str], ...]:
        self._coordinator.require_consistent_snapshot_owner()
        aliases: list[tuple[str, str]] = []
        for character in self._character_view.character_profiles():
            if isinstance(character, Character):
                normalized = normalize_identity_alias(character.display_name)
                if normalized is not None:
                    aliases.append((normalized, character.character_id))
        for record in self._registry.all():
            if not isinstance(record, CharacterWindowRecord):
                continue
            for value in (record.display_name, *record.aliases):
                normalized = normalize_identity_alias(value)
                if normalized is not None:
                    aliases.append((normalized, record.character_id))
        for group in self._configuration.groups():
            for entry in group.entries:
                normalized = normalize_identity_alias(entry.role_id)
                if normalized is not None:
                    aliases.append((normalized, entry.entry_id))
        return tuple(dict.fromkeys(aliases))

    def retained_target_is_current_in_snapshot(
        self,
        target: ReconnectAuthorizationTarget,
    ) -> bool:
        """Rebuild the absent target's unique source binding in this generation."""

        self._coordinator.require_consistent_snapshot_owner()
        if (
            not isinstance(target, ReconnectAuthorizationTarget)
            or target.character_id is None
            or target.shortcut_seal is None
        ):
            return False
        path = Path(
            target.shortcut_seal.file_identity.normalized_path
        ).resolve(strict=False)
        groups = tuple(self._configuration.groups())
        membership_paths = tuple(
            entry.shortcut_path.resolve(strict=False)
            for group in groups
            for entry in group.entries
        )
        catalog_provider = self._ungrouped_shortcut_catalog_provider
        catalog_paths: tuple[Path, ...] = ()
        if catalog_provider is not None:
            try:
                catalog_paths = tuple(
                    dict.fromkeys(
                        Path(item).resolve(strict=False)
                        for item in catalog_provider()
                    )
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return False
        paths = tuple(dict.fromkeys((*membership_paths, *catalog_paths, path)))
        fingerprints_by_path: dict[Path, str] = {}
        try:
            resolved = self._resolver.resolve(paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            resolved = {}
        if isinstance(resolved, Mapping):
            requested_paths = frozenset(paths)
            for raw_path, raw_fingerprint in resolved.items():
                try:
                    candidate = Path(raw_path).resolve(strict=False)
                except (OSError, TypeError, ValueError):
                    continue
                fingerprint = normalize_launch_fingerprint(raw_fingerprint)
                if candidate in requested_paths and fingerprint is not None:
                    fingerprints_by_path[candidate] = fingerprint
        matching_paths = frozenset(
            candidate
            for candidate in paths
            if fingerprints_by_path.get(candidate) == target.fingerprint
        )
        if matching_paths != frozenset((path,)):
            return False

        characters_by_id = self._items_by_character_id(
            self._character_view.character_profiles()
        )
        records_by_id = self._items_by_character_id(self._registry.all())
        memberships = tuple(
            (group, entry)
            for group in groups
            for entry in group.entries
            if entry.shortcut_path.resolve(strict=False) == path
        )
        if memberships:
            current_targets = tuple(
                current
                for group, entry in memberships
                if (
                    current := self._target_from_membership(
                        target.fingerprint,
                        group,
                        entry,
                        characters_by_id,
                        records_by_id,
                    )
                )
                is not None
            )
            unique_targets = tuple(dict.fromkeys(current_targets))
            if len(unique_targets) != 1:
                return False
            current = unique_targets[0]
        else:
            if catalog_provider is None or path not in catalog_paths:
                return False
            shortcut_identity = normalize_identity_alias(path.stem)
            matches: list[tuple[str, Character, CharacterWindowRecord]] = []
            for character_id, characters in characters_by_id.items():
                records = records_by_id.get(character_id, ())
                if len(characters) != 1 or len(records) != 1:
                    continue
                character = characters[0]
                record = records[0]
                if (
                    not isinstance(character, Character)
                    or not isinstance(record, CharacterWindowRecord)
                    or character.display_name != record.display_name
                    or shortcut_identity is None
                    or shortcut_identity not in self._aliases(
                        character,
                        record,
                        None,
                    )
                ):
                    continue
                matches.append((character_id, character, record))
            if len(matches) != 1 or matches[0][0] != target.character_id:
                return False
            character_id, character, record = matches[0]
            saved = self._saved.get(target.fingerprint)
            if (
                saved is not None
                and saved.character_id != character_id
            ):
                return False
            current = SmartReconnectTargetIdentity(
                fingerprint=target.fingerprint,
                character_id=character_id,
                role_aliases=self._aliases(character, record, None),
                importance=character.importance,
                shortcut_path=path,
                original_slot_index=(
                    saved.slot_index if saved is not None else None
                ),
                original_line_number=(
                    saved.line_number if saved is not None else None
                ),
            )
        return (
            current.fingerprint == target.fingerprint
            and current.character_id == target.character_id
            and current.role_aliases == target.role_aliases
            and current.importance is target.importance
            and current.original_slot_index == target.original_slot_index
            and current.original_line_number == target.original_line_number
            and current.shortcut_path.resolve(strict=False) == path
        )

    def _build_requested_targets_unlocked(
        self,
        fingerprints: tuple[str, ...],
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        self._coordinator.require_consistent_snapshot_owner()
        requested = frozenset(fingerprints)
        if not requested:
            return ()
        groups = self._configuration.groups()
        characters = self._character_view.character_profiles()
        records = self._registry.all()
        characters_by_id = self._items_by_character_id(characters)
        records_by_id = self._items_by_character_id(records)

        memberships_by_path: dict[
            Path,
            list[tuple[GroupConfiguration, GroupConfigurationEntry]],
        ] = {}
        for group in groups:
            for entry in group.entries:
                path = entry.shortcut_path.resolve(strict=False)
                memberships_by_path.setdefault(path, []).append((group, entry))

        catalog_paths: tuple[Path, ...] = ()
        catalog_provider = self._ungrouped_shortcut_catalog_provider
        if catalog_provider is not None:
            try:
                catalog_paths = tuple(
                    dict.fromkeys(
                        Path(item).resolve(strict=False)
                        for item in catalog_provider()
                    )
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return ()
        all_source_paths = tuple(
            dict.fromkeys((*memberships_by_path, *catalog_paths))
        )

        fingerprints_by_path: dict[Path, str] = {}
        try:
            raw = self._resolver.resolve(all_source_paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            raw = {}
        if isinstance(raw, Mapping):
            requested_paths = frozenset(all_source_paths)
            for raw_path, raw_fingerprint in raw.items():
                try:
                    path = Path(raw_path).resolve(strict=False)
                except (OSError, TypeError, ValueError):
                    continue
                resolved = normalize_launch_fingerprint(raw_fingerprint)
                if path in requested_paths and resolved is not None:
                    fingerprints_by_path[path] = resolved
        paths_by_fingerprint: dict[str, list[Path]] = {}
        for path, fingerprint in fingerprints_by_path.items():
            paths_by_fingerprint.setdefault(fingerprint, []).append(path)

        bindings: dict[
            str,
            dict[
                tuple[str, Path],
                list[tuple[GroupConfiguration, GroupConfigurationEntry]],
            ],
        ] = {fingerprint: {} for fingerprint in fingerprints}
        for path, memberships in memberships_by_path.items():
            fingerprint = fingerprints_by_path.get(path)
            if fingerprint not in requested:
                continue
            for group, entry in memberships:
                bindings[fingerprint].setdefault(
                    (entry.entry_id, path),
                    [],
                ).append((group, entry))

        candidates: dict[str, SmartReconnectTargetIdentity] = {}
        for fingerprint in fingerprints:
            source_path_count = len(
                paths_by_fingerprint.get(fingerprint, ())
            )
            if (
                source_path_count > 1
                or (
                    catalog_provider is not None
                    and source_path_count != 1
                )
            ):
                continue
            fingerprint_bindings = bindings[fingerprint]
            if len(fingerprint_bindings) > 1:
                continue
            if fingerprint_bindings:
                (_binding, memberships), = fingerprint_bindings.items()
                resolved_targets: list[SmartReconnectTargetIdentity] = []
                for group, entry in memberships:
                    try:
                        target = self._target_from_membership(
                            fingerprint,
                            group,
                            entry,
                            characters_by_id,
                            records_by_id,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        continue
                    if target is not None:
                        resolved_targets.append(target)
                resolved_memberships = tuple(resolved_targets)
                unique = tuple(dict.fromkeys(resolved_memberships))
                if len(unique) == 1:
                    candidates[fingerprint] = unique[0]
                continue
            target = self._ungrouped_target(
                fingerprint,
                characters_by_id,
                records_by_id,
            )
            if target is not None:
                candidates[fingerprint] = target

        character_counts: dict[str, int] = {}
        for target in candidates.values():
            character_counts[target.character_id] = (
                character_counts.get(target.character_id, 0) + 1
            )
        return tuple(
            target
            for fingerprint in fingerprints
            if (target := candidates.get(fingerprint)) is not None
            and character_counts[target.character_id] == 1
        )

    @staticmethod
    def _items_by_character_id(values: Iterable[object]) -> dict[str, tuple[object, ...]]:
        result: dict[str, list[object]] = {}
        for value in values:
            character_id = getattr(value, "character_id", None)
            if isinstance(character_id, str) and character_id.strip():
                result.setdefault(character_id.strip(), []).append(value)
        return {key: tuple(items) for key, items in result.items()}

    def _target_from_membership(
        self,
        fingerprint: str,
        group: GroupConfiguration,
        entry: GroupConfigurationEntry,
        characters_by_id: Mapping[str, tuple[object, ...]],
        records_by_id: Mapping[str, tuple[object, ...]],
    ) -> SmartReconnectTargetIdentity | None:
        characters = characters_by_id.get(entry.entry_id, ())
        records = records_by_id.get(entry.entry_id, ())
        if len(characters) != 1 or len(records) != 1:
            return None
        character = characters[0]
        record = records[0]
        if (
            not isinstance(character, Character)
            or not isinstance(record, CharacterWindowRecord)
            or character.display_name != record.display_name
        ):
            return None
        aliases = self._aliases(character, record, entry.role_id)
        if not aliases:
            return None
        saved = self._saved.get(fingerprint)
        return SmartReconnectTargetIdentity(
            fingerprint=fingerprint,
            character_id=entry.entry_id,
            role_aliases=aliases,
            importance=character.importance,
            shortcut_path=entry.shortcut_path,
            original_slot_index=(
                saved.slot_index
                if saved is not None and saved.character_id == entry.entry_id
                else None
            ),
            original_line_number=(
                saved.line_number
                if saved is not None and saved.character_id == entry.entry_id
                else None
            ),
        )

    def _ungrouped_target(
        self,
        fingerprint: str,
        characters_by_id: Mapping[str, tuple[object, ...]],
        records_by_id: Mapping[str, tuple[object, ...]],
    ) -> SmartReconnectTargetIdentity | None:
        provider = self._ungrouped_shortcut_provider
        if provider is None:
            return None
        try:
            candidate_path = provider(fingerprint)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if not isinstance(candidate_path, Path):
            return None
        path = candidate_path.resolve(strict=False)
        if path.suffix.casefold() != ".lnk":
            return None
        try:
            resolved = self._resolver.resolve((path,))
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if (
            not isinstance(resolved, Mapping)
            or set(resolved) != {path}
            or normalize_launch_fingerprint(resolved.get(path)) != fingerprint
        ):
            return None
        saved = self._saved.get(fingerprint)
        shortcut_identity = normalize_identity_alias(path.stem)
        matches: list[SmartReconnectTargetIdentity] = []
        for character_id, characters in characters_by_id.items():
            if saved is not None and character_id != saved.character_id:
                continue
            records = records_by_id.get(character_id, ())
            if len(characters) != 1 or len(records) != 1:
                continue
            character = characters[0]
            record = records[0]
            if (
                not isinstance(character, Character)
                or not isinstance(record, CharacterWindowRecord)
                or character.display_name != record.display_name
            ):
                continue
            aliases = self._aliases(character, record, None)
            if shortcut_identity is None or shortcut_identity not in aliases:
                continue
            matches.append(
                SmartReconnectTargetIdentity(
                    fingerprint=fingerprint,
                    character_id=character_id,
                    role_aliases=aliases,
                    importance=character.importance,
                    shortcut_path=path,
                    original_slot_index=(
                        saved.slot_index if saved is not None else None
                    ),
                    original_line_number=(
                        saved.line_number if saved is not None else None
                    ),
                )
            )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _aliases(
        character: Character,
        record: CharacterWindowRecord,
        role_id: object,
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                normalized
                for normalized in (
                    normalize_identity_alias(character.display_name),
                    normalize_identity_alias(record.display_name),
                    normalize_identity_alias(role_id),
                    *(normalize_identity_alias(value) for value in record.aliases),
                )
                if normalized is not None
            )
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
                    shortcut_path=path,
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
            targets = self._build_requested_targets_unlocked((normalized,))
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
