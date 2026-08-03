"""已確認的活動週期、角色層級限制與安全待確認狀態。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.progress import TAIPEI_TIMEZONE


ALL_WEEKDAYS = tuple(range(7))
GLOBAL_SUBJECT_ID = "global"


class ReminderScope(str, Enum):
    """提醒與完成進度要綁定到哪個層級。"""

    GLOBAL_ONCE = "全體共用一次"
    PER_SUBJECT = "依參與對象"
    UNCONFIRMED = "參與對象待確認"


@dataclass(frozen=True, slots=True)
class ScheduledActivityRule:
    """不包含 UI、點擊或未確認完成條件的排程事實。"""

    definition: ActivityDefinition
    weekdays: tuple[int, ...]
    local_start: time | None
    local_end: time | None = None
    reminder_lead_minutes: int = 0
    reminder_enabled: bool = False
    reminder_scope: ReminderScope = ReminderScope.UNCONFIRMED
    eligible_levels: tuple[int, ...] = ()
    every_n_weeks: int = 1
    anchor_date: date | None = None

    def __post_init__(self) -> None:
        weekdays = tuple(self.weekdays)
        eligible_levels = tuple(self.eligible_levels)
        if not weekdays:
            raise ValueError("weekdays must not be empty.")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6
            for item in weekdays
        ):
            raise ValueError("weekdays must contain integers from 0 to 6.")
        if len(weekdays) != len(set(weekdays)):
            raise ValueError("weekdays cannot contain duplicates.")
        if self.local_start is not None and self.local_start.tzinfo is not None:
            raise ValueError("local_start must be a timezone-free wall-clock time.")
        if self.local_end is not None and self.local_end.tzinfo is not None:
            raise ValueError("local_end must be a timezone-free wall-clock time.")
        if self.local_start is None and self.local_end is not None:
            raise ValueError("local_end requires local_start.")
        if (
            isinstance(self.reminder_lead_minutes, bool)
            or not isinstance(self.reminder_lead_minutes, int)
            or self.reminder_lead_minutes < 0
            or self.reminder_lead_minutes > 24 * 60
        ):
            raise ValueError(
                "reminder_lead_minutes must be an integer from 0 to 1440."
            )
        if not isinstance(self.reminder_enabled, bool):
            raise TypeError("reminder_enabled must be bool.")
        if self.reminder_enabled and self.local_start is None:
            raise ValueError("Timed reminders require local_start.")
        if not isinstance(self.reminder_scope, ReminderScope):
            raise TypeError("reminder_scope must be ReminderScope.")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in eligible_levels
        ):
            raise ValueError("eligible_levels must contain positive integers.")
        if len(eligible_levels) != len(set(eligible_levels)):
            raise ValueError("eligible_levels cannot contain duplicates.")
        if (
            isinstance(self.every_n_weeks, bool)
            or not isinstance(self.every_n_weeks, int)
            or self.every_n_weeks <= 0
        ):
            raise ValueError("every_n_weeks must be a positive integer.")
        if self.every_n_weeks > 1 and self.anchor_date is None:
            raise ValueError("Alternating schedules require anchor_date.")
        if self.anchor_date is not None and self.anchor_date.weekday() not in weekdays:
            raise ValueError("anchor_date must fall on an allowed weekday.")
        object.__setattr__(self, "weekdays", weekdays)
        object.__setattr__(self, "eligible_levels", eligible_levels)

    @property
    def activity_id(self) -> str:
        return self.definition.activity_id

    @property
    def is_timed(self) -> bool:
        return self.local_start is not None

    @property
    def is_ready_for_reminders(self) -> bool:
        return self.reminder_enabled and self.local_start is not None

    def occurs_on(self, local_date: date) -> bool:
        if local_date.weekday() not in self.weekdays:
            return False
        if self.every_n_weeks == 1:
            return True
        assert self.anchor_date is not None
        elapsed_weeks = (local_date - self.anchor_date).days // 7
        return elapsed_weeks % self.every_n_weeks == 0

    def occurrence_on(self, local_date: date) -> datetime | None:
        if self.local_start is None or not self.occurs_on(local_date):
            return None
        return datetime.combine(
            local_date,
            self.local_start,
            tzinfo=TAIPEI_TIMEZONE,
        )

    def reminder_on(self, local_date: date) -> datetime | None:
        occurrence = self.occurrence_on(local_date)
        if occurrence is None or not self.reminder_enabled:
            return None
        return occurrence - timedelta(minutes=self.reminder_lead_minutes)

    @property
    def time_text(self) -> str:
        if self.local_start is None:
            return "無固定時間"
        start = self.local_start.strftime("%H:%M")
        if self.local_end is None:
            return start
        return f"{start}–{self.local_end.strftime('%H:%M')}"

    def level_eligibility(self, level: int) -> bool | None:
        if isinstance(level, bool) or not isinstance(level, int) or level <= 0:
            raise ValueError("level must be a positive integer.")
        if self.eligible_levels:
            return level in self.eligible_levels
        if self.reminder_scope is ReminderScope.GLOBAL_ONCE:
            return True
        return None

    def progress_subject_id(self, subject_id: str | None = None) -> str:
        if self.reminder_scope is ReminderScope.GLOBAL_ONCE:
            return GLOBAL_SUBJECT_ID
        if self.reminder_scope is ReminderScope.UNCONFIRMED:
            raise ValueError("Participation scope is not confirmed.")
        if subject_id is None or not subject_id.strip():
            raise ValueError("subject_id is required for per-subject progress.")
        return subject_id.strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "definition": self.definition.to_dict(),
            "weekdays": list(self.weekdays),
            "local_start": (
                self.local_start.strftime("%H:%M") if self.local_start else None
            ),
            "local_end": (
                self.local_end.strftime("%H:%M") if self.local_end else None
            ),
            "reminder_lead_minutes": self.reminder_lead_minutes,
            "reminder_enabled": self.reminder_enabled,
            "reminder_scope": self.reminder_scope.value,
            "eligible_levels": list(self.eligible_levels),
            "every_n_weeks": self.every_n_weeks,
            "anchor_date": self.anchor_date.isoformat() if self.anchor_date else None,
        }


class ActivityScheduleCatalog:
    def __init__(self, rules: tuple[ScheduledActivityRule, ...]):
        rules = tuple(rules)
        by_id = {rule.activity_id: rule for rule in rules}
        if len(by_id) != len(rules):
            raise ValueError("Activity schedule IDs must be unique.")
        self._rules = rules
        self._by_id = by_id

    def all(self) -> tuple[ScheduledActivityRule, ...]:
        return self._rules

    def get(self, activity_id: str) -> ScheduledActivityRule:
        try:
            return self._by_id[activity_id.strip()]
        except KeyError as exc:
            raise KeyError(f"Unknown scheduled activity: {activity_id}") from exc

    def due_on(self, local_date: date) -> tuple[ScheduledActivityRule, ...]:
        return tuple(rule for rule in self._rules if rule.occurs_on(local_date))

    def next_timed_after(self, after: datetime) -> tuple[datetime, ScheduledActivityRule]:
        if after.tzinfo is None or after.utcoffset() is None:
            raise ValueError("after must include timezone information.")
        local_after = after.astimezone(TAIPEI_TIMEZONE)
        candidates: list[tuple[datetime, ScheduledActivityRule]] = []
        for offset in range(0, 22):
            local_date = local_after.date() + timedelta(days=offset)
            for rule in self._rules:
                occurrence = rule.occurrence_on(local_date)
                if occurrence is not None and occurrence > local_after:
                    candidates.append((occurrence, rule))
            if candidates:
                return min(candidates, key=lambda item: (item[0], item[1].activity_id))
        raise RuntimeError("No timed activity found in the supported schedule horizon.")


def _activity(
    activity_id: str,
    name: str,
    *,
    calendar: bool,
    max_completions: int | None = None,
) -> ActivityDefinition:
    return ActivityDefinition(
        activity_id=activity_id,
        name=name,
        activity_type=ActivityType.CALENDAR if calendar else ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=max_completions,
    )


def build_confirmed_activity_catalog() -> ActivityScheduleCatalog:
    """建立 2026-07-26 已由玩家明確說明的週期與提醒事實。"""

    rules = (
        ScheduledActivityRule(
            _activity("hall-of-demons", "諸魔殿", calendar=False),
            ALL_WEEKDAYS,
            time(13, 0),
            time(14, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("world-boss", "世界BOSS", calendar=True),
            (0, 3, 5),
            time(14, 30),
            time(15, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
            eligible_levels=(160,),
        ),
        ScheduledActivityRule(
            _activity("academy-duel", "學院對抗賽", calendar=False),
            ALL_WEEKDAYS,
            time(18, 55),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity(
                "mystery-examiner",
                "神秘考官",
                calendar=False,
                max_completions=1,
            ),
            ALL_WEEKDAYS,
            time(19, 50),
            reminder_lead_minutes=5,
            reminder_enabled=True,
            reminder_scope=ReminderScope.GLOBAL_ONCE,
        ),
        ScheduledActivityRule(
            _activity("golden-ticket-duel", "東玄對抗賽", calendar=True),
            (0,),
            time(19, 0),
            time(20, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("carefree-defense", "無憂保衛戰", calendar=True),
            (0, 1, 2, 3, 4),
            time(19, 0),
            time(20, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("quiz-contest", "答題大賽", calendar=True),
            (0, 3, 5),
            time(20, 0),
            time(20, 20),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("brave-battlefield", "勇者戰場", calendar=True),
            (4,),
            time(21, 0),
            time(22, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("void-fury-wild-ghost", "惡靈現世", calendar=True),
            (5,),
            time(15, 0),
            time(16, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("treasure-battlefield", "奪寶奇兵", calendar=True),
            (5,),
            time(19, 0),
            time(20, 10),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("fishing-contest", "釣魚大賽", calendar=True),
            (6,),
            time(14, 0),
            time(15, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("strange-stone-square", "奇石廣場", calendar=True),
            (6,),
            time(14, 0),
            time(23, 59),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("eastern-mystic-arena", "東玄角斗場", calendar=True),
            (6,),
            time(20, 0),
            time(21, 0),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("strange-stone-1420", "奇石", calendar=True),
            (6,),
            time(14, 20),
            reminder_lead_minutes=5,
            reminder_enabled=True,
            every_n_weeks=2,
            anchor_date=date(2026, 8, 2),
        ),
        ScheduledActivityRule(
            _activity(
                "fantasy-realm-alternating-1420",
                "幻境（隔週14:20）",
                calendar=True,
            ),
            (6,),
            time(14, 20),
            reminder_lead_minutes=5,
            reminder_enabled=True,
            every_n_weeks=2,
            anchor_date=date(2026, 7, 26),
        ),
        ScheduledActivityRule(
            _activity("fantasy-realm-1530", "幻境（15:30）", calendar=True),
            (6,),
            time(15, 30),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("maze", "迷陣", calendar=False),
            ALL_WEEKDAYS,
            time(0, 0),
            time(23, 59),
            reminder_lead_minutes=5,
            reminder_enabled=True,
        ),
        ScheduledActivityRule(
            _activity("magic-soldiers", "魔兵降臨", calendar=False),
            ALL_WEEKDAYS,
            None,
            reminder_enabled=False,
            eligible_levels=(120, 160),
        ),
    )
    return ActivityScheduleCatalog(rules)
