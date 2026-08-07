"""管理工作區當下狀態，不負責決定玩家該做什麼。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from domain.activity import ActivityDefinition
from domain.group import CharacterGroup
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)
from workspace.models import WorkspaceState


class WorkspaceService:
    def __init__(
        self,
        coordinator: IdentityDataTransactionCoordinator,
        initial_state: WorkspaceState | None = None,
    ):
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator.")
        if initial_state is not None and not isinstance(initial_state, WorkspaceState):
            raise TypeError("initial_state must be WorkspaceState or None.")
        self._coordinator = coordinator
        self._lock = threading.RLock()
        self._state = initial_state or WorkspaceState()
        self._change_listeners: list[Callable[[], None]] = []

    @property
    def coordinator(self) -> IdentityDataTransactionCoordinator:
        return self._coordinator

    @property
    def state(self) -> WorkspaceState:
        return self.snapshot()

    def snapshot(self) -> WorkspaceState:
        """Wait for identity publication, then return one immutable state."""
        return self._coordinator.read_consistent(self._snapshot_locked)

    def _snapshot_locked(self) -> WorkspaceState:
        with self._lock:
            return self._state

    def subscribe(self, listener: Callable[[], None]) -> None:
        if not callable(listener):
            raise TypeError("listener must be callable.")
        with self._lock:
            if listener not in self._change_listeners:
                self._change_listeners.append(listener)

    def unsubscribe(self, listener: Callable[[], None]) -> None:
        with self._lock:
            if listener in self._change_listeners:
                self._change_listeners.remove(listener)

    def set_current_group(
        self, group: CharacterGroup | None
    ) -> WorkspaceState:
        changed = self._coordinator.execute(
            lambda transaction: self.stage_set_current_group(transaction, group)
        )
        return self.notify_current_group_committed(changed)

    def stage_set_current_group(
        self,
        transaction: IdentityDataTransaction,
        group: CharacterGroup | None,
    ) -> bool:
        self._coordinator.require_transaction(transaction)
        if group is not None and not isinstance(group, CharacterGroup):
            raise TypeError("group must be CharacterGroup or None.")
        with self._lock:
            if self._state.current_group == group:
                return False
            transaction.stage_memory(
                IdentityDataResource.CURRENT_GROUP,
                self.current_group_for_publication,
                lambda: self.install_current_group_from_publication(group),
                self.restore_current_group_from_publication,
            )
        return True

    def notify_current_group_committed(self, changed: bool) -> WorkspaceState:
        """Notify only after the caller's whole outer transaction committed."""
        if not isinstance(changed, bool):
            raise TypeError("changed must be bool.")
        if changed:
            self._notify_change()
        return self.snapshot()

    def current_group_for_publication(self) -> CharacterGroup | None:
        self._coordinator.require_active_transaction_owner()
        with self._lock:
            return self._state.current_group

    def install_current_group_from_publication(
        self,
        group: CharacterGroup | None,
    ) -> None:
        self._coordinator.require_active_transaction_owner()
        if group is not None and not isinstance(group, CharacterGroup):
            raise TypeError("group must be CharacterGroup or None.")
        with self._lock:
            self._replace_current_group(group)

    def restore_current_group_from_publication(self, group: object) -> None:
        self._coordinator.require_active_transaction_owner()
        if group is not None and not isinstance(group, CharacterGroup):
            raise TypeError("invalid workspace current-group snapshot")
        with self._lock:
            self._replace_current_group(group)

    def set_current_activity(
        self, activity: ActivityDefinition | None
    ) -> WorkspaceState:
        return self._replace_nonidentity(current_activity=activity)

    def set_next_step(self, next_step: str | None) -> WorkspaceState:
        return self._replace_nonidentity(next_step=next_step)

    def clear(self) -> WorkspaceState:
        before = self.snapshot()
        group_changed = False
        if before.current_group is not None:
            group_changed = self._coordinator.execute(
                lambda transaction: self.stage_set_current_group(
                    transaction,
                    None,
                )
            )

        state, nonidentity_changed = self._coordinator.read_consistent(
            self._clear_nonidentity_locked
        )
        if group_changed or nonidentity_changed:
            self._notify_change()
        return state

    def _clear_nonidentity_locked(self) -> tuple[WorkspaceState, bool]:
        with self._lock:
            state = replace(
                self._state,
                current_activity=None,
                next_step=None,
            )
            changed = state != self._state
            self._state = state
            return self._state, changed

    def _replace_current_group(self, group: CharacterGroup | None) -> None:
        self._state = replace(self._state, current_group=group)

    def _replace_nonidentity(self, **changes: object) -> WorkspaceState:
        state, changed = self._coordinator.read_consistent(
            lambda: self._replace_nonidentity_locked(**changes)
        )
        if changed:
            self._notify_change()
        return state

    def _replace_nonidentity_locked(
        self,
        **changes: object,
    ) -> tuple[WorkspaceState, bool]:
        with self._lock:
            state = replace(self._state, **changes)
            if state == self._state:
                return self._state, False
            self._state = state
            return self._state, True

    def _notify_change(self) -> None:
        with self._lock:
            listeners = tuple(self._change_listeners)
        for listener in listeners:
            listener()
