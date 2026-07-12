"""活動進度、完成次數與每日 00:00 重置。"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Mapping
from zoneinfo import ZoneInfo

from domain.activity import ActivityDefinition, ResetRule
from domain.status import ActivityStatus


TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value


@dataclass(frozen=True, slots=True)
class ActivityProgress:
    activity_id: str
    subject_id: str
    current_count: int = 0
    status: ActivityStatus = ActivityStatus.STANDBY
    period_started_on: date | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

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
        )

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
        try:
            period = date.fromisoformat(period_value) if isinstance(period_value, str) else None
            started = datetime.fromisoformat(started_value) if isinstance(started_value, str) else None
            completed = datetime.fromisoformat(completed_value) if isinstance(completed_value, str) else None
        except ValueError as exc:
            raise ValueError("Progress date or time is invalid.") from exc

        return cls(
            activity_id=activity_id,
            subject_id=subject_id,
            current_count=current_count,
            status=ActivityStatus(status),
            period_started_on=period,
            started_at=started,
            completed_at=completed,
        )
