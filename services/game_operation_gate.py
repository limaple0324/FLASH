"""Serialize game-changing operations and make group rebinding atomic."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GameOperationGateSnapshot:
    open: bool
    active_operation: str | None
    waiting_operations: int


class GameOperationLease:
    """One exclusive permission to change game state."""

    def __init__(
        self,
        gate: "GameOperationGate",
        operation: str,
    ) -> None:
        self._gate = gate
        self.operation = operation
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._gate._release(self)

    def __enter__(self) -> "GameOperationLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class GameOperationGate:
    """Allow only one mutating batch and stop new work during rebinding."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._open = True
        self._active: GameOperationLease | None = None
        self._waiting = 0

    def snapshot(self) -> GameOperationGateSnapshot:
        with self._condition:
            return GameOperationGateSnapshot(
                self._open,
                (
                    self._active.operation
                    if self._active is not None
                    else None
                ),
                self._waiting,
            )

    def acquire(
        self,
        operation: object,
        *,
        execution_guard: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
    ) -> GameOperationLease | None:
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must not be empty.")
        if (
            timeout_seconds is not None
            and (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or timeout_seconds < 0
            )
        ):
            raise ValueError("timeout_seconds must be non-negative.")
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + float(timeout_seconds)
        )
        with self._condition:
            self._waiting += 1
            try:
                while True:
                    if not self._open:
                        return None
                    if execution_guard is not None:
                        try:
                            if not bool(execution_guard()):
                                return None
                        except Exception:
                            return None
                    if self._active is None:
                        lease = GameOperationLease(
                            self,
                            operation.strip(),
                        )
                        self._active = lease
                        return lease
                    wait_seconds = 0.02
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return None
                        wait_seconds = min(wait_seconds, remaining)
                    self._condition.wait(wait_seconds)
            finally:
                self._waiting -= 1

    def close_and_wait(self, timeout_seconds: float = 5.0) -> bool:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be non-negative.")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            self._open = False
            self._condition.notify_all()
            while self._active is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._open = True
                    self._condition.notify_all()
                    return False
                self._condition.wait(min(0.02, remaining))
            return True

    def reopen(self) -> None:
        with self._condition:
            if self._active is not None:
                raise RuntimeError(
                    "cannot reopen while an operation is still active."
                )
            self._open = True
            self._condition.notify_all()

    def _release(self, lease: GameOperationLease) -> None:
        with self._condition:
            if self._active is lease:
                self._active = None
                self._condition.notify_all()
