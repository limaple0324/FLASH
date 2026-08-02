"""In-process event bus for FLASH."""

from collections import defaultdict
from collections.abc import Callable
from threading import RLock
from typing import Any

from services.logger_service import LoggerService


class EventBus:
    def __init__(self, logger: LoggerService | None = None):
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = RLock()
        self.logger = logger

    def subscribe(self, event_name: str, handler: Callable[[Any], None]) -> bool:
        if not isinstance(event_name, str) or not event_name.strip():
            raise ValueError("event_name must not be empty.")
        if not callable(handler):
            raise TypeError("handler must be callable.")
        event_name = event_name.strip()
        with self._lock:
            handlers = self._subscribers[event_name]
            if handler in handlers:
                return False
            handlers.append(handler)
        self._log("info", f"Subscribed handler to event: {event_name}")
        return True

    def unsubscribe(self, event_name: str, handler: Callable[[Any], None]) -> bool:
        if not isinstance(event_name, str) or not event_name.strip():
            raise ValueError("event_name must not be empty.")
        if not callable(handler):
            raise TypeError("handler must be callable.")
        event_name = event_name.strip()
        with self._lock:
            handlers = self._subscribers.get(event_name)
            if handlers is None or handler not in handlers:
                return False
            handlers.remove(handler)
            if not handlers:
                self._subscribers.pop(event_name, None)
        self._log("info", f"Unsubscribed handler from event: {event_name}")
        return True

    def publish(self, event_name: str, payload: Any = None) -> None:
        if not isinstance(event_name, str) or not event_name.strip():
            raise ValueError("event_name must not be empty.")
        event_name = event_name.strip()
        self._log("info", f"Publishing event: {event_name}")
        with self._lock:
            handlers = tuple(self._subscribers.get(event_name, ()))
        for handler in handlers:
            try:
                handler(payload)
            except Exception as error:
                self._log(
                    "error",
                    f"Event handler failed and was isolated: {event_name}: {error}",
                )

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)(message)
        except Exception:
            pass
