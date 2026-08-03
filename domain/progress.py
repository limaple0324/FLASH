"""活動進度、完成次數與每日 00:00 重置。"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Mapping
from zoneinfo import ZoneInfo

from domain.activity import ActivityDefinition, ResetRule
from domain.status import ActivityStatus


TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value


class ActivityInterruptionReason(str, Enum):
    DISCONNECTED = "disconnected"
    GAME_CLOSED = "game_closed"


@dataclass(frozen=True, slots=True)
class ActivityInterruption:
    reason: ActivityInterruptionReason
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ActivityInterruptionReason):
            raise TypeError("reason must be ActivityInterruptionReason.")
        _require_aware(self.occurred_at, "occurred_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "reason": self.reason.value,
            "occurred_at": self.occurred_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ActivityInterruption":
        reason = payload.get("reason")
        occurred_at = payload.get("occurred_at")
        if not isinstance(reason, str) or not isinstance(occurred_at, str):
            raise ValueError("Interruption fields must be strings.")
        try:
            return cls(
                ActivityInterruptionReason(reason),
                datetime.fromisoformat(occurred_at),
            )
        except ValueError as exc:
            raise ValueError("Interruption data is invalid.") from exc


@dataclass(frozen=True, slots=True)
class ActivityProgress:
    activity_id: str
    subject_id: str
    current_count: int = 0
    status: ActivityStatus = ActivityStatus.STANDBY
    period_started_on: date | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    interruption: ActivityInterruption | None = None

    def __post_init__(self) -> None:
        activity_id = self.activity_id.strip()
        subject_id = self.subject_id.strip()
        if not activity_id:
            raise ValueError("activity_id must not be empty.")
        if not subject_id:
            raise ValueError("subject_id must not be empty.")
        if (
            isinstance(self.current_count, bool)
            or not isinstance(self.current_count, int)
            or self.current_count < 0
        ):
            raise ValueError("current_count must be zero or greater.")
        if not isinstance(self.status, ActivityStatus):
            raise TypeError("status must be ActivityStatus.")
        if self.started_at is not None:
            _require_aware(self.started_at, "started_at")
        if self.completed_at is not None:
            _require_aware(self.completed_at, "completed_at")
        if self.interruption is not None and not isinstance(
            self.interruption,
            ActivityInterruption,
        ):
            raise TypeError("interruption must be ActivityInterruption or None.")
        if (
            self.interruption is not None
            and self.status is not ActivityStatus.RUNNING
        ):
            raise ValueError("Only running progress can hold an interruption.")
        object.__setattr__(self, "activity_id", activity_id)
        object.__setattr__(self, "subject_id", subject_id)

    def _assert_activity(self, definition: ActivityDefinition) -> None:
        if definition.activity_id != self.activity_id:
            raise ValueError("Activity definition does not match this progress.")

    def start(self, at: datetime) -> "ActivityProgress":
        at = _require_aware(at, "at")
        return replace(
            self,
            status=ActivityStatus.RUNNING,
            period_started_on=self.period_started_on or at.astimezone(TAIPEI_TIMEZONE).date(),
            started_at=at,
            interruption=None,
        )

    def record_completion(
        self,
        definition: ActivityDefinition,
        at: datetime,
    ) -> "ActivityProgress":
        self._assert_activity(definition)
        at = _require_aware(at, "at")
        maximum = definition.max_completions
        if maximum is not None and self.current_count >= maximum:
            return self
        next_count = self.current_count + 1
        next_status = (
            ActivityStatus.COMPLETED
            if maximum is not None and next_count >= maximum
            else ActivityStatus.STANDBY
        )
        return replace(
            self,
            current_count=next_count,
            status=next_status,
            period_started_on=self.period_started_on or at.astimezone(TAIPEI_TIMEZONE).date(),
            completed_at=at,
            interruption=None,
        )

    def record_interruption(
        self,
        reason: ActivityInterruptionReason,
        at: datetime,
    ) -> "ActivityProgress":
        if not isinstance(reason, ActivityInterruptionReason):
            raise TypeError("reason must be ActivityInterruptionReason.")
        at = _require_aware(at, "at")
        if self.status is not ActivityStatus.RUNNING:
            return self
        if (
            self.interruption is not None
            and self.interruption.reason is reason
        ):
            return self
        return replace(
            self,
            interruption=ActivityInterruption(reason, at),
        )

    def clear_interruption(self) -> "ActivityProgress":
        if self.interruption is None:
            return self
        return replace(self, interruption=None)

    def reset_if_due(
        self,
        definition: ActivityDefinition,
        now: datetime,
    ) -> "ActivityProgress":
        self._assert_activity(definition)
        now = _require_aware(now, "now")
        if definition.reset_rule is not ResetRule.DAILY_MIDNIGHT:
            return self
        today = now.astimezone(TAIPEI_TIMEZONE).date()
        if self.period_started_on is None:
            return replace(self, period_started_on=today)
        if self.period_started_on >= today:
            return self
        if self.status is ActivityStatus.RUNNING:
            return ActivityProgress(
                activity_id=self.activity_id,
                subject_id=self.subject_id,
                current_count=0,
                status=ActivityStatus.RUNNING,
                period_started_on=today,
                started_at=self.started_at,
                interruption=self.interruption,
            )
        return ActivityProgress(
            activity_id=self.activity_id,
            subject_id=self.subject_id,
            period_started_on=today,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "activity_id": self.activity_id,
            "subject_id": self.subject_id,
            "current_count": self.current_count,
            "status": self.status.value,
            "period_started_on": self.period_started_on.isoformat() if self.period_started_on else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "interruption": (
                self.interruption.to_dict()
                if self.interruption is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ActivityProgress":
        activity_id = payload.get("activity_id")
        subject_id = payload.get("subject_id")
        current_count = payload.get("current_count", 0)
        status = payload.get("status", ActivityStatus.STANDBY.value)
        if not isinstance(activity_id, str) or not isinstance(subject_id, str):
            raise ValueError("Progress identity fields must be strings.")
        if not isinstance(status, str):
            raise ValueError("Progress status must be a string.")

        period_value = payload.get("period_started_on")
        started_value = payload.get("started_at")
        completed_value = payload.get("completed_at")
        interruption_value = payload.get("interruption")
        try:
            period = date.fromisoformat(period_value) if isinstance(period_value, str) else None
            started = datetime.fromisoformat(started_value) if isinstance(started_value, str) else None
            completed = datetime.fromisoformat(completed_value) if isinstance(completed_value, str) else None
        except ValueError as exc:
            raise ValueError("Progress date or time is invalid.") from exc
        if interruption_value is not None and not isinstance(
            interruption_value,
            Mapping,
        ):
            raise ValueError("interruption must be an object or null.")

        return cls(
            activity_id=activity_id,
            subject_id=subject_id,
            current_count=current_count,
            status=ActivityStatus(status),
            period_started_on=period,
            started_at=started,
            completed_at=completed,
            interruption=(
                ActivityInterruption.from_dict(interruption_value)
                if interruption_value is not None
                else None
            ),
        )
