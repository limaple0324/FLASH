from services.event_bus import EventBus
from services.event_subscription_scope import EventSubscriptionScope


def test_scope_deduplicates_and_closes_all_subscriptions_once():
    bus = EventBus()
    scope = EventSubscriptionScope(bus)
    calls = []

    def handler(payload):
        calls.append(payload)

    assert scope.subscribe("changed", handler) is True
    assert scope.subscribe("changed", handler) is False
    bus.publish("changed", 1)
    assert calls == [1]

    assert scope.close() is True
    assert scope.close() is True
    assert scope.subscribe("changed", handler) is False
    bus.publish("changed", 2)
    assert calls == [1]


def test_closed_scope_rejects_new_subscriptions():
    scope = EventSubscriptionScope(EventBus())
    scope.close()

    assert scope.subscribe("changed", lambda _payload: None) is False


def test_partial_close_failure_restores_removed_subscriptions():
    class FailingBus(EventBus):
        def __init__(self):
            super().__init__()
            self.failed_handler = None

        def unsubscribe(self, event_name, handler):
            if handler is self.failed_handler:
                return False
            return super().unsubscribe(event_name, handler)

    bus = FailingBus()
    scope = EventSubscriptionScope(bus)
    calls = []

    def first(payload):
        calls.append(("first", payload))

    def second(payload):
        calls.append(("second", payload))

    scope.subscribe("changed", first)
    scope.subscribe("changed", second)
    bus.failed_handler = first

    assert scope.close() is False
    additional_calls = []
    assert scope.subscribe(
        "additional",
        additional_calls.append,
    ) is True
    bus.publish("additional", 4)
    assert additional_calls == [4]
    bus.publish("changed", 3)
    assert calls == [("first", 3), ("second", 3)]
