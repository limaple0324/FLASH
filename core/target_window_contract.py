"""Versioned internal contract shared by all target-window features."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from core.smart_reconnect_authorization import (
    ShortcutFileIdentity,
    ShortcutSeal,
    _normalized_sha256,
)
from core.window_instance import WindowInstanceToken


class TargetWindowPhase(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"
    MINIMIZED = "minimized"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class ObservationFreshness(str, Enum):
    """Whether pixels prove the current desktop frame for this exact window."""

    PROVEN_CURRENT = "proven_current"
    UNPROVEN = "unproven"


@dataclass(frozen=True, slots=True)
class ShortcutObservationCacheKey:
    normalized_path: str
    file_identity: ShortcutFileIdentity
    modified_ns: int
    size: int
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.normalized_path, str) or not self.normalized_path:
            raise ValueError("shortcut cache path must not be empty")
        if not isinstance(self.file_identity, ShortcutFileIdentity):
            raise TypeError("shortcut cache key requires file identity")
        if self.file_identity.normalized_path != self.normalized_path:
            raise ValueError("shortcut cache file identity path disagrees")
        for value in (self.modified_ns, self.size):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("shortcut cache values must be non-negative")
        digest = self.content_sha256.strip().casefold()
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("shortcut cache key requires a content SHA-256")
        object.__setattr__(self, "content_sha256", digest)


@dataclass(frozen=True, slots=True)
class ProcessObservationCacheKey:
    process_id: int
    process_lifecycle_token: int

    def __post_init__(self) -> None:
        for value in (self.process_id, self.process_lifecycle_token):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("process cache values must be positive")


@dataclass(frozen=True, slots=True)
class RoleObservationCacheKey:
    instance: WindowInstanceToken
    fingerprint: str
    shortcut_seal: ShortcutSeal
    source_generation: int
    role_region_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("role cache key requires a complete window instance")
        fingerprint = _normalized_sha256(self.fingerprint)
        if fingerprint is None:
            raise ValueError("role cache key requires a launch fingerprint")
        if not isinstance(self.shortcut_seal, ShortcutSeal):
            raise TypeError("role cache key requires a shortcut seal")
        if self.shortcut_seal.launch_fingerprint != fingerprint:
            raise ValueError("role cache shortcut seal disagrees")
        if (
            isinstance(self.source_generation, bool)
            or not isinstance(self.source_generation, int)
            or self.source_generation <= 0
        ):
            raise ValueError("role cache source generation must be positive")
        role_region_sha256 = _normalized_sha256(self.role_region_sha256)
        if role_region_sha256 is None:
            raise ValueError("role cache key requires a role-region SHA-256")
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(
            self,
            "role_region_sha256",
            role_region_sha256,
        )


@dataclass(frozen=True, slots=True, eq=False)
class ObservationActionLease:
    """Identity-only, non-forgeable lease for one published action snapshot."""

    request_serial: int
    observation_generation: int
    deadline_monotonic: float

    def __post_init__(self) -> None:
        for value in (self.request_serial, self.observation_generation):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("action lease serials must be positive")
        if (
            isinstance(self.deadline_monotonic, bool)
            or not isinstance(self.deadline_monotonic, (int, float))
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("action lease deadline must be positive")


@dataclass(frozen=True, slots=True)
class TargetWindowContract:
    """One resolved group role and its current Windows state.

    ``handle`` is deliberately internal-only.  Player-facing snapshots use
    :meth:`to_public_dict` and never expose it.
    """

    schema_version: int
    group_name: str
    window_code: str
    display_name: str
    binding_role: str
    role_id: str | None
    character_id: str | None
    process_id: int | None
    fingerprint: str | None
    phase: TargetWindowPhase
    safe: bool
    failure_codes: tuple[str, ...] = ()
    handle: int | None = None
    rect: tuple[int, int, int, int] | None = None
    visible: bool = False
    thread_id: int | None = None
    window_class: str | None = None
    process_lifecycle_token: int | None = None

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError("unsupported target-window contract version.")
        for value, field in (
            (self.group_name, "group_name"),
            (self.window_code, "window_code"),
            (self.display_name, "display_name"),
            (self.binding_role, "binding_role"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must not be empty.")
        if not isinstance(self.phase, TargetWindowPhase):
            raise TypeError("phase must be TargetWindowPhase.")
        if type(self.safe) is not bool:
            raise TypeError("safe must be bool.")
        if self.safe and (self.handle is None or self.fingerprint is None):
            raise ValueError("a safe target must have a handle and fingerprint.")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "group_name": self.group_name,
            "window_code": self.window_code,
            "display_name": self.display_name,
            "binding_role": self.binding_role,
            "role_id": self.role_id,
            "character_id": self.character_id,
            "process_id": self.process_id,
            "anonymous_fingerprint": self.fingerprint,
            "window_state": self.phase.value,
            "safe": self.safe,
            "failure_codes": list(self.failure_codes),
        }


@dataclass(frozen=True, slots=True)
class TargetWindowSnapshot:
    schema_version: int
    group_name: str
    targets: tuple[TargetWindowContract, ...] = ()
    failure_codes: tuple[str, ...] = ()

    SCHEMA_VERSION = 1

    @property
    def safe_targets(self) -> tuple[TargetWindowContract, ...]:
        return tuple(target for target in self.targets if target.safe)

    def to_public_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "group_name": self.group_name,
            "targets": [target.to_public_dict() for target in self.targets],
            "failure_codes": list(self.failure_codes),
        }


@dataclass(frozen=True, slots=True)
class ActualWindowContract:
    """One currently existing game window with complete immutable identity."""

    fingerprint: str
    instance: WindowInstanceToken
    visible: bool

    def __post_init__(self) -> None:
        fingerprint = _normalized_sha256(self.fingerprint)
        if fingerprint is None:
            raise ValueError("actual window fingerprint must be complete SHA-256")
        if not isinstance(self.instance, WindowInstanceToken):
            raise TypeError("actual window instance must be WindowInstanceToken")
        if type(self.visible) is not bool:
            raise TypeError("actual window visibility must be bool")
        object.__setattr__(self, "fingerprint", fingerprint)


@dataclass(frozen=True, slots=True)
class ActualWindowSnapshot:
    """Group-independent snapshot used only by per-window smart reconnect."""

    schema_version: int
    targets: tuple[ActualWindowContract, ...] = ()
    blocked_fingerprints: frozenset[str] = frozenset()
    isolated_window_count: int = 0
    anonymous_isolated_window_count: int = 0
    failure_codes: tuple[str, ...] = ()
    observation_generation: int = 0
    observation_request_serial: int = 0
    observation_static_generation: int = 0
    changed_fingerprints: frozenset[str] = frozenset()
    action_lease: ObservationActionLease | None = None

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError("unsupported actual-window contract version")
        targets = tuple(self.targets)
        if any(not isinstance(target, ActualWindowContract) for target in targets):
            raise TypeError("actual-window targets are invalid")
        fingerprints = tuple(target.fingerprint for target in targets)
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("actual-window targets contain duplicate fingerprints")
        blocked = frozenset(
            normalized
            for normalized in (
                _normalized_sha256(value)
                for value in self.blocked_fingerprints
            )
            if normalized is not None
        )
        if (
            len(blocked) != len(self.blocked_fingerprints)
            or blocked.intersection(fingerprints)
        ):
            raise ValueError("blocked actual-window fingerprints are invalid")
        for value, field in (
            (self.isolated_window_count, "isolated_window_count"),
            (
                self.anonymous_isolated_window_count,
                "anonymous_isolated_window_count",
            ),
            (self.observation_generation, "observation_generation"),
            (self.observation_request_serial, "observation_request_serial"),
            (
                self.observation_static_generation,
                "observation_static_generation",
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if (
            self.anonymous_isolated_window_count > self.isolated_window_count
            or self.isolated_window_count < len(blocked)
        ):
            raise ValueError("actual-window isolation counts disagree")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "blocked_fingerprints", blocked)
        changed = frozenset(
            normalized
            for normalized in (
                _normalized_sha256(value) for value in self.changed_fingerprints
            )
            if normalized is not None
        )
        if len(changed) != len(self.changed_fingerprints):
            raise ValueError("changed actual-window fingerprints are invalid")
        if self.action_lease is not None:
            if not isinstance(self.action_lease, ObservationActionLease):
                raise TypeError("actual-window action lease is invalid")
            if (
                self.action_lease.request_serial
                != self.observation_request_serial
                or self.action_lease.observation_generation
                != self.observation_generation
            ):
                raise ValueError("actual-window action lease disagrees")
        object.__setattr__(self, "changed_fingerprints", changed)
