"""Exactly-once deferred synchronization while a role reconnects."""

from __future__ import annotations

import threading
import time
import json
import os
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class DeferredSyncFailureRecord:
    sequence: int
    target_id: str
    operation: str
    failure_code: str
    occurred_at: float


@dataclass(slots=True)
class _DeferredOperation:
    sequence: int
    target_id: str
    operation: str
    kind: str
    payload: dict[str, object]
    state: str = "pending"
    executor: Callable[[], bool] | None = None


class DeferredSyncOperationService:
    """Queue each arrival once and drain independently per unique role."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        on_failure: (
            Callable[[DeferredSyncFailureRecord], object] | None
        ) = None,
        state_path: Path | None = None,
    ) -> None:
        self._clock = clock
        self._on_failure = on_failure
        self._lock = threading.RLock()
        self._next_sequence = 1
        self._queues: dict[str, deque[_DeferredOperation]] = {}
        self._processing: set[str] = set()
        self._state_path = Path(state_path) if state_path is not None else None
        self._handlers: dict[
            str,
            Callable[[str, Mapping[str, object]], bool],
        ] = {}
        self._load()

    def register_handler(
        self,
        kind: str,
        handler: Callable[[str, Mapping[str, object]], bool],
    ) -> None:
        if not kind or not callable(handler):
            raise ValueError("kind and handler are required.")
        with self._lock:
            self._handlers[kind] = handler

    def _load(self) -> None:
        path = self._state_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, Mapping):
            return
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return
        maximum_sequence = 0
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            try:
                sequence = int(raw["sequence"])
                target_id = str(raw["target_id"])
                operation = str(raw["operation"])
                kind = str(raw["kind"])
                item_payload = raw.get("payload")
                state = str(raw.get("state", "pending"))
            except (KeyError, TypeError, ValueError):
                continue
            if (
                sequence <= 0
                or not target_id
                or not operation
                or not kind
                or not isinstance(item_payload, Mapping)
                or state not in {"pending", "executing"}
            ):
                continue
            self._queues.setdefault(target_id, deque()).append(
                _DeferredOperation(
                    sequence,
                    target_id,
                    operation,
                    kind,
                    dict(item_payload),
                    state,
                )
            )
            maximum_sequence = max(maximum_sequence, sequence)
        for target_id, queue in self._queues.items():
            self._queues[target_id] = deque(
                sorted(queue, key=lambda item: item.sequence)
            )
        self._next_sequence = maximum_sequence + 1

    def _save(self) -> None:
        path = self._state_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "next_sequence": self._next_sequence,
            "items": [
                {
                    "sequence": item.sequence,
                    "target_id": item.target_id,
                    "operation": item.operation,
                    "kind": item.kind,
                    "payload": item.payload,
                    "state": item.state,
                }
                for queue in self._queues.values()
                for item in queue
            ],
        }
        temporary = path.with_name(
            f".{path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def enqueue(
        self,
        target_id: str,
        operation: str,
        executor: Callable[[], bool] | None = None,
        *,
        kind: str = "callable",
        payload: Mapping[str, object] | None = None,
    ) -> int:
        if (
            not target_id
            or not operation
            or not kind
            or (executor is None and kind == "callable")
            or (executor is not None and not callable(executor))
        ):
            raise ValueError("a target and executable operation are required.")
        with self._lock:
            sequence = self._next_sequence
            self._next_sequence += 1
            self._queues.setdefault(target_id, deque()).append(
                _DeferredOperation(
                    sequence,
                    target_id,
                    operation,
                    kind,
                    dict(payload or {}),
                    "pending",
                    executor,
                )
            )
            self._save()
            return sequence

    def pending(self, target_id: str | None = None) -> int:
        with self._lock:
            if target_id is not None:
                return len(self._queues.get(target_id, ()))
            return sum(len(queue) for queue in self._queues.values())

    def pending_targets(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._queues)

    def _record_failure(
        self,
        item: _DeferredOperation,
        failure_code: str,
    ) -> None:
        record = DeferredSyncFailureRecord(
            item.sequence,
            item.target_id,
            item.operation,
            failure_code,
            self._clock(),
        )
        if self._on_failure is not None:
            try:
                self._on_failure(record)
            except Exception:
                pass

    def _drain_target(self, target_id: str) -> None:
        try:
            while True:
                with self._lock:
                    queue = self._queues.get(target_id)
                    item = queue[0] if queue else None
                if item is None:
                    return
                if item.state == "executing":
                    self._record_failure(
                        item,
                        "previous_delivery_uncertain",
                    )
                    with self._lock:
                        queue = self._queues.get(target_id)
                        if queue and queue[0].sequence == item.sequence:
                            queue.popleft()
                            if not queue:
                                self._queues.pop(target_id, None)
                            self._save()
                    continue
                with self._lock:
                    item.state = "executing"
                    self._save()
                    handler = self._handlers.get(item.kind)
                try:
                    delivered = bool(
                        item.executor()
                        if item.executor is not None
                        else handler is not None
                        and handler(item.target_id, item.payload)
                    )
                except Exception:
                    delivered = False
                if not delivered:
                    self._record_failure(
                        item,
                        "operation_screen_not_safe",
                    )
                with self._lock:
                    queue = self._queues.get(target_id)
                    if queue and queue[0].sequence == item.sequence:
                        queue.popleft()
                        stopped = tuple(queue) if not delivered else ()
                        if not delivered:
                            queue.clear()
                        if not queue:
                            self._queues.pop(target_id, None)
                        self._save()
                if not delivered:
                    for pending in stopped:
                        self._record_failure(
                            pending,
                            "stopped_after_unsafe_operation",
                        )
                    return
        finally:
            with self._lock:
                self._processing.discard(target_id)

    def process_ready(
        self,
        *,
        reconnecting_targets: Iterable[str],
        failed_targets: Iterable[str],
        ready_targets: Iterable[str] | None = None,
    ) -> None:
        reconnecting = set(reconnecting_targets)
        failed = set(failed_targets)
        ready = set(ready_targets) if ready_targets is not None else None
        with self._lock:
            targets = tuple(self._queues)
        # A reconnect failure now triggers the exact-role restart flow. Queued
        # group operations remain durable and paused while that recovery runs.
        if failed:
            return
        # One reconnecting role pauses the entire synchronized batch. This
        # keeps every role's queued sequence aligned until the group is safe.
        if reconnecting:
            return
        for target_id in targets:
            if ready is not None and target_id not in ready:
                continue
            with self._lock:
                if target_id in self._processing:
                    continue
                if not self._queues.get(target_id):
                    continue
                self._processing.add(target_id)
            threading.Thread(
                target=self._drain_target,
                args=(target_id,),
                name="FLASH-DeferredSync",
                daemon=True,
            ).start()


class DeferredSyncOperationMonitor:
    def __init__(
        self,
        service: DeferredSyncOperationService,
        reconnecting_provider: Callable[[], Iterable[str]],
        failed_provider: Callable[[], Iterable[str]],
        ready_provider: Callable[[], Iterable[str]] | None = None,
        *,
        interval_seconds: float = 1.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._service = service
        self._reconnecting_provider = reconnecting_provider
        self._failed_provider = failed_provider
        self._ready_provider = ready_provider
        self._interval_seconds = float(interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._service.process_ready(
                    reconnecting_targets=self._reconnecting_provider(),
                    failed_targets=self._failed_provider(),
                    ready_targets=(
                        self._ready_provider()
                        if self._ready_provider is not None
                        else None
                    ),
                )
            except Exception:
                pass
            self._stop_event.wait(self._interval_seconds)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="FLASH-DeferredSyncMonitor",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self._stop_event.set()
        if thread is not threading.current_thread():
            thread.join(max(0.0, timeout_seconds))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped
