"""Prepare one complete reconnect authorization inside one source generation."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping

from adapters.windows_shortcut_seal import ShortcutSealResolver
from adapters.windows_smart_reconnect_observation_broker import (
    SmartReconnectObservationSnapshot,
    WindowsSmartReconnectObservationBroker,
)
from config.config_manager import ConfigManager
from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectSourceIdentity,
    ShortcutSeal,
)
from core.target_window_contract import (
    ActualWindowSnapshot,
    ObservationActionLease,
    ObservationFreshness,
)
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)
from services.smart_reconnect_authorization_coordinator import (
    ReconnectPreparationToken,
    SmartReconnectAuthorizationCoordinator,
)
from services.smart_reconnect_target_identity_service import (
    complete_reconnect_role_alias,
    SmartReconnectIdentityEvidence,
    SmartReconnectIdentityEvidenceResult,
    SmartReconnectPendingIdentityCandidate,
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
        role_identity_reader: Callable[[int], object] | None = None,
        observation_broker: (
            WindowsSmartReconnectObservationBroker | None
        ) = None,
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
        if role_identity_reader is not None and not callable(role_identity_reader):
            raise TypeError("role_identity_reader must be callable or None")
        self._target_identity = target_identity_service
        self._target_windows = target_window_contract_service
        self._shortcut_seals = shortcut_seal_resolver
        self._authorization = authorization_coordinator
        self._identity = identity_coordinator
        self._config = config
        self._product_launch_mode = product_launch_mode
        self._role_identity_reader = role_identity_reader
        self._observation_broker = observation_broker
        self._prepare_lock = Lock()
        self._last_static_generation: int | None = None
        self._last_identity_generation: int | None = None
        self._last_config_revision: int | None = None
        self._last_requires_confirmation = True

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
        preparation_token = (
            self._authorization.begin_reprepare()
            if self._observation_broker is None
            else None
        )
        authorization_state = self._authorization.state
        authorization_epoch = self._authorization.epoch
        authorization_batch = self._authorization.current_authorization()
        try:
            (
                identity_generation,
                config_revision,
                identity_source,
            ) = self._capture_source()
            self._notify_observation_source(
                identity_generation,
                config_revision,
            )
            window_snapshot = self._target_windows.actual_snapshot()
            observation_snapshot = self._matching_observation_snapshot(
                window_snapshot
            )
            if window_snapshot.failure_codes:
                raise SmartReconnectPreparationError(
                    "actual game windows cannot be enumerated safely"
                )
            current_batch = self._authorization.current_authorization()
            if (
                current_batch is not None
                and window_snapshot.observation_static_generation > 0
                and not window_snapshot.changed_fingerprints
                and self._last_static_generation
                == window_snapshot.observation_static_generation
                and self._last_identity_generation == identity_generation
                and self._last_config_revision == config_revision
                and not self._last_requires_confirmation
                and current_batch.launch_mode is launch_mode
                and current_batch.source.identity_generation
                == identity_generation
                and current_batch.source.config_revision == config_revision
            ):
                return current_batch
            fingerprints = tuple(
                target.fingerprint for target in window_snapshot.targets
            )
            resolution = (
                self._target_identity
                .resolve_for_fingerprints_from_source_snapshot(
                    fingerprints,
                    identity_source,
                    observation_snapshot=observation_snapshot,
                )
            )
            evidence_candidates = tuple(
                dict.fromkeys(
                    (
                        *resolution.pending_candidates,
                        *resolution.verification_candidates,
                    )
                )
            )
            verified_fingerprints: frozenset[str] = frozenset()
            observations = self._identity_evidence(
                evidence_candidates,
                window_snapshot,
                identity_source,
                observation_snapshot=observation_snapshot,
            )
            if observation_snapshot is None:
                observed_fingerprints = frozenset(
                    candidate.fingerprint
                    for candidate in evidence_candidates
                )
            else:
                observed_fingerprints = frozenset(
                    candidate.fingerprint
                    for candidate in evidence_candidates
                    if (
                        (window := observation_snapshot.window_for(
                            candidate.fingerprint
                        ))
                        is not None
                        and window.capture_route == "visible"
                        and window.fresh_capture is True
                        and window.freshness
                        is ObservationFreshness.PROVEN_CURRENT
                        and window.sample is not None
                        and window.sample.api_succeeded is True
                    )
                )
            persist_candidates = tuple(
                candidate
                for candidate in evidence_candidates
                if candidate.fingerprint in observed_fingerprints
            )
            if persist_candidates:
                evidence_result = self._persist_identity_evidence(
                    persist_candidates,
                    observations,
                    expected_identity_generation=identity_generation,
                    expected_config_revision=config_revision,
                    expected_source=identity_source,
                    expected_observation_generation=(
                        observation_snapshot.generation
                        if observation_snapshot is not None
                        else None
                    ),
                    expected_action_lease=window_snapshot.action_lease,
                )
                verified_fingerprints = (
                    evidence_result.confirmed_fingerprints
                )
                (
                    identity_generation,
                    refreshed_config_revision,
                    identity_source,
                ) = self._capture_source()
                self._notify_observation_source(
                    identity_generation,
                    refreshed_config_revision,
                )
                if refreshed_config_revision != config_revision:
                    raise SmartReconnectPreparationError(
                        "configuration changed during identity enrollment"
                    )
                window_snapshot = self._target_windows.actual_snapshot()
                observation_snapshot = self._matching_observation_snapshot(
                    window_snapshot
                )
                if window_snapshot.failure_codes:
                    raise SmartReconnectPreparationError(
                        "actual game windows cannot be enumerated safely"
                    )
                fingerprints = tuple(
                    target.fingerprint for target in window_snapshot.targets
                )
                resolution = (
                    self._target_identity
                    .resolve_for_fingerprints_from_source_snapshot(
                        fingerprints,
                        identity_source,
                        observation_snapshot=observation_snapshot,
                    )
                )
            verification_fingerprints = frozenset(
                candidate.fingerprint
                for candidate in resolution.verification_candidates
            )
            identities = tuple(
                identity
                for identity in resolution.targets
                if identity.fingerprint not in verification_fingerprints
                or identity.fingerprint in verified_fingerprints
            )
            seals = self._resolve_seals(
                identities,
                observation_snapshot=observation_snapshot,
            )
            targets = self._build_authorization_targets(
                identities,
                window_snapshot,
                seals,
                identity_source,
                config_revision,
                verified_fingerprints,
            )
            targets = self._retain_absent_pending_targets(
                targets,
                retained_targets,
                window_snapshot,
                identity_source,
                config_revision,
                observation_snapshot=observation_snapshot,
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
            batch = self._publish_if_source_current(
                preparation_token=preparation_token,
                expected_authorization_state=authorization_state,
                expected_authorization_epoch=authorization_epoch,
                expected_authorization_batch=authorization_batch,
                expected_identity_generation=identity_generation,
                expected_config_revision=config_revision,
                expected_identity_source=identity_source,
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
                expected_observation_generation=(
                    observation_snapshot.generation
                    if observation_snapshot is not None
                    else None
                ),
                expected_action_lease=window_snapshot.action_lease,
            )
            self._last_static_generation = (
                window_snapshot.observation_static_generation
            )
            self._last_identity_generation = identity_generation
            self._last_config_revision = config_revision
            self._last_requires_confirmation = bool(
                frozenset(
                    candidate.fingerprint for candidate in evidence_candidates
                )
                - verified_fingerprints
            )
            return batch
        except Exception as error:
            if preparation_token is not None:
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

    def _notify_observation_source(
        self,
        identity_generation: int,
        config_revision: int,
    ) -> None:
        broker = self._observation_broker
        notifier = (
            getattr(broker, "set_identity_source", None)
            if broker is not None
            else None
        )
        if callable(notifier):
            notifier(identity_generation, config_revision)

    def _matching_observation_snapshot(
        self,
        window_snapshot: ActualWindowSnapshot,
    ) -> SmartReconnectObservationSnapshot | None:
        broker = self._observation_broker
        if broker is None:
            return None
        action_reader = getattr(broker, "action_snapshot", None)
        action = action_reader() if callable(action_reader) else None
        if action is not None:
            observation, action_lease = action
        else:
            observation = broker.current_snapshot()
            action_lease = None
        if (
            observation is None
            or
            observation.generation <= 0
            or observation.generation
            != window_snapshot.observation_generation
            or observation.failure_codes
            or (
                window_snapshot.action_lease is not None
                and action_lease is not window_snapshot.action_lease
            )
        ):
            raise SmartReconnectPreparationError(
                "window and observation generations disagree"
            )
        return observation

    def _identity_evidence(
        self,
        candidates: tuple[SmartReconnectPendingIdentityCandidate, ...],
        window_snapshot: ActualWindowSnapshot,
        identity_source: SmartReconnectTargetIdentitySourceSnapshot,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> tuple[SmartReconnectIdentityEvidence, ...]:
        if observation_snapshot is not None:
            return self._identity_evidence_from_observation(
                candidates,
                observation_snapshot,
            )
        reader = self._role_identity_reader
        if reader is None or not candidates:
            return ()
        windows = {
            target.fingerprint: target
            for target in window_snapshot.targets
        }
        paths = tuple(
            dict.fromkeys(candidate.shortcut_path for candidate in candidates)
        )
        try:
            raw_seals = self._shortcut_seals.resolve(paths)
        except (OSError, RuntimeError, TypeError, ValueError):
            return ()
        if not isinstance(raw_seals, Mapping):
            return ()
        evidence: list[SmartReconnectIdentityEvidence] = []
        for candidate in candidates:
            window = windows.get(candidate.fingerprint)
            seal = raw_seals.get(candidate.shortcut_path)
            if (
                window is None
                or not isinstance(seal, ShortcutSeal)
                or seal.launch_fingerprint != candidate.fingerprint
                or self._normalized_path(
                    seal.file_identity.normalized_path
                )
                != self._normalized_path(candidate.shortcut_path)
            ):
                continue
            try:
                result = reader(window.instance.handle)
            except Exception:
                continue
            role_id = getattr(result, "role_id", None)
            alias = complete_reconnect_role_alias(role_id)
            if (
                getattr(result, "success", None) is not True
                or not isinstance(role_id, str)
                or alias is None
                or any(character.isspace() for character in role_id)
                or "..." in role_id
                or "…" in role_id
            ):
                continue
            evidence.append(
                SmartReconnectIdentityEvidence(
                    candidate=candidate,
                    instance=window.instance,
                    shortcut_seal=seal,
                    role_alias=alias,
                )
            )
        final_snapshot = self._target_windows.actual_snapshot()
        if final_snapshot.failure_codes:
            raise SmartReconnectPreparationError(
                "actual game windows changed during identity observation"
            )
        final_windows = {
            target.fingerprint: target
            for target in final_snapshot.targets
        }
        final_resolution = (
            self._target_identity.resolve_for_fingerprints_from_source_snapshot(
                tuple(item.candidate.fingerprint for item in evidence),
                identity_source,
            )
        )
        final_candidates = {
            item.fingerprint: item
            for item in (
                *final_resolution.pending_candidates,
                *final_resolution.verification_candidates,
            )
        }
        accepted: list[SmartReconnectIdentityEvidence] = []
        for item in evidence:
            current_window = final_windows.get(item.candidate.fingerprint)
            current_candidate = final_candidates.get(
                item.candidate.fingerprint
            )
            if (
                current_window is None
                or current_window.instance != item.instance
                or current_candidate != item.candidate
            ):
                continue
            try:
                seal_is_current = (
                    self._shortcut_seals.revalidate(item.shortcut_seal)
                    is True
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                seal_is_current = False
            if seal_is_current:
                accepted.append(item)
        return tuple(accepted)

    def _identity_evidence_from_observation(
        self,
        candidates: tuple[SmartReconnectPendingIdentityCandidate, ...],
        observation: SmartReconnectObservationSnapshot,
    ) -> tuple[SmartReconnectIdentityEvidence, ...]:
        evidence: list[SmartReconnectIdentityEvidence] = []
        for candidate in candidates:
            window = observation.window_for(candidate.fingerprint)
            shortcut = observation.shortcut_for(candidate.fingerprint)
            role_id = window.role_id if window is not None else None
            alias = complete_reconnect_role_alias(role_id)
            if (
                window is None
                or window.instance is None
                or window.capture_route != "visible"
                or window.fresh_capture is not True
                or window.freshness
                is not ObservationFreshness.PROVEN_CURRENT
                or window.sample is None
                or window.sample.api_succeeded is not True
                or shortcut is None
                or shortcut.seal is None
                or shortcut.failure_codes
                or shortcut.seal.launch_fingerprint != candidate.fingerprint
                or self._normalized_path(shortcut.path)
                != self._normalized_path(candidate.shortcut_path)
                or not isinstance(role_id, str)
                or alias is None
                or any(character.isspace() for character in role_id)
                or "..." in role_id
                or "\u2026" in role_id
            ):
                continue
            try:
                evidence.append(
                    SmartReconnectIdentityEvidence(
                        candidate=candidate,
                        instance=window.instance,
                        shortcut_seal=shortcut.seal,
                        role_alias=alias,
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(evidence)

    def _persist_identity_evidence(
        self,
        candidates: tuple[SmartReconnectPendingIdentityCandidate, ...],
        observations: tuple[SmartReconnectIdentityEvidence, ...],
        *,
        expected_identity_generation: int,
        expected_config_revision: int,
        expected_source: SmartReconnectTargetIdentitySourceSnapshot,
        expected_observation_generation: int | None,
        expected_action_lease: ObservationActionLease | None,
    ) -> SmartReconnectIdentityEvidenceResult:
        def persist_current_evidence():
            with self._config.resource_guard():
                if (
                    self._config.snapshot_state_locked().revision
                    != expected_config_revision
                ):
                    raise SmartReconnectPreparationError(
                        "configuration changed during identity enrollment"
                    )
                result = self._target_identity.record_identity_evidence(
                    candidates,
                    observations,
                    expected_generation=expected_identity_generation,
                    expected_config_revision=expected_config_revision,
                    expected_source=expected_source,
                )
                if not result.source_current:
                    raise SmartReconnectPreparationError(
                        "identity source changed during identity enrollment"
                    )
                return result

        if expected_observation_generation is None:
            return persist_current_evidence()
        broker = self._observation_broker
        if broker is None:
            raise SmartReconnectPreparationError(
                "observation broker became unavailable"
            )
        current, result = self._run_if_observation_current(
            broker,
            expected_observation_generation,
            expected_action_lease,
            persist_current_evidence,
        )
        if not current or result is None:
            raise SmartReconnectPreparationError(
                "observation changed before identity enrollment"
            )
        return result

    def _publish_if_source_current(
        self,
        *,
        preparation_token: ReconnectPreparationToken | None,
        expected_authorization_state: object,
        expected_authorization_epoch: int,
        expected_authorization_batch: ReconnectAuthorizationBatch | None,
        expected_identity_generation: int,
        expected_config_revision: int,
        expected_identity_source: SmartReconnectTargetIdentitySourceSnapshot,
        source: ReconnectSourceIdentity,
        launch_mode: ReconnectLaunchMode,
        targets: tuple[ReconnectAuthorizationTarget, ...],
        isolated: frozenset[str],
        isolated_window_count: int,
        anonymous_isolated_window_count: int,
        expected_observation_generation: int | None = None,
        expected_action_lease: ObservationActionLease | None = None,
    ) -> ReconnectAuthorizationBatch:
        def publish_current_source():
            with self._config.resource_guard():
                current_config_revision = (
                    self._config.snapshot_state_locked().revision
                )

                def publish(current_identity_generation: int):
                    current_identity_source = (
                        self._target_identity
                        .capture_source_snapshot_in_current()
                    )
                    if (
                        current_config_revision != expected_config_revision
                        or current_identity_generation
                        != expected_identity_generation
                        or current_identity_source.saved_targets
                        != expected_identity_source.saved_targets
                        or current_identity_source.state_writable
                        != expected_identity_source.state_writable
                    ):
                        raise SmartReconnectPreparationError(
                            "identity or configuration changed during preparation"
                        )
                    current_token = preparation_token
                    if current_token is None:
                        if (
                            self._authorization.state
                            is not expected_authorization_state
                            or self._authorization.epoch
                            != expected_authorization_epoch
                            or self._authorization.current_authorization()
                            is not expected_authorization_batch
                        ):
                            raise SmartReconnectPreparationError(
                                "authorization changed during preparation"
                            )
                        current_token = (
                            self._authorization.begin_reprepare()
                        )
                    return self._authorization.publish_if_current(
                        current_token,
                        source,
                        launch_mode,
                        targets,
                        isolated,
                        isolated_window_count,
                        anonymous_isolated_window_count,
                    )

                return self._identity.snapshot_with_generation(publish)

        if expected_observation_generation is None:
            return publish_current_source()
        broker = self._observation_broker
        if broker is None:
            raise SmartReconnectPreparationError(
                "observation broker became unavailable"
            )
        current, batch = self._run_if_observation_current(
            broker,
            expected_observation_generation,
            expected_action_lease,
            publish_current_source,
        )
        if not current or batch is None:
            raise SmartReconnectPreparationError(
                "observation changed during authorization publish"
            )
        return batch

    @staticmethod
    def _run_if_observation_current(
        broker: WindowsSmartReconnectObservationBroker,
        generation: int,
        lease: ObservationActionLease | None,
        callback,
    ):
        action_runner = getattr(broker, "run_if_action_current", None)
        if isinstance(lease, ObservationActionLease) and callable(action_runner):
            return action_runner(lease, callback)
        return broker.run_if_generation_current(generation, callback)

    def _resolve_seals(
        self,
        identities: tuple[SmartReconnectTargetIdentity, ...],
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> dict[str, ShortcutSeal]:
        if observation_snapshot is not None:
            identities_by_fingerprint = {
                identity.fingerprint: identity for identity in identities
            }
            return {
                item.fingerprint: item.seal
                for item in observation_snapshot.shortcuts
                if item.fingerprint in identities_by_fingerprint
                and item.seal is not None
                and not item.failure_codes
                and identities_by_fingerprint[item.fingerprint].shortcut_path
                is not None
                and self._normalized_path(item.path)
                == self._normalized_path(
                    identities_by_fingerprint[item.fingerprint].shortcut_path
                )
            }
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
        identity_source: SmartReconnectTargetIdentitySourceSnapshot,
        config_revision: int,
        verified_fingerprints: frozenset[str],
    ) -> tuple[ReconnectAuthorizationTarget, ...]:
        if not isinstance(window_snapshot, ActualWindowSnapshot):
            raise SmartReconnectPreparationError("actual window snapshot is invalid")
        identity_by_fingerprint = {
            identity.fingerprint: identity for identity in identities
        }
        saved_by_fingerprint = identity_source.saved_by_fingerprint()
        result: list[ReconnectAuthorizationTarget] = []
        for window in window_snapshot.targets:
            identity = identity_by_fingerprint.get(window.fingerprint)
            seal = seals.get(window.fingerprint)
            saved = saved_by_fingerprint.get(window.fingerprint)
            if (
                identity is None
                or seal is None
            ):
                continue
            if saved is not None and saved.has_complete_evidence:
                if (
                    not saved.is_confirmed
                    or window.fingerprint not in verified_fingerprints
                    or saved.instance != window.instance
                    or saved.shortcut_seal != seal
                    or saved.shortcut_path != identity.shortcut_path
                    or saved.config_revision != config_revision
                    or saved.evidence_alias not in identity.role_aliases
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
        config_revision: int,
        *,
        observation_snapshot: SmartReconnectObservationSnapshot | None = None,
    ) -> tuple[ReconnectAuthorizationTarget, ...]:
        actual = frozenset(target.fingerprint for target in window_snapshot.targets)
        blocked = window_snapshot.blocked_fingerprints
        result = list(current)
        seen = set(actual)
        saved_by_fingerprint = identity_source.saved_by_fingerprint()
        for target in retained:
            saved = saved_by_fingerprint.get(
                getattr(target, "fingerprint", "")
            )
            if (
                not isinstance(target, ReconnectAuthorizationTarget)
                or target.fingerprint in seen
                or target.fingerprint in blocked
                or (
                    saved is not None
                    and saved.has_complete_evidence
                    and (
                        not saved.is_confirmed
                        or saved.config_revision != config_revision
                    )
                )
                or not self._target_identity
                .retained_target_is_current_from_source_snapshot(
                    target,
                    identity_source,
                    observation_snapshot=observation_snapshot,
                )
            ):
                continue
            if observation_snapshot is not None:
                shortcut = observation_snapshot.shortcut_for(
                    target.fingerprint
                )
                seal_is_current = bool(
                    shortcut is not None
                    and shortcut.seal is not None
                    and not shortcut.failure_codes
                    and shortcut.seal == target.shortcut_seal
                )
            else:
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
                os.path.abspath(os.fspath(Path(path)))
            )
        )


__all__ = [
    "SmartReconnectPreparationError",
    "SmartReconnectPreparationService",
]
