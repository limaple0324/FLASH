"""管理工作區當下狀態，不負責決定玩家該做什麼。"""

from collections.abc import Callable
from dataclasses import replace

from domain.activity import ActivityDefinition
from domain.group import CharacterGroup
from workspace.models import WorkspaceState


class WorkspaceService:
    def __init__(self, initial_state: WorkspaceState | None = None):
        if initial_state is not None and not isinstance(initial_state, WorkspaceState):
            raise TypeError("initial_state must be WorkspaceState or None.")
        self._state = initial_state or WorkspaceState()
        self._change_listeners: list[Callable[[], None]] = []

    @property
    def state(self) -> WorkspaceState:
        return self._state

    def snapshot(self) -> WorkspaceState:
        """Return the current immutable state for read-only consumers."""
        return self._state

    def subscribe(self, listener: Callable[[], None]) -> None:
        if not callable(listener):
            raise TypeError("listener must be callable.")
        if listener not in self._change_listeners:
            self._change_listeners.append(listener)

    def unsubscribe(self, listener: Callable[[], None]) -> None:
        if listener in self._change_listeners:
            self._change_listeners.remove(listener)

    def set_current_group(
        self, group: CharacterGroup | None
    ) -> WorkspaceState:
        return self._replace(current_group=group)

    def set_current_activity(
        self, activity: ActivityDefinition | None
    ) -> WorkspaceState:
        return self._replace(current_activity=activity)

    def set_next_step(self, next_step: str | None) -> WorkspaceState:
        return self._replace(next_step=next_step)

    def clear(self) -> WorkspaceState:
        return self._set_state(WorkspaceState())

    def _replace(self, **changes: object) -> WorkspaceState:
        return self._set_state(replace(self._state, **changes))

    def _set_state(self, state: WorkspaceState) -> WorkspaceState:
        if state == self._state:
            return self._state
        self._state = state
        for listener in tuple(self._change_listeners):
            listener()
        return self._state
