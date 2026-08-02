from datetime import date, datetime

import pytest

from domain.activity_schedule import (
    ActivityScheduleCatalog,
    GLOBAL_SUBJECT_ID,
    ReminderScope,
    ScheduledActivityRule,
    build_confirmed_activity_catalog,
)
from domain.progress import TAIPEI_TIMEZONE
from main import build_services
from services.activity_progress_service import ActivityProgressService
from services.app_context import AppContext


def test_catalog_preserves_all_confirmed_timed_activity_facts():
    catalog = build_confirmed_activity_catalog()

    expected = {
        "golden-ticket-duel": ("東玄對抗賽", (0,), "19:00", "20:00"),
        "carefree-defense": (
            "無憂保衛戰",
            (0, 1, 2, 3, 4),
            "19:00",
            "20:00",
        ),
        "world-boss": ("世界BOSS", (0, 3, 5), "14:30", "15:00"),
        "quiz-contest": ("答題大賽", (0, 3, 5), "20:00", "20:20"),
        "brave-battlefield": ("勇者戰場", (4,), "21:00", "22:00"),
        "void-fury-wild-ghost": ("惡靈現世", (5,), "15:00", "16:00"),
        "treasure-battlefield": ("奪寶奇兵", (5,), "19:00", "20:10"),
        "fishing-contest": ("釣魚大賽", (6,), "14:00", "15:00"),
        "strange-stone-square": ("奇石廣場", (6,), "14:00", "23:59"),
        "eastern-mystic-arena": ("東玄角斗場", (6,), "20:00", "21:00"),
        "maze": ("迷陣", (0, 1, 2, 3, 4, 5, 6), "00:00", "23:59"),
        "magic-soldiers": (
            "魔兵降臨",
            (0, 1, 2, 3, 4, 5, 6),
            "00:00",
            "23:59",
        ),
        "hall-of-demons": (
            "諸魔殿",
            (0, 1, 2, 3, 4, 5, 6),
            "13:00",
            "14:00",
        ),
    }

    for activity_id, (name, weekdays, local_start, local_end) in expected.items():
        rule = catalog.get(activity_id)
        assert rule.definition.name == name
        assert rule.weekdays == weekdays
        assert rule.local_start.strftime("%H:%M") == local_start
        assert rule.local_end.strftime("%H:%M") == local_end
        assert rule.reminder_lead_minutes == 5
        assert rule.reminder_enabled is True
        assert rule.is_ready_for_reminders is True
        assert rule.definition.reset_rule.value == "每日00:00"


def test_sunday_1420_alternates_from_the_supplied_anchor_week():
    catalog = build_confirmed_activity_catalog()
    strange_stone = catalog.get("strange-stone-1420")
    fantasy_realm = catalog.get("fantasy-realm-alternating-1420")

    assert strange_stone.occurs_on(date(2026, 7, 26)) is False
    assert fantasy_realm.occurs_on(date(2026, 7, 26)) is True
    assert strange_stone.occurs_on(date(2026, 8, 2)) is True
    assert fantasy_realm.occurs_on(date(2026, 8, 2)) is False
    assert fantasy_realm.occurs_on(date(2026, 8, 9)) is True


def test_confirmed_level_restrictions_do_not_guess_other_activity_audiences():
    catalog = build_confirmed_activity_catalog()
    world_boss = catalog.get("world-boss")
    magic_soldiers = catalog.get("magic-soldiers")
    hall = catalog.get("hall-of-demons")

    assert world_boss.level_eligibility(160) is True
    assert world_boss.level_eligibility(120) is False
    assert magic_soldiers.level_eligibility(120) is True
    assert magic_soldiers.level_eligibility(160) is True
    assert magic_soldiers.level_eligibility(100) is False
    assert magic_soldiers.local_start.strftime("%H:%M") == "00:00"
    assert hall.level_eligibility(160) is None
    assert hall.is_ready_for_reminders is True
    assert hall.reminder_scope is ReminderScope.UNCONFIRMED


def test_mystery_examiner_is_one_shared_daily_subject():
    rule = build_confirmed_activity_catalog().get("mystery-examiner")

    assert rule.reminder_scope is ReminderScope.GLOBAL_ONCE
    assert rule.definition.max_completions == 1
    assert rule.progress_subject_id("ignored-character") == GLOBAL_SUBJECT_ID
    assert rule.is_ready_for_reminders is True


def test_unconfirmed_subject_scope_fails_closed():
    rule = build_confirmed_activity_catalog().get("world-boss")

    with pytest.raises(ValueError, match="not confirmed"):
        rule.progress_subject_id("level-160-character")


def test_next_timed_activity_uses_taipei_time_and_skips_untimed_requirement():
    catalog = build_confirmed_activity_catalog()
    after = datetime(2026, 7, 26, 14, 10, tzinfo=TAIPEI_TIMEZONE)

    occurrence, rule = catalog.next_timed_after(after)

    assert occurrence == datetime(2026, 7, 26, 14, 20, tzinfo=TAIPEI_TIMEZONE)
    assert rule.activity_id == "fantasy-realm-alternating-1420"


def test_build_services_registers_catalog_and_all_progress_definitions(tmp_path):
    build_services(root=tmp_path)

    catalog = AppContext.get(ActivityScheduleCatalog)
    progress = AppContext.get(ActivityProgressService)

    assert isinstance(catalog, ActivityScheduleCatalog)
    assert progress.definition("mystery-examiner").max_completions == 1
    assert progress.definition("magic-soldiers").name == "魔兵降臨"


def test_schedule_rule_rejects_invalid_weekday_and_missing_alternating_anchor():
    rule = build_confirmed_activity_catalog().get("golden-ticket-duel")

    with pytest.raises(ValueError, match="integers from 0 to 6"):
        ScheduledActivityRule(rule.definition, (7,), rule.local_start)
    with pytest.raises(ValueError, match="anchor_date"):
        ScheduledActivityRule(
            rule.definition,
            rule.weekdays,
            rule.local_start,
            every_n_weeks=2,
        )
