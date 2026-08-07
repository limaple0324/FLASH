"""Pure immutable contracts for one smart-reconnect authorization epoch."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

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
        if not character_ids or len(character_ids) != len(set(character_ids)):
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
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "role_aliases", aliases)

    def matches_observed_identity(self, value: object) -> bool:
        return observed_alias_matches(self.role_aliases, value)


@dataclass(frozen=True, slots=True)
class ReconnectAuthorizationBatch:
    epoch: int
    batch_id: str
    source: ReconnectSourceIdentity
    launch_mode: ReconnectLaunchMode
    targets: tuple[ReconnectAuthorizationTarget, ...]

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
        if not targets or any(
            not isinstance(target, ReconnectAuthorizationTarget)
            for target in targets
        ):
            raise ValueError("authorization targets must be a non-empty complete batch")
        self._validate_unique_targets(targets)
        self._validate_complete_targets(targets)
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "targets", targets)

    def target_for(self, fingerprint: object) -> ReconnectAuthorizationTarget | None:
        normalized = _normalized_sha256(fingerprint)
        if normalized is None:
            return None
        return next(
            (target for target in self.targets if target.fingerprint == normalized),
            None,
        )

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
                or target.importance is None
                or target.original_slot_index is None
                or target.original_line_number is None
                or target.shortcut_seal is None
            ):
                raise ValueError("authorization target is incomplete")
        target_character_ids = tuple(
            target.character_id for target in targets if target.character_id is not None
        )
        if target_character_ids != self.source.character_ids:
            raise ValueError("authorization targets do not match source identities")
        for index, left in enumerate(targets):
            for right in targets[index + 1 :]:
                if any(
                    identity_aliases_conflict(left_alias, right_alias)
                    for left_alias in left.role_aliases
                    for right_alias in right.role_aliases
                ):
                    raise ValueError("cross-target identity aliases are ambiguous")


@dataclass(frozen=True, slots=True)
class ReconnectFrameWitness:
    authorization_epoch: int
    batch_id: str
    source_generation: int
    instance: WindowInstanceToken
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
        if not isinstance(self.phase, ReconnectEvidencePhase):
            raise TypeError("phase must be ReconnectEvidencePhase")
        batch_id = _required_text(self.batch_id, "batch_id")
        digest = _normalized_sha256(self.frame_sha256)
        if digest is None:
            raise ValueError("frame digest must be complete SHA-256")
        object.__setattr__(self, "batch_id", batch_id)
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
