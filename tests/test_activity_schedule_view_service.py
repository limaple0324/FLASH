from datetime import datetime

import pytest

from domain.activity_schedule import build_confirmed_activity_catalog
from domain.progress import TAIPEI_TIMEZONE
from services.activity_schedule_view_service import ActivityScheduleViewService
from ui.home import _activity_schedule_text


def _service() -> ActivityScheduleViewService:
    return ActivityScheduleViewService(build_confirmed_activity_catalog())


def test_current_sunday_shows_corrected_fantasy_realm_not_strange_stone() -> None:
    state = _service().snapshot(
        datetime(2026, 7, 26, 10, 0, tzinfo=TAIPEI_TIMEZONE)
    )

    names = tuple(item.name for item in state.activities)
    assert "幻境（隔週14:20）" in names
    assert "奇石" not in names
    assert _activity_schedule_text(state) == (
        "00:00–23:59　魔兵降臨｜僅 120／160 等級\n"
        "00:00–23:59　迷陣\n"
        "13:00–14:00　諸魔殿\n"
        "14:00–15:00　釣魚大賽\n"
        "14:00–23:59　奇石廣場\n"
        "14:20　幻境（隔週14:20）\n"
        "15:30　幻境（15:30）\n"
        "18:55　學院對抗賽\n"
        "19:50　神秘考官\n"
        "20:00–21:00　東玄角斗場"
    )


def test_next_sunday_shows_strange_stone_not_alternating_fantasy_realm() -> None:
    state = _service().snapshot(
        datetime(2026, 8, 2, 10, 0, tzinfo=TAIPEI_TIMEZONE)
    )

    names = tuple(item.name for item in state.activities)
    assert "奇石" in names
    assert "幻境（隔週14:20）" not in names
    assert "幻境（15:30）" in names


def test_schedule_view_requires_timezone_aware_clock() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _service().snapshot(datetime(2026, 7, 26, 10, 0))
