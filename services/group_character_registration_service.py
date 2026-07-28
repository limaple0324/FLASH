"""Populate player-facing character data from the selected saved group."""

from __future__ import annotations

from collections.abc import Iterable

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore
from services.group_configuration_service import GroupConfigurationService


CONFIRMED_GROUP_CHARACTER_PROFILES: dict[
    tuple[str, str],
    tuple[str, int, CharacterImportance],
] = {
    **{
        ("14支", name): (name, 100, CharacterImportance.SECONDARY)
        for name in ("100古", "100靈", "100福", "100獵")
    },
    **{
        ("14支", name): (name, 120, CharacterImportance.PRIMARY)
        for name in ("120古", "120靈", "120射", "120福", "120獵")
    },
    ("14支", "160福"): ("亞洛", 160, CharacterImportance.PRIMARY),
    **{
        ("14支", name): (name, 160, CharacterImportance.PRIMARY)
        for name in ("餐廳", "大排", "160帥", "和尚")
    },
}


class GroupCharacterRegistrationService:
    """Create stable records only from the user's saved group configuration."""

    def __init__(
        self,
        registry: WindowRegistry,
        registry_store: WindowRegistryStore,
        character_store: CharacterStore,
        configuration: GroupConfigurationService,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        if not isinstance(registry_store, WindowRegistryStore):
            raise TypeError("registry_store must be WindowRegistryStore.")
        if not isinstance(character_store, CharacterStore):
            raise TypeError("character_store must be CharacterStore.")
        if not isinstance(configuration, GroupConfigurationService):
            raise TypeError("configuration must be GroupConfigurationService.")
        self._registry = registry
        self._registry_store = registry_store
        self._character_store = character_store
        self._configuration = configuration

    def ensure_group(
        self,
        group_name: object,
        profiles: Iterable[Character],
    ) -> tuple[Character, ...]:
        existing_profiles = tuple(profiles)
        profile_by_id = {
            character.character_id: character
            for character in existing_profiles
        }
        if len(profile_by_id) != len(existing_profiles):
            raise ValueError("Duplicate stable character identity.")
        group = self._configuration.group(group_name)
        if group is None:
            return existing_profiles

        candidate = WindowRegistry.from_dict(self._registry.to_dict())
        existing_records = {
            record.character_id: record
            for record in candidate.all()
        }
        new_records: list[tuple[str, str, str]] = []
        updated_records: list[tuple[str, str, str]] = []
        profiles_changed = False
        for entry in group.entries:
            confirmed = CONFIRMED_GROUP_CHARACTER_PROFILES.get(
                (group.name, entry.display_name)
            )
            display_name = (
                confirmed[0] if confirmed is not None else entry.display_name
            )
            importance = confirmed[2] if confirmed is not None else None
            if entry.entry_id not in existing_records:
                role = (
                    importance.value
                    if importance is not None
                    else (
                        "主控"
                        if entry.role == "主窗口"
                        else "同步"
                    )
                )
                candidate.register_character(
                    entry.entry_id,
                    display_name,
                    group=group.name,
                    role=role,
                )
                new_records.append((entry.entry_id, display_name, role))
            else:
                role = (
                    importance.value
                    if importance is not None
                    else (
                        "主控"
                        if entry.role == "主窗口"
                        else "同步"
                    )
                )
                existing = existing_records[entry.entry_id]
                if existing.group is None:
                    candidate.set_group_role(
                        entry.entry_id,
                        group=group.name,
                        role=role,
                    )
                    updated_records.append(
                        (entry.entry_id, group.name, role)
                    )
                elif (
                    existing.group == group.name
                    and existing.role != role
                ):
                    candidate.set_group_role(
                        entry.entry_id,
                        group=group.name,
                        role=role,
                    )
                    updated_records.append(
                        (entry.entry_id, group.name, role)
                    )
                # A shortcut identity can be reused by another group whose
                # role selection is different. Never move an existing
                # character record across groups merely because that group
                # was selected; a confirmed role ID must establish a separate
                # character identity first.
            if confirmed is not None and entry.entry_id not in profile_by_id:
                profile_by_id[entry.entry_id] = Character(
                    entry.entry_id,
                    display_name,
                    confirmed[1],
                    importance,
                )
                profiles_changed = True

        if profiles_changed:
            self._character_store.save(profile_by_id.values())
        if new_records or updated_records:
            self._registry_store.save(candidate)
            for character_id, display_name, role in new_records:
                self._registry.register_character(
                    character_id,
                    display_name,
                    group=group.name,
                    role=role,
                )
            for character_id, group_name, role in updated_records:
                self._registry.set_group_role(
                    character_id,
                    group=group_name,
                    role=role,
                )
        return tuple(profile_by_id.values())

    def detach_entries(
        self,
        group_name: object,
        entry_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Detach removed saved entries without deleting identity or notes."""
        if not isinstance(group_name, str) or not group_name.strip():
            return ()
        cleaned_group = group_name.strip()
        identities = tuple(
            dict.fromkeys(
                entry_id.strip()
                for entry_id in entry_ids
                if isinstance(entry_id, str) and entry_id.strip()
            )
        )
        if not identities:
            return ()

        candidate = WindowRegistry.from_dict(self._registry.to_dict())
        detached: list[str] = []
        for character_id in identities:
            try:
                record = candidate.get(character_id)
            except KeyError:
                continue
            if record.group != cleaned_group:
                continue
            candidate.set_group_role(
                character_id,
                group=None,
                role=None,
            )
            detached.append(character_id)

        if not detached:
            return ()
        self._registry_store.save(candidate)
        for character_id in detached:
            self._registry.set_group_role(
                character_id,
                group=None,
                role=None,
            )
        return tuple(detached)
