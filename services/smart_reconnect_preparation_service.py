"""Prepare one complete reconnect authorization inside one source generation."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Mapping

from adapters.windows_shortcut_seal import ShortcutSealResolver
from config.config_manager import ConfigManager
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectSourceIdentity,
    ShortcutSeal,
)
from core.target_window_contract import ActualWindowSnapshot
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)
from services.smart_reconnect_authorization_coordinator import (
    ReconnectPreparationToken,
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_target_identity_service import (
    SmartReconnectTargetIdentity,
    SmartReconnectTargetIdentitySourceSnapshot,
    SmartReconnectTargetIdentityService,
)
from services.target_window_contract_service import TargetWindowContractService


_ACTUAL_WINDOW_SCOPE_ID = "smart-reconnect-actual-windows"
_ACTUAL_WINDOW_SCOPE_NAME = "實際存在視窗"


class SmartReconnectPreparationError(RuntimeError):
    """A complete same-generation authorization could not be prepared."""


class SmartReconnectPreparationService:
    """Snapshot briefly, verify externally, then publish under the lock order."""

    def __init__(
        self,
        *,
        target_identity_service: SmartReconnectTargetIdentityService,
        target_window_contract_service: TargetWindowContractService,
        shortcut_seal_resolver: ShortcutSealResolver,
        authorization_coordinator: SmartReconnectAuthorizationCoordinator,
        identity_coordinator: IdentityDataTransactionCoordinator,
        config: ConfigManager,
        product_launch_mode: ReconnectLaunchMode,
    ) -> None:
        if not isinstance(identity_coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("identity_coordinator must be IdentityDataTransactionCoordinator")
        if target_identity_service.coordinator is not identity_coordinator:
            raise ValueError("target identity service must share the identity coordinator")
        if not callable(
            getattr(target_window_contract_service, "actual_snapshot", None)
        ):
            raise TypeError(
                "target_window_contract_service must provide actual_snapshot"
            )
        if not callable(getattr(shortcut_seal_resolver, "resolve", None)):
            raise TypeError("shortcut_seal_resolver must provide resolve")
        if not isinstance(authorization_coordinator, SmartReconnectAuthorizationCoordinator):
            raise TypeError(
                "authorization_coordinator must be SmartReconnectAuthorizationCoordinator"
            )
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager")
        if product_launch_mode is not ReconnectLaunchMode.IDENTITY_BOUND:
            raise ValueError("product launch mode must remain identity-bound")
        self._target_identity = target_identity_service
        self._target_windows = target_window_contract_service
        self._shortcut_seals = shortcut_seal_resolver
        self._authorization = authorization_coordinator
        self._identity = identity_coordinator
        self._config = config
        self._product_launch_mode = product_launch_mode
        self._prepare_lock = Lock()

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
        retained_targets: tuple[ReconnectAuthorizationTarget, ...] = (),
    ) -> ReconnectAuthorizationBatch:
        """Publish only if short pre/post snapshots prove the same source."""
        if not isinstance(launch_mode, ReconnectLaunchMode):
            raise TypeError("launch_mode must be explicitly supplied")
        with self._prepare_lock:
            return self._prepare_serialized(
                launch_mode,
                tuple(retained_targets),
            )

    def _prepare_serialized(
        self,
        launch_mode: ReconnectLaunchMode,
        retained_targets: tuple[ReconnectAuthorizationTarget, ...],
    ) -> ReconnectAuthorizationBatch:
        preparation_token = self._authorization.begin_reprepare()
        try:
            (
                identity_generation,
                config_revision,
                identity_source,
            ) = self._capture_source()
            window_snapshot = self._target_windows.actual_snapshot()
            if window_snapshot.failure_codes:
                raise SmartReconnectPreparationError(
                    "actual game windows cannot be enumerated safely"
                )
            fingerprints = tuple(
                target.fingerprint for target in window_snapshot.targets
            )
            identities = (
                self._target_identity
                .targets_for_fingerprints_from_source_snapshot(
                    fingerprints,
                    identity_source,
                )
            )
            seals = self._resolve_seals(identities)
            targets = self._build_authorization_targets(
                identities,
                window_snapshot,
                seals,
            )
            targets = self._retain_absent_pending_targets(
                targets,
                retained_targets,
                window_snapshot,
                identity_source,
            )
            targets = self._without_conflicting_targets(targets)
            source = ReconnectSourceIdentity(
                identity_generation=identity_generation,
                config_revision=config_revision,
                group_id=_ACTUAL_WINDOW_SCOPE_ID,
                group_name=_ACTUAL_WINDOW_SCOPE_NAME,
                character_ids=tuple(
                    target.character_id
                    for target in targets
                    if target.character_id is not None
                ),
            )
            isolated = window_snapshot.blocked_fingerprints | (
                frozenset(fingerprints)
                - frozenset(target.fingerprint for target in targets)
            )
            newly_isolated = (
                frozenset(fingerprints)
                - frozenset(target.fingerprint for target in targets)
            )
            return self._publish_if_source_current(
                preparation_token=preparation_token,
                expected_identity_generation=identity_generation,
                expected_config_revision=config_revision,
                source=source,
                launch_mode=launch_mode,
                targets=targets,
                isolated=isolated,
                isolated_window_count=(
                    window_snapshot.isolated_window_count
                    + len(newly_isolated)
                ),
                anonymous_isolated_window_count=(
                    window_snapshot.anonymous_isolated_window_count
                ),
            )
        except Exception as error:
            self._authorization.fail_preparation(preparation_token)
            if isinstance(error, SmartReconnectPreparationError):
                raise
            raise SmartReconnectPreparationError(
                "smart reconnect authorization preparation failed"
            ) from error

    def _capture_source(
        self,
    ) -> tuple[
        int,
        int,
        SmartReconnectTargetIdentitySourceSnapshot,
    ]:
        with self._config.resource_guard():
            config_revision = self._config.snapshot_state_locked().revision

            identity_snapshot = self._identity.capture_snapshot(
                self._target_identity.capture_source_snapshot_in_current
            )
        return (
            identity_snapshot.generation,
            config_revision,
            identity_snapshot.value,
        )

    def _publish_if_source_current(
        self,
        *,
        preparation_token: ReconnectPreparationToken,
        expected_identity_generation: int,
        expected_config_revision: int,
        source: ReconnectSourceIdentity,
        launch_mode: ReconnectLaunchMode,
        targets: tuple[ReconnectAuthorizationTarget, ...],
        isolated: frozenset[str],
        isolated_window_count: int,
        anonymous_isolated_window_count: int,
    ) -> ReconnectAuthorizationBatch:
        with self._config.resource_guard():
            current_config_revision = (
                self._config.snapshot_state_locked().revision
            )

            def publish(current_identity_generation: int):
                if (
                    current_config_revision != expected_config_revision
                    or current_identity_generation
                    != expected_identity_generation
                ):
                    raise SmartReconnectPreparationError(
                        "identity or configuration changed during preparation"
                    )
                return self._authorization.publish_if_current(
                    preparation_token,
                    source,
                    launch_mode,
                    targets,
                    isolated,
                    isolated_window_count,
                    anonymous_isolated_window_count,
                )

            return self._identity.snapshot_with_generation(publish)

    def _resolve_seals(
        self,
        identities: tuple[SmartReconnectTargetIdentity, ...],
    ) -> dict[str, ShortcutSeal]:
        seals: dict[str, ShortcutSeal] = {}
        for identity in identities:
            if identity.shortcut_path is None:
                continue
            path = self._normalized_path(identity.shortcut_path)
            try:
                raw = self._shortcut_seals.resolve((path,))
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if not isinstance(raw, Mapping) or set(raw) != {path}:
                continue
            seal = raw.get(path)
            if (
                not isinstance(seal, ShortcutSeal)
                or seal.launch_fingerprint != identity.fingerprint
                or self._normalized_path(
                    seal.file_identity.normalized_path
                )
                != path
            ):
                continue
            seals[identity.fingerprint] = seal
        return seals

    @staticmethod
    def _build_authorization_targets(
        identities: tuple[SmartReconnectTargetIdentity, ...],
        window_snapshot: ActualWindowSnapshot,
        seals: Mapping[str, ShortcutSeal],
    ) -> tuple[ReconnectAuthorizationTarget, ...]:
        if not isinstance(window_snapshot, ActualWindowSnapshot):
            raise SmartReconnectPreparationError("actual window snapshot is invalid")
        identity_by_fingerprint = {
            identity.fingerprint: identity for identity in identities
        }
        result: list[ReconnectAuthorizationTarget] = []
        for window in window_snapshot.targets:
            identity = identity_by_fingerprint.get(window.fingerprint)
            seal = seals.get(window.fingerprint)
            if (
                identity is None
                or seal is None
            ):
                continue
            try:
                target = ReconnectAuthorizationTarget(
                    fingerprint=identity.fingerprint,
                    instance=window.instance,
                    character_id=identity.character_id,
                    role_aliases=identity.role_aliases,
                    importance=identity.importance,
                    original_slot_index=identity.original_slot_index,
                    original_line_number=identity.original_line_number,
                    shortcut_seal=seal,
                )
            except (TypeError, ValueError):
                continue
            result.append(target)
        return tuple(result)

    def _retain_absent_pending_targets(
        self,
        current: tuple[ReconnectAuthorizationTarget, ...],
        retained: tuple[ReconnectAuthorizationTarget, ...],
        window_snapshot: ActualWindowSnapshot,
        identity_source: SmartReconnectTargetIdentitySourceSnapshot,
    ) -> tuple[ReconnectAuthorizationTarget, ...]:
        actual = frozenset(target.fingerprint for target in window_snapshot.targets)
        blocked = window_snapshot.blocked_fingerprints
        result = list(current)
        seen = set(actual)
        for target in retained:
            if (
                not isinstance(target, ReconnectAuthorizationTarget)
                or target.fingerprint in seen
                or target.fingerprint in blocked
                or not self._target_identity
                .retained_target_is_current_from_source_snapshot(
                    target,
                    identity_source,
                )
            ):
                continue
            try:
                seal_is_current = (
                    self._shortcut_seals.revalidate(target.shortcut_seal)
                    is True
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                seal_is_current = False
            if not seal_is_current:
                continue
            result.append(target)
            seen.add(target.fingerprint)
        return tuple(result)

    @staticmethod
    def _without_conflicting_targets(
        targets: tuple[ReconnectAuthorizationTarget, ...],
    ) -> tuple[ReconnectAuthorizationTarget, ...]:
        character_counts = Counter(target.character_id for target in targets)
        path_counts = Counter(
            target.shortcut_seal.file_identity.normalized_path
            for target in targets
            if target.shortcut_seal is not None
        )
        file_counts = Counter(
            target.shortcut_seal.file_identity.stable_key
            for target in targets
            if target.shortcut_seal is not None
        )
        return tuple(
            target
            for target in targets
            if target.shortcut_seal is not None
            and character_counts[target.character_id] == 1
            and path_counts[
                target.shortcut_seal.file_identity.normalized_path
            ]
            == 1
            and file_counts[target.shortcut_seal.file_identity.stable_key]
            == 1
        )

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
