"""Pure immutable contracts for one smart-reconnect authorization epoch."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from core.window_instance import WindowInstanceToken
from domain.character import CharacterImportance


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ABBREVIATION_SUFFIXES = ("…", "...")


def normalize_identity_alias(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = "".join(
        character
        for character in value.strip().casefold()
        if not character.isspace() and ord(character) >= 32
    )
    return normalized or None


def observed_alias_matches(
    aliases: tuple[str, ...],
    observed_value: object,
) -> bool:
    observed = normalize_identity_alias(observed_value)
    if observed is None:
        return False
    abbreviated = observed.endswith(_ABBREVIATION_SUFFIXES)
    comparison = observed.rstrip(".…") if abbreviated else observed
    if not comparison or (abbreviated and len(comparison) < 3):
        return False
    if abbreviated:
        return sum(alias.startswith(comparison) for alias in aliases) == 1
    return comparison in aliases


def identity_aliases_conflict(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) < 3 or len(right) < 3:
        return False
    shared = 0
    for left_character, right_character in zip(left, right):
        if left_character != right_character:
            break
        shared += 1
    return shared >= 3


class ReconnectLaunchMode(str, Enum):
    IDENTITY_BOUND = "identity_bound"
    COMPATIBILITY = "compatibility"


class ReconnectEvidencePhase(str, Enum):
    CAPTURED = "captured"
    RECOGNIZED = "recognized"
    IDENTITY_CONFIRMED = "identity_confirmed"
    ACTION_AUTHORIZED = "action_authorized"


class ReconnectRevocationReason(str, Enum):
    IDENTITY_WRITE = "identity_write"
    REPREPARE = "reprepare"
    PREPARATION_FAILED = "preparation_failed"
    SOURCE_CHANGED = "source_changed"
    STOPPED = "stopped"
    EXPLICIT = "explicit"


@dataclass(frozen=True, slots=True)
class ShortcutFileIdentity:
    normalized_path: str
    volume_serial_number: int
    file_index: int

    def __post_init__(self) -> None:
        path = os.path.normcase(os.path.abspath(os.fspath(self.normalized_path)))
        if not path or not os.path.isabs(path):
            raise ValueError("shortcut path must be absolute")
        if (
            isinstance(self.volume_serial_number, bool)
            or not isinstance(self.volume_serial_number, int)
            or self.volume_serial_number < 0
            or isinstance(self.file_index, bool)
            or not isinstance(self.file_index, int)
            or self.file_index < 0
        ):
            raise ValueError("shortcut file identity is incomplete")
        object.__setattr__(self, "normalized_path", path)

    @property
    def stable_key(self) -> tuple[int, int]:
        return self.volume_serial_number, self.file_index


@dataclass(frozen=True, slots=True)
class ShortcutSeal:
    file_identity: ShortcutFileIdentity
    content_sha256: str
    launch_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.file_identity, ShortcutFileIdentity):
            raise TypeError("file_identity must be ShortcutFileIdentity")
        content_digest = _normalized_sha256(self.content_sha256)
        fingerprint = _normalized_sha256(self.launch_fingerprint)
        if content_digest is None:
            raise ValueError("shortcut content digest must be complete SHA-256")
        if fingerprint is None:
            raise ValueError("shortcut launch fingerprint must be complete SHA-256")
        object.__setattr__(self, "content_sha256", content_digest)
        object.__setattr__(self, "launch_fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class ReconnectSourceIdentity:
    identity_generation: int
    config_revision: int
    group_id: str
    group_name: str
    character_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.identity_generation, bool)
            or not isinstance(self.identity_generation, int)
            or self.identity_generation < 0
            or isinstance(self.config_revision, bool)
            or not isinstance(self.config_revision, int)
            or self.config_revision < 0
        ):
            raise ValueError("source generations must be non-negative integers")
        group_id = _required_text(self.group_id, "group_id")
        group_name = _required_text(self.group_name, "group_name")
        character_ids = tuple(
            _required_text(value, "character_id") for value in self.character_ids
        )
        if len(character_ids) != len(set(character_ids)):
            raise ValueError(
                "source character identities contain an empty or duplicate value"
            )
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "group_name", group_name)
        object.__setattr__(self, "character_ids", character_ids)

    @property
    def source_generation(self) -> int:
        return self.identity_generation


@dataclass(frozen=True, slots=True)
class ReconnectAuthorizationTarget:
    fingerprint: str
    instance: WindowInstanceToken
    character_id: str | None = None
    role_aliases: tuple[str, ...] = ()
    importance: CharacterImportance | None = None
    original_slot_index: int | None = None
    original_line_number: int | None = None
    shortcut_seal: ShortcutSeal | None = None
    authorization_epoch: int | None = None
    authorization_id: str | None = None
    source_generation: int | None = None

    def __post_init__(self) -> None:
        fingerprint = _normalized_sha256(self.fingerprint)
        if fingerprint is None:
            raise ValueError("target fingerprint must be complete SHA-256")
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("instance must be a complete WindowInstanceToken")
        character_id = (
            _required_text(self.character_id, "character_id")
            if self.character_id is not None
            else None
        )
        aliases = tuple(
            dict.fromkeys(
                normalized
                for normalized in (
                    normalize_identity_alias(value) for value in self.role_aliases
                )
                if normalized is not None
            )
        )
        if self.importance is not None and not isinstance(
            self.importance, CharacterImportance
        ):
            raise TypeError("importance must be CharacterImportance or None")
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
        if self.shortcut_seal is not None:
            if not isinstance(self.shortcut_seal, ShortcutSeal):
                raise TypeError("shortcut_seal must be ShortcutSeal or None")
            if self.shortcut_seal.launch_fingerprint != fingerprint:
                raise ValueError("shortcut seal and target fingerprint disagree")
        grant_values = (
            self.authorization_epoch,
            self.authorization_id,
            self.source_generation,
        )
        if any(value is not None for value in grant_values):
            if (
                isinstance(self.authorization_epoch, bool)
                or not isinstance(self.authorization_epoch, int)
                or self.authorization_epoch <= 0
                or not isinstance(self.authorization_id, str)
                or not self.authorization_id.strip()
                or isinstance(self.source_generation, bool)
                or not isinstance(self.source_generation, int)
                or self.source_generation < 0
            ):
                raise ValueError("target authorization grant is incomplete")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "role_aliases", aliases)
        if self.authorization_id is not None:
            object.__setattr__(
                self,
                "authorization_id",
                self.authorization_id.strip(),
            )

    def matches_observed_identity(self, value: object) -> bool:
        return observed_alias_matches(self.role_aliases, value)

    def has_same_authorization_evidence(
        self,
        other: object,
    ) -> bool:
        """Compare immutable safety evidence without comparing its grant."""

        if not isinstance(other, ReconnectAuthorizationTarget):
            return False
        return (
            self.fingerprint,
            self.instance,
            self.character_id,
            self.role_aliases,
            self.importance,
            self.original_slot_index,
            self.original_line_number,
            self.shortcut_seal,
        ) == (
            other.fingerprint,
            other.instance,
            other.character_id,
            other.role_aliases,
            other.importance,
            other.original_slot_index,
            other.original_line_number,
            other.shortcut_seal,
        )


@dataclass(frozen=True, slots=True)
class ReconnectAuthorizationBatch:
    epoch: int
    batch_id: str
    source: ReconnectSourceIdentity
    launch_mode: ReconnectLaunchMode
    targets: tuple[ReconnectAuthorizationTarget, ...]
    isolated_fingerprints: frozenset[str] = frozenset()
    isolated_window_count: int | None = None
    anonymous_isolated_window_count: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or self.epoch <= 0
        ):
            raise ValueError("authorization epoch must be positive")
        batch_id = _required_text(self.batch_id, "batch_id")
        if not isinstance(self.source, ReconnectSourceIdentity):
            raise TypeError("source must be ReconnectSourceIdentity")
        if not isinstance(self.launch_mode, ReconnectLaunchMode):
            raise TypeError("launch_mode must be ReconnectLaunchMode")
        targets = tuple(self.targets)
        if any(
            not isinstance(target, ReconnectAuthorizationTarget)
            for target in targets
        ):
            raise ValueError("authorization targets must be a complete collection")
        self._validate_unique_targets(targets)
        self._validate_complete_targets(targets)
        isolated = frozenset(
            normalized
            for normalized in (
                _normalized_sha256(value)
                for value in self.isolated_fingerprints
            )
            if normalized is not None
        )
        if (
            len(isolated) != len(self.isolated_fingerprints)
            or isolated.intersection(target.fingerprint for target in targets)
        ):
            raise ValueError("isolated fingerprints are invalid or authorized")
        isolated_count = (
            len(isolated)
            if self.isolated_window_count is None
            else self.isolated_window_count
        )
        if (
            isinstance(isolated_count, bool)
            or not isinstance(isolated_count, int)
            or isolated_count < len(isolated)
            or isinstance(self.anonymous_isolated_window_count, bool)
            or not isinstance(self.anonymous_isolated_window_count, int)
            or self.anonymous_isolated_window_count < 0
            or self.anonymous_isolated_window_count > isolated_count
        ):
            raise ValueError("authorization isolation counts are invalid")
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "isolated_fingerprints", isolated)
        object.__setattr__(self, "isolated_window_count", isolated_count)

    def target_for(self, fingerprint: object) -> ReconnectAuthorizationTarget | None:
        normalized = _normalized_sha256(fingerprint)
        if normalized is None:
            return None
        return next(
            (target for target in self.targets if target.fingerprint == normalized),
            None,
        )

    def unique_target_for_observed_identity(
        self,
        value: object,
        known_aliases: Iterable[object] = (),
    ) -> ReconnectAuthorizationTarget | None:
        """Return one target only when the current collection proves it unique."""

        observed = normalize_identity_alias(value)
        if observed is None:
            return None
        comparison = observed.rstrip(".…")
        if len(comparison) < 3:
            return None
        owners_by_alias: dict[str, set[str]] = {}
        target_owner: dict[str, str] = {}
        for target in self.targets:
            owner = target.character_id or target.fingerprint
            target_owner[target.fingerprint] = owner
            for alias in target.role_aliases:
                owners_by_alias.setdefault(alias, set()).add(owner)
        for item in known_aliases:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[1], str)
                and item[1].strip()
            ):
                normalized = normalize_identity_alias(item[0])
                owner = item[1].strip()
            else:
                normalized = normalize_identity_alias(item)
                current_owners = (
                    owners_by_alias.get(normalized, set())
                    if normalized is not None
                    else set()
                )
                owner = (
                    next(iter(current_owners))
                    if len(current_owners) == 1
                    else f"catalog:{normalized}"
                )
            if normalized is not None:
                owners_by_alias.setdefault(normalized, set()).add(owner)
        candidate_owners = {
            owner
            for alias, owners in owners_by_alias.items()
            if alias.startswith(comparison)
            for owner in owners
        }
        if len(candidate_owners) != 1:
            return None
        expected_owner = next(iter(candidate_owners))
        matches = tuple(
            target
            for target in self.targets
            if (
                target_owner[target.fingerprint] == expected_owner
                and target.matches_observed_identity(value)
            )
        )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _validate_unique_targets(
        targets: tuple[ReconnectAuthorizationTarget, ...],
    ) -> None:
        fingerprints = [target.fingerprint for target in targets]
        instances = [target.instance for target in targets]
        character_ids = [
            target.character_id
            for target in targets
            if target.character_id is not None
        ]
        shortcut_paths = [
            target.shortcut_seal.file_identity.normalized_path
            for target in targets
            if target.shortcut_seal is not None
        ]
        shortcut_files = [
            target.shortcut_seal.file_identity.stable_key
            for target in targets
            if target.shortcut_seal is not None
        ]
        for values, label in (
            (fingerprints, "fingerprint"),
            (instances, "window instance"),
            (character_ids, "character identity"),
            (shortcut_paths, "shortcut path"),
            (shortcut_files, "shortcut file identity"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} in authorization batch")

    def _validate_complete_targets(
        self,
        targets: tuple[ReconnectAuthorizationTarget, ...],
    ) -> None:
        if len(targets) != len(self.source.character_ids):
            raise ValueError("authorization targets do not match source identities")
        for target in targets:
            if (
                target.character_id is None
                or not target.role_aliases
                or (
                    target.importance is None
                    and self.launch_mode is not ReconnectLaunchMode.IDENTITY_BOUND
                )
                or target.shortcut_seal is None
            ):
                raise ValueError("authorization target is incomplete")
        target_character_ids = tuple(
            target.character_id for target in targets if target.character_id is not None
        )
        if target_character_ids != self.source.character_ids:
            raise ValueError("authorization targets do not match source identities")


@dataclass(frozen=True, slots=True)
class ReconnectActionContext:
    """Immutable evidence naming exactly one target in one authorization batch."""

    authorization_epoch: int
    batch_id: str
    source_generation: int
    fingerprint: str
    character_id: str
    instance: WindowInstanceToken
    launch_mode: ReconnectLaunchMode

    def __post_init__(self) -> None:
        if (
            isinstance(self.authorization_epoch, bool)
            or not isinstance(self.authorization_epoch, int)
            or self.authorization_epoch <= 0
            or isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 0
        ):
            raise ValueError("action context generations are invalid")
        batch_id = _required_text(self.batch_id, "batch_id")
        fingerprint = _normalized_sha256(self.fingerprint)
        character_id = _required_text(self.character_id, "character_id")
        if fingerprint is None:
            raise ValueError("action context fingerprint must be complete SHA-256")
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("action context instance must be WindowInstanceToken")
        if not isinstance(self.launch_mode, ReconnectLaunchMode):
            raise TypeError("action context launch_mode must be ReconnectLaunchMode")
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "character_id", character_id)

    @classmethod
    def from_batch_target(
        cls,
        batch: ReconnectAuthorizationBatch,
        target: ReconnectAuthorizationTarget,
    ) -> "ReconnectActionContext":
        if not isinstance(batch, ReconnectAuthorizationBatch):
            raise TypeError("batch must be ReconnectAuthorizationBatch")
        if not isinstance(target, ReconnectAuthorizationTarget):
            raise TypeError("target must be ReconnectAuthorizationTarget")
        if batch.target_for(target.fingerprint) != target or target.character_id is None:
            raise ValueError("target does not belong to the authorization batch")
        return cls(
            authorization_epoch=(
                target.authorization_epoch
                if target.authorization_epoch is not None
                else batch.epoch
            ),
            batch_id=(
                target.authorization_id
                if target.authorization_id is not None
                else batch.batch_id
            ),
            source_generation=(
                target.source_generation
                if target.source_generation is not None
                else batch.source.source_generation
            ),
            fingerprint=target.fingerprint,
            character_id=target.character_id,
            instance=target.instance,
            launch_mode=batch.launch_mode,
        )


@dataclass(frozen=True, slots=True)
class ReconnectFrameWitness:
    authorization_epoch: int
    batch_id: str
    source_generation: int
    fingerprint: str
    character_id: str
    instance: WindowInstanceToken
    launch_mode: ReconnectLaunchMode
    phase: ReconnectEvidencePhase
    frame_sha256: str
    observed_at_ns: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.authorization_epoch, bool)
            or not isinstance(self.authorization_epoch, int)
            or self.authorization_epoch <= 0
            or isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation < 0
            or isinstance(self.observed_at_ns, bool)
            or not isinstance(self.observed_at_ns, int)
            or self.observed_at_ns <= 0
        ):
            raise ValueError("frame witness generations and time are invalid")
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("instance must be WindowInstanceToken")
        if not isinstance(self.launch_mode, ReconnectLaunchMode):
            raise TypeError("launch_mode must be ReconnectLaunchMode")
        if not isinstance(self.phase, ReconnectEvidencePhase):
            raise TypeError("phase must be ReconnectEvidencePhase")
        batch_id = _required_text(self.batch_id, "batch_id")
        fingerprint = _normalized_sha256(self.fingerprint)
        character_id = _required_text(self.character_id, "character_id")
        digest = _normalized_sha256(self.frame_sha256)
        if fingerprint is None:
            raise ValueError("frame fingerprint must be complete SHA-256")
        if digest is None:
            raise ValueError("frame digest must be complete SHA-256")
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "frame_sha256", digest)


def _normalized_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _SHA256_PATTERN.fullmatch(normalized) else None


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value.strip()


__all__ = [
    "ReconnectActionContext",
    "ReconnectAuthorizationBatch",
    "ReconnectAuthorizationTarget",
    "ReconnectEvidencePhase",
    "ReconnectFrameWitness",
    "ReconnectLaunchMode",
    "ReconnectRevocationReason",
    "ReconnectSourceIdentity",
    "ShortcutFileIdentity",
    "ShortcutSeal",
    "identity_aliases_conflict",
    "normalize_identity_alias",
    "observed_alias_matches",
]
