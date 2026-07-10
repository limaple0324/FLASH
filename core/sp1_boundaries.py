"""Stable SP1 contracts for recovery, reconnect, and external integrations.

This module defines boundaries only. Concrete game-specific behavior belongs in
adapters or plugins so the SP1 foundation remains testable and replaceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable


class RecoveryState(str, Enum):
    """Observable recovery lifecycle states."""

    IDLE = "idle"
    DETECTING = "detecting"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    FAILED = "failed"


class ReconnectState(str, Enum):
    """Observable smart reconnect lifecycle states."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    RECONNECTED = "reconnected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Common result returned by SP1 boundary operations."""

    success: bool
    code: str
    message: str = ""
    details: Mapping[str, object] | None = None


@runtime_checkable
class RecoveryBoundary(Protocol):
    """Contract for restoring FLASH to a known safe state."""

    @property
    def state(self) -> RecoveryState:
        """Return the current recovery state."""

    def detect(self) -> OperationResult:
        """Detect whether recovery is required without changing state."""

    def recover(self) -> OperationResult:
        """Attempt recovery and return a structured result."""


@runtime_checkable
class SmartReconnectBoundary(Protocol):
    """Contract for detecting connection loss and reconnecting safely."""

    @property
    def state(self) -> ReconnectState:
        """Return the current reconnect state."""

    def check_connection(self) -> OperationResult:
        """Check current connection health without forcing reconnection."""

    def reconnect(self) -> OperationResult:
        """Attempt reconnection and return a structured result."""


@runtime_checkable
class ExternalAdapter(Protocol):
    """Minimal API boundary for game-, window-, or platform-specific adapters."""

    @property
    def name(self) -> str:
        """Return a stable adapter name for diagnostics."""

    def health_check(self) -> OperationResult:
        """Verify that the adapter can be used safely."""

    def shutdown(self) -> None:
        """Release adapter resources without raising on repeated calls."""
