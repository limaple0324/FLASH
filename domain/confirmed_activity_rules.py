"""具型別的已定案活動事件與可保存狀態。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Mapping

from domain.character import Character, CharacterImportance
from domain.group import CharacterGroup


CONFIRMED_ACTIVITY_RULE_EVENT = "confirmed_activity_rule_event"
CONFIRMED_ACTIVITY_RULE_CHANGED_EVENT = "confirmed_activity_rule_changed"


class ConfirmedActivityKind(str, Enum):
    MAGIC_SOLDIERS = "magic-soldiers"
    DIMENSION_SPACE = "dimension-space"
    FANTASY_TRAINING = "fantasy-training"
    ESTATE_FIRST_ROUND = "estate-first-round"
    ARTIFACT_DAILY = "artifact-daily"
    DEITY_CULTIVATION = "deity-cultivation"
    GOLDEN_TICKET_EXCHANGE = "golden-ticket-exchange"


class ConfirmedActivityEventType(str, Enum):
    CONFIRMED_COMPLETE = "confirmed-complete"
    DIMENSION_ENTERED = "dimension-entered"
    TRAINING_STARTED = "training-started"
    TRAINING_COLLECTED = "training-collected"
    ESTATE_FIRST_OPENED = "estate-first-opened"
    ESTATE_SECOND_OPENED = "estate-second-opened"
    ARTIFACT_INTERFACE_OPENED = "artifact-interface-opened"
    ARTIFACT_INTERFACE_CLOSED = "artifact-interface-closed"
    DEITY_TASK_STARTED = "deity-task-started"
    DEITY_TASK_COMPLETED = "deity-task-completed"
    GOLDEN_TICKET_INTERFACE_OPENED = "golden-ticket-interface-opened"


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value.astimezone(timezone.utc)


def _group_from_dict(payload: object) -> CharacterGroup:
    if not isinstance(payload, Mapping):
        raise ValueError("group must be an object.")
    raw_characters = payload.get("characters")
    if not isinstance(raw_characters, list):
        raise ValueError("group characters must be a list.")
    characters: list[Character] = []
    for raw_character in raw_characters:
        if not isinstance(raw_character, Mapping):
            raise ValueError("group character must be an object.")
        characters.append(
            Character(
                character_id=_required_text(
                    raw_character.get("character_id"),
                    "character_id",
                ),
                display_name=_required_text(
                    raw_character.get("display_name"),
                    "display_name",
                ),
                level=raw_character.get("level"),
                importance=CharacterImportance(
                    _required_text(
                        raw_character.get("importance"),
                        "importance",
                    )
                ),
            )
        )
    return CharacterGroup(
        group_id=_required_text(payload.get("group_id"), "group_id"),
        name=_required_text(payload.get("name"), "group_name"),
        characters=tuple(characters),
    )


_ROLE_EVENT_TYPES = frozenset(
    {
        ConfirmedActivityEventType.CONFIRMED_COMPLETE,
        ConfirmedActivityEventType.DIMENSION_ENTERED,
        ConfirmedActivityEventType.TRAINING_STARTED,
        ConfirmedActivityEventType.TRAINING_COLLECTED,
        ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
        ConfirmedActivityEventType.ARTIFACT_INTERFACE_CLOSED,
        ConfirmedActivityEventType.DEITY_TASK_STARTED,
        ConfirmedActivityEventType.DEITY_TASK_COMPLETED,
        ConfirmedActivityEventType.GOLDEN_TICKET_INTERFACE_OPENED,
    }
)

_ALLOWED_EVENT_TYPES = {
    ConfirmedActivityKind.MAGIC_SOLDIERS: frozenset(
        {ConfirmedActivityEventType.CONFIRMED_COMPLETE}
    ),
    ConfirmedActivityKind.DIMENSION_SPACE: frozenset(
        {ConfirmedActivityEventType.DIMENSION_ENTERED}
    ),
    ConfirmedActivityKind.FANTASY_TRAINING: frozenset(
        {
            ConfirmedActivityEventType.TRAINING_STARTED,
            ConfirmedActivityEventType.TRAINING_COLLECTED,
        }
    ),
    ConfirmedActivityKind.ESTATE_FIRST_ROUND: frozenset(
        {
            ConfirmedActivityEventType.ESTATE_FIRST_OPENED,
            ConfirmedActivityEventType.ESTATE_SECOND_OPENED,
        }
    ),
    ConfirmedActivityKind.ARTIFACT_DAILY: frozenset(
        {
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_OPENED,
            ConfirmedActivityEventType.ARTIFACT_INTERFACE_CLOSED,
        }
    ),
    ConfirmedActivityKind.DEITY_CULTIVATION: frozenset(
        {
            ConfirmedActivityEventType.DEITY_TASK_STARTED,
            ConfirmedActivityEventType.DEITY_TASK_COMPLETED,
        }
    ),
    ConfirmedActivityKind.GOLDEN_TICKET_EXCHANGE: frozenset(
        {ConfirmedActivityEventType.GOLDEN_TICKET_INTERFACE_OPENED}
    ),
}


@dataclass(frozen=True, slots=True)
class ConfirmedActivityEvent:
    """只接受玩家確認或可信觀測所建立的活動事件。"""

    activity: ConfirmedActivityKind
    event_type: ConfirmedActivityEventType
    group: CharacterGroup
    observed_at: datetime
    subject_id: str | None = None
    floor: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.activity, ConfirmedActivityKind):
            raise TypeError("activity must be ConfirmedActivityKind.")
        if not isinstance(self.event_type, ConfirmedActivityEventType):
            raise TypeError("event_type must be ConfirmedActivityEventType.")
        if not isinstance(self.group, CharacterGroup):
            raise TypeError("group must be CharacterGroup.")
        if self.event_type not in _ALLOWED_EVENT_TYPES[self.activity]:
            raise ValueError("event_type is not confirmed for activity.")
        if self.event_type in _ROLE_EVENT_TYPES:
            subject_id = _required_text(self.subject_id, "subject_id")
            if subject_id not in self.group.character_ids:
                raise ValueError("subject_id must belong to group.")
            object.__setattr__(self, "subject_id", subject_id)
        elif self.subject_id is not None:
            raise ValueError("group-level estate events cannot include subject_id.")
        if self.event_type is ConfirmedActivityEventType.DIMENSION_ENTERED:
            if (
                isinstance(self.floor, bool)
                or not isinstance(self.floor, int)
                or self.floor not in {1, 2}
            ):
                raise ValueError("dimension floor must be 1 or 2.")
        elif self.floor is not None:
            raise ValueError("floor is only valid for dimension entry.")
        object.__setattr__(
            self,
            "observed_at",
            _aware(self.observed_at, "observed_at"),
        )


@dataclass(frozen=True, slots=True)
class ConfirmedActivityRecord:
    """僅保存目前可證明的活動語意，不保存操作歷史。"""

    record_id: str
    activity: ConfirmedActivityKind
    group: CharacterGroup
    scope_id: str
    day: date
    subject_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: int | None = None
    paused_at: datetime | None = None
    paused_seconds: int = 0
    last_reminder_at: datetime | None = None
    stage: str = "待命"
    handled_by_disconnect: bool = False
    reopen_reminder: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.activity, ConfirmedActivityKind):
            raise TypeError("activity must be ConfirmedActivityKind.")
        if not isinstance(self.group, CharacterGroup):
            raise TypeError("group must be CharacterGroup.")
        if not isinstance(self.day, date) or isinstance(self.day, datetime):
            raise TypeError("day must be date.")
        record_id = _required_text(self.record_id, "record_id")
        scope_id = _required_text(self.scope_id, "scope_id")
        stage = _required_text(self.stage, "stage")
        subject_id = self.subject_id
        if subject_id is not None:
            subject_id = _required_text(subject_id, "subject_id")
            if subject_id not in self.group.character_ids:
                raise ValueError("subject_id must belong to group.")
        if self.duration_seconds is not None and (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, int)
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be a positive integer.")
        if (
            isinstance(self.paused_seconds, bool)
            or not isinstance(self.paused_seconds, int)
            or self.paused_seconds < 0
        ):
            raise ValueError("paused_seconds must be a non-negative integer.")
        if not isinstance(self.handled_by_disconnect, bool):
            raise TypeError("handled_by_disconnect must be bool.")
        if not isinstance(self.reopen_reminder, bool):
            raise TypeError("reopen_reminder must be bool.")
        for field in (
            "started_at",
            "completed_at",
            "paused_at",
            "last_reminder_at",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _aware(value, field))
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "subject_id", subject_id)

    def elapsed_at(self, now: datetime) -> timedelta:
        if self.started_at is None:
            return timedelta(0)
        current = _aware(now, "now")
        paused = self.paused_seconds
        if self.paused_at is not None:
            paused += max(0, int((current - self.paused_at).total_seconds()))
        elapsed = current - self.started_at - timedelta(seconds=paused)
        return max(elapsed, timedelta(0))

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "activity": self.activity.value,
            "group": self.group.to_dict(),
            "scope_id": self.scope_id,
            "day": self.day.isoformat(),
            "subject_id": self.subject_id,
            "started_at": (
                self.started_at.isoformat() if self.started_at is not None else None
            ),
            "completed_at": (
                self.completed_at.isoformat()
                if self.completed_at is not None
                else None
            ),
            "duration_seconds": self.duration_seconds,
            "paused_at": (
                self.paused_at.isoformat() if self.paused_at is not None else None
            ),
            "paused_seconds": self.paused_seconds,
            "last_reminder_at": (
                self.last_reminder_at.isoformat()
                if self.last_reminder_at is not None
                else None
            ),
            "stage": self.stage,
            "handled_by_disconnect": self.handled_by_disconnect,
            "reopen_reminder": self.reopen_reminder,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "ConfirmedActivityRecord":
        if not isinstance(payload, Mapping):
            raise ValueError("record must be an object.")

        def optional_time(field: str) -> datetime | None:
            raw = payload.get(field)
            if raw is None:
                return None
            return datetime.fromisoformat(_required_text(raw, field))

        raw_subject = payload.get("subject_id")
        if raw_subject is not None and not isinstance(raw_subject, str):
            raise ValueError("subject_id must be a string or null.")
        return cls(
            record_id=_required_text(payload.get("record_id"), "record_id"),
            activity=ConfirmedActivityKind(
                _required_text(payload.get("activity"), "activity")
            ),
            group=_group_from_dict(payload.get("group")),
            scope_id=_required_text(payload.get("scope_id"), "scope_id"),
            day=date.fromisoformat(_required_text(payload.get("day"), "day")),
            subject_id=raw_subject,
            started_at=optional_time("started_at"),
            completed_at=optional_time("completed_at"),
            duration_seconds=payload.get("duration_seconds"),
            paused_at=optional_time("paused_at"),
            paused_seconds=payload.get("paused_seconds", 0),
            last_reminder_at=optional_time("last_reminder_at"),
            stage=_required_text(payload.get("stage", "待命"), "stage"),
            handled_by_disconnect=payload.get("handled_by_disconnect", False),
            reopen_reminder=payload.get("reopen_reminder", False),
        )


@dataclass(frozen=True, slots=True)
class ConfirmedActivityRuleChange:
    activity: ConfirmedActivityKind
    scope_id: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.activity, ConfirmedActivityKind):
            raise TypeError("activity must be ConfirmedActivityKind.")
        object.__setattr__(self, "scope_id", _required_text(self.scope_id, "scope_id"))
        object.__setattr__(
            self,
            "occurred_at",
            _aware(self.occurred_at, "occurred_at"),
        )
