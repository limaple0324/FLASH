"""Prepare one complete reconnect authorization inside one source generation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from adapters.windows_shortcut_seal import ShortcutSealResolver
from config.config_manager import ConfigManager, ConfigStateSnapshot
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectSourceIdentity,
    ShortcutSeal,
)
from core.target_window_contract import (
    TargetWindowContract,
    TargetWindowPhase,
    TargetWindowSnapshot,
)
from core.window_instance import WindowInstanceToken
from core.window_registry import CharacterWindowRecord, WindowRegistry
from domain.character import Character
from domain.group import CharacterGroup
from services.character_view_service import CharacterViewService
from services.group_configuration_service import (
    GroupConfiguration,
    GroupConfigurationEntry,
    GroupConfigurationService,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)
from services.smart_reconnect_authorization_coordinator import (
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentity,
    SmartReconnectTargetIdentityError,
    SmartReconnectTargetIdentityService,
)
from services.target_window_contract_service import TargetWindowContractService
from workspace.service import WorkspaceService


class SmartReconnectPreparationError(RuntimeError):
    """A complete same-generation authorization could not be prepared."""


class SmartReconnectPreparationService:
    """Hold Config -> identity -> authorization in the one allowed order."""

    def __init__(
        self,
        *,
        target_identity_service: SmartReconnectTargetIdentityService,
        target_window_contract_service: TargetWindowContractService,
        shortcut_seal_resolver: ShortcutSealResolver,
        authorization_coordinator: SmartReconnectAuthorizationCoordinator,
        identity_coordinator: IdentityDataTransactionCoordinator,
        configuration: GroupConfigurationService,
        character_view: CharacterViewService,
        registry: WindowRegistry,
        workspace: WorkspaceService,
        config: ConfigManager,
        current_group_name_key: str,
        product_launch_mode: ReconnectLaunchMode,
    ) -> None:
        if not isinstance(identity_coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("identity_coordinator must be IdentityDataTransactionCoordinator")
        if target_identity_service.coordinator is not identity_coordinator:
            raise ValueError("target identity service must share the identity coordinator")
        if getattr(configuration, "coordinator", None) is not identity_coordinator:
            raise ValueError("configuration must share the identity coordinator")
        if getattr(character_view, "coordinator", None) is not identity_coordinator:
            raise ValueError("character view must share the identity coordinator")
        if getattr(workspace, "coordinator", None) is not identity_coordinator:
            raise ValueError("workspace must share the identity coordinator")
        if not callable(getattr(target_window_contract_service, "snapshot", None)):
            raise TypeError("target_window_contract_service must provide snapshot")
        if not callable(getattr(shortcut_seal_resolver, "resolve", None)):
            raise TypeError("shortcut_seal_resolver must provide resolve")
        if not isinstance(authorization_coordinator, SmartReconnectAuthorizationCoordinator):
            raise TypeError(
                "authorization_coordinator must be SmartReconnectAuthorizationCoordinator"
            )
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry")
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager")
        if not isinstance(current_group_name_key, str) or not current_group_name_key.strip():
            raise ValueError("current_group_name_key must not be empty")
        if product_launch_mode is not ReconnectLaunchMode.IDENTITY_BOUND:
            raise ValueError("product launch mode must remain identity-bound")
        self._target_identity = target_identity_service
        self._target_windows = target_window_contract_service
        self._shortcut_seals = shortcut_seal_resolver
        self._authorization = authorization_coordinator
        self._identity = identity_coordinator
        self._configuration = configuration
        self._character_view = character_view
        self._registry = registry
        self._workspace = workspace
        self._config = config
        self._current_group_name_key = current_group_name_key.strip()
        self._product_launch_mode = product_launch_mode

    @property
    def authorization_coordinator(self) -> SmartReconnectAuthorizationCoordinator:
        return self._authorization

    @property
    def identity_coordinator(self) -> IdentityDataTransactionCoordinator:
        return self._identity

    @property
    def product_launch_mode(self) -> ReconnectLaunchMode:
        return self._product_launch_mode

    def prepare(
        self,
        *,
        launch_mode: ReconnectLaunchMode,
    ) -> ReconnectAuthorizationBatch:
        """Publish only while Config and identity generations are still held."""
        if not isinstance(launch_mode, ReconnectLaunchMode):
            raise TypeError("launch_mode must be explicitly supplied")
        with self._config.resource_guard():
            config_snapshot = self._config.snapshot_state_locked()
            return self._identity.snapshot_with_generation(
                lambda generation: self._prepare_locked(
                    generation,
                    config_snapshot,
                    launch_mode,
                )
            )

    def _prepare_locked(
        self,
        identity_generation: int,
        config_snapshot: ConfigStateSnapshot,
        launch_mode: ReconnectLaunchMode,
    ) -> ReconnectAuthorizationBatch:
        self._identity.require_consistent_snapshot_owner()
        self._authorization.begin_reprepare()
        try:
            group = self._resolve_group(config_snapshot)
            workspace_group = self._workspace.snapshot().current_group
            self._validate_workspace_group(group, workspace_group)
            entries = tuple(group.entries)
            characters = self._character_view.character_profiles()
            records = self._registry.all()
            self._validate_identity_sources(
                group,
                workspace_group,
                characters,
                records,
            )
            identities = self._target_identity.targets_for_group_in_snapshot(
                group.name
            )
            window_snapshot = self._target_windows.snapshot(
                group.name,
                expanded_sync_scope=False,
            )
            seals = self._resolve_seals(entries)
            targets = self._build_authorization_targets(
                entries,
                identities,
                window_snapshot,
                seals,
            )
            source = ReconnectSourceIdentity(
                identity_generation=identity_generation,
                config_revision=config_snapshot.revision,
                group_id=group.group_id,
                group_name=group.name,
                character_ids=tuple(entry.entry_id for entry in entries),
            )
            return self._authorization.publish(source, launch_mode, targets)
        except Exception as error:
            self._authorization.fail_preparation()
            if isinstance(error, SmartReconnectPreparationError):
                raise
            raise SmartReconnectPreparationError(
                "smart reconnect authorization preparation failed"
            ) from error

    def _resolve_group(
        self,
        config_snapshot: ConfigStateSnapshot,
    ) -> GroupConfiguration:
        raw_group_name = config_snapshot.data.get(self._current_group_name_key)
        if not isinstance(raw_group_name, str) or not raw_group_name.strip():
            raise SmartReconnectPreparationError("current group is unavailable")
        group = self._configuration.group(raw_group_name.strip())
        if group is None or not group.entries:
            raise SmartReconnectPreparationError("configured group is unavailable")
        return group

    @staticmethod
    def _validate_workspace_group(
        group: GroupConfiguration,
        workspace_group: CharacterGroup | None,
    ) -> None:
        if (
            not isinstance(workspace_group, CharacterGroup)
            or workspace_group.group_id != group.group_id
            or workspace_group.name != group.name
            or workspace_group.character_ids
            != tuple(entry.entry_id for entry in group.entries)
        ):
            raise SmartReconnectPreparationError(
                "workspace and configured group identities disagree"
            )

    @staticmethod
    def _validate_identity_sources(
        group: GroupConfiguration,
        workspace_group: CharacterGroup,
        characters: tuple[Character, ...],
        records: tuple[CharacterWindowRecord, ...],
    ) -> None:
        character_by_id = SmartReconnectPreparationService._unique_by_character_id(
            characters,
            "character",
        )
        record_by_id = SmartReconnectPreparationService._unique_by_character_id(
            records,
            "registry",
        )
        workspace_by_id = SmartReconnectPreparationService._unique_by_character_id(
            workspace_group.characters,
            "workspace",
        )
        expected = tuple(entry.entry_id for entry in group.entries)
        if (
            any(character_id not in character_by_id for character_id in expected)
            or any(character_id not in record_by_id for character_id in expected)
            or tuple(workspace_by_id) != expected
        ):
            raise SmartReconnectPreparationError(
                "character, registry, or workspace batch is incomplete"
            )
        for character_id in expected:
            character = character_by_id[character_id]
            record = record_by_id[character_id]
            workspace_character = workspace_by_id[character_id]
            if (
                character != workspace_character
                or character.display_name != record.display_name
                or record.group not in (None, group.name)
            ):
                raise SmartReconnectPreparationError(
                    "same-generation character sources disagree"
                )

    @staticmethod
    def _unique_by_character_id(values, label: str):
        result = {}
        for value in values:
            character_id = getattr(value, "character_id", None)
            if (
                not isinstance(character_id, str)
                or not character_id.strip()
                or character_id in result
            ):
                raise SmartReconnectPreparationError(
                    f"{label} character identities are duplicated"
                )
            result[character_id] = value
        return result

    def _resolve_seals(
        self,
        entries: tuple[GroupConfigurationEntry, ...],
    ) -> dict[Path, ShortcutSeal]:
        paths = tuple(entry.shortcut_path for entry in entries)
        try:
            raw = self._shortcut_seals.resolve(paths)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise SmartReconnectPreparationError(
                "shortcut seal batch is unavailable"
            ) from error
        if not isinstance(raw, Mapping):
            raise SmartReconnectPreparationError(
                "shortcut seal result is invalid"
            )
        seals = {
            self._normalized_path(path): seal
            for path, seal in raw.items()
            if isinstance(path, Path) and isinstance(seal, ShortcutSeal)
        }
        expected_paths = tuple(
            self._normalized_path(entry.shortcut_path) for entry in entries
        )
        if (
            len(expected_paths) != len(set(expected_paths))
            or any(path not in seals for path in expected_paths)
            or len(seals) != len(expected_paths)
        ):
            raise SmartReconnectPreparationError(
                "shortcut seal batch is incomplete or duplicated"
            )
        if any(
            self._normalized_path(seals[path].file_identity.normalized_path) != path
            for path in expected_paths
        ):
            raise SmartReconnectPreparationError(
                "shortcut seal path identity changed"
            )
        return seals

    @staticmethod
    def _build_authorization_targets(
        entries: tuple[GroupConfigurationEntry, ...],
        identities: tuple[SmartReconnectTargetIdentity, ...],
        window_snapshot: TargetWindowSnapshot,
        seals: Mapping[Path, ShortcutSeal],
    ) -> tuple[ReconnectAuthorizationTarget, ...]:
        if (
            not isinstance(window_snapshot, TargetWindowSnapshot)
            or window_snapshot.failure_codes
            or len(window_snapshot.targets) != len(entries)
            or len(identities) != len(entries)
        ):
            raise SmartReconnectPreparationError(
                "window or identity batch is incomplete"
            )
        identity_by_character = {
            identity.character_id: identity for identity in identities
        }
        windows_by_character: dict[str, TargetWindowContract] = {}
        for window in window_snapshot.targets:
            if (
                not window.safe
                or window.failure_codes
                or not isinstance(window.character_id, str)
                or window.character_id in windows_by_character
            ):
                raise SmartReconnectPreparationError(
                    "window batch contains an unsafe or duplicate target"
                )
            windows_by_character[window.character_id] = window
        result: list[ReconnectAuthorizationTarget] = []
        for entry in entries:
            identity = identity_by_character.get(entry.entry_id)
            window = windows_by_character.get(entry.entry_id)
            seal = seals.get(SmartReconnectPreparationService._normalized_path(entry.shortcut_path))
            if identity is None or window is None or seal is None:
                raise SmartReconnectPreparationError(
                    "authorization target source is missing"
                )
            if (
                identity.fingerprint != window.fingerprint
                or identity.fingerprint != seal.launch_fingerprint
            ):
                raise SmartReconnectPreparationError(
                    "window, character, and shortcut fingerprints disagree"
                )
            instance = SmartReconnectPreparationService._window_instance(window)
            result.append(
                ReconnectAuthorizationTarget(
                    fingerprint=identity.fingerprint,
                    instance=instance,
                    character_id=identity.character_id,
                    role_aliases=identity.role_aliases,
                    importance=identity.importance,
                    original_slot_index=identity.original_slot_index,
                    original_line_number=identity.original_line_number,
                    shortcut_seal=seal,
                )
            )
        return tuple(result)

    @staticmethod
    def _window_instance(window: TargetWindowContract) -> WindowInstanceToken:
        try:
            return WindowInstanceToken(
                handle=window.handle,
                process_id=window.process_id,
                thread_id=window.thread_id,
                window_class=window.window_class,
                rect=window.rect,
                minimized=window.phase is TargetWindowPhase.MINIMIZED,
                process_lifecycle_token=window.process_lifecycle_token,
            )
        except (TypeError, ValueError) as error:
            raise SmartReconnectPreparationError(
                "window instance is incomplete"
            ) from error

    @staticmethod
    def _normalized_path(path: Path) -> Path:
        return Path(
            os.path.normcase(
                os.path.abspath(os.fspath(Path(path).resolve(strict=False)))
            )
        )


__all__ = [
    "SmartReconnectPreparationError",
    "SmartReconnectPreparationService",
]
