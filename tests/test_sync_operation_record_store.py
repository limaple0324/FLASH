from datetime import datetime, timedelta, timezone

from services.sync_operation_record_store import (
    SyncOperationRecordStore,
)


def test_daily_file_has_every_configured_role_and_groups_by_role(tmp_path):
    now = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
        now_provider=lambda: now,
        role_names_provider=lambda: ("角色甲", "角色乙", "角色丙"),
    )
    store.append("偵測", "角色乙", "已開啟")
    store.append("操作", "角色甲", "同步成功")
    path = store.daily_files()[0]
    text = path.read_text(encoding="utf-8")

    assert "【角色：角色甲】" in text
    assert "【角色：角色乙】" in text
    assert "【角色：角色丙】" in text
    assert text.index("【角色：角色甲】") < text.index("【角色：角色乙】")
    assert text.index("角色甲｜同步成功") < text.index("【角色：角色乙】")


def test_same_day_keeps_one_file_and_preserves_existing_records(tmp_path):
    now = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
        now_provider=lambda: now,
        role_names_provider=lambda: ("角色甲",),
    )
    first = store.append("操作", "角色甲", "第一次")
    second = store.append("操作", "角色甲", "第二次")

    assert len(store.daily_files()) == 1
    text = store.daily_files()[0].read_text(encoding="utf-8")
    assert first.record_id in text
    assert second.record_id in text
    assert text.index("第一次") < text.index("第二次")


def test_search_by_date_and_partial_role_opens_corresponding_daily_file(
    tmp_path,
):
    now = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
        now_provider=lambda: now,
        role_names_provider=lambda: ("100古", "120古"),
    )
    store.append("操作", "100古", "同步成功")
    store.append("操作", "120古", "啟動成功")

    results = store.search("2026-07-26", "100")

    assert len(results) == 1
    assert results[0].role_name == "100古"
    assert results[0].daily_file == store.daily_files()[0]


def test_records_leave_in_app_month_but_daily_text_remains_forever(tmp_path):
    current = [datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)]
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
        now_provider=lambda: current[0],
        role_names_provider=lambda: ("角色甲",),
    )
    old = store.append("操作", "角色甲", "很早以前")
    daily_path = store.daily_files()[0]

    current[0] += timedelta(days=31)
    store.archive_expired()

    assert store.records() == ()
    assert daily_path.is_file()
    assert old.record_id in daily_path.read_text(encoding="utf-8")
