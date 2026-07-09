"""In-process event bus for FLASH."""

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from services.logger_service import LoggerService


class EventBus:
    def __init__(self, logger: LoggerService | None = None):
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self.logger = logger

    def subscribe(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._subscribers[event_name].append(handler)
        if self.logger:
            self.logger.info(f"Subscribed handler to event: {event_name}")

    def publish(self, event_name: str, payload: Any = None) -> None:
        if self.logger:
            self.logger.info(f"Publishing event: {event_name}")
        for handler in self._subscribers.get(event_name, []):
            handler(payload)
