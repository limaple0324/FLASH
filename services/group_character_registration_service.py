"""Populate player-facing character data from the selected saved group."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationService,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)


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


@dataclass(frozen=True, slots=True)
class GroupCharacterRegistrationResult:
    profiles: tuple[Character, ...]
    group: GroupConfiguration | None


@dataclass(frozen=True, slots=True)
class GroupCharacterReconcileResult:
    profiles: tuple[Character, ...]
    groups: tuple[GroupConfiguration | None, ...]
    detached_entry_ids: tuple[str, ...]


class GroupCharacterRegistrationService:
    """Create stable records only from the user's saved group configuration."""

    def __init__(
        self,
        registry: WindowRegistry,
        registry_store: WindowRegistryStore,
        character_store: CharacterStore,
        configuration: GroupConfigurationService,
        coordinator: IdentityDataTransactionCoordinator,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        if not isinstance(registry_store, WindowRegistryStore):
            raise TypeError("registry_store must be WindowRegistryStore.")
        if not isinstance(character_store, CharacterStore):
            raise TypeError("character_store must be CharacterStore.")
        if not isinstance(configuration, GroupConfigurationService):
            raise TypeError("configuration must be GroupConfigurationService.")
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator.")
        if registry_store.coordinator is not coordinator:
            raise ValueError("registry_store must use the injected coordinator.")
        if character_store.coordinator is not coordinator:
            raise ValueError("character_store must use the injected coordinator.")
        if configuration.coordinator is not coordinator:
            raise ValueError("configuration must use the injected coordinator.")
        self._registry = registry
        self._registry_store = registry_store
        self._character_store = character_store
        self._configuration = configuration
        self._coordinator = coordinator

    def ensure_group(
        self,
        group_name: object,
        profiles: Iterable[Character],
    ) -> tuple[Character, ...]:
        return self._coordinator.execute(
            lambda transaction: self.stage_ensure_group(
                transaction,
                group_name,
                profiles,
            )
        ).profiles

    def stage_ensure_group(
        self,
        transaction: IdentityDataTransaction,
        group_name: object,
        profiles: Iterable[Character],
    ) -> GroupCharacterRegistrationResult:
        reconciled = self.stage_reconcile(
            transaction,
            profiles=profiles,
            group_names=(group_name,),
        )
        return GroupCharacterRegistrationResult(
            reconciled.profiles,
            reconciled.groups[0],
        )

    def stage_reconcile(
        self,
        transaction: IdentityDataTransaction,
        *,
        profiles: Iterable[Character],
        group_names: Iterable[object] = (),
        detachments: Iterable[tuple[object, Iterable[str]]] = (),
        group_overrides: Iterable[GroupConfiguration] = (),
    ) -> GroupCharacterReconcileResult:
        """Build and stage one complete candidate for a composed publication."""
        self._coordinator.require_transaction(transaction)
        existing_profiles = tuple(profiles)
        if any(not isinstance(character, Character) for character in existing_profiles):
            raise TypeError("profiles must contain only Character values.")
        profile_by_id = {
            character.character_id: character
            for character in existing_profiles
        }
        if len(profile_by_id) != len(existing_profiles):
            raise ValueError("Duplicate stable character identity.")
        requested_groups = tuple(group_names)
        requested_detachments = tuple(detachments)
        overrides: dict[str, GroupConfiguration] = {}
        for group in group_overrides:
            self._validate_group_override(group)
            if group.name in overrides:
                raise ValueError("duplicate group override")
            overrides[group.name] = group
        requested_names = {
            name.strip()
            for name in requested_groups
            if isinstance(name, str) and name.strip()
        }
        if any(name not in requested_names for name in overrides):
            raise ValueError("group override must match a requested group")
        candidate = self._registry.clone_runtime()
        existing_records = {
            record.character_id: record
            for record in candidate.all()
        }
        registry_changed = False
        profiles_changed = False
        detached: list[str] = []
        for group_name, entry_ids in requested_detachments:
            detached.extend(
                self._apply_detachments(candidate, group_name, entry_ids)
            )
        if detached:
            registry_changed = True
            for character_id in detached:
                existing_records[character_id] = candidate.get(character_id)

        actual_groups: list[GroupConfiguration | None] = []
        for group_name in requested_groups:
            cleaned_name = (
                group_name.strip()
                if isinstance(group_name, str) and group_name.strip()
                else None
            )
            group = (
                overrides[cleaned_name]
                if cleaned_name in overrides
                else self._configuration.group_in_transaction(
                    transaction,
                    group_name,
                )
            )
            actual_groups.append(group)
            if group is None:
                continue
            group_registry_changed, group_profiles_changed = self._apply_group(
                candidate,
                existing_records,
                profile_by_id,
                group,
            )
            registry_changed = registry_changed or group_registry_changed
            profiles_changed = profiles_changed or group_profiles_changed

        if profiles_changed:
            self._character_store.stage_save(transaction, profile_by_id.values())
        if registry_changed:
            self._stage_registry_candidate(transaction, candidate)
        return GroupCharacterReconcileResult(
            tuple(profile_by_id.values()),
            tuple(actual_groups),
            tuple(dict.fromkeys(detached)),
        )

    @staticmethod
    def _validate_group_override(group: object) -> None:
        if not isinstance(group, GroupConfiguration):
            raise TypeError("group overrides must contain GroupConfiguration values")
        if not group.name or group.name != group.name.strip():
            raise ValueError("group override name must already be normalized")
        if not group.group_id or group.group_id != group.group_id.strip():
            raise ValueError("group override identity must already be normalized")
        identities = tuple(entry.entry_id for entry in group.entries)
        if any(not identity or identity != identity.strip() for identity in identities):
            raise ValueError("group override entry identities must be normalized")
        if len(identities) != len(set(identities)):
            raise ValueError("group override entry identities must be unique")

    @staticmethod
    def _apply_group(
        candidate: WindowRegistry,
        existing_records: dict[str, object],
        profile_by_id: dict[str, Character],
        group: GroupConfiguration,
    ) -> tuple[bool, bool]:
        registry_changed = False
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
                existing_records[entry.entry_id] = candidate.get(entry.entry_id)
                registry_changed = True
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
                    existing_records[entry.entry_id] = candidate.get(entry.entry_id)
                    registry_changed = True
                elif (
                    existing.group == group.name
                    and existing.role != role
                ):
                    candidate.set_group_role(
                        entry.entry_id,
                        group=group.name,
                        role=role,
                    )
                    existing_records[entry.entry_id] = candidate.get(entry.entry_id)
                    registry_changed = True
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
        return registry_changed, profiles_changed

    def detach_entries(
        self,
        group_name: object,
        entry_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Detach removed saved entries without deleting identity or notes."""
        return self._coordinator.execute(
            lambda transaction: self.stage_detach_entries(
                transaction,
                group_name,
                entry_ids,
            )
        )

    def stage_detach_entries(
        self,
        transaction: IdentityDataTransaction,
        group_name: object,
        entry_ids: Iterable[str],
    ) -> tuple[str, ...]:
        profiles: tuple[Character, ...] = ()
        reconciled = self.stage_reconcile(
            transaction,
            profiles=profiles,
            detachments=((group_name, entry_ids),),
        )
        return reconciled.detached_entry_ids

    @staticmethod
    def _apply_detachments(
        candidate: WindowRegistry,
        group_name: object,
        entry_ids: Iterable[str],
    ) -> tuple[str, ...]:
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
        return tuple(detached)

    def _stage_registry_candidate(
        self,
        transaction: IdentityDataTransaction,
        candidate: WindowRegistry,
    ) -> None:
        self._registry_store.stage_save(transaction, candidate)
        transaction.stage_memory(
            IdentityDataResource.WINDOW_REGISTRY,
            self._registry.clone_runtime,
            lambda: self._registry.replace_runtime(candidate),
            self._restore_registry,
        )

    def _restore_registry(self, snapshot: object) -> None:
        if not isinstance(snapshot, WindowRegistry):
            raise TypeError("invalid runtime registry snapshot")
        self._registry.replace_runtime(snapshot)
