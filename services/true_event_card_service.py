"""將可信狀態變更轉成不重複的提醒卡。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from domain.status import ActivityStatus
from services.activity_progress_service import ActivityProgressChange
from services.card_coordinator import CardCoordinator
from services.group_role_status_service import (
    GroupRoleStatusChange,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_FAILED,
    ROLE_STATUS_OPEN,
    ROLE_STATUS_RECONNECTING,
)
from workspace.models import WorkspaceState


_INTERRUPTED_ROLE_STATUSES = frozenset(
    {
        ROLE_STATUS_DISCONNECTED,
        ROLE_STATUS_RECONNECTING,
        ROLE_STATUS_FAILED,
    }
)
_EVENT_RETENTION = timedelta(days=31)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


class TrueEventCardService:
    """Only emits cards from typed, observed runtime events."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        coordinator: CardCoordinator,
        workspace_provider: Callable[[], WorkspaceState],
        activity_definition_provider: Callable[[str], ActivityDefinition],
        *,
        state_path: Path | None = None,
        record_callback: Callable[[str, str, str], object] | None = None,
    ) -> None:
        if not isinstance(coordinator, CardCoordinator):
            raise TypeError("coordinator must be CardCoordinator.")
        if not callable(workspace_provider):
            raise TypeError("workspace_provider must be callable.")
        if not callable(activity_definition_provider):
            raise TypeError("activity_definition_provider must be callable.")
        self._coordinator = coordinator
        self._workspace_provider = workspace_provider
        self._definition_provider = activity_definition_provider
        self._state_path = Path(state_path) if state_path is not None else None
        self._record_callback = record_callback
        self._lock = threading.RLock()
        self._role_statuses, self._emitted_events = self._load_state()

    @property
    def state_path(self) -> Path | None:
        return self._state_path

    def _load_state(self) -> tuple[dict[str, str], dict[str, datetime]]:
        if self._state_path is None or not self._state_path.is_file():
            return {}, {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}, {}
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != self.SCHEMA_VERSION
            or not isinstance(payload.get("role_statuses"), Mapping)
            or not isinstance(payload.get("emitted_events"), Mapping)
        ):
            return {}, {}
        statuses = {
            str(key): str(value)
            for key, value in payload["role_statuses"].items()
            if isinstance(key, str)
            and key.strip()
            and isinstance(value, str)
            and value.strip()
        }
        emitted: dict[str, datetime] = {}
        for key, value in payload["emitted_events"].items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            try:
                occurred_at = datetime.fromisoformat(value)
            except ValueError:
                continue
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                continue
            emitted[key] = occurred_at.astimezone(timezone.utc)
        return statuses, emitted

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "role_statuses": dict(sorted(self._role_statuses.items())),
            "emitted_events": {
                key: value.isoformat()
                for key, value in sorted(self._emitted_events.items())
            },
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, role_name: str, detail: str) -> None:
        if self._record_callback is None:
            return
        try:
            self._record_callback("提醒卡", role_name, detail)
        except Exception:
            pass

    def _workspace(self) -> WorkspaceState:
        workspace = self._workspace_provider()
        if not isinstance(workspace, WorkspaceState):
            raise TypeError("workspace_provider must return WorkspaceState.")
        return workspace

    @staticmethod
    def _event_group(
        workspace: WorkspaceState,
        group_name: str,
    ) -> CharacterGroup:
        current = workspace.current_group
        if current is not None and current.name == group_name:
            return current
        return CharacterGroup(
            group_id=f"observed-{_digest(group_name)}",
            name=group_name,
        )

    @staticmethod
    def _affected_character_ids(
        group: CharacterGroup,
        *,
        display_name: str | None = None,
        subject_id: str | None = None,
    ) -> tuple[str, ...]:
        if subject_id is not None and subject_id in group.character_ids:
            return (subject_id,)
        if display_name is None:
            return ()
        matches = tuple(
            character.character_id
            for character in group.characters
            if character.display_name == display_name
        )
        return matches if len(matches) == 1 else ()

    def handle_role_status(
        self,
        change: GroupRoleStatusChange,
        *,
        occurred_at: datetime | None = None,
    ) -> GroupCard | None:
        if not isinstance(change, GroupRoleStatusChange):
            raise TypeError("change must be GroupRoleStatusChange.")
        occurred_at = occurred_at or datetime.now(timezone.utc)
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include timezone information.")
        key = f"{change.group_name}\0{change.current.action_id}"
        with self._lock:
            persisted_previous = self._role_statuses.get(
                key,
                change.previous_status,
            )
            current_status = change.current.status
            if persisted_previous == current_status:
                return None
            self._role_statuses[key] = current_status
            self._save_state()

        if current_status in _INTERRUPTED_ROLE_STATUSES:
            reason = CardPriorityReason.DISCONNECTION
            suffix = "斷線"
            progress_suffix = "已中斷"
            next_step = "等待智慧重連完成"
        elif (
            current_status == ROLE_STATUS_OPEN
            and persisted_previous in _INTERRUPTED_ROLE_STATUSES
        ):
            reason = CardPriorityReason.RECOVERY
            suffix = "已恢復"
            progress_suffix = "可繼續"
            next_step = None
        else:
            return None

        workspace = self._workspace()
        group = self._event_group(workspace, change.group_name)
        current_activity = workspace.current_activity
        progress = (
            f"{current_activity.name}－{progress_suffix}"
            if current_activity is not None
            else f"{change.current.display_name}－{suffix}"
        )
        stable_id = _digest(key)
        card = GroupCard(
            card_id=f"role-connection:{stable_id}",
            group=group,
            activity=ActivityDefinition(
                activity_id=f"role-connection:{stable_id}",
                name=f"{change.current.display_name}－{suffix}",
                activity_type=ActivityType.PERMANENT,
                reset_rule=ResetRule.NONE,
            ),
            current_progress=progress,
            affected_character_ids=self._affected_character_ids(
                group,
                display_name=change.current.display_name,
            ),
            requires_player_action=reason is CardPriorityReason.DISCONNECTION,
            next_step=next_step,
            priority_reason=reason,
        )
        self._coordinator.show(
            card,
            shown_at=occurred_at.astimezone(timezone.utc),
        )
        self._record(change.current.display_name, suffix)
        return card

    def handle_activity_progress(
        self,
        change: ActivityProgressChange,
    ) -> GroupCard | None:
        if not isinstance(change, ActivityProgressChange):
            raise TypeError("change must be ActivityProgressChange.")
        if change.reason != "completion_recorded":
            return None
        definition = self._definition_provider(change.current.activity_id)
        local_day = (
            change.current.period_started_on.isoformat()
            if change.current.period_started_on is not None
            else change.changed_at.date().isoformat()
        )
        event_id = (
            f"completion:{change.current.activity_id}:"
            f"{change.current.subject_id}:{local_day}:"
            f"{change.current.current_count}"
        )
        occurred_at = change.changed_at.astimezone(timezone.utc)
        cutoff = occurred_at - _EVENT_RETENTION
        with self._lock:
            self._emitted_events = {
                key: value
                for key, value in self._emitted_events.items()
                if value >= cutoff
            }
            if event_id in self._emitted_events:
                return None
            self._emitted_events[event_id] = occurred_at
            self._save_state()

        workspace = self._workspace()
        group = workspace.current_group or CharacterGroup(
            group_id="activity-progress",
            name="活動進度",
        )
        fully_completed = change.current.status is ActivityStatus.COMPLETED
        current_progress = (
            f"{definition.name}－已完成"
            if fully_completed
            else f"{definition.name}－完成第 {change.current.current_count} 次"
        )
        card = GroupCard(
            card_id=event_id,
            group=group,
            activity=definition,
            current_progress=current_progress,
            affected_character_ids=self._affected_character_ids(
                group,
                subject_id=change.current.subject_id,
            ),
            requires_player_action=False,
            priority_reason=CardPriorityReason.ACTIVITY,
        )
        self._coordinator.show(card, shown_at=occurred_at)
        role_name = next(
            (
                character.display_name
                for character in group.characters
                if character.character_id == change.current.subject_id
            ),
            change.current.subject_id,
        )
        self._record(role_name, current_progress)
        return card
