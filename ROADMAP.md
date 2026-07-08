
from collections import defaultdict
from typing import Callable, Dict, List, Any

class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event: str, handler: Callable[[Any], None]):
        self._subscribers[event].append(handler)

    def publish(self, event: str, payload: Any = None):
        for h in self._subscribers.get(event, []):
            h(payload)
