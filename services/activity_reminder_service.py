"""Create each confirmed activity reminder once, five minutes before it starts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardCapacityError
from domain.activity_schedule import ActivityScheduleCatalog
from domain.group import CharacterGroup
from domain.progress import TAIPEI_TIMEZONE
from services.card_coordinator import CardCoordinator
from workspace.models import WorkspaceState


GLOBAL_REMINDER_GROUP = CharacterGroup(
    group_id="activity-reminders",
    name="活動提醒",
)


class ActivityReminderService:
    """Turn confirmed schedule facts into player-visible reminder cards."""

    def __init__(
        self,
        catalog: ActivityScheduleCatalog,
        coordinator: CardCoordinator,
        workspace_provider: Callable[[], WorkspaceState],
        *,
        state_path: Path | None = None,
    ) -> None:
        if not isinstance(catalog, ActivityScheduleCatalog):
            raise TypeError("catalog must be ActivityScheduleCatalog.")
        if not isinstance(coordinator, CardCoordinator):
            raise TypeError("coordinator must be CardCoordinator.")
        if not callable(workspace_provider):
            raise TypeError("workspace_provider must be callable.")
        self._catalog = catalog
        self._coordinator = coordinator
        self._workspace_provider = workspace_provider
        self._state_path = Path(state_path) if state_path is not None else None
        self._emitted = self._load_emitted()

    def _load_emitted(self) -> dict[str, datetime]:
        if self._state_path is None or not self._state_path.is_file():
            return {}
        try:
            payload = json.loads(
                self._state_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("emitted"), dict)
        ):
            return {}
        emitted: dict[str, datetime] = {}
        for card_id, raw_occurrence in payload["emitted"].items():
            if not isinstance(card_id, str) or not isinstance(
                raw_occurrence,
                str,
            ):
                continue
            try:
                occurrence = datetime.fromisoformat(raw_occurrence)
            except ValueError:
                continue
            if occurrence.tzinfo is None or occurrence.utcoffset() is None:
                continue
            emitted[card_id] = occurrence.astimezone(TAIPEI_TIMEZONE)
        return emitted

    def _save_emitted(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        payload = {
            "schema_version": 1,
            "emitted": {
                card_id: occurrence.isoformat()
                for card_id, occurrence in sorted(self._emitted.items())
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

    @staticmethod
    def _card_id(activity_id: str, occurrence: datetime) -> str:
        return (
            f"activity-reminder:{activity_id}:"
            f"{occurrence.strftime('%Y%m%dT%H%M%z')}"
        )

    def poll(self, now: datetime) -> tuple[GroupCard, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include timezone information.")
        local_now = now.astimezone(TAIPEI_TIMEZONE)
        workspace = self._workspace_provider()
        if not isinstance(workspace, WorkspaceState):
            raise TypeError("workspace_provider must return WorkspaceState.")

        cutoff = local_now - timedelta(days=1)
        retained = {
            card_id: occurrence
            for card_id, occurrence in self._emitted.items()
            if occurrence > cutoff
        }
        if retained != self._emitted:
            self._emitted = retained
            self._save_emitted()

        candidates: list[tuple[datetime, object]] = []
        for offset in (0, 1):
            local_date = local_now.date() + timedelta(days=offset)
            for rule in self._catalog.all():
                reminder_at = rule.reminder_on(local_date)
                occurrence = rule.occurrence_on(local_date)
                if reminder_at is None or occurrence is None:
                    continue
                if reminder_at <= local_now < occurrence:
                    candidates.append((occurrence, rule))

        shown: list[GroupCard] = []
        for occurrence, rule in sorted(
            candidates,
            key=lambda item: (item[0], item[1].activity_id),
        ):
            card_id = self._card_id(rule.activity_id, occurrence)
            if card_id in self._emitted:
                continue
            card = GroupCard(
                card_id=card_id,
                group=workspace.current_group or GLOBAL_REMINDER_GROUP,
                activity=rule.definition,
                current_progress=rule.definition.name,
                requires_player_action=False,
                priority_reason=CardPriorityReason.ACTIVITY,
                name_only=True,
            )
            shown_at = local_now.astimezone(timezone.utc)
            lifetime = occurrence.astimezone(timezone.utc) - shown_at
            if lifetime <= timedelta(0):
                continue
            try:
                self._coordinator.show(
                    card,
                    shown_at=shown_at,
                    lifetime=lifetime,
                )
            except CardCapacityError:
                continue
            self._emitted[card_id] = occurrence
            self._save_emitted()
            shown.append(card)
        return tuple(shown)
