"""管理工作區當下狀態，不負責決定玩家該做什麼。"""

from dataclasses import replace

from domain.group import CharacterGroup
from workspace.models import WorkspaceState


class WorkspaceService:
    def __init__(self, initial_state: WorkspaceState | None = None):
        if initial_state is not None and not isinstance(initial_state, WorkspaceState):
            raise TypeError("initial_state must be WorkspaceState or None.")
        self._state = initial_state or WorkspaceState()

    def snapshot(self) -> WorkspaceState:
        """Return the current immutable state for read-only consumers."""
        return self._state

    def set_current_group(
        self, group: CharacterGroup | None
    ) -> WorkspaceState:
        return self._replace(current_group=group)

    def set_next_step(self, next_step: str | None) -> WorkspaceState:
        return self._replace(next_step=next_step)

    def _replace(self, **changes: object) -> WorkspaceState:
        state = replace(self._state, **changes)
        if state != self._state:
            self._state = state
        return self._state
