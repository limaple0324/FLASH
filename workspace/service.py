"""管理工作區當下狀態，不負責決定玩家該做什麼。"""

from dataclasses import replace

from domain.activity import ActivityDefinition
from domain.group import CharacterGroup
from workspace.models import WorkspaceState


class WorkspaceService:
    def __init__(self, initial_state: WorkspaceState | None = None):
        if initial_state is not None and not isinstance(initial_state, WorkspaceState):
            raise TypeError("initial_state must be WorkspaceState or None.")
        self._state = initial_state or WorkspaceState()

    @property
    def state(self) -> WorkspaceState:
        return self._state

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
        self._state = WorkspaceState()
        return self._state

    def _replace(self, **changes: object) -> WorkspaceState:
        self._state = replace(self._state, **changes)
        return self._state
