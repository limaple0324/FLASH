"""集中管理一個介面生命週期內的事件訂閱。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from services.event_bus import EventBus


class EventSubscriptionScope:
    def __init__(self, event_bus: EventBus) -> None:
        if not isinstance(event_bus, EventBus):
            raise TypeError("event_bus must be EventBus.")
        self._event_bus = event_bus
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        self._closed = False

    def subscribe(
        self,
        event_name: str,
        handler: Callable[[Any], None],
    ) -> bool:
        if self._closed:
            return False
        subscription = (event_name.strip(), handler)
        if subscription in self._subscriptions:
            return False
        if not self._event_bus.subscribe(event_name, handler):
            return False
        self._subscriptions.append(subscription)
        return True

    def close(self) -> bool:
        if self._closed:
            return True
        succeeded = True
        removed: list[tuple[str, Callable[[Any], None]]] = []
        for event_name, handler in reversed(self._subscriptions):
            try:
                if self._event_bus.unsubscribe(event_name, handler):
                    removed.append((event_name, handler))
                else:
                    succeeded = False
            except Exception:
                succeeded = False
        if succeeded:
            self._subscriptions.clear()
            self._closed = True
        else:
            for event_name, handler in reversed(removed):
                try:
                    self._event_bus.subscribe(event_name, handler)
                except Exception:
                    pass
        return succeeded
