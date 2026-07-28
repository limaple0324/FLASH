"""阻止主介面關閉後執行已排程或新送達的回呼。"""

from __future__ import annotations

from collections.abc import Callable
import threading


class UiCallbackDispatcher:
    def __init__(
        self,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel: Callable[[object], None],
    ) -> None:
        if not callable(schedule) or not callable(cancel):
            raise TypeError("schedule and cancel must be callable.")
        self._schedule = schedule
        self._cancel = cancel
        self._pending: set[object] = set()
        self._active = True
        self._closed = False
        self._lock = threading.RLock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def dispatch(self, callback: Callable[[], None]) -> object | None:
        if not callable(callback):
            raise TypeError("callback must be callable.")
        with self._lock:
            if not self._active or self._closed:
                return None
            holder: dict[str, object] = {}

            def guarded() -> None:
                token = holder.get("token")
                with self._lock:
                    if token is not None:
                        self._pending.discard(token)
                    allowed = self._active and not self._closed
                if allowed:
                    callback()

            try:
                token = self._schedule(0, guarded)
            except Exception:
                return None
            holder["token"] = token
            self._pending.add(token)
            return token

    def pause(self) -> None:
        with self._lock:
            self._active = False
            pending = tuple(self._pending)
            self._pending.clear()
        for token in pending:
            try:
                self._cancel(token)
            except Exception:
                pass

    def resume(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._active = True
            return True

    def close(self) -> None:
        self.pause()
        with self._lock:
            self._closed = True
