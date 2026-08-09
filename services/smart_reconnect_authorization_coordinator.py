"""Serialize publication, revocation, and use of reconnect authorization."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, TypeVar
from uuid import uuid4

from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectRevocationReason,
    ReconnectSourceIdentity,
    ShortcutSeal,
)
from core.window_instance import WindowInstanceToken


_RESULT = TypeVar("_RESULT")


class ReconnectAuthorizationState(str, Enum):
    EMPTY = "empty"
    REBINDING = "rebinding"
    ACTIVE = "active"
    STOPPED = "stopped"


class ReconnectAuthorizationError(RuntimeError):
    """Base error for reconnect authorization coordination."""


class ReconnectAuthorizationUnavailableError(ReconnectAuthorizationError):
    """Raised when no current immutable authorization may be used."""


class ReconnectAuthorizationMismatchError(ReconnectAuthorizationError):
    """Raised when a caller presents stale or different source evidence."""


@dataclass(frozen=True, slots=True, eq=False)
class ReconnectPreparationToken:
    serial: int


class SmartReconnectAuthorizationCoordinator:
    """One startup-owned lock around a complete authorization epoch.

    The coordinator never returns a reusable permit. The supplied callback is
    invoked while the same lock still protects the validated authorization.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = ReconnectAuthorizationState.EMPTY
        self._epoch = 0
        self._current: ReconnectAuthorizationBatch | None = None
        self._rebinding_base: ReconnectAuthorizationBatch | None = None
        self._shortcut_seal_baselines: dict[str, ShortcutSeal] = {}
        self._target_source_generations: dict[str, int] = {}
        self._last_revocation_reason: ReconnectRevocationReason | None = None
        self._preparation_serial = 0
        self._active_preparation_token: ReconnectPreparationToken | None = None

    @property
    def state(self) -> ReconnectAuthorizationState:
        with self._lock:
            return self._state

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def last_revocation_reason(self) -> ReconnectRevocationReason | None:
        with self._lock:
            return self._last_revocation_reason

    def current_authorization(self) -> ReconnectAuthorizationBatch | None:
        with self._lock:
            if self._state is not ReconnectAuthorizationState.ACTIVE:
                return None
            return self._current

    def begin_reprepare(self) -> ReconnectPreparationToken:
        with self._lock:
            self._require_not_stopped()
            self._preparation_serial += 1
            token = ReconnectPreparationToken(self._preparation_serial)
            self._active_preparation_token = token
            if self._state is ReconnectAuthorizationState.ACTIVE:
                self._rebinding_base = self._current
            self._current = None
            self._state = ReconnectAuthorizationState.REBINDING
            self._last_revocation_reason = ReconnectRevocationReason.REPREPARE
            return token

    def publish(
        self,
        source: ReconnectSourceIdentity,
        launch_mode: ReconnectLaunchMode,
        targets: tuple[ReconnectAuthorizationTarget, ...],
        isolated_fingerprints: frozenset[str] = frozenset(),
        isolated_window_count: int | None = None,
        anonymous_isolated_window_count: int = 0,
        *,
        preparation_token: ReconnectPreparationToken | None = None,
    ) -> ReconnectAuthorizationBatch:
        """Publish one immutable collection while preserving unchanged grants."""
        with self._lock:
            self._require_not_stopped()
            if self._state is ReconnectAuthorizationState.REBINDING and (
                preparation_token is None
                or preparation_token is not self._active_preparation_token
            ):
                raise ReconnectAuthorizationMismatchError(
                    "reconnect preparation token is missing or stale"
                )
            if (
                preparation_token is not None
                and preparation_token is not self._active_preparation_token
            ):
                raise ReconnectAuthorizationMismatchError(
                    "reconnect preparation token is stale"
                )
            base = (
                self._rebinding_base
                if self._state is ReconnectAuthorizationState.REBINDING
                else self._current
            )
            self._current = None
            self._state = ReconnectAuthorizationState.REBINDING
            self._last_revocation_reason = ReconnectRevocationReason.REPREPARE
            try:
                collection_epoch = self._epoch + 1
                requested_targets = tuple(targets)
                requested_character_ids = tuple(
                    target.character_id
                    for target in requested_targets
                    if target.character_id is not None
                )
                if requested_character_ids != source.character_ids:
                    raise ValueError(
                        "authorization targets do not match source identities"
                    )
                assigned: list[ReconnectAuthorizationTarget] = []
                seal_changed: set[str] = set()
                for target in requested_targets:
                    baseline = self._shortcut_seal_baselines.get(
                        target.fingerprint
                    )
                    if (
                        baseline is not None
                        and baseline != target.shortcut_seal
                    ):
                        seal_changed.add(target.fingerprint)
                        continue
                    assigned.append(
                        self._assign_target_grant(
                            target,
                            base,
                            source,
                            collection_epoch,
                        )
                    )
                assigned_targets = tuple(assigned)
                isolated = frozenset(
                    (*isolated_fingerprints, *seal_changed)
                )
                base_isolated_count = (
                    len(isolated_fingerprints)
                    if isolated_window_count is None
                    else isolated_window_count
                )
                effective_source = replace(
                    source,
                    character_ids=tuple(
                        target.character_id
                        for target in assigned_targets
                        if target.character_id is not None
                    ),
                )
                batch = ReconnectAuthorizationBatch(
                    epoch=collection_epoch,
                    batch_id=uuid4().hex,
                    source=effective_source,
                    launch_mode=launch_mode,
                    targets=assigned_targets,
                    isolated_fingerprints=isolated,
                    isolated_window_count=(
                        base_isolated_count
                        + len(seal_changed - set(isolated_fingerprints))
                    ),
                    anonymous_isolated_window_count=(
                        anonymous_isolated_window_count
                    ),
                )
            except BaseException:
                if preparation_token is self._active_preparation_token:
                    self._active_preparation_token = None
                self._rebinding_base = None
                self._state = ReconnectAuthorizationState.EMPTY
                self._last_revocation_reason = (
                    ReconnectRevocationReason.PREPARATION_FAILED
                )
                raise
            self._epoch = batch.epoch
            self._current = batch
            for target in batch.targets:
                if target.source_generation is not None:
                    self._target_source_generations[target.fingerprint] = max(
                        target.source_generation,
                        self._target_source_generations.get(
                            target.fingerprint,
                            target.source_generation,
                        ),
                    )
                if target.shortcut_seal is not None:
                    self._shortcut_seal_baselines.setdefault(
                        target.fingerprint,
                        target.shortcut_seal,
                    )
            self._rebinding_base = None
            if preparation_token is self._active_preparation_token:
                self._active_preparation_token = None
            self._state = ReconnectAuthorizationState.ACTIVE
            self._last_revocation_reason = None
            return batch

    def publish_if_current(
        self,
        preparation_token: ReconnectPreparationToken,
        source: ReconnectSourceIdentity,
        launch_mode: ReconnectLaunchMode,
        targets: tuple[ReconnectAuthorizationTarget, ...],
        isolated_fingerprints: frozenset[str] = frozenset(),
        isolated_window_count: int | None = None,
        anonymous_isolated_window_count: int = 0,
    ) -> ReconnectAuthorizationBatch:
        return self.publish(
            source,
            launch_mode,
            targets,
            isolated_fingerprints,
            isolated_window_count,
            anonymous_isolated_window_count,
            preparation_token=preparation_token,
        )

    def _assign_target_grant(
        self,
        target: ReconnectAuthorizationTarget,
        base: ReconnectAuthorizationBatch | None,
        source: ReconnectSourceIdentity,
        collection_epoch: int,
    ) -> ReconnectAuthorizationTarget:
        previous = base.target_for(target.fingerprint) if base is not None else None
        if (
            previous is not None
            and base is not None
            and previous.has_same_authorization_evidence(target)
            and previous.authorization_epoch is not None
            and previous.authorization_id is not None
            and previous.source_generation is not None
        ):
            return replace(
                target,
                authorization_epoch=previous.authorization_epoch,
                authorization_id=previous.authorization_id,
                source_generation=previous.source_generation,
            )
        latest_generation = self._target_source_generations.get(
            target.fingerprint
        )
        if previous is not None and previous.source_generation is not None:
            latest_generation = max(
                previous.source_generation,
                (
                    latest_generation
                    if latest_generation is not None
                    else previous.source_generation
                ),
            )
        return replace(
            target,
            authorization_epoch=collection_epoch,
            authorization_id=uuid4().hex,
            source_generation=(
                source.source_generation
                if latest_generation is None
                else max(source.source_generation, latest_generation + 1)
            ),
        )

    def revoke(self, reason: ReconnectRevocationReason) -> None:
        if not isinstance(reason, ReconnectRevocationReason):
            raise TypeError("reason must be ReconnectRevocationReason")
        with self._lock:
            if self._state is ReconnectAuthorizationState.STOPPED:
                return
            self._active_preparation_token = None
            self._current = None
            self._rebinding_base = None
            if reason is ReconnectRevocationReason.EXPLICIT:
                self._shortcut_seal_baselines.clear()
            self._state = ReconnectAuthorizationState.EMPTY
            self._last_revocation_reason = reason

    def revoke_target(
        self,
        fingerprint: object,
        reason: ReconnectRevocationReason = ReconnectRevocationReason.SOURCE_CHANGED,
    ) -> bool:
        """Revoke exactly one target without invalidating unchanged siblings."""

        if not isinstance(reason, ReconnectRevocationReason):
            raise TypeError("reason must be ReconnectRevocationReason")
        with self._lock:
            if self._state is ReconnectAuthorizationState.STOPPED:
                return False
            if self._state is ReconnectAuthorizationState.REBINDING:
                self._active_preparation_token = None
                self._current = None
                self._rebinding_base = None
                self._state = ReconnectAuthorizationState.EMPTY
                self._last_revocation_reason = reason
                return False
            self._active_preparation_token = None
            batch = self._current
            if batch is None or self._state is not ReconnectAuthorizationState.ACTIVE:
                return False
            target = batch.target_for(fingerprint)
            if target is None:
                return False
            remaining = tuple(item for item in batch.targets if item is not target)
            self._epoch += 1
            self._last_revocation_reason = reason
            source = replace(
                batch.source,
                character_ids=tuple(
                    item.character_id
                    for item in remaining
                    if item.character_id is not None
                ),
            )
            self._current = ReconnectAuthorizationBatch(
                epoch=self._epoch,
                batch_id=uuid4().hex,
                source=source,
                launch_mode=batch.launch_mode,
                targets=remaining,
                isolated_fingerprints=frozenset(
                    (*batch.isolated_fingerprints, target.fingerprint)
                ),
                isolated_window_count=(
                    (batch.isolated_window_count or 0) + 1
                ),
                anonymous_isolated_window_count=(
                    batch.anonymous_isolated_window_count
                ),
            )
            self._state = ReconnectAuthorizationState.ACTIVE
            return True

    def fail_preparation(
        self,
        preparation_token: ReconnectPreparationToken | None = None,
    ) -> bool:
        if preparation_token is None:
            self.revoke(ReconnectRevocationReason.PREPARATION_FAILED)
            return True
        with self._lock:
            if (
                self._state is not ReconnectAuthorizationState.REBINDING
                or preparation_token is not self._active_preparation_token
            ):
                return False
            self._active_preparation_token = None
            self._current = None
            self._rebinding_base = None
            self._state = ReconnectAuthorizationState.EMPTY
            self._last_revocation_reason = (
                ReconnectRevocationReason.PREPARATION_FAILED
            )
            return True

    def stop(self) -> None:
        with self._lock:
            self._active_preparation_token = None
            self._current = None
            self._rebinding_base = None
            self._shortcut_seal_baselines.clear()
            self._state = ReconnectAuthorizationState.STOPPED
            self._last_revocation_reason = ReconnectRevocationReason.STOPPED

    def validate(
        self,
        *,
        epoch: int,
        batch_id: str,
        source_generation: int,
        fingerprint: object,
        character_id: str | None,
        instance: WindowInstanceToken,
        callback: Callable[[ReconnectAuthorizationTarget], _RESULT],
    ) -> _RESULT:
        """Validate and consume authority only for the duration of ``callback``."""
        return self.run_authorized(
            epoch=epoch,
            batch_id=batch_id,
            source_generation=source_generation,
            fingerprint=fingerprint,
            character_id=character_id,
            instance=instance,
            callback=callback,
        )

    def run_authorized(
        self,
        *,
        epoch: int,
        batch_id: str,
        source_generation: int,
        fingerprint: object,
        character_id: str | None,
        instance: WindowInstanceToken,
        callback: Callable[[ReconnectAuthorizationTarget], _RESULT],
    ) -> _RESULT:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            batch = self._current
            if (
                self._state is not ReconnectAuthorizationState.ACTIVE
                or batch is None
            ):
                raise ReconnectAuthorizationUnavailableError(
                    "reconnect authorization is not active"
                )
            if (
                (target := batch.target_for(fingerprint)) is None
                or target.authorization_epoch != epoch
                or target.authorization_id != batch_id
                or target.source_generation != source_generation
                or target.character_id != character_id
                or target.instance != instance
            ):
                raise ReconnectAuthorizationMismatchError(
                    "authorization target identity changed"
                )
            return callback(target)

    def _require_not_stopped(self) -> None:
        if self._state is ReconnectAuthorizationState.STOPPED:
            raise ReconnectAuthorizationUnavailableError(
                "reconnect authorization coordinator is stopped"
            )


__all__ = [
    "ReconnectAuthorizationError",
    "ReconnectAuthorizationMismatchError",
    "ReconnectAuthorizationState",
    "ReconnectAuthorizationUnavailableError",
    "ReconnectPreparationToken",
    "SmartReconnectAuthorizationCoordinator",
]
