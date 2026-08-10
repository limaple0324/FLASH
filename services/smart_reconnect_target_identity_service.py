"""Build one complete reconnect-identity batch from one identity generation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from adapters.windows_launch_fingerprint import (
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectObservationSnapshot,
    WindowsSmartReconnectObservationBroker,
)
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationTarget,
    ShortcutFileIdentity,
    ShortcutSeal,
    normalize_identity_alias,
    observed_alias_matches,
)
from core.window_instance import WindowInstanceToken
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


def complete_reconnect_role_alias(value: object) -> str | None:
    """Accept a complete named role, never a bare visible level."""

    alias = normalize_identity_alias(value)
    if alias is None or len(alias) < 3 or alias.isdecimal():
        return None
    return alias


def _identity_path(value: object) -> Path:
    return Path(os.path.normcase(os.path.abspath(os.fspath(value))))


@dataclass(frozen=True, slots=True)
class SmartReconnectTargetIdentity:
    fingerprint: str
    character_id: str
    role_aliases: tuple[str, ...]
    importance: CharacterImportance | None
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
        if self.importance is not None and not isinstance(
            self.importance,
            CharacterImportance,
        ):
            raise TypeError("importance must be CharacterImportance or None")
        shortcut_path = (
            _identity_path(self.shortcut_path)
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
    verified_aliases: tuple[str, ...] = ()
    status: str = "confirmed"
    shortcut_path: Path | None = None
    instance: WindowInstanceToken | None = None
    shortcut_seal: ShortcutSeal | None = None
    evidence_alias: str | None = None
    identity_generation: int | None = None
    config_revision: int | None = None
    evidence_revision: int = 0

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def is_confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def has_complete_evidence(self) -> bool:
        return (
            self.shortcut_path is not None
            and self.instance is not None
            and self.shortcut_seal is not None
            and self.evidence_alias is not None
            and self.identity_generation is not None
            and self.config_revision is not None
            and self.evidence_revision > 0
        )


@dataclass(frozen=True, slots=True)
class SmartReconnectPendingIdentityCandidate:
    """One live fingerprint whose shortcut is unique but role is unknown."""

    fingerprint: str
    shortcut_path: Path

    def __post_init__(self) -> None:
        fingerprint = normalize_launch_fingerprint(self.fingerprint)
        if fingerprint is None:
            raise ValueError("fingerprint must be a complete SHA-256 digest")
        shortcut_path = _identity_path(self.shortcut_path)
        if shortcut_path.suffix.casefold() != ".lnk":
            raise ValueError("shortcut_path must identify a Windows shortcut")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "shortcut_path", shortcut_path)


@dataclass(frozen=True, slots=True)
class SmartReconnectIdentityEvidence:
    """One complete background observation for a unique live shortcut."""

    candidate: SmartReconnectPendingIdentityCandidate
    instance: WindowInstanceToken
    shortcut_seal: ShortcutSeal
    role_alias: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SmartReconnectPendingIdentityCandidate):
            raise TypeError("candidate must be a pending identity candidate")
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("instance must be a complete window instance")
        if not isinstance(self.shortcut_seal, ShortcutSeal):
            raise TypeError("shortcut_seal must be complete")
        alias = complete_reconnect_role_alias(self.role_alias)
        if (
            alias is None
            or any(character.isspace() for character in self.role_alias)
            or "..." in self.role_alias
            or "…" in self.role_alias
        ):
            raise ValueError("role_alias must be a complete stable identity")
        expected_path = _identity_path(self.candidate.shortcut_path)
        actual_path = _identity_path(
            self.shortcut_seal.file_identity.normalized_path
        )
        if (
            self.shortcut_seal.launch_fingerprint
            != self.candidate.fingerprint
            or actual_path != expected_path
        ):
            raise ValueError("identity evidence does not match its shortcut")
        object.__setattr__(self, "role_alias", alias)


@dataclass(frozen=True, slots=True)
class SmartReconnectIdentityEvidenceResult:
    """Result of one atomic pending-evidence update."""

    source_current: bool
    confirmed_fingerprints: frozenset[str] = frozenset()
    state_changed: bool = False


@dataclass(frozen=True, slots=True)
class SmartReconnectTargetResolution:
    targets: tuple[SmartReconnectTargetIdentity, ...]
    pending_candidates: tuple[SmartReconnectPendingIdentityCandidate, ...]
    verification_candidates: tuple[
        SmartReconnectPendingIdentityCandidate, ...
    ] = ()


@dataclass(frozen=True, slots=True)
class SmartReconnectTargetIdentitySourceSnapshot:
    """Immutable identity-only input captured while the coordinator is held."""

    groups: tuple[GroupConfiguration, ...]
    characters: tuple[Character, ...]
    records: tuple[CharacterWindowRecord, ...]
    saved_targets: tuple[tuple[str, _SavedTargetState], ...]
    identity_generation: int = 0
    state_writable: bool = True

    def saved_by_fingerprint(self) -> dict[str, _SavedTargetState]:
        return dict(self.saved_targets)


class SmartReconnectTargetIdentityError(RuntimeError):
    """The complete identity source cannot form one unambiguous batch."""


class SmartReconnectTargetIdentityService:
    """Resolve all target identities while one shared coordinator owns the source."""

    SCHEMA_VERSION = 2

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
        observation_broker: (
            WindowsSmartReconnectObservationBroker | None
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
        self._observation_broker = observation_broker
        self._state_path = Path(state_path)
        self._state_write_blocked = False
        # Construction has not published this service to any caller yet.  Read
        # the persisted file before joining the shared identity lock so disk I/O
        # can never hold UI identity readers behind it.
        self._saved = self._load_state_unlocked()
        self._remember_lock = Lock()

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
        source = self._capture_source()
        observation = self._observation_for_source(source.value)
        try:
            targets = self.targets_for_group_from_source_snapshot(
                group_name,
                source.value,
                observation_snapshot=observation,
            )
        except SmartReconnectTargetIdentityError:
            return ()
        return targets if self._source_is_current(source.generation) else ()

    def target_for(self, fingerprint: object) -> SmartReconnectTargetIdentity | None:
        normalized = normalize_launch_fingerprint(fingerprint)
        if normalized is None:
            return None
        source = self._capture_source()
        observation = self._observation_for_source(source.value)
        try:
            targets = self.targets_for_fingerprints_from_source_snapshot(
                (normalized,),
                source.value,
                observation_snapshot=observation,
            )
        except SmartReconnectTargetIdentityError:
            return None
        if not self._source_is_current(source.generation):
            return None
        return next(
            (target for target in targets if target.fingerprint == normalized),
            None,
        )

    def targets_for_fingerprints(
        self,
        fingerprints: Iterable[object],
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        """Resolve externally, then reject a result from a changed generation."""

        source = self._capture_source()
        observation = self._observation_for_source(source.value)
        targets = self.targets_for_fingerprints_from_source_snapshot(
            fingerprints,
            source.value,
            observation_snapshot=observation,
        )
        return targets if self._source_is_current(source.generation) else ()

    def _capture_source(self):
        return self._coordinator.capture_snapshot(
            self.capture_source_snapshot_in_current
        )

    def _source_is_current(self, expected_generation: int) -> bool:
        return (
            self._coordinator.capture_snapshot(lambda: None).generation
            == expected_generation
        )

    @staticmethod
    def _source_shortcut_paths(
        source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                Path(entry.shortcut_path)
                for group in source.groups
                for entry in group.entries
            )
        )

    def _observation_for_source(
        self,
        source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> SmartReconnectObservationSnapshot | None:
        broker = self._observation_broker
        if broker is None:
            return None
        return broker.refresh(self._source_shortcut_paths(source))

    def _latest_observation_for_source(
        self,
        source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> SmartReconnectObservationSnapshot | None:
        del source
        broker = self._observation_broker
        return broker.current_snapshot() if broker is not None else None

    def targets_for_group_from_source_snapshot(
        self,
        group_name: object,
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        if not isinstance(source, SmartReconnectTargetIdentitySourceSnapshot):
            raise TypeError("source must be a target identity source snapshot")
        if not source.state_writable:
            return ()
        return self._build_targets_from_source(
            group_name,
            source,
            observation_snapshot=observation_snapshot,
        )

    def capture_source_snapshot_in_current(
        self,
    ) -> SmartReconnectTargetIdentitySourceSnapshot:
        """Copy only in-memory identity data while owning a short snapshot."""

        self._coordinator.require_consistent_snapshot_owner()
        return SmartReconnectTargetIdentitySourceSnapshot(
            groups=tuple(self._configuration.groups()),
            characters=tuple(self._character_view.character_profiles()),
            records=tuple(self._registry.all()),
            saved_targets=tuple(sorted(self._saved.items())),
            identity_generation=self._coordinator.generation,
            state_writable=not self._state_write_blocked,
        )

    def targets_for_fingerprints_from_source_snapshot(
        self,
        fingerprints: Iterable[object],
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        """Resolve external shortcut evidence after releasing identity locks."""

        if not isinstance(source, SmartReconnectTargetIdentitySourceSnapshot):
            raise TypeError("source must be a target identity source snapshot")
        normalized = tuple(
            dict.fromkeys(
                value
                for value in (
                    normalize_launch_fingerprint(item) for item in fingerprints
                )
                if value is not None
            )
        )
        return self.resolve_for_fingerprints_from_source_snapshot(
            normalized,
            source,
            observation_snapshot=observation_snapshot,
        ).targets

    def resolve_for_fingerprints_from_source_snapshot(
        self,
        fingerprints: Iterable[object],
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> SmartReconnectTargetResolution:
        """Return authorized identities and safe-to-observe unknown windows."""

        if not isinstance(source, SmartReconnectTargetIdentitySourceSnapshot):
            raise TypeError("source must be a target identity source snapshot")
        if not source.state_writable:
            return SmartReconnectTargetResolution((), (), ())
        normalized = tuple(
            dict.fromkeys(
                value
                for value in (
                    normalize_launch_fingerprint(item) for item in fingerprints
                )
                if value is not None
            )
        )
        return self._resolve_requested_targets_from_source(
            normalized,
            source,
            observation_snapshot=observation_snapshot,
        )

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
        for saved in self._saved.values():
            if not saved.is_confirmed:
                continue
            for alias in saved.verified_aliases:
                aliases.append((alias, saved.character_id))
        return tuple(dict.fromkeys(aliases))

    def retained_target_is_current(
        self,
        target: ReconnectAuthorizationTarget,
    ) -> bool:
        source = self._capture_source()
        observation = self._observation_for_source(source.value)
        is_current = self.retained_target_is_current_from_source_snapshot(
            target,
            source.value,
            observation_snapshot=observation,
        )
        return is_current and self._source_is_current(source.generation)

    def retained_target_is_current_from_source_snapshot(
        self,
        target: ReconnectAuthorizationTarget,
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> bool:
        """Recheck one absent target using copied identity and external evidence."""

        if not isinstance(source, SmartReconnectTargetIdentitySourceSnapshot):
            return False
        if not source.state_writable:
            return False
        if (
            not isinstance(target, ReconnectAuthorizationTarget)
            or target.character_id is None
            or target.shortcut_seal is None
        ):
            return False
        path = _identity_path(
            target.shortcut_seal.file_identity.normalized_path
        )
        groups = source.groups
        saved_targets = source.saved_by_fingerprint()
        saved_for_target = saved_targets.get(target.fingerprint)
        if saved_for_target is not None:
            if saved_for_target.is_pending:
                return False
            if (
                saved_for_target.has_complete_evidence
                and saved_for_target.identity_generation
                != source.identity_generation
            ):
                return False
        membership_paths = tuple(
            _identity_path(entry.shortcut_path)
            for group in groups
            for entry in group.entries
        )
        catalog_provider = self._ungrouped_shortcut_catalog_provider
        catalog_paths: tuple[Path, ...] = ()
        fingerprints_by_path: dict[Path, str] = {}
        if observation_snapshot is not None:
            if observation_snapshot.failure_codes:
                return False
            catalog_provider = lambda: ()
            catalog_paths = tuple(
                dict.fromkeys(
                    _identity_path(item.path)
                    for item in observation_snapshot.shortcuts
                    if item.fingerprint is not None
                    and item.seal is not None
                    and not item.failure_codes
                )
            )
            fingerprints_by_path = {
                _identity_path(item.path): item.fingerprint
                for item in observation_snapshot.shortcuts
                if item.fingerprint is not None
                and item.seal is not None
                and not item.failure_codes
            }
        elif catalog_provider is not None:
            try:
                catalog_paths = tuple(
                    dict.fromkeys(
                        _identity_path(item)
                        for item in catalog_provider()
                    )
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return False
        paths = tuple(dict.fromkeys((*membership_paths, *catalog_paths, path)))
        if observation_snapshot is None:
            try:
                resolved = self._resolver.resolve(paths)
            except (OSError, RuntimeError, TypeError, ValueError):
                resolved = {}
            if isinstance(resolved, Mapping):
                requested_paths = frozenset(paths)
                for raw_path, raw_fingerprint in resolved.items():
                    try:
                        candidate = _identity_path(raw_path)
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
            source.characters
        )
        records_by_id = self._items_by_character_id(source.records)
        memberships = tuple(
            (group, entry)
            for group in groups
            for entry in group.entries
            if _identity_path(entry.shortcut_path) == path
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
                        saved_targets,
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
            saved = saved_targets.get(target.fingerprint)
            if len(matches) == 1 and matches[0][0] == target.character_id:
                character_id, character, record = matches[0]
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
            elif (
                matches
                or saved is None
                or saved.character_id != target.character_id
                or not self._saved_verified_identity_is_safe(
                    target.fingerprint,
                    saved,
                    source,
                )
            ):
                return False
            else:
                current = SmartReconnectTargetIdentity(
                    fingerprint=target.fingerprint,
                    character_id=saved.character_id,
                    role_aliases=saved.verified_aliases,
                    importance=None,
                    shortcut_path=path,
                    original_slot_index=saved.slot_index,
                    original_line_number=saved.line_number,
                )
        return (
            current.fingerprint == target.fingerprint
            and current.character_id == target.character_id
            and current.role_aliases == target.role_aliases
            and current.importance is target.importance
            and current.original_slot_index == target.original_slot_index
            and current.original_line_number == target.original_line_number
            and _identity_path(current.shortcut_path) == path
        )

    def _resolve_requested_targets_from_source(
        self,
        fingerprints: tuple[str, ...],
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> SmartReconnectTargetResolution:
        requested = frozenset(fingerprints)
        if not requested:
            return SmartReconnectTargetResolution((), ())
        groups = source.groups
        characters = source.characters
        records = source.records
        saved_targets = source.saved_by_fingerprint()
        characters_by_id = self._items_by_character_id(characters)
        records_by_id = self._items_by_character_id(records)

        memberships_by_path: dict[
            Path,
            list[tuple[GroupConfiguration, GroupConfigurationEntry]],
        ] = {}
        for group in groups:
            for entry in group.entries:
                path = _identity_path(entry.shortcut_path)
                memberships_by_path.setdefault(path, []).append((group, entry))

        catalog_paths: tuple[Path, ...] = ()
        catalog_provider = self._ungrouped_shortcut_catalog_provider
        fingerprints_by_path: dict[Path, str] = {}
        broker_catalog = observation_snapshot is not None
        if observation_snapshot is not None:
            if observation_snapshot.failure_codes:
                return SmartReconnectTargetResolution((), ())
            catalog_paths = tuple(
                dict.fromkeys(
                    _identity_path(item.path)
                    for item in observation_snapshot.shortcuts
                    if item.fingerprint is not None
                    and item.seal is not None
                    and not item.failure_codes
                )
            )
            fingerprints_by_path = {
                _identity_path(item.path): item.fingerprint
                for item in observation_snapshot.shortcuts
                if item.fingerprint is not None
                and item.seal is not None
                and not item.failure_codes
            }
        elif catalog_provider is not None:
            try:
                catalog_paths = tuple(
                    dict.fromkeys(
                        _identity_path(item)
                        for item in catalog_provider()
                    )
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return SmartReconnectTargetResolution((), ())
        all_source_paths = tuple(
            dict.fromkeys((*memberships_by_path, *catalog_paths))
        )

        if observation_snapshot is None:
            try:
                raw = self._resolver.resolve(all_source_paths)
            except (OSError, RuntimeError, TypeError, ValueError):
                raw = {}
            if isinstance(raw, Mapping):
                requested_paths = frozenset(all_source_paths)
                for raw_path, raw_fingerprint in raw.items():
                    try:
                        path = _identity_path(raw_path)
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
        pending_candidates: dict[
            str,
            SmartReconnectPendingIdentityCandidate,
        ] = {}
        verification_candidates: dict[
            str,
            SmartReconnectPendingIdentityCandidate,
        ] = {}
        for fingerprint in fingerprints:
            source_path_count = len(
                paths_by_fingerprint.get(fingerprint, ())
            )
            if (
                source_path_count > 1
                or (
                    (catalog_provider is not None or broker_catalog)
                    and source_path_count != 1
                )
            ):
                continue
            saved = saved_targets.get(fingerprint)
            unique_candidate = (
                SmartReconnectPendingIdentityCandidate(
                    fingerprint,
                    paths_by_fingerprint[fingerprint][0],
                )
                if source_path_count == 1
                else None
            )
            # A persisted pending identity is observation-only even if a later
            # group entry happens to mention the same shortcut.  It must finish
            # the independent evidence cycle before entering authorization.
            if saved is not None and saved.is_pending:
                if unique_candidate is not None:
                    pending_candidates[fingerprint] = unique_candidate
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
                            saved_targets,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        continue
                    if target is not None:
                        resolved_targets.append(target)
                resolved_memberships = tuple(resolved_targets)
                unique = tuple(dict.fromkeys(resolved_memberships))
                if len(unique) == 1:
                    candidates[fingerprint] = unique[0]
                    if (
                        unique_candidate is not None
                        and saved is not None
                        and saved.is_confirmed
                        and saved.has_complete_evidence
                    ):
                        verification_candidates[fingerprint] = unique_candidate
                continue
            target = self._ungrouped_target(
                fingerprint,
                characters_by_id,
                records_by_id,
                saved_targets,
                source,
                candidate_path=(
                    unique_candidate.shortcut_path
                    if (
                        observation_snapshot is not None
                        and unique_candidate is not None
                    )
                    else None
                ),
            )
            if target is not None:
                candidates[fingerprint] = target
                if (
                    unique_candidate is not None
                    and saved is not None
                    and saved.is_confirmed
                    and saved.has_complete_evidence
                ):
                    verification_candidates[fingerprint] = unique_candidate
            elif unique_candidate is not None:
                pending_candidates[fingerprint] = unique_candidate

        character_counts: dict[str, int] = {}
        for target in candidates.values():
            character_counts[target.character_id] = (
                character_counts.get(target.character_id, 0) + 1
            )
        return SmartReconnectTargetResolution(
            tuple(
                target
                for fingerprint in fingerprints
                if (target := candidates.get(fingerprint)) is not None
                and character_counts[target.character_id] == 1
            ),
            tuple(
                pending_candidates[fingerprint]
                for fingerprint in fingerprints
                if fingerprint in pending_candidates
            ),
            tuple(
                verification_candidates[fingerprint]
                for fingerprint in fingerprints
                if fingerprint in verification_candidates
            ),
        )

    def _build_requested_targets_from_source(
        self,
        fingerprints: tuple[str, ...],
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        return self._resolve_requested_targets_from_source(
            fingerprints,
            source,
            observation_snapshot=observation_snapshot,
        ).targets

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
        saved_targets: Mapping[str, _SavedTargetState],
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
        saved = saved_targets.get(fingerprint)
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
        saved_targets: Mapping[str, _SavedTargetState],
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        candidate_path: Path | None = None,
    ) -> SmartReconnectTargetIdentity | None:
        if candidate_path is None:
            provider = self._ungrouped_shortcut_provider
            if provider is None:
                return None
            try:
                candidate_path = provider(fingerprint)
            except (OSError, RuntimeError, TypeError, ValueError):
                return None
            if not isinstance(candidate_path, Path):
                return None
            path = _identity_path(candidate_path)
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
        else:
            path = _identity_path(candidate_path)
        if path.suffix.casefold() != ".lnk":
            return None
        saved = saved_targets.get(fingerprint)
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
        if len(matches) == 1:
            return matches[0]
        if (
            matches
            or saved is None
            or not saved.verified_aliases
            or not self._saved_verified_identity_is_safe(
                fingerprint,
                saved,
                source,
            )
        ):
            return None
        return SmartReconnectTargetIdentity(
            fingerprint=fingerprint,
            character_id=saved.character_id,
            role_aliases=saved.verified_aliases,
            importance=None,
            shortcut_path=path,
            original_slot_index=saved.slot_index,
            original_line_number=saved.line_number,
        )

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

    @staticmethod
    def _alias_owners_from_source(
        source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> dict[str, set[str]]:
        owners: dict[str, set[str]] = {}

        def remember(value: object, owner: object) -> None:
            alias = normalize_identity_alias(value)
            if (
                alias is not None
                and isinstance(owner, str)
                and owner.strip()
            ):
                owners.setdefault(alias, set()).add(owner.strip())

        for character in source.characters:
            if isinstance(character, Character):
                remember(character.display_name, character.character_id)
        for record in source.records:
            if not isinstance(record, CharacterWindowRecord):
                continue
            remember(record.display_name, record.character_id)
            for alias in record.aliases:
                remember(alias, record.character_id)
        for group in source.groups:
            for entry in group.entries:
                remember(entry.role_id, entry.entry_id)
        for saved in source.saved_by_fingerprint().values():
            if not saved.is_confirmed:
                continue
            for alias in saved.verified_aliases:
                remember(alias, saved.character_id)
        return owners

    @classmethod
    def _saved_verified_identity_is_safe(
        cls,
        fingerprint: str,
        saved: _SavedTargetState,
        source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> bool:
        if not saved.is_confirmed or not saved.verified_aliases:
            return False
        if any(
            getattr(value, "character_id", None) == saved.character_id
            for value in (*source.characters, *source.records)
        ):
            return False
        same_character_fingerprints = tuple(
            current_fingerprint
            for current_fingerprint, current in source.saved_targets
            if (
                current.character_id == saved.character_id
                and current.is_confirmed
                and current.verified_aliases
            )
        )
        if same_character_fingerprints != (fingerprint,):
            return False
        owners = cls._alias_owners_from_source(source)
        for alias in saved.verified_aliases:
            for known_alias, known_owners in owners.items():
                if (
                    alias == known_alias
                    and known_owners != {saved.character_id}
                ):
                    return False
        return True

    def record_identity_evidence(
        self,
        candidates: Iterable[SmartReconnectPendingIdentityCandidate],
        observations: Iterable[SmartReconnectIdentityEvidence],
        *,
        expected_generation: int,
        expected_config_revision: int,
        expected_source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> SmartReconnectIdentityEvidenceResult:
        """Persist one independent observation cycle without external I/O."""

        if (
            not isinstance(expected_source, SmartReconnectTargetIdentitySourceSnapshot)
            or isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 0
            or isinstance(expected_config_revision, bool)
            or not isinstance(expected_config_revision, int)
            or expected_config_revision < 0
        ):
            return SmartReconnectIdentityEvidenceResult(False)
        normalized_candidates = tuple(
            item
            for item in candidates
            if isinstance(item, SmartReconnectPendingIdentityCandidate)
        )
        fingerprint_counts: dict[str, int] = {}
        path_counts: dict[Path, int] = {}
        for item in normalized_candidates:
            fingerprint_counts[item.fingerprint] = (
                fingerprint_counts.get(item.fingerprint, 0) + 1
            )
            path_counts[item.shortcut_path] = (
                path_counts.get(item.shortcut_path, 0) + 1
            )
        unique_candidates = {
            item.fingerprint: item
            for item in normalized_candidates
            if fingerprint_counts[item.fingerprint] == 1
            and path_counts[item.shortcut_path] == 1
        }
        observed_by_fingerprint: dict[str, SmartReconnectIdentityEvidence] = {}
        duplicate_observations: set[str] = set()
        for item in observations:
            if (
                not isinstance(item, SmartReconnectIdentityEvidence)
                or unique_candidates.get(item.candidate.fingerprint)
                != item.candidate
            ):
                continue
            if item.candidate.fingerprint in observed_by_fingerprint:
                duplicate_observations.add(item.candidate.fingerprint)
                continue
            observed_by_fingerprint[item.candidate.fingerprint] = item
        for fingerprint in duplicate_observations:
            observed_by_fingerprint.pop(fingerprint, None)

        expected_saved = expected_source.saved_by_fingerprint()
        alias_owners = self._alias_owners_from_source(expected_source)
        observed_alias_counts: dict[str, int] = {}
        for item in observed_by_fingerprint.values():
            observed_alias_counts[item.role_alias] = (
                observed_alias_counts.get(item.role_alias, 0) + 1
            )
        rejected_aliases: set[str] = set()
        for fingerprint, item in observed_by_fingerprint.items():
            current = expected_saved.get(fingerprint)
            other_owners = alias_owners.get(item.role_alias, set()) - (
                {current.character_id} if current is not None else set()
            )
            if observed_alias_counts[item.role_alias] != 1 or other_owners:
                rejected_aliases.add(fingerprint)
        for fingerprint in rejected_aliases:
            observed_by_fingerprint.pop(fingerprint, None)

        candidate_state = dict(expected_saved)
        confirmed: set[str] = set()
        changed = False
        committed_generation = expected_generation + 1
        for fingerprint, candidate in unique_candidates.items():
            current = candidate_state.get(fingerprint)
            original_revision = (
                current.evidence_revision if current is not None else 0
            )
            path_changed = (
                current is not None
                and current.shortcut_path is not None
                and current.shortcut_path != candidate.shortcut_path
            )
            if current is None:
                current = _SavedTargetState(
                    character_id=f"smart-reconnect-{uuid4().hex}",
                    slot_index=None,
                    line_number=None,
                    status="pending",
                    shortcut_path=candidate.shortcut_path,
                )
                candidate_state[fingerprint] = current
                changed = True
            elif path_changed:
                current = replace(
                    current,
                    slot_index=None,
                    line_number=None,
                    verified_aliases=(),
                    status="pending",
                    shortcut_path=candidate.shortcut_path,
                    instance=None,
                    shortcut_seal=None,
                    evidence_alias=None,
                    identity_generation=None,
                    config_revision=None,
                    evidence_revision=original_revision + 1,
                )
                candidate_state[fingerprint] = current
                changed = True

            observation = observed_by_fingerprint.get(fingerprint)
            if observation is None:
                continue
            same_evidence = (
                current.has_complete_evidence
                and current.shortcut_path == candidate.shortcut_path
                and current.instance == observation.instance
                and current.shortcut_seal == observation.shortcut_seal
                and current.evidence_alias == observation.role_alias
                and current.identity_generation == expected_generation
                and current.config_revision == expected_config_revision
            )
            if current.is_confirmed and same_evidence:
                confirmed.add(fingerprint)
                continue
            if (
                current.is_pending
                and same_evidence
                and current.identity_generation == expected_generation
            ):
                updated = replace(
                    current,
                    verified_aliases=(observation.role_alias,),
                    status="confirmed",
                    identity_generation=committed_generation,
                )
                candidate_state[fingerprint] = updated
                confirmed.add(fingerprint)
                changed = True
                continue
            updated = replace(
                current,
                slot_index=None,
                line_number=None,
                verified_aliases=(),
                status="pending",
                shortcut_path=candidate.shortcut_path,
                instance=observation.instance,
                shortcut_seal=observation.shortcut_seal,
                evidence_alias=observation.role_alias,
                identity_generation=committed_generation,
                config_revision=expected_config_revision,
                evidence_revision=original_revision + 1,
            )
            if updated != candidate_state.get(fingerprint):
                candidate_state[fingerprint] = updated
                changed = True

        if changed:
            candidate_state = self._advance_complete_evidence_generation(
                candidate_state,
                expected_generation=expected_generation,
                committed_generation=committed_generation,
            )

        def source_is_current() -> bool:
            return (
                self._coordinator.generation == expected_generation
                and tuple(sorted(self._saved.items()))
                == expected_source.saved_targets
                and (not self._state_write_blocked)
                == expected_source.state_writable
            )

        if not changed or self._state_write_blocked:
            snapshot = self._coordinator.capture_snapshot(source_is_current)
            return SmartReconnectIdentityEvidenceResult(
                source_current=(
                    snapshot.generation == expected_generation
                    and snapshot.value
                ),
                confirmed_fingerprints=(
                    frozenset(confirmed)
                    if not self._state_write_blocked
                    else frozenset()
                ),
                state_changed=False,
            )

        with self._remember_lock:
            def prepare(
                transaction: IdentityDataTransaction,
            ) -> SmartReconnectIdentityEvidenceResult:
                if not source_is_current():
                    return SmartReconnectIdentityEvidenceResult(False)
                content = self._serialize_state(candidate_state)
                transaction.stage_file(
                    IdentityDataResource.RECONNECT_IDENTITY,
                    self._state_path,
                    content,
                    lambda serialized: self._validate_serialized_state(
                        serialized,
                        candidate_state,
                    ),
                )
                transaction.stage_memory(
                    IdentityDataResource.RECONNECT_IDENTITY,
                    lambda: dict(self._saved),
                    lambda: self._install_saved(candidate_state),
                    self._restore_saved,
                )
                return SmartReconnectIdentityEvidenceResult(
                    True,
                    frozenset(confirmed),
                    True,
                )

            try:
                return self._coordinator.execute(prepare)
            except IdentityTransactionRollbackError:
                raise
            except (
                IdentityTransactionError,
                OSError,
                TypeError,
                ValueError,
            ):
                return SmartReconnectIdentityEvidenceResult(False)

    def _build_targets_from_source(
        self,
        group_name: object | None,
        source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> tuple[SmartReconnectTargetIdentity, ...]:
        groups = self._selected_groups_from_source(
            group_name,
            source.groups,
        )
        saved_targets = source.saved_by_fingerprint()
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
            dict.fromkeys(_identity_path(entry.shortcut_path) for _, entry in entries)
        )
        if observation_snapshot is not None:
            if observation_snapshot.failure_codes:
                raise SmartReconnectTargetIdentityError(
                    "observation source is unavailable"
                )
            fingerprints = {
                _identity_path(item.path): item.fingerprint
                for item in observation_snapshot.shortcuts
                if item.fingerprint is not None
                and item.seal is not None
                and not item.failure_codes
            }
        else:
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
                _identity_path(path): normalize_launch_fingerprint(value)
                for path, value in raw_fingerprints.items()
                if isinstance(path, Path)
            }
        if any(fingerprints.get(path) is None for path in paths):
            raise SmartReconnectTargetIdentityError(
                "shortcut fingerprint batch is incomplete"
            )

        characters = source.characters
        records = source.records
        characters_by_id = self._unique_characters(characters)
        records_by_id = self._unique_records(records)
        targets: list[SmartReconnectTargetIdentity] = []
        exact_memberships: set[tuple[str, str, Path]] = set()
        for group, entry in entries:
            path = _identity_path(entry.shortcut_path)
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
            saved = saved_targets.get(fingerprint)
            if saved is not None and saved.is_pending:
                continue
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

    @staticmethod
    def _selected_groups_from_source(
        group_name: object | None,
        groups: tuple[GroupConfiguration, ...],
    ) -> tuple[GroupConfiguration, ...]:
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
        with self._remember_lock:
            return self._remember_serialized(
                fingerprint,
                character_id,
                slot_index=slot_index,
                line_number=line_number,
            )

    def _remember_serialized(
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

        try:
            source = self._capture_source()
            if not source.value.state_writable:
                return False
            observation = self._latest_observation_for_source(source.value)
            targets = self.targets_for_fingerprints_from_source_snapshot(
                (normalized,),
                source.value,
                observation_snapshot=observation,
            )
        except (
            IdentityTransactionError,
            OSError,
            SmartReconnectTargetIdentityError,
            TypeError,
            ValueError,
        ):
            return False
        target = next(
            (item for item in targets if item.fingerprint == normalized),
            None,
        )
        if target is None or target.character_id != character_id.strip():
            return False

        def prepare(transaction: IdentityDataTransaction) -> bool:
            if (
                self._coordinator.generation != source.generation
                or tuple(sorted(self._saved.items()))
                != source.value.saved_targets
            ):
                return False
            current = self._saved.get(
                target.fingerprint,
                _SavedTargetState(target.character_id, None, None),
            )
            if current.is_pending:
                return False
            updated = replace(
                current,
                character_id=target.character_id,
                slot_index=(
                    current.slot_index if slot_index is None else slot_index
                ),
                line_number=(
                    current.line_number if line_number is None else line_number
                ),
            )
            if updated == current:
                return True
            candidate = dict(self._saved)
            candidate[target.fingerprint] = updated
            candidate = self._advance_complete_evidence_generation(
                candidate,
                expected_generation=source.generation,
                committed_generation=source.generation + 1,
            )
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
        except FileNotFoundError:
            return {}
        except OSError:
            self._state_write_blocked = True
            return {}
        try:
            return self._parse_state(content)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            self._state_write_blocked = True
            return {}

    @classmethod
    def _parse_state(cls, content: bytes) -> dict[str, _SavedTargetState]:
        payload = json.loads(content.decode("utf-8"))
        if (
            not isinstance(payload, Mapping)
            or payload.get("version") not in (1, cls.SCHEMA_VERSION)
        ):
            raise ValueError("unsupported target identity state")
        version = payload.get("version")
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
            raw_aliases = (
                raw_value.get("verified_aliases", ())
                if version == cls.SCHEMA_VERSION
                else ()
            )
            if not isinstance(raw_aliases, (list, tuple)):
                raise ValueError("target identity state entry is invalid")
            verified_aliases = tuple(
                dict.fromkeys(
                    alias
                    for alias in (
                        normalize_identity_alias(value)
                        for value in raw_aliases
                    )
                    if alias is not None
                )
            )
            status = (
                raw_value.get("status", "confirmed")
                if version == cls.SCHEMA_VERSION
                else "confirmed"
            )
            raw_path = (
                raw_value.get("shortcut_path")
                if version == cls.SCHEMA_VERSION
                else None
            )
            shortcut_path = None
            if raw_path is not None:
                if not isinstance(raw_path, str) or not raw_path.strip():
                    raise ValueError("target identity state entry is invalid")
                shortcut_path = _identity_path(raw_path)
                if shortcut_path.suffix.casefold() != ".lnk":
                    raise ValueError("target identity state entry is invalid")
            instance = cls._parse_saved_instance(
                raw_value.get("instance")
                if version == cls.SCHEMA_VERSION
                else None
            )
            shortcut_seal = cls._parse_saved_seal(
                raw_value.get("shortcut_seal")
                if version == cls.SCHEMA_VERSION
                else None
            )
            raw_evidence_alias = (
                raw_value.get("evidence_alias")
                if version == cls.SCHEMA_VERSION
                else None
            )
            evidence_alias = normalize_identity_alias(raw_evidence_alias)
            identity_generation = (
                raw_value.get("identity_generation")
                if version == cls.SCHEMA_VERSION
                else None
            )
            config_revision = (
                raw_value.get("config_revision")
                if version == cls.SCHEMA_VERSION
                else None
            )
            evidence_revision = (
                raw_value.get("evidence_revision", 0)
                if version == cls.SCHEMA_VERSION
                else 0
            )
            evidence_fields = (
                instance,
                shortcut_seal,
                evidence_alias,
                identity_generation,
                config_revision,
            )
            evidence_is_empty = all(value is None for value in evidence_fields)
            evidence_is_complete = all(value is not None for value in evidence_fields)
            v2_structure_is_valid = (
                version != cls.SCHEMA_VERSION
                or (
                    status == "confirmed"
                    and (
                        (
                            not verified_aliases
                            and shortcut_path is None
                            and evidence_is_empty
                            and evidence_revision == 0
                        )
                        or (
                            shortcut_path is not None
                            and evidence_is_complete
                            and evidence_revision > 0
                            and verified_aliases == (evidence_alias,)
                        )
                    )
                )
                or (
                    version == cls.SCHEMA_VERSION
                    and status == "pending"
                    and not verified_aliases
                    and shortcut_path is not None
                    and (evidence_is_empty or evidence_is_complete)
                )
            )
            if (
                not isinstance(character_id, str)
                or not character_id.strip()
                or status not in {"pending", "confirmed"}
                or len(verified_aliases) != len(raw_aliases)
                or any(len(alias) < 3 for alias in verified_aliases)
                or (
                    raw_evidence_alias is not None
                    and (
                        not isinstance(raw_evidence_alias, str)
                        or evidence_alias is None
                        or len(evidence_alias) < 3
                        or any(
                            character.isspace()
                            for character in raw_evidence_alias
                        )
                        or "..." in raw_evidence_alias
                        or "…" in raw_evidence_alias
                    )
                )
                or (not evidence_is_empty and not evidence_is_complete)
                or (evidence_is_complete and shortcut_path is None)
                or (
                    identity_generation is not None
                    and (
                        isinstance(identity_generation, bool)
                        or not isinstance(identity_generation, int)
                        or identity_generation < 0
                    )
                )
                or (
                    config_revision is not None
                    and (
                        isinstance(config_revision, bool)
                        or not isinstance(config_revision, int)
                        or config_revision < 0
                    )
                )
                or isinstance(evidence_revision, bool)
                or not isinstance(evidence_revision, int)
                or evidence_revision < 0
                or (evidence_is_complete and evidence_revision == 0)
                or (
                    evidence_is_empty
                    and shortcut_path is None
                    and evidence_revision != 0
                )
                or not v2_structure_is_valid
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
                verified_aliases,
                status,
                shortcut_path,
                instance,
                shortcut_seal,
                evidence_alias,
                identity_generation,
                config_revision,
                evidence_revision,
            )
        return result

    @staticmethod
    def _parse_saved_instance(raw: object) -> WindowInstanceToken | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError("target identity instance is invalid")
        rect = raw.get("rect")
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise ValueError("target identity instance is invalid")
        return WindowInstanceToken(
            handle=raw.get("handle"),
            process_id=raw.get("process_id"),
            thread_id=raw.get("thread_id"),
            window_class=raw.get("window_class"),
            rect=tuple(rect),
            minimized=raw.get("minimized"),
            process_lifecycle_token=raw.get("process_lifecycle_token"),
        )

    @staticmethod
    def _parse_saved_seal(raw: object) -> ShortcutSeal | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError("target identity shortcut seal is invalid")
        raw_identity = raw.get("file_identity")
        if not isinstance(raw_identity, Mapping):
            raise ValueError("target identity shortcut seal is invalid")
        return ShortcutSeal(
            ShortcutFileIdentity(
                normalized_path=raw_identity.get("normalized_path"),
                volume_serial_number=raw_identity.get(
                    "volume_serial_number"
                ),
                file_index=raw_identity.get("file_index"),
            ),
            content_sha256=raw.get("content_sha256"),
            launch_fingerprint=raw.get("launch_fingerprint"),
        )

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
                    "verified_aliases": list(value.verified_aliases),
                    "status": value.status,
                    "shortcut_path": (
                        str(value.shortcut_path)
                        if value.shortcut_path is not None
                        else None
                    ),
                    "instance": (
                        {
                            "handle": value.instance.handle,
                            "process_id": value.instance.process_id,
                            "thread_id": value.instance.thread_id,
                            "window_class": value.instance.window_class,
                            "rect": list(value.instance.rect),
                            "minimized": value.instance.minimized,
                            "process_lifecycle_token": (
                                value.instance.process_lifecycle_token
                            ),
                        }
                        if value.instance is not None
                        else None
                    ),
                    "shortcut_seal": (
                        {
                            "file_identity": {
                                "normalized_path": (
                                    value.shortcut_seal.file_identity.normalized_path
                                ),
                                "volume_serial_number": (
                                    value.shortcut_seal.file_identity.volume_serial_number
                                ),
                                "file_index": (
                                    value.shortcut_seal.file_identity.file_index
                                ),
                            },
                            "content_sha256": value.shortcut_seal.content_sha256,
                            "launch_fingerprint": (
                                value.shortcut_seal.launch_fingerprint
                            ),
                        }
                        if value.shortcut_seal is not None
                        else None
                    ),
                    "evidence_alias": value.evidence_alias,
                    "identity_generation": value.identity_generation,
                    "config_revision": value.config_revision,
                    "evidence_revision": value.evidence_revision,
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
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return False

    @staticmethod
    def _advance_complete_evidence_generation(
        candidate: Mapping[str, _SavedTargetState],
        *,
        expected_generation: int,
        committed_generation: int,
    ) -> dict[str, _SavedTargetState]:
        return {
            fingerprint: (
                replace(
                    state,
                    identity_generation=committed_generation,
                )
                if (
                    state.has_complete_evidence
                    and state.identity_generation == expected_generation
                )
                else state
            )
            for fingerprint, state in candidate.items()
        }

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
    "SmartReconnectIdentityEvidence",
    "SmartReconnectIdentityEvidenceResult",
    "SmartReconnectPendingIdentityCandidate",
    "SmartReconnectTargetIdentity",
    "SmartReconnectTargetIdentityError",
    "SmartReconnectTargetResolution",
    "SmartReconnectTargetIdentitySourceSnapshot",
    "SmartReconnectTargetIdentityService",
    "complete_reconnect_role_alias",
    "normalize_reconnect_role_alias",
]
