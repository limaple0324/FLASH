"""Create each confirmed activity reminder once, five minutes before it starts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from cards.service import CardCapacityError
from domain.activity_schedule import ActivityScheduleCatalog
from domain.progress import TAIPEI_TIMEZONE
from services.card_coordinator import CardCoordinator
from workspace.models import WorkspaceState


class ActivityReminderService:
    """Turn confirmed schedule facts into player-visible reminder cards."""

    def __init__(
        self,
        catalog: ActivityScheduleCatalog,
        coordinator: CardCoordinator,
        workspace_provider: Callable[[], WorkspaceState],
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
        self._emitted: dict[str, datetime] = {}

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
        if workspace.current_group is None:
            return ()

        cutoff = local_now - timedelta(days=1)
        self._emitted = {
            card_id: occurrence
            for card_id, occurrence in self._emitted.items()
            if occurrence > cutoff
        }

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
                group=workspace.current_group,
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
            shown.append(card)
        return tuple(shown)
