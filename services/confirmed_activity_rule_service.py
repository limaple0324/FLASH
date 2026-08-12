"""保存並輪詢八項已定案活動中不涉及遊戲操作的規則。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
import uuid

from cards.models import GroupCard
from cards.priority import CardPriorityReason
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.confirmed_activity_rules import (
    CONFIRMED_ACTIVITY_RULE_CHANGED_EVENT,
    ConfirmedActivityEvent,
    ConfirmedActivityEventType,
    ConfirmedActivityKind,
    ConfirmedActivityRecord,
    ConfirmedActivityRuleChange,
)
from domain.group import CharacterGroup
from domain.progress import TAIPEI_TIMEZONE
from services.card_coordinator import CardCoordinator
from services.event_bus import EventBus
from services.group_role_status_service import (
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_OPEN,
)


FANTASY_FIRST_THREE_DURATION = timedelta(minutes=10)
FANTASY_LATER_DURATION = timedelta(hours=6)
FANTASY_REMINDER_INTERVAL = timedelta(minutes=5)
DIMENSION_DURATION = timedelta(minutes=60)
ESTATE_FIRST_ROUND_DURATION = timedelta(hours=8)
DEITY_CULTIVATION_DURATION = timedelta(minutes=15)
GOLDEN_TICKET_CARD_LIFETIME = timedelta(minutes=10)


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value.astimezone(timezone.utc)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _local_day(value: datetime) -> date:
    return _aware(value, "value").astimezone(TAIPEI_TIMEZONE).date()


@dataclass(frozen=True, slots=True)
class _CardPlan:
    card: GroupCard
    shown_at: datetime
    lifetime: timedelta | None = None
    remaining_time: timedelta | None = None
    current_group_progress: bool = False
    important_today: bool = False


class ConfirmedActivityRuleService:
    """只接收具型別事件，先保存再發布活動狀態或卡片。"""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        coordinator: CardCoordinator,
        *,
        state_path: Path | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        if not isinstance(coordinator, CardCoordinator):
            raise TypeError("coordinator must be CardCoordinator.")
        if event_bus is not None and not isinstance(event_bus, EventBus):
            raise TypeError("event_bus must be EventBus or None.")
        self._coordinator = coordinator
        self._event_bus = event_bus
        self._state_path = Path(state_path) if state_path is not None else None
        self._lock = RLock()
        (
            self._records,
            self._groups,
            self._fantasy_collections,
            self._ticket_reminders,
            self._artifact_prompts,
        ) = self._load()

    def records(self) -> tuple[ConfirmedActivityRecord, ...]:
        with self._lock:
            return tuple(
                self._records[key] for key in sorted(self._records)
            )

    def record(self, record_id: str) -> ConfirmedActivityRecord | None:
        record_id = _required_text(record_id, "record_id")
        with self._lock:
            return self._records.get(record_id)

    def register_group(self, group: CharacterGroup) -> bool:
        """登記唯一目前群組；歷史活動仍保留在各自紀錄中。"""
        if not isinstance(group, CharacterGroup):
            raise TypeError("group must be CharacterGroup.")
        with self._lock:
            groups = {group.group_id: group}
            if groups != self._groups and not self._commit_locked(
                self._records,
                groups,
                self._fantasy_collections,
                self._ticket_reminders,
                self._artifact_prompts,
            ):
                return False
        for card_id in self._inactive_confirmed_card_ids(group.group_id):
            self._coordinator.cards.remove(card_id)
        return True

    def clear_current_group(self) -> bool:
        """清除目前提醒範圍，不清除既有活動紀錄。"""
        with self._lock:
            if self._groups and not self._commit_locked(
                self._records,
                {},
                self._fantasy_collections,
                self._ticket_reminders,
                self._artifact_prompts,
            ):
                return False
        for card_id in self._inactive_confirmed_card_ids(None):
            self._coordinator.cards.remove(card_id)
        return True

    def handle(self, event: ConfirmedActivityEvent) -> bool:
        """處理一次玩家確認或可信觀測；失敗時不留下半套狀態。"""
        if not isinstance(event, ConfirmedActivityEvent):
            raise TypeError("event must be ConfirmedActivityEvent.")
        now = event.observed_at
        if not self._event_is_level_eligible(event):
            return False
        with self._lock:
            records = dict(self._records)
            groups = dict(self._groups)
            collections = dict(self._fantasy_collections)
            tickets = dict(self._ticket_reminders)
            prompts = dict(self._artifact_prompts)
            changes: list[ConfirmedActivityRuleChange] = []
            if groups.get(event.group.group_id) != event.group:
                return False
            accepted = self._apply_event(
                event,
                records,
                collections,
                changes,
            )
            if not accepted:
                return False
            if not self._commit_locked(
                records,
                groups,
                collections,
                tickets,
                prompts,
            ):
                return False
        self._publish_changes(changes)
        removals, plans = self._completion_card_effects(
            event,
            records,
            tickets,
        )
        for card_id in sorted(removals):
            self._coordinator.cards.remove(card_id)
        self._submit_plans(plans)
        return True

    def poll(self, now: datetime) -> tuple[GroupCard, ...]:
        """只根據已保存、已確認的狀態產生必要提醒。"""
        now = _aware(now, "now")
        with self._lock:
            records = dict(self._records)
            groups = dict(self._groups)
            collections = dict(self._fantasy_collections)
            tickets = dict(self._ticket_reminders)
            prompts = dict(self._artifact_prompts)
            changes: list[ConfirmedActivityRuleChange] = []
            plans: list[_CardPlan] = []
            active_group_ids = frozenset(groups)
            changed = self._poll_dimension(now, records, changes)
            changed = (
                self._poll_fantasy(
                    now,
                    records,
                    collections,
                    changes,
                    plans,
                    active_group_ids,
                )
                or changed
            )
            changed = (
                self._poll_estate(
                    now,
                    records,
                    changes,
                    plans,
                    active_group_ids,
                )
                or changed
            )
            changed = self._poll_deity(now, records, changes) or changed
            changed = (
                self._poll_golden_tickets(
                    now,
                    records,
                    groups,
                    tickets,
                    plans,
                )
                or changed
            )
            if changed:
                if not self._commit_locked(
                    records,
                    groups,
                    collections,
                    tickets,
                    prompts,
                ):
                    return ()
        self._publish_changes(changes)
        return self._submit_plans(plans)

    def handle_role_status(
        self,
        subject_id: str,
        status: str,
        occurred_at: datetime,
    ) -> tuple[GroupCard, ...]:
        """只處理可靠角色狀態，關閉遊戲與關閉輔不混為一談。"""
        subject_id = _required_text(subject_id, "subject_id")
        occurred_at = _aware(occurred_at, "occurred_at")
        if status not in {
            ROLE_STATUS_DISCONNECTED,
            ROLE_STATUS_CLOSED,
            ROLE_STATUS_OPEN,
        }:
            return ()
        with self._lock:
            records = dict(self._records)
            groups = dict(self._groups)
            collections = dict(self._fantasy_collections)
            tickets = dict(self._ticket_reminders)
            prompts = dict(self._artifact_prompts)
            changes: list[ConfirmedActivityRuleChange] = []
            plans: list[_CardPlan] = []
            remove_card_ids: set[str] = set()
            changed = self._apply_role_status(
                subject_id,
                status,
                occurred_at,
                records,
                groups,
                prompts,
                changes,
                plans,
                remove_card_ids,
            )
            if not changed:
                return ()
            if not self._commit_locked(
                records,
                groups,
                collections,
                tickets,
                prompts,
            ):
                return ()
        self._publish_changes(changes)
        for card_id in sorted(remove_card_ids):
            self._coordinator.cards.remove(card_id)
        return self._submit_plans(plans)

    def _event_is_level_eligible(self, event: ConfirmedActivityEvent) -> bool:
        if event.subject_id is None:
            return True
        character = next(
            item
            for item in event.group.characters
            if item.character_id == event.subject_id
        )
        if event.activity in {
            ConfirmedActivityKind.MAGIC_SOLDIERS,
            ConfirmedActivityKind.ARTIFACT_DAILY,
        }:
            return character.level in {120, 160}
        if event.activity is ConfirmedActivityKind.DEITY_CULTIVATION:
            return character.level == 160
        return True

    @staticmethod
    def _daily_record_id(
        activity: ConfirmedActivityKind,
        scope_id: str,
        day: date,
    ) -> str:
        return f"{activity.value}:{scope_id}:{day.isoformat()}"

    @staticmethod
    def _fantasy_count_key(subject_id: str, day: date) -> str:
        return f"{subject_id}:{day.isoformat()}"

    def _apply_event(
        self,
        event: ConfirmedActivityEvent,
        records: dict[str, ConfirmedActivityRecord],
        collections: dict[str, int],
        changes: list[ConfirmedActivityRuleChange],
    ) -> bool:
        local_day = _local_day(event.observed_at)
        activity = event.activity
        subject_id = event.subject_id

        if activity is ConfirmedActivityKind.MAGIC_SOLDIERS:
            assert subject_id is not None
            record_id = self._daily_record_id(activity, subject_id, local_day)
            record = records.get(record_id)
            if record is not None and record.stage == "已完成":
                return False
            records[record_id] = ConfirmedActivityRecord(
                record_id=record_id,
                activity=activity,
                group=event.group,
                scope_id=subject_id,
                subject_id=subject_id,
                day=local_day,
                completed_at=event.observed_at,
                stage="已完成",
            )
            changes.append(
                ConfirmedActivityRuleChange(activity, subject_id, event.observed_at)
            )
            return True

        if activity is ConfirmedActivityKind.DIMENSION_SPACE:
            assert subject_id is not None
            record_id = self._daily_record_id(activity, subject_id, local_day)
            current = records.get(record_id)
            active_records = self._uncompleted_records(
                records,
                activity,
                subject_id,
            )
            if event.floor == 1:
                if current is not None or active_records:
                    return False
                records[record_id] = ConfirmedActivityRecord(
                    record_id=record_id,
                    activity=activity,
                    group=event.group,
                    scope_id=subject_id,
                    subject_id=subject_id,
                    day=local_day,
                    started_at=event.observed_at,
                    duration_seconds=int(DIMENSION_DURATION.total_seconds()),
                    stage="進行中",
                )
            else:
                if (
                    len(active_records) != 1
                    or active_records[0].stage != "進行中"
                ):
                    return False
                active = active_records[0]
                records[active.record_id] = replace(active, stage="第二層")
            changes.append(
                ConfirmedActivityRuleChange(activity, subject_id, event.observed_at)
            )
            return True

        if activity is ConfirmedActivityKind.FANTASY_TRAINING:
            assert subject_id is not None
            record_id = f"{activity.value}:{subject_id}"
            current = records.get(record_id)
            if event.event_type is ConfirmedActivityEventType.TRAINING_STARTED:
                if current is not None and current.stage in {"修練中", "等待領取"}:
                    return False
                count = collections.get(
                    self._fantasy_count_key(subject_id, local_day),
                    0,
                )
                duration = (
                    FANTASY_FIRST_THREE_DURATION
                    if count < 3
                    else FANTASY_LATER_DURATION
                )
                records[record_id] = ConfirmedActivityRecord(
                    record_id=record_id,
                    activity=activity,
                    group=event.group,
                    scope_id=subject_id,
                    subject_id=subject_id,
                    day=local_day,
                    started_at=event.observed_at,
                    duration_seconds=int(duration.total_seconds()),
                    stage="修練中",
                )
            else:
                if current is None or current.stage not in {"修練中", "等待領取"}:
                    return False
                count_key = self._fantasy_count_key(subject_id, local_day)
                collections[count_key] = collections.get(count_key, 0) + 1
                records[record_id] = replace(
                    current,
                    completed_at=event.observed_at,
                    paused_at=None,
                    stage="已領取",
                )
            changes.append(
                ConfirmedActivityRuleChange(activity, subject_id, event.observed_at)
            )
            return True

        if activity is ConfirmedActivityKind.ESTATE_FIRST_ROUND:
            group_scope = event.group.group_id
            active = self._latest_estate_record(records, group_scope)
            if event.event_type is ConfirmedActivityEventType.ESTATE_FIRST_OPENED:
                if active is not None:
                    return False
                record_id = self._daily_record_id(activity, group_scope, local_day)
                if record_id in records:
                    return False
                records[record_id] = ConfirmedActivityRecord(
                    record_id=record_id,
                    activity=activity,
                    group=event.group,
                    scope_id=group_scope,
                    day=local_day,
                    started_at=event.observed_at,
                    duration_seconds=int(ESTATE_FIRST_ROUND_DURATION.total_seconds()),
                    stage="第一輪已開啟",
                )
            else:
                if active is None:
                    return False
                records[active.record_id] = replace(
                    active,
                    completed_at=event.observed_at,
                    last_reminder_at=None,
                    reopen_reminder=False,
                    stage="第二次已開啟",
                )
            changes.append(
                ConfirmedActivityRuleChange(activity, group_scope, event.observed_at)
            )
            return True

        if activity is ConfirmedActivityKind.ARTIFACT_DAILY:
            assert subject_id is not None
            record_id = self._daily_record_id(activity, subject_id, local_day)
            current = records.get(record_id)
            active_records = self._uncompleted_records(
                records,
                activity,
                subject_id,
            )
            if event.event_type is ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED:
                if current is not None:
                    if current.stage != "斷線未完成":
                        return False
                    records[current.record_id] = replace(
                        current,
                        group=event.group,
                        stage="介面已開啟",
                    )
                elif active_records:
                    if (
                        len(active_records) != 1
                        or active_records[0].stage != "斷線未完成"
                    ):
                        return False
                    active = active_records[0]
                    records[active.record_id] = replace(
                        active,
                        group=event.group,
                        stage="介面已開啟",
                    )
                else:
                    records[record_id] = ConfirmedActivityRecord(
                        record_id=record_id,
                        activity=activity,
                        group=event.group,
                        scope_id=subject_id,
                        subject_id=subject_id,
                        day=local_day,
                        started_at=event.observed_at,
                        stage="介面已開啟",
                    )
            else:
                if len(active_records) != 1:
                    return False
                active = active_records[0]
                if active.day != local_day:
                    if current is not None:
                        return False
                    records[active.record_id] = replace(
                        active,
                        completed_at=event.observed_at,
                        stage="跨日關閉",
                    )
                    records[record_id] = ConfirmedActivityRecord(
                        record_id=record_id,
                        activity=activity,
                        group=event.group,
                        scope_id=subject_id,
                        subject_id=subject_id,
                        day=local_day,
                        completed_at=event.observed_at,
                        stage="已完成",
                    )
                else:
                    records[active.record_id] = replace(
                        active,
                        completed_at=event.observed_at,
                        stage="已完成",
                    )
            changes.append(
                ConfirmedActivityRuleChange(activity, subject_id, event.observed_at)
            )
            return True

        if activity is ConfirmedActivityKind.DEITY_CULTIVATION:
            group_scope = event.group.group_id
            record_id = self._daily_record_id(activity, group_scope, local_day)
            current = records.get(record_id)
            active_records = self._uncompleted_records(
                records,
                activity,
                group_scope,
            )
            if event.event_type is ConfirmedActivityEventType.DEITY_TASK_STARTED:
                if current is not None or active_records:
                    return False
                records[record_id] = ConfirmedActivityRecord(
                    record_id=record_id,
                    activity=activity,
                    group=event.group,
                    scope_id=group_scope,
                    day=local_day,
                    started_at=event.observed_at,
                    duration_seconds=int(DEITY_CULTIVATION_DURATION.total_seconds()),
                    stage="進行中",
                )
            else:
                if len(active_records) != 1:
                    return False
                active = active_records[0]
                if active.day != local_day:
                    if current is not None:
                        return False
                    records[active.record_id] = replace(
                        active,
                        completed_at=event.observed_at,
                        stage="跨日完成",
                    )
                    records[record_id] = ConfirmedActivityRecord(
                        record_id=record_id,
                        activity=activity,
                        group=event.group,
                        scope_id=group_scope,
                        day=local_day,
                        completed_at=event.observed_at,
                        stage="已完成",
                    )
                else:
                    records[record_id] = replace(
                        active,
                        completed_at=event.observed_at,
                        stage="已完成",
                    )
            changes.append(
                ConfirmedActivityRuleChange(activity, group_scope, event.observed_at)
            )
            return True

        assert activity is ConfirmedActivityKind.GOLDEN_TICKET_EXCHANGE
        assert subject_id is not None
        record_id = self._daily_record_id(activity, subject_id, local_day)
        current = records.get(record_id)
        if current is not None and current.stage == "已完成":
            return False
        records[record_id] = ConfirmedActivityRecord(
            record_id=record_id,
            activity=activity,
            group=event.group,
            scope_id=subject_id,
            subject_id=subject_id,
            day=local_day,
            completed_at=event.observed_at,
            stage="已完成",
        )
        changes.append(
            ConfirmedActivityRuleChange(activity, subject_id, event.observed_at)
        )
        return True

    @staticmethod
    def _uncompleted_records(
        records: Mapping[str, ConfirmedActivityRecord],
        activity: ConfirmedActivityKind,
        scope_id: str,
    ) -> tuple[ConfirmedActivityRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in records.values()
                    if record.activity is activity
                    and record.scope_id == scope_id
                    and record.completed_at is None
                ),
                key=lambda record: (
                    record.started_at or datetime.min.replace(tzinfo=timezone.utc),
                    record.record_id,
                ),
            )
        )

    @staticmethod
    def _latest_estate_record(
        records: Mapping[str, ConfirmedActivityRecord],
        group_id: str,
    ) -> ConfirmedActivityRecord | None:
        candidates = tuple(
            record
            for record in records.values()
            if record.activity is ConfirmedActivityKind.ESTATE_FIRST_ROUND
            and record.scope_id == group_id
            and not record.handled_by_disconnect
            and record.stage in {"第一輪已開啟", "等待第二次開啟"}
        )
        return max(
            candidates,
            key=lambda record: record.started_at or datetime.min.replace(tzinfo=timezone.utc),
            default=None,
        )

    def _poll_dimension(
        self,
        now: datetime,
        records: dict[str, ConfirmedActivityRecord],
        changes: list[ConfirmedActivityRuleChange],
    ) -> bool:
        changed = False
        for record in tuple(records.values()):
            if (
                record.activity is ConfirmedActivityKind.DIMENSION_SPACE
                and record.stage in {"進行中", "第二層"}
                and record.elapsed_at(now) >= DIMENSION_DURATION
            ):
                records[record.record_id] = replace(
                    record,
                    completed_at=now,
                    stage="已完成",
                )
                changes.append(
                    ConfirmedActivityRuleChange(
                        record.activity,
                        record.scope_id,
                        now,
                    )
                )
                changed = True
        return changed

    def _poll_fantasy(
        self,
        now: datetime,
        records: dict[str, ConfirmedActivityRecord],
        collections: Mapping[str, int],
        changes: list[ConfirmedActivityRuleChange],
        plans: list[_CardPlan],
        active_group_ids: frozenset[str],
    ) -> bool:
        changed = False
        for record in tuple(records.values()):
            if (
                record.activity is not ConfirmedActivityKind.FANTASY_TRAINING
                or record.group.group_id not in active_group_ids
                or record.stage not in {"修練中", "等待領取"}
                or record.duration_seconds is None
                or record.elapsed_at(now)
                < timedelta(seconds=record.duration_seconds)
            ):
                continue
            if (
                record.last_reminder_at is not None
                and now - record.last_reminder_at < FANTASY_REMINDER_INTERVAL
            ):
                continue
            updated = replace(
                record,
                last_reminder_at=now,
                stage="等待領取",
            )
            records[record.record_id] = updated
            assert updated.subject_id is not None
            today_count = collections.get(
                self._fantasy_count_key(
                    updated.subject_id,
                    _local_day(now),
                ),
                0,
            ) + 1
            changes.append(
                ConfirmedActivityRuleChange(record.activity, record.scope_id, now)
            )
            plans.append(
                _CardPlan(
                    self._card(
                        card_id=f"confirmed:{_digest(updated.record_id)}",
                        group=updated.group,
                        activity_id=updated.activity.value,
                        name="幻魔修練",
                        affected_character_ids=(updated.subject_id,),
                        current_progress=(
                            f"今天第{today_count}次修練完成，"
                            "等待玩家確認領取"
                        ),
                        priority_reason=CardPriorityReason.TIME_LIMIT,
                    ),
                    shown_at=now,
                )
            )
            changed = True
        return changed

    def _poll_estate(
        self,
        now: datetime,
        records: dict[str, ConfirmedActivityRecord],
        changes: list[ConfirmedActivityRuleChange],
        plans: list[_CardPlan],
        active_group_ids: frozenset[str],
    ) -> bool:
        changed = False
        for record in tuple(records.values()):
            if (
                record.activity is not ConfirmedActivityKind.ESTATE_FIRST_ROUND
                or record.group.group_id not in active_group_ids
                or record.stage != "第一輪已開啟"
                or record.handled_by_disconnect
                or record.elapsed_at(now) < ESTATE_FIRST_ROUND_DURATION
                or record.last_reminder_at is not None
            ):
                continue
            updated = replace(
                record,
                last_reminder_at=now,
                reopen_reminder=False,
                stage="等待第二次開啟",
            )
            records[record.record_id] = updated
            changes.append(
                ConfirmedActivityRuleChange(record.activity, record.scope_id, now)
            )
            plans.append(
                _CardPlan(
                    self._card(
                        card_id=f"confirmed:{_digest(updated.record_id)}",
                        group=updated.group,
                        activity_id=updated.activity.value,
                        name="莊園第一輪",
                        affected_character_ids=updated.group.character_ids,
                        current_progress="第一輪已滿八小時，等待第二次開啟",
                        priority_reason=CardPriorityReason.ACTIVITY,
                    ),
                    shown_at=now,
                    current_group_progress=True,
                )
            )
            changed = True
        return changed

    def _poll_deity(
        self,
        now: datetime,
        records: dict[str, ConfirmedActivityRecord],
        changes: list[ConfirmedActivityRuleChange],
    ) -> bool:
        changed = False
        for record in tuple(records.values()):
            if (
                record.activity is ConfirmedActivityKind.DEITY_CULTIVATION
                and record.stage == "進行中"
                and record.duration_seconds is not None
                and record.elapsed_at(now)
                >= timedelta(seconds=record.duration_seconds)
            ):
                records[record.record_id] = replace(
                    record,
                    stage="等待確認完成",
                )
                changes.append(
                    ConfirmedActivityRuleChange(
                        record.activity,
                        record.scope_id,
                        now,
                    )
                )
                changed = True
        return changed

    def _poll_golden_tickets(
        self,
        now: datetime,
        records: Mapping[str, ConfirmedActivityRecord],
        groups: Mapping[str, CharacterGroup],
        ticket_reminders: dict[str, datetime],
        plans: list[_CardPlan],
    ) -> bool:
        local_now = now.astimezone(TAIPEI_TIMEZONE)
        slot_start = self._golden_ticket_slot_start(local_now)
        if slot_start is None:
            return False
        changed = False
        for group in tuple(groups[key] for key in sorted(groups)):
            eligible = tuple(character.character_id for character in group.characters)
            if not eligible:
                continue
            unfinished = tuple(
                subject_id
                for subject_id in eligible
                if not self._record_is_completed(
                    records,
                    ConfirmedActivityKind.GOLDEN_TICKET_EXCHANGE,
                    subject_id,
                    local_now.date(),
                )
            )
            if not unfinished:
                continue
            marker = (
                f"{group.group_id}:{local_now.date().isoformat()}:"
                f"{slot_start.strftime('%H%M')}"
            )
            if marker in ticket_reminders:
                continue
            ticket_reminders[marker] = now
            remaining = (slot_start + GOLDEN_TICKET_CARD_LIFETIME) - local_now
            plans.append(
                _CardPlan(
                    self._card(
                        card_id=f"confirmed:golden-ticket:{_digest(marker)}",
                        group=group,
                        activity_id=ConfirmedActivityKind.GOLDEN_TICKET_EXCHANGE.value,
                        name="金票兌換",
                        affected_character_ids=unfinished,
                        current_progress="尚未完成今日金票兌換的角色",
                        priority_reason=CardPriorityReason.ACTIVITY,
                    ),
                    shown_at=now,
                    lifetime=remaining,
                    remaining_time=remaining,
                    important_today=True,
                )
            )
            changed = True
        return changed

    @staticmethod
    def _golden_ticket_slot_start(local_now: datetime) -> datetime | None:
        for slot in (time(12, 0), time(18, 0)):
            start = datetime.combine(local_now.date(), slot, tzinfo=TAIPEI_TIMEZONE)
            if start <= local_now < start + GOLDEN_TICKET_CARD_LIFETIME:
                return start
        return None

    def _apply_role_status(
        self,
        subject_id: str,
        status: str,
        occurred_at: datetime,
        records: dict[str, ConfirmedActivityRecord],
        groups: Mapping[str, CharacterGroup],
        prompts: dict[str, datetime],
        changes: list[ConfirmedActivityRuleChange],
        plans: list[_CardPlan],
        remove_card_ids: set[str],
    ) -> bool:
        changed = False
        artifact_reconnect_groups: dict[str, CharacterGroup] = {}
        for record in tuple(records.values()):
            if record.group.group_id not in groups:
                continue
            if (
                record.activity is ConfirmedActivityKind.FANTASY_TRAINING
                and record.subject_id == subject_id
                and record.stage in {"修練中", "等待領取"}
            ):
                if status == ROLE_STATUS_CLOSED and record.paused_at is None:
                    records[record.record_id] = replace(record, paused_at=occurred_at)
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    changed = True
                elif status == ROLE_STATUS_OPEN and record.paused_at is not None:
                    paused_seconds = record.paused_seconds + max(
                        0,
                        int((occurred_at - record.paused_at).total_seconds()),
                    )
                    records[record.record_id] = replace(
                        record,
                        paused_at=None,
                        paused_seconds=paused_seconds,
                    )
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    changed = True
            if (
                record.activity is ConfirmedActivityKind.ESTATE_FIRST_ROUND
                and subject_id in record.group.character_ids
                and record.stage in {"第一輪已開啟", "等待第二次開啟"}
            ):
                if status == ROLE_STATUS_DISCONNECTED and not record.handled_by_disconnect:
                    records[record.record_id] = replace(
                        record,
                        handled_by_disconnect=True,
                    )
                    remove_card_ids.add(f"confirmed:{_digest(record.record_id)}")
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    changed = True
                elif status == ROLE_STATUS_CLOSED and not record.handled_by_disconnect:
                    records[record.record_id] = replace(
                        record,
                        reopen_reminder=True,
                        last_reminder_at=None,
                        stage="第一輪已開啟",
                    )
                    remove_card_ids.add(f"confirmed:{_digest(record.record_id)}")
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    changed = True
                elif status == ROLE_STATUS_OPEN and record.reopen_reminder:
                    due = (
                        record.elapsed_at(occurred_at)
                        >= ESTATE_FIRST_ROUND_DURATION
                    )
                    updated = replace(
                        record,
                        reopen_reminder=False,
                        last_reminder_at=(occurred_at if due else None),
                        stage=("等待第二次開啟" if due else "第一輪已開啟"),
                    )
                    records[record.record_id] = updated
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    if due:
                        plans.append(
                            _CardPlan(
                                self._card(
                                    card_id=(
                                        "confirmed:"
                                        f"{_digest(updated.record_id)}"
                                    ),
                                    group=updated.group,
                                    activity_id=updated.activity.value,
                                    name="莊園第一輪",
                                    affected_character_ids=(
                                        updated.group.character_ids
                                    ),
                                    current_progress=(
                                        "遊戲重新開啟，等待第二次開啟"
                                    ),
                                    priority_reason=CardPriorityReason.ACTIVITY,
                                ),
                                shown_at=occurred_at,
                                current_group_progress=True,
                            )
                        )
                    changed = True

            if (
                record.activity is ConfirmedActivityKind.ARTIFACT_DAILY
                and record.subject_id == subject_id
                and record.stage == "介面已開啟"
                and self._is_eligible_artifact_role(record.group, subject_id)
            ):
                if status == ROLE_STATUS_DISCONNECTED:
                    records[record.record_id] = replace(
                        record,
                        stage="斷線未完成",
                    )
                    artifact_reconnect_groups[record.group.group_id] = record.group
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    changed = True
                elif status == ROLE_STATUS_CLOSED:
                    records[record.record_id] = replace(
                        record,
                        completed_at=occurred_at,
                        stage="遊戲關閉不提醒",
                    )
                    changes.append(
                        ConfirmedActivityRuleChange(
                            record.activity,
                            record.scope_id,
                            occurred_at,
                        )
                    )
                    changed = True

        if status == ROLE_STATUS_DISCONNECTED:
            for group_id in sorted(artifact_reconnect_groups):
                group = artifact_reconnect_groups[group_id]
                local_day = _local_day(occurred_at)
                marker = f"{group.group_id}:{local_day.isoformat()}"
                prompts.pop(marker, None)
                remove_card_ids.add(
                    f"confirmed:artifact:{_digest(marker)}"
                )

        if status == ROLE_STATUS_OPEN:
            local_day = _local_day(occurred_at)
            for group in tuple(groups[key] for key in sorted(groups)):
                if subject_id not in group.character_ids:
                    continue
                eligible = tuple(
                    character.character_id
                    for character in group.characters
                    if character.level in {120, 160}
                )
                unfinished = tuple(
                    item
                    for item in eligible
                    if self._artifact_is_pending(
                        records,
                        item,
                        local_day,
                    )
                )
                marker = f"{group.group_id}:{local_day.isoformat()}"
                if not unfinished or marker in prompts:
                    continue
                prompts[marker] = occurred_at
                plans.append(
                    _CardPlan(
                        self._card(
                            card_id=f"confirmed:artifact:{_digest(marker)}",
                            group=group,
                            activity_id=ConfirmedActivityKind.ARTIFACT_DAILY.value,
                            name="魂器每日處理",
                            affected_character_ids=unfinished,
                            current_progress="今日尚未處理的120與160角色",
                            priority_reason=CardPriorityReason.ACTIVITY,
                        ),
                        shown_at=occurred_at,
                        current_group_progress=True,
                    )
                )
                changed = True
        return changed

    @staticmethod
    def _is_eligible_artifact_role(
        group: CharacterGroup,
        subject_id: str,
    ) -> bool:
        return any(
            character.character_id == subject_id
            and character.level in {120, 160}
            for character in group.characters
        )

    @staticmethod
    def _record_is_completed(
        records: Mapping[str, ConfirmedActivityRecord],
        activity: ConfirmedActivityKind,
        subject_id: str,
        day: date,
    ) -> bool:
        record_id = ConfirmedActivityRuleService._daily_record_id(
            activity,
            subject_id,
            day,
        )
        record = records.get(record_id)
        return record is not None and record.stage == "已完成"

    @staticmethod
    def _card(
        *,
        card_id: str,
        group: CharacterGroup,
        activity_id: str,
        name: str,
        affected_character_ids: tuple[str | None, ...],
        current_progress: str,
        priority_reason: CardPriorityReason,
    ) -> GroupCard:
        affected = tuple(
            item for item in affected_character_ids if isinstance(item, str)
        )
        return GroupCard(
            card_id=card_id,
            group=group,
            activity=ActivityDefinition(
                activity_id=activity_id,
                name=name,
                activity_type=ActivityType.DAILY,
                reset_rule=ResetRule.DAILY_MIDNIGHT,
            ),
            current_progress=current_progress,
            affected_character_ids=affected,
            requires_player_action=True,
            priority_reason=priority_reason,
        )

    def _completion_card_effects(
        self,
        event: ConfirmedActivityEvent,
        records: Mapping[str, ConfirmedActivityRecord],
        ticket_reminders: Mapping[str, datetime],
    ) -> tuple[set[str], list[_CardPlan]]:
        """保存完成結果後，才撤除或縮小既有無期限提醒卡。"""
        removals: set[str] = set()
        plans: list[_CardPlan] = []
        local_day = _local_day(event.observed_at)
        if event.event_type is ConfirmedActivityEventType.TRAINING_COLLECTED:
            assert event.subject_id is not None
            record_id = f"{event.activity.value}:{event.subject_id}"
            removals.add(f"confirmed:{_digest(record_id)}")
        elif event.event_type is ConfirmedActivityEventType.ESTATE_SECOND_OPENED:
            record = max(
                (
                    item
                    for item in records.values()
                    if item.activity is ConfirmedActivityKind.ESTATE_FIRST_ROUND
                    and item.scope_id == event.group.group_id
                    and item.stage == "第二次已開啟"
                    and item.completed_at == event.observed_at
                ),
                key=lambda item: item.started_at
                or datetime.min.replace(tzinfo=timezone.utc),
                default=None,
            )
            if record is not None:
                removals.add(f"confirmed:{_digest(record.record_id)}")
        elif event.event_type in {
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_CLOSED,
        }:
            if (
                event.event_type
                is ConfirmedActivityEventType.ARTIFACT_INTERFACE_CLOSED
            ):
                for record in records.values():
                    if (
                        record.activity is ConfirmedActivityKind.ARTIFACT_DAILY
                        and record.subject_id == event.subject_id
                        and record.stage == "跨日關閉"
                        and record.completed_at == event.observed_at
                    ):
                        removals.add(
                            "confirmed:artifact:"
                            f"{_digest(record.group.group_id + ':' + record.day.isoformat())}"
                        )
            plans.extend(
                self._update_existing_artifact_card(
                    records,
                    group=event.group,
                    day=local_day,
                    card_id=(
                        "confirmed:artifact:"
                        f"{_digest(event.group.group_id + ':' + local_day.isoformat())}"
                    ),
                    shown_at=event.observed_at,
                    removals=removals,
                )
            )
        elif (
            event.event_type
            is ConfirmedActivityEventType.GOLDEN_TICKET_INTERFACE_OPENED
        ):
            prefix = f"{event.group.group_id}:{local_day.isoformat()}:"
            for marker in sorted(
                key
                for key in ticket_reminders
                if key.startswith(prefix)
            ):
                plans.extend(
                    self._update_existing_group_card(
                        records,
                        group=event.group,
                        activity=ConfirmedActivityKind.GOLDEN_TICKET_EXCHANGE,
                        day=local_day,
                        card_id=(
                            "confirmed:golden-ticket:"
                            f"{_digest(marker)}"
                        ),
                        eligible_character_ids=event.group.character_ids,
                        name="金票兌換",
                        current_progress="尚未完成今日金票兌換的角色",
                        priority_reason=CardPriorityReason.ACTIVITY,
                        shown_at=event.observed_at,
                        important_today=True,
                        removals=removals,
                    )
                )
        return removals, plans

    def _update_existing_group_card(
        self,
        records: Mapping[str, ConfirmedActivityRecord],
        *,
        group: CharacterGroup,
        activity: ConfirmedActivityKind,
        day: date,
        card_id: str,
        eligible_character_ids: tuple[str, ...],
        name: str,
        current_progress: str,
        priority_reason: CardPriorityReason,
        shown_at: datetime,
        removals: set[str],
        current_group_progress: bool = False,
        important_today: bool = False,
    ) -> list[_CardPlan]:
        if not self._card_exists(card_id):
            return []
        unfinished = tuple(
            subject_id
            for subject_id in eligible_character_ids
            if not self._record_is_completed(
                records,
                activity,
                subject_id,
                day,
            )
        )
        if not unfinished:
            removals.add(card_id)
            return []
        return [
            _CardPlan(
                self._card(
                    card_id=card_id,
                    group=group,
                    activity_id=activity.value,
                    name=name,
                    affected_character_ids=unfinished,
                    current_progress=current_progress,
                    priority_reason=priority_reason,
                ),
                shown_at=shown_at,
                current_group_progress=current_group_progress,
                important_today=important_today,
            )
        ]

    def _update_existing_artifact_card(
        self,
        records: Mapping[str, ConfirmedActivityRecord],
        *,
        group: CharacterGroup,
        day: date,
        card_id: str,
        shown_at: datetime,
        removals: set[str],
    ) -> list[_CardPlan]:
        if not self._card_exists(card_id):
            return []
        unfinished = tuple(
            character.character_id
            for character in group.characters
            if character.level in {120, 160}
            and self._artifact_is_pending(
                records,
                character.character_id,
                day,
            )
        )
        if not unfinished:
            removals.add(card_id)
            return []
        return [
            _CardPlan(
                self._card(
                    card_id=card_id,
                    group=group,
                    activity_id=ConfirmedActivityKind.ARTIFACT_DAILY.value,
                    name="魂器每日處理",
                    affected_character_ids=unfinished,
                    current_progress="今日尚未處理的120與160角色",
                    priority_reason=CardPriorityReason.ACTIVITY,
                ),
                shown_at=shown_at,
                current_group_progress=True,
            )
        ]

    def _card_exists(self, card_id: str) -> bool:
        return any(
            card.card_id == card_id
            for card in self._coordinator.cards.all_cards
        )

    def _inactive_confirmed_card_ids(
        self,
        active_group_id: str | None,
    ) -> tuple[str, ...]:
        return tuple(
            card.card_id
            for card in self._coordinator.cards.all_cards
            if card.card_id.startswith("confirmed:")
            and (
                active_group_id is None
                or card.group.group_id != active_group_id
            )
        )

    @staticmethod
    def _artifact_is_pending(
        records: Mapping[str, ConfirmedActivityRecord],
        subject_id: str,
        day: date,
    ) -> bool:
        record_id = ConfirmedActivityRuleService._daily_record_id(
            ConfirmedActivityKind.ARTIFACT_DAILY,
            subject_id,
            day,
        )
        record = records.get(record_id)
        if record is not None:
            return record.stage == "斷線未完成"
        active_records = ConfirmedActivityRuleService._uncompleted_records(
            records,
            ConfirmedActivityKind.ARTIFACT_DAILY,
            subject_id,
        )
        if not active_records:
            return True
        return (
            len(active_records) == 1
            and active_records[0].stage == "斷線未完成"
        )

    def _submit_plans(self, plans: list[_CardPlan]) -> tuple[GroupCard, ...]:
        presented: list[GroupCard] = []
        for plan in plans:
            try:
                result = self._coordinator.submit(
                    self._coordinator.candidate_for_card(
                        plan.card,
                        remaining_time=plan.remaining_time,
                        is_current_group_progress=plan.current_group_progress,
                        is_important_today=plan.important_today,
                    ),
                    plan.card,
                    shown_at=plan.shown_at,
                    lifetime=plan.lifetime,
                )
            except Exception:
                continue
            if result is not None:
                presented.append(result)
        return tuple(presented)

    def _load(
        self,
    ) -> tuple[
        dict[str, ConfirmedActivityRecord],
        dict[str, CharacterGroup],
        dict[str, int],
        dict[str, datetime],
        dict[str, datetime],
    ]:
        empty = ({}, {}, {}, {}, {})
        if self._state_path is None or not self._state_path.is_file():
            return empty
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema_version") != self.SCHEMA_VERSION
                or not isinstance(payload.get("records"), list)
                or not isinstance(payload.get("groups"), list)
                or not isinstance(payload.get("fantasy_collections"), Mapping)
                or not isinstance(payload.get("ticket_reminders"), Mapping)
                or not isinstance(payload.get("artifact_prompts"), Mapping)
            ):
                return empty
            records = {
                record.record_id: record
                for raw in payload["records"]
                if (record := ConfirmedActivityRecord.from_dict(raw)) is not None
            }
            if len(records) != len(payload["records"]):
                return empty
            groups = {}
            for raw_group in payload["groups"]:
                record = ConfirmedActivityRecord(
                    record_id="group-loader",
                    activity=ConfirmedActivityKind.ESTATE_FIRST_ROUND,
                    group=self._group_from_payload(raw_group),
                    scope_id="group-loader",
                    day=date(2000, 1, 1),
                )
                groups[record.group.group_id] = record.group
            collections: dict[str, int] = {}
            for key, value in payload["fantasy_collections"].items():
                if (
                    not isinstance(key, str)
                    or isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    return empty
                collections[key] = value
            tickets = self._read_markers(payload["ticket_reminders"])
            prompts = self._read_markers(payload["artifact_prompts"])
            return records, groups, collections, tickets, prompts
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return empty

    @staticmethod
    def _group_from_payload(payload: object) -> CharacterGroup:
        return ConfirmedActivityRecord.from_dict(
            {
                "record_id": "group-loader",
                "activity": ConfirmedActivityKind.ESTATE_FIRST_ROUND.value,
                "group": payload,
                "scope_id": "group-loader",
                "day": "2000-01-01",
                "stage": "待命",
            }
        ).group

    @staticmethod
    def _read_markers(payload: Mapping[str, object]) -> dict[str, datetime]:
        markers: dict[str, datetime] = {}
        for key, raw in payload.items():
            if not isinstance(key, str) or not isinstance(raw, str):
                raise ValueError("marker must contain text key and timestamp.")
            markers[_required_text(key, "marker")] = _aware(
                datetime.fromisoformat(raw),
                "marker timestamp",
            )
        return markers

    def _persist(
        self,
        records: Mapping[str, ConfirmedActivityRecord],
        groups: Mapping[str, CharacterGroup],
        collections: Mapping[str, int],
        ticket_reminders: Mapping[str, datetime],
        artifact_prompts: Mapping[str, datetime],
    ) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [
                records[key].to_dict() for key in sorted(records)
            ],
            "groups": [groups[key].to_dict() for key in sorted(groups)],
            "fantasy_collections": dict(sorted(collections.items())),
            "ticket_reminders": {
                key: ticket_reminders[key].isoformat()
                for key in sorted(ticket_reminders)
            },
            "artifact_prompts": {
                key: artifact_prompts[key].isoformat()
                for key in sorted(artifact_prompts)
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

    def _commit_locked(
        self,
        records: Mapping[str, ConfirmedActivityRecord],
        groups: Mapping[str, CharacterGroup],
        collections: Mapping[str, int],
        ticket_reminders: Mapping[str, datetime],
        artifact_prompts: Mapping[str, datetime],
    ) -> bool:
        try:
            self._persist(
                records,
                groups,
                collections,
                ticket_reminders,
                artifact_prompts,
            )
        except OSError:
            return False
        self._records = dict(records)
        self._groups = dict(groups)
        self._fantasy_collections = dict(collections)
        self._ticket_reminders = dict(ticket_reminders)
        self._artifact_prompts = dict(artifact_prompts)
        return True

    def _publish_changes(
        self,
        changes: list[ConfirmedActivityRuleChange],
    ) -> None:
        if self._event_bus is None:
            return
        for change in changes:
            self._event_bus.publish(CONFIRMED_ACTIVITY_RULE_CHANGED_EVENT, change)
