from datetime import datetime

import pytest

from config.config_manager import ConfigManager
from domain.activity_schedule import build_confirmed_activity_catalog
from domain.progress_store import ActivityProgressStore
from domain.progress import TAIPEI_TIMEZONE
from services.activity_description_service import ActivityDescriptionService
from services.activity_progress_service import ActivityProgressService
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
        "魔兵降臨｜00:00–23:59｜適用：僅 120／160 等級"
        "｜狀態：活動時段中｜下一步：等待可信遊戲進度\n"
        "迷陣｜00:00–23:59｜適用：尚未確認"
        "｜狀態：活動時段中｜下一步：等待可信遊戲進度\n"
        "諸魔殿｜13:00–14:00｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 13:00\n"
        "釣魚大賽｜14:00–15:00｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 14:00\n"
        "奇石廣場｜14:00–23:59｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 14:00\n"
        "幻境（隔週14:20）｜14:20｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 14:20\n"
        "幻境（15:30）｜15:30｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 15:30\n"
        "學院對抗賽｜18:55｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 18:55\n"
        "神秘考官｜19:50｜適用：全體共用一次"
        "｜狀態：尚未開始｜下一步：等待 19:50\n"
        "東玄角斗場｜20:00–21:00｜適用：尚未確認"
        "｜狀態：尚未開始｜下一步：等待 20:00"
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


def test_player_description_and_trusted_progress_are_visible_without_guessing(
    tmp_path,
) -> None:
    catalog = build_confirmed_activity_catalog()
    descriptions = ActivityDescriptionService(
        ConfigManager(tmp_path / "settings.json"),
        catalog,
    )
    progress = ActivityProgressService(
        ActivityProgressStore(tmp_path / "activity_progress.json")
    )
    for rule in catalog.all():
        progress.register_definition(rule.definition)
    descriptions.set_description("world-boss", "只進入已確認的 160 角色")
    now = datetime(2026, 7, 27, 14, 40, tzinfo=TAIPEI_TIMEZONE)
    progress.start("world-boss", "160-main", now)
    service = ActivityScheduleViewService(catalog, descriptions, progress)

    state = service.snapshot(now)
    world_boss = next(
        item for item in state.activities if item.activity_id == "world-boss"
    )

    assert world_boss.description == "只進入已確認的 160 角色"
    assert world_boss.eligibility_text == "僅 160 等級"
    assert world_boss.status_text == "執行中"
    assert world_boss.next_step == "等待可信完成判定"
    assert "敘述：只進入已確認的 160 角色" in _activity_schedule_text(state)


def test_schedule_status_uses_time_only_without_claiming_completion() -> None:
    service = _service()

    before = service.snapshot(
        datetime(2026, 7, 27, 14, 0, tzinfo=TAIPEI_TIMEZONE)
    )
    during = service.snapshot(
        datetime(2026, 7, 27, 14, 40, tzinfo=TAIPEI_TIMEZONE)
    )
    after = service.snapshot(
        datetime(2026, 7, 27, 15, 10, tzinfo=TAIPEI_TIMEZONE)
    )

    before_boss = next(
        item for item in before.activities if item.activity_id == "world-boss"
    )
    during_boss = next(
        item for item in during.activities if item.activity_id == "world-boss"
    )
    after_boss = next(
        item for item in after.activities if item.activity_id == "world-boss"
    )
    assert before_boss.status_text == "尚未開始"
    assert during_boss.status_text == "活動時段中"
    assert after_boss.status_text == "今日時段已結束"
    assert "已完成" not in {
        before_boss.status_text,
        during_boss.status_text,
        after_boss.status_text,
    }
