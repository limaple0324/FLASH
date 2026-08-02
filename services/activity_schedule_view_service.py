"""把已確認活動週期轉成玩家可見、唯讀且不推測進度的今日清單。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from domain.activity_schedule import (
    ActivityScheduleCatalog,
    ReminderScope,
    ScheduledActivityRule,
)
from domain.progress import TAIPEI_TIMEZONE
from domain.status import ActivityStatus
from services.activity_description_service import ActivityDescriptionService
from services.activity_progress_service import ActivityProgressService


@dataclass(frozen=True, slots=True)
class PlayerScheduledActivity:
    activity_id: str
    name: str
    time_text: str
    eligibility_text: str = ""
    status_text: str = "尚未確認"
    next_step: str = "尚未設定"
    description: str = ""


@dataclass(frozen=True, slots=True)
class PlayerActivitySchedule:
    local_date: date
    activities: tuple[PlayerScheduledActivity, ...]


class ActivityScheduleViewService:
    """只呈現玩家已確認的排程，不產生提醒、完成判定或遊戲輸入。"""

    def __init__(
        self,
        catalog: ActivityScheduleCatalog,
        descriptions: ActivityDescriptionService | None = None,
        progress: ActivityProgressService | None = None,
    ):
        if not isinstance(catalog, ActivityScheduleCatalog):
            raise TypeError("catalog must be ActivityScheduleCatalog.")
        self._catalog = catalog
        self._descriptions = descriptions
        self._progress = progress

    @staticmethod
    def _eligibility_text(rule: ScheduledActivityRule) -> str:
        if rule.eligible_levels:
            levels = "／".join(str(level) for level in rule.eligible_levels)
            return f"僅 {levels} 等級"
        if rule.reminder_scope is ReminderScope.GLOBAL_ONCE:
            return "全體共用一次"
        return "尚未確認"

    def _recorded_status(
        self,
        rule: ScheduledActivityRule,
        local_date: date,
    ) -> str | None:
        if self._progress is None:
            return None
        records = tuple(
            item
            for item in self._progress.all()
            if item.activity_id == rule.activity_id
            and item.period_started_on == local_date
        )
        if not records:
            return None
        statuses = {item.status for item in records}
        if statuses == {ActivityStatus.COMPLETED}:
            return ActivityStatus.COMPLETED.value
        if ActivityStatus.RUNNING in statuses:
            return ActivityStatus.RUNNING.value
        if ActivityStatus.COMPLETED in statuses:
            return "部分完成"
        return ActivityStatus.STANDBY.value

    @staticmethod
    def _time_state(
        rule: ScheduledActivityRule,
        current: datetime,
    ) -> tuple[str, str]:
        occurrence = rule.occurrence_on(current.date())
        if occurrence is None:
            return "時間未確認", "尚未設定"
        if current < occurrence:
            return "尚未開始", f"等待 {rule.local_start.strftime('%H:%M')}"
        if rule.local_end is None:
            return "已到開始時間", "等待可信遊戲進度"
        ending = datetime.combine(
            current.date(),
            rule.local_end,
            tzinfo=TAIPEI_TIMEZONE,
        )
        if current <= ending:
            return "活動時段中", "等待可信遊戲進度"
        return "今日時段已結束", "等待下次排程"

    def snapshot(self, now: datetime | None = None) -> PlayerActivitySchedule:
        current = now or datetime.now(TAIPEI_TIMEZONE)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must include timezone information.")
        local_current = current.astimezone(TAIPEI_TIMEZONE)
        local_date = local_current.date()
        rules = self._catalog.due_on(local_date)
        ordered = sorted(
            rules,
            key=lambda rule: (
                rule.local_start is None,
                rule.local_start or datetime.max.time(),
                rule.activity_id,
            ),
        )
        return PlayerActivitySchedule(
            local_date=local_date,
            activities=tuple(
                self._player_activity(rule, local_current)
                for rule in ordered
            ),
        )

    def _player_activity(
        self,
        rule: ScheduledActivityRule,
        current: datetime,
    ) -> PlayerScheduledActivity:
        time_status, next_step = self._time_state(rule, current)
        recorded_status = self._recorded_status(rule, current.date())
        if recorded_status == ActivityStatus.COMPLETED.value:
            next_step = "等待每日 00:00 重置"
        elif recorded_status == ActivityStatus.RUNNING.value:
            next_step = "等待可信完成判定"
        description = (
            self._descriptions.description(rule.activity_id)
            if self._descriptions is not None
            else ""
        )
        return PlayerScheduledActivity(
            activity_id=rule.activity_id,
            name=rule.definition.name,
            time_text=rule.time_text,
            eligibility_text=self._eligibility_text(rule),
            status_text=recorded_status or time_status,
            next_step=next_step,
            description=description,
        )
