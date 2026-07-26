"""Player-facing group choices from confirmed registry and legacy group names."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.window_registry import WindowRegistry
from domain.group import CharacterGroup


@dataclass(frozen=True, slots=True)
class PlayerGroupChoice:
    """A safe group choice without shortcut paths or launch arguments."""

    group_id: str
    name: str
    character_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id.strip():
            raise ValueError("group_id must not be empty.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must not be empty.")
        if (
            isinstance(self.character_count, bool)
            or not isinstance(self.character_count, int)
            or self.character_count < 0
        ):
            raise ValueError("character_count must be a non-negative integer.")


def default_legacy_group_config_path() -> Path | None:
    """Return the known old-program config path without scanning user folders."""
    app_data = os.environ.get("APPDATA")
    if not app_data:
        return None
    return Path(app_data) / "輔V0.2" / "sync_launch_config_v02.json"


class GroupSelectionService:
    """Build and select groups while failing closed on malformed old data."""

    def __init__(
        self,
        registry: WindowRegistry,
        *,
        legacy_config_path: Path | None = None,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        self._registry = registry
        self._legacy_config_path = (
            Path(legacy_config_path)
            if legacy_config_path is not None
            else default_legacy_group_config_path()
        )
        self._legacy_active_group: str | None = None

    @staticmethod
    def _group_id(name: str) -> str:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        return f"group-{digest}"

    @staticmethod
    def _clean_name(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        name = value.strip()
        if not name or len(name) > 80 or any(ord(character) < 32 for character in name):
            return None
        return name

    def _legacy_groups(self) -> dict[str, int]:
        path = self._legacy_config_path
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping):
            return {}

        app_state = payload.get("app_state")
        if isinstance(app_state, Mapping):
            self._legacy_active_group = self._clean_name(
                app_state.get("active_group_name")
            )

        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list):
            return {}

        groups: dict[str, int] = {}
        for raw_group in raw_groups:
            if not isinstance(raw_group, Mapping):
                continue
            name = self._clean_name(raw_group.get("name"))
            if name is None:
                continue
            launch_entries = raw_group.get("launch_entries")
            count = len(launch_entries) if isinstance(launch_entries, list) else 0
            groups[name] = max(groups.get(name, 0), count)
        return groups

    def choices(self) -> tuple[PlayerGroupChoice, ...]:
        counts = self._legacy_groups()
        registry_counts: dict[str, int] = {}
        for record in self._registry.all():
            name = self._clean_name(record.group)
            if name is not None:
                registry_counts[name] = registry_counts.get(name, 0) + 1
        for name in sorted(registry_counts, key=str.casefold):
            counts[name] = max(counts.get(name, 0), registry_counts[name])

        return tuple(
            PlayerGroupChoice(
                group_id=self._group_id(name),
                name=name,
                character_count=counts[name],
            )
            for name in counts
        )

    def find(self, name: object) -> PlayerGroupChoice | None:
        cleaned = self._clean_name(name)
        if cleaned is None:
            return None
        return next(
            (choice for choice in self.choices() if choice.name == cleaned),
            None,
        )

    def initial_choice(self, configured_name: object = None) -> PlayerGroupChoice | None:
        configured = self.find(configured_name)
        if configured is not None:
            return configured
        choices = self.choices()
        legacy = self.find(self._legacy_active_group)
        if legacy is not None:
            return legacy
        return choices[0] if choices else None

    @staticmethod
    def workspace_group(choice: PlayerGroupChoice) -> CharacterGroup:
        if not isinstance(choice, PlayerGroupChoice):
            raise TypeError("choice must be PlayerGroupChoice.")
        return CharacterGroup(group_id=choice.group_id, name=choice.name)
