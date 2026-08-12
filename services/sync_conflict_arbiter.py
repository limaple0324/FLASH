"""First-arrival arbitration for overlapping cross-group sync operations."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class SyncConflictRecord:
    target_id: str
    active_operation: str
    skipped_operation: str
    occurred_at: float


@dataclass(slots=True)
class _TargetQueue:
    operation: str
    waiters: deque[threading.Event]


class SyncOperationLease:
    def __init__(
        self,
        arbiter: "SyncConflictArbiter",
        target_id: str,
        operation: str,
    ) -> None:
        self._arbiter = arbiter
        self._target_id = target_id
        self._operation = operation
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._arbiter._release(self._target_id, self._operation)

class SyncConflictArbiter:
    """Keep the first different operation active for each unique role."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        on_conflict: Callable[[SyncConflictRecord], object] | None = None,
    ) -> None:
        self._clock = clock
        self._on_conflict = on_conflict
        self._lock = threading.RLock()
        self._active: dict[str, _TargetQueue] = {}

    def try_begin(
        self,
        target_id: str,
        operation: str,
    ) -> SyncOperationLease | None:
        if not target_id or not operation:
            raise ValueError("target_id and operation are required.")
        conflict: SyncConflictRecord | None = None
        waiter: threading.Event | None = None
        with self._lock:
            active = self._active.get(target_id)
            if active is None:
                self._active[target_id] = _TargetQueue(
                    operation,
                    deque(),
                )
            elif active.operation == operation:
                waiter = threading.Event()
                active.waiters.append(waiter)
            else:
                conflict = SyncConflictRecord(
                    target_id,
                    active.operation,
                    operation,
                    self._clock(),
                )
        if conflict is not None:
            if self._on_conflict is not None:
                try:
                    self._on_conflict(conflict)
                except Exception:
                    pass
            return None
        if waiter is not None:
            # Same operations are intentionally not merged. The per-role FIFO
            # makes each one run in arrival order without blocking other roles.
            waiter.wait()
        return SyncOperationLease(self, target_id, operation)

    def _release(self, target_id: str, operation: str) -> None:
        with self._lock:
            active = self._active.get(target_id)
            if active is None or active.operation != operation:
                return
            if active.waiters:
                active.waiters.popleft().set()
            else:
                self._active.pop(target_id, None)

    def waiting_count(self, target_id: str) -> int:
        with self._lock:
            active = self._active.get(target_id)
            return len(active.waiters) if active is not None else 0
