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
        "hall-of-demons": ("諸魔殿", (0, 1, 2, 3, 4, 5, 6), "12:55"),
        "world-boss": ("世界BOSS", (1, 2, 5), "14:25"),
        "academy-duel": ("學院對抗賽", (0, 1, 2, 3, 4, 5, 6), "18:55"),
        "mystery-examiner": ("神秘考官", (0, 1, 2, 3, 4, 5, 6), "19:50"),
        "golden-ticket-duel": ("金票對抗賽", (0,), "19:00"),
        "brave-battlefield": ("勇者戰場", (4,), "21:00"),
        "void-fury-wild-ghost": ("虛空憤怒野鬼", (5,), "15:00"),
        "treasure-battlefield": ("奪寶戰場", (5,), "19:00"),
        "fishing-contest": ("釣魚大賽", (6,), "14:00"),
        "strange-stone-1420": ("奇石", (6,), "14:20"),
        "fantasy-realm-alternating-1420": ("幻境（隔週14:20）", (6,), "14:20"),
        "fantasy-realm-1530": ("幻境（15:30）", (6,), "15:30"),
    }

    for activity_id, (name, weekdays, local_start) in expected.items():
        rule = catalog.get(activity_id)
        assert rule.definition.name == name
        assert rule.weekdays == weekdays
        assert rule.local_start.strftime("%H:%M") == local_start
        assert rule.definition.reset_rule.value == "每日00:00"


def test_sunday_1420_alternates_from_the_supplied_anchor_week():
    catalog = build_confirmed_activity_catalog()
    strange_stone = catalog.get("strange-stone-1420")
    fantasy_realm = catalog.get("fantasy-realm-alternating-1420")

    assert strange_stone.occurs_on(date(2026, 7, 26)) is True
    assert fantasy_realm.occurs_on(date(2026, 7, 26)) is False
    assert strange_stone.occurs_on(date(2026, 8, 2)) is False
    assert fantasy_realm.occurs_on(date(2026, 8, 2)) is True
    assert strange_stone.occurs_on(date(2026, 8, 9)) is True


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
    assert magic_soldiers.local_start is None
    assert hall.level_eligibility(160) is None
    assert hall.is_ready_for_reminders is False


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
    assert rule.activity_id == "strange-stone-1420"


def test_build_services_registers_catalog_and_all_progress_definitions(tmp_path):
    build_services(root=tmp_path)

    catalog = AppContext.get(ActivityScheduleCatalog)
    progress = AppContext.get(ActivityProgressService)

    assert isinstance(catalog, ActivityScheduleCatalog)
    assert progress.definition("mystery-examiner").max_completions == 1
    assert progress.definition("magic-soldiers").name == "魔兵"


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
