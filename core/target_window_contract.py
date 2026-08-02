"""Versioned internal contract shared by all target-window features."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class TargetWindowPhase(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"
    MINIMIZED = "minimized"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


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
