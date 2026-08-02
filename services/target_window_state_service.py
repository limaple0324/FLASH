"""Runtime-only SP2 state fed by the SP1 target-window observation event."""

from __future__ import annotations

from collections.abc import Callable

from core.target_window_observation import TargetWindowObservation
from services.event_bus import EventBus
from services.logger_service import LoggerService


TARGET_WINDOW_OBSERVED_EVENT = "target_window.observed"


class TargetWindowStateService:
    """Keep only the latest immutable, player-safe target-window fact."""

    def __init__(
        self,
        event_bus: EventBus,
        logger: LoggerService | None = None,
    ) -> None:
        if not isinstance(event_bus, EventBus):
            raise TypeError("event_bus must be EventBus.")
        self._event_bus = event_bus
        self._logger = logger
        self._state = TargetWindowObservation.not_observed()
        self._change_listeners: list[Callable[[], None]] = []
        event_bus.subscribe(TARGET_WINDOW_OBSERVED_EVENT, self._on_observed)

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    def snapshot(self) -> TargetWindowObservation:
        return self._state

    def subscribe(self, listener: Callable[[], None]) -> None:
        if not callable(listener):
            raise TypeError("listener must be callable.")
        if listener not in self._change_listeners:
            self._change_listeners.append(listener)

    def unsubscribe(self, listener: Callable[[], None]) -> None:
        if listener in self._change_listeners:
            self._change_listeners.remove(listener)

    def close(self) -> bool:
        self._event_bus.unsubscribe(
            TARGET_WINDOW_OBSERVED_EVENT,
            self._on_observed,
        )
        self._change_listeners.clear()
        return True

    def _on_observed(self, payload: object) -> None:
        if not isinstance(payload, TargetWindowObservation):
            self._log_error(
                "Target-window observation event was ignored because its payload "
                "was invalid."
            )
            return
        if payload == self._state:
            return

        self._state = payload
        for listener in tuple(self._change_listeners):
            try:
                listener()
            except Exception as error:
                self._log_error(
                    "Target-window state listener failed and was isolated: "
                    f"{error}"
                )

    def _log_error(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.error(message)
        except Exception:
            pass
