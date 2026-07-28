from core.target_window_observation import TargetWindowObservation
from services.event_bus import EventBus
from services.target_window_state_service import (
    TARGET_WINDOW_OBSERVED_EVENT,
    TargetWindowStateService,
)


class _RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)


def _ready() -> TargetWindowObservation:
    return TargetWindowObservation.from_detection(
        {
            "configured": True,
            "safe": True,
            "code": "window.ready",
        }
    )


def test_event_bus_updates_the_runtime_only_read_only_snapshot() -> None:
    event_bus = EventBus()
    service = TargetWindowStateService(event_bus)
    calls: list[str] = []
    service.subscribe(lambda: calls.append("changed"))

    assert service.snapshot() == TargetWindowObservation.not_observed()

    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, _ready())

    assert service.snapshot() == _ready()
    assert calls == ["changed"]


def test_repeated_observation_does_not_emit_a_false_change() -> None:
    event_bus = EventBus()
    service = TargetWindowStateService(event_bus)
    calls: list[str] = []
    service.subscribe(lambda: calls.append("changed"))
    observation = _ready()

    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, observation)
    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, observation)

    assert calls == ["changed"]


def test_invalid_event_keeps_previous_snapshot_and_logs_without_raising() -> None:
    event_bus = EventBus()
    logger = _RecordingLogger()
    service = TargetWindowStateService(event_bus, logger)
    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, _ready())
    previous = service.snapshot()

    event_bus.publish(
        TARGET_WINDOW_OBSERVED_EVENT,
        {
            "configured": True,
            "safe": True,
            "code": "window.ready",
            "details": {"handle": 123},
        },
    )

    assert service.snapshot() is previous
    assert len(logger.errors) == 1
    assert "payload was invalid" in logger.errors[0]
    assert "123" not in logger.errors[0]


def test_failing_listener_is_isolated_and_other_listeners_still_run() -> None:
    event_bus = EventBus()
    logger = _RecordingLogger()
    service = TargetWindowStateService(event_bus, logger)
    calls: list[str] = []

    def fail() -> None:
        raise OSError(r"C:\private\listener.txt")

    service.subscribe(fail)
    service.subscribe(lambda: calls.append("safe"))

    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, _ready())

    assert calls == ["safe"]
    assert service.snapshot() == _ready()
    assert r"C:\private\listener.txt" in logger.errors[0]


def test_subscribe_requires_a_callable() -> None:
    service = TargetWindowStateService(EventBus())

    try:
        service.subscribe(None)
    except TypeError as error:
        assert str(error) == "listener must be callable."
    else:
        raise AssertionError("non-callable listener should be rejected")


def test_close_detaches_event_subscription_and_local_listeners() -> None:
    event_bus = EventBus()
    service = TargetWindowStateService(event_bus)
    calls: list[str] = []
    service.subscribe(lambda: calls.append("changed"))

    assert service.close() is True
    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, _ready())

    assert service.snapshot() == TargetWindowObservation.not_observed()
    assert calls == []
