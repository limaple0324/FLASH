from datetime import date

import pytest

from domain.activity_schedule import (
    ActivityScheduleCatalog,
    ReminderScope,
    ScheduledActivityRule,
    build_confirmed_activity_catalog,
)
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
        assert rule.local_start is not None
        assert rule.definition.reset_rule.value == "每日00:00"

    magic_soldiers = catalog.get("magic-soldiers")
    assert magic_soldiers.local_start is None
    assert magic_soldiers.local_end is None
    assert magic_soldiers.reminder_enabled is False


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

    assert world_boss.eligible_levels == (160,)
    assert magic_soldiers.eligible_levels == (120, 160)
    assert magic_soldiers.local_start is None
    assert hall.eligible_levels == ()
    assert hall.reminder_enabled is True
    assert hall.local_start is not None
    assert hall.reminder_scope is ReminderScope.UNCONFIRMED


def test_mystery_examiner_is_one_shared_daily_subject():
    rule = build_confirmed_activity_catalog().get("mystery-examiner")

    assert rule.reminder_scope is ReminderScope.GLOBAL_ONCE
    assert rule.definition.max_completions == 1
    assert rule.reminder_enabled is True
    assert rule.local_start is not None


def test_build_services_registers_catalog_and_all_progress_definitions(tmp_path):
    build_services(root=tmp_path)

    progress = AppContext.get(ActivityProgressService)

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
