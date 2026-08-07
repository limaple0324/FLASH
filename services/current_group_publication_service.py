"""Atomically publish one verified current-group identity across all stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from config.config_manager import ConfigManager, ConfigStateSnapshot
from domain.group import CharacterGroup
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)
from workspace.service import WorkspaceService


_RESULT = TypeVar("_RESULT")


@dataclass(frozen=True, slots=True)
class CurrentGroupPublicationPlan(Generic[_RESULT]):
    group_name: str
    workspace_group: CharacterGroup | None
    result: _RESULT


@dataclass(frozen=True, slots=True)
class CurrentGroupPublicationResult(Generic[_RESULT]):
    group_name: str
    workspace_group: CharacterGroup | None
    result: _RESULT
    config_changed: bool
    current_group_changed: bool


class CurrentGroupPublicationNotificationError(RuntimeError):
    """Expose an already committed result when a listener fails afterward."""

    def __init__(
        self,
        result: CurrentGroupPublicationResult[object],
        cause: BaseException,
    ) -> None:
        super().__init__(f"current-group listener failed after commit: {cause}")
        self.result = result


@dataclass(frozen=True, slots=True)
class _CompositeSnapshot:
    config: ConfigStateSnapshot
    current_group: CharacterGroup | None


class CurrentGroupPublicationService:
    """Hold Config then identity coordination through the complete publication."""

    def __init__(
        self,
        config: ConfigManager,
        workspace: WorkspaceService,
        coordinator: IdentityDataTransactionCoordinator,
        *,
        current_group_name_key: str,
    ) -> None:
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager.")
        if not isinstance(workspace, WorkspaceService):
            raise TypeError("workspace must be WorkspaceService.")
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator.")
        if workspace.coordinator is not coordinator:
            raise ValueError("workspace must use the injected coordinator.")
        if not isinstance(current_group_name_key, str) or not current_group_name_key:
            raise ValueError("current_group_name_key must not be empty.")
        self._config = config
        self._workspace = workspace
        self._coordinator = coordinator
        self._current_group_name_key = current_group_name_key

    def execute(
        self,
        prepare: Callable[
            [IdentityDataTransaction],
            CurrentGroupPublicationPlan[_RESULT],
        ],
    ) -> CurrentGroupPublicationResult[_RESULT]:
        if not callable(prepare):
            raise TypeError("prepare must be callable.")

        with self._config.resource_guard():
            original_config = self._config.snapshot_state_locked()

            def prepare_transaction(
                transaction: IdentityDataTransaction,
            ) -> tuple[
                CurrentGroupPublicationPlan[_RESULT],
                bool,
                bool,
            ]:
                plan = prepare(transaction)
                self._validate_plan(plan)
                if self._config.revision != original_config.revision:
                    raise RuntimeError(
                        "configuration changed inside current-group preparation"
                    )
                candidate = self._config.candidate_with_updates_locked(
                    {self._current_group_name_key: plan.group_name},
                    base=original_config.data,
                )
                config_changed = candidate != original_config.data
                current_group_changed = (
                    self._workspace.current_group_for_publication()
                    != plan.workspace_group
                )
                if config_changed:
                    self._config.ensure_parent_directory_locked()
                    content = self._config.serialize_candidate(candidate)
                    transaction.stage_file(
                        IdentityDataResource.CURRENT_GROUP,
                        self._config.config_path,
                        content,
                        lambda value, expected=candidate: (
                            self._config.validate_serialized_candidate(
                                value,
                                expected,
                            )
                        ),
                    )
                if config_changed or current_group_changed:
                    transaction.stage_memory(
                        IdentityDataResource.CURRENT_GROUP,
                        self._composite_snapshot,
                        lambda: self._install_composite(
                            candidate,
                            plan.workspace_group,
                            expected_revision=original_config.revision,
                        ),
                        self._restore_composite,
                    )
                return plan, config_changed, current_group_changed

            plan, config_changed, current_group_changed = (
                self._coordinator.execute(prepare_transaction)
            )

        result = CurrentGroupPublicationResult(
            group_name=plan.group_name,
            workspace_group=plan.workspace_group,
            result=plan.result,
            config_changed=config_changed,
            current_group_changed=current_group_changed,
        )
        try:
            self._workspace.notify_current_group_committed(current_group_changed)
        except BaseException as error:
            raise CurrentGroupPublicationNotificationError(
                result,
                error,
            ) from error
        return result

    @staticmethod
    def _validate_plan(plan: object) -> None:
        if not isinstance(plan, CurrentGroupPublicationPlan):
            raise TypeError("prepare must return CurrentGroupPublicationPlan.")
        if not isinstance(plan.group_name, str):
            raise TypeError("group_name must be str.")
        if plan.group_name != plan.group_name.strip():
            raise ValueError("group_name must already be normalized.")
        if plan.workspace_group is None:
            if plan.group_name:
                raise ValueError("empty workspace group requires an empty group name.")
            return
        if not isinstance(plan.workspace_group, CharacterGroup):
            raise TypeError("workspace_group must be CharacterGroup or None.")
        if plan.workspace_group.name != plan.group_name:
            raise ValueError("workspace group name does not match publication name.")

    def _composite_snapshot(self) -> _CompositeSnapshot:
        return _CompositeSnapshot(
            config=self._config.snapshot_state_locked(),
            current_group=self._workspace.current_group_for_publication(),
        )

    def _install_composite(
        self,
        candidate: dict[str, object],
        current_group: CharacterGroup | None,
        *,
        expected_revision: int,
    ) -> None:
        self._config.install_candidate_locked(
            candidate,
            expected_revision=expected_revision,
        )
        self._workspace.install_current_group_from_publication(current_group)

    def _restore_composite(self, snapshot: object) -> None:
        if not isinstance(snapshot, _CompositeSnapshot):
            raise TypeError("invalid current-group composite snapshot")
        errors: list[BaseException] = []
        try:
            self._config.restore_state_locked(snapshot.config)
        except BaseException as error:
            errors.append(error)
        try:
            self._workspace.restore_current_group_from_publication(
                snapshot.current_group
            )
        except BaseException as error:
            errors.append(error)
        if errors:
            raise RuntimeError(
                "current-group composite rollback failed"
            ) from errors[0]
