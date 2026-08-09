"""Serialize publication, revocation, and use of reconnect authorization."""

from __future__ import annotations

import threading
from enum import Enum
from typing import Callable, TypeVar
from uuid import uuid4

from core.smart_reconnect_authorization import (
    ReconnectAuthorizationBatch,
    ReconnectAuthorizationTarget,
    ReconnectLaunchMode,
    ReconnectRevocationReason,
    ReconnectSourceIdentity,
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
        self._last_revocation_reason: ReconnectRevocationReason | None = None

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

    def begin_reprepare(self) -> None:
        with self._lock:
            self._require_not_stopped()
            self._current = None
            self._state = ReconnectAuthorizationState.REBINDING
            self._last_revocation_reason = ReconnectRevocationReason.REPREPARE

    def publish(
        self,
        source: ReconnectSourceIdentity,
        launch_mode: ReconnectLaunchMode,
        targets: tuple[ReconnectAuthorizationTarget, ...],
    ) -> ReconnectAuthorizationBatch:
        """Replace authorization atomically; invalid input leaves zero authorization."""
        with self._lock:
            self._require_not_stopped()
            self._current = None
            self._state = ReconnectAuthorizationState.REBINDING
            self._last_revocation_reason = ReconnectRevocationReason.REPREPARE
            try:
                batch = ReconnectAuthorizationBatch(
                    epoch=self._epoch + 1,
                    batch_id=uuid4().hex,
                    source=source,
                    launch_mode=launch_mode,
                    targets=tuple(targets),
                )
            except BaseException:
                self._state = ReconnectAuthorizationState.EMPTY
                self._last_revocation_reason = (
                    ReconnectRevocationReason.PREPARATION_FAILED
                )
                raise
            self._epoch = batch.epoch
            self._current = batch
            self._state = ReconnectAuthorizationState.ACTIVE
            self._last_revocation_reason = None
            return batch

    def revoke(self, reason: ReconnectRevocationReason) -> None:
        if not isinstance(reason, ReconnectRevocationReason):
            raise TypeError("reason must be ReconnectRevocationReason")
        with self._lock:
            if self._state is ReconnectAuthorizationState.STOPPED:
                return
            self._current = None
            self._state = ReconnectAuthorizationState.EMPTY
            self._last_revocation_reason = reason

    def fail_preparation(self) -> None:
        self.revoke(ReconnectRevocationReason.PREPARATION_FAILED)

    def stop(self) -> None:
        with self._lock:
            self._current = None
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
                epoch != batch.epoch
                or batch_id != batch.batch_id
                or source_generation != batch.source.source_generation
            ):
                raise ReconnectAuthorizationMismatchError(
                    "authorization epoch or source generation changed"
                )
            target = batch.target_for(fingerprint)
            if (
                target is None
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
    "SmartReconnectAuthorizationCoordinator",
]
