"""One low-overhead delayed-dispatch queue shared by a sync controller."""

from __future__ import annotations

import heapq
from collections.abc import Callable
from threading import Condition, Thread
from time import monotonic


class SyncDispatchScheduler:
    """Run independent target delays without creating one thread per event."""

    def __init__(self, *, thread_name: str) -> None:
        self._condition = Condition()
        self._queue: list[
            tuple[float, int, int, Callable[[], object]]
        ] = []
        self._sequence = 0
        self._generation = 0
        self._closed = False
        self._worker = Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )
        self._worker.start()

    def schedule(
        self,
        delay_ms: int,
        callback: Callable[[], object],
    ) -> bool:
        if delay_ms <= 0:
            callback()
            return True
        with self._condition:
            if self._closed:
                return False
            self._sequence += 1
            heapq.heappush(
                self._queue,
                (
                    monotonic() + (delay_ms / 1000.0),
                    self._sequence,
                    self._generation,
                    callback,
                ),
            )
            self._condition.notify()
            return True

    def invalidate(self) -> None:
        """Cancel every pending callback while keeping the worker reusable."""
        with self._condition:
            self._generation += 1
            self._queue.clear()
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._generation += 1
            self._queue.clear()
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            callback: Callable[[], object] | None = None
            with self._condition:
                while not self._closed:
                    if not self._queue:
                        self._condition.wait()
                        continue
                    due_at, _, generation, queued_callback = self._queue[0]
                    remaining = due_at - monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    heapq.heappop(self._queue)
                    if generation == self._generation:
                        callback = queued_callback
                    break
                if self._closed:
                    return
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
