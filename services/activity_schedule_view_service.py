"""把已確認活動週期轉成玩家可見、唯讀且不推測進度的今日清單。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from domain.activity_schedule import ActivityScheduleCatalog, ScheduledActivityRule
from domain.progress import TAIPEI_TIMEZONE


@dataclass(frozen=True, slots=True)
class PlayerScheduledActivity:
    activity_id: str
    name: str
    time_text: str
    eligibility_text: str = ""


@dataclass(frozen=True, slots=True)
class PlayerActivitySchedule:
    local_date: date
    activities: tuple[PlayerScheduledActivity, ...]


class ActivityScheduleViewService:
    """只呈現玩家已確認的排程，不產生提醒、完成判定或遊戲輸入。"""

    def __init__(self, catalog: ActivityScheduleCatalog):
        if not isinstance(catalog, ActivityScheduleCatalog):
            raise TypeError("catalog must be ActivityScheduleCatalog.")
        self._catalog = catalog

    @staticmethod
    def _eligibility_text(rule: ScheduledActivityRule) -> str:
        if not rule.eligible_levels:
            return ""
        levels = "／".join(str(level) for level in rule.eligible_levels)
        return f"僅 {levels} 等級"

    def snapshot(self, now: datetime | None = None) -> PlayerActivitySchedule:
        current = now or datetime.now(TAIPEI_TIMEZONE)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must include timezone information.")
        local_date = current.astimezone(TAIPEI_TIMEZONE).date()
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
                PlayerScheduledActivity(
                    activity_id=rule.activity_id,
                    name=rule.definition.name,
                    time_text=rule.time_text,
                    eligibility_text=self._eligibility_text(rule),
                )
                for rule in ordered
            ),
        )
