"""Player-facing group choices from confirmed registry and legacy group names."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.window_registry import WindowRegistry
from domain.character import Character
from domain.group import CharacterGroup
from services.group_configuration_service import GroupConfigurationService


@dataclass(frozen=True, slots=True)
class PlayerGroupMember:
    """Versioned, player-safe group member without shortcut paths."""

    entry_id: str
    display_name: str
    role: str
    role_id: str | None = None
    character_id: str | None = None

    SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PlayerGroupChoice:
    """A safe group choice without shortcut paths or launch arguments."""

    group_id: str
    name: str
    character_count: int
    members: tuple[PlayerGroupMember, ...] = ()

    SCHEMA_VERSION = 1

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
        if any(
            not isinstance(member, PlayerGroupMember)
            for member in self.members
        ):
            raise TypeError("members must contain PlayerGroupMember values.")
        if self.members and self.character_count != len(self.members):
            raise ValueError("character_count must match available members.")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "group_id": self.group_id,
            "name": self.name,
            "character_count": self.character_count,
            "members": [
                {
                    "schema_version": member.SCHEMA_VERSION,
                    "entry_id": member.entry_id,
                    "display_name": member.display_name,
                    "role": member.role,
                    "role_id": member.role_id,
                    "character_id": member.character_id,
                }
                for member in self.members
            ],
        }


def default_legacy_group_config_path() -> Path | None:
    """Return a known old-program config path without scanning user folders."""
    app_data = os.environ.get("APPDATA")
    if not app_data:
        return None
    directory = Path(app_data) / "輔V0.2"
    candidates = (
        directory / "sync_launch_config.json",
        directory / "sync_launch_config_v02.json",
    )
    return next(
        (candidate for candidate in candidates if candidate.is_file()),
        candidates[0],
    )


class GroupSelectionService:
    """Build and select groups while failing closed on malformed old data."""

    def __init__(
        self,
        registry: WindowRegistry,
        *,
        legacy_config_path: Path | None = None,
        configuration: GroupConfigurationService | None = None,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        self._registry = registry
        self._configuration = configuration
        self._legacy_config_path = (
            Path(legacy_config_path)
            if legacy_config_path is not None
            else default_legacy_group_config_path()
        )
        self._legacy_active_group: str | None = None

    @staticmethod
    def _group_id(name: str) -> str:
        return GroupConfigurationService.group_id_for_name(name)

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
        configured_members: dict[str, tuple[PlayerGroupMember, ...]] = {}
        configured_group_ids: dict[str, str] = {}
        if self._configuration is not None:
            for group in self._configuration.groups():
                configured_group_ids[group.name] = group.group_id
                members: list[PlayerGroupMember] = []
                for entry in group.entries:
                    try:
                        record = self._registry.get(entry.entry_id)
                    except KeyError:
                        record = None
                    members.append(
                        PlayerGroupMember(
                            entry_id=entry.entry_id,
                            display_name=entry.display_name,
                            role=entry.role,
                            role_id=entry.role_id or None,
                            character_id=(
                                record.character_id
                                if record is not None
                                and record.group == group.name
                                else None
                            ),
                        )
                    )
                configured_members[group.name] = tuple(members)
                counts[group.name] = len(members)
        registry_members: dict[str, list[PlayerGroupMember]] = {}
        for record in self._registry.all():
            name = self._clean_name(record.group)
            if name is not None:
                registry_members.setdefault(name, []).append(
                    PlayerGroupMember(
                        entry_id=record.character_id,
                        display_name=record.display_name,
                        role=record.role or "",
                        character_id=record.character_id,
                    )
                )
        for name in sorted(registry_members, key=str.casefold):
            if name not in configured_members:
                configured_members[name] = tuple(registry_members[name])
            counts[name] = max(
                counts.get(name, 0),
                len(registry_members[name]),
            )

        return tuple(
            PlayerGroupChoice(
                group_id=configured_group_ids.get(
                    name,
                    self._group_id(name),
                ),
                name=name,
                character_count=(
                    len(configured_members[name])
                    if name in configured_members
                    else counts[name]
                ),
                members=configured_members.get(name, ()),
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
    def workspace_group(
        choice: PlayerGroupChoice,
        characters: tuple[Character, ...] = (),
    ) -> CharacterGroup:
        if not isinstance(choice, PlayerGroupChoice):
            raise TypeError("choice must be PlayerGroupChoice.")
        by_id = {
            character.character_id: character
            for character in characters
            if isinstance(character, Character)
        }
        ordered = tuple(
            by_id[member.character_id or member.entry_id]
            for member in choice.members
            if (member.character_id or member.entry_id) in by_id
        )
        return CharacterGroup(
            group_id=choice.group_id,
            name=choice.name,
            characters=ordered,
        )
