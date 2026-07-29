from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
)
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_window import WindowInfo
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


def test_hot_sync_records_are_coalesced_into_one_persistence_batch(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "services.sync_operation_record_store.DEFERRED_FLUSH_SECONDS",
        60,
    )
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
        role_names_provider=lambda: tuple(
            f"角色{index}" for index in range(14)
        ),
    )
    daily_batches = []
    save_calls = []
    monkeypatch.setattr(
        store,
        "_append_daily_records",
        lambda items: daily_batches.append(items)
        or (tmp_path / "daily.txt",),
    )
    monkeypatch.setattr(
        store,
        "_save_records",
        lambda _items: save_calls.append(True),
    )

    for index in range(14):
        store.append_deferred(
            "同步操作",
            f"角色{index}",
            "同步左鍵－成功",
        )

    assert len(store.records()) == 14
    assert daily_batches == []
    assert save_calls == []
    assert store.flush() is True
    assert len(daily_batches) == 1
    assert len(daily_batches[0]) == 14
    assert save_calls == [True]
    assert store.close() is True


def test_close_flushes_deferred_records_without_data_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "services.sync_operation_record_store.DEFERRED_FLUSH_SECONDS",
        60,
    )
    active = tmp_path / "active.json"
    daily = tmp_path / "daily"
    store = SyncOperationRecordStore(active, daily)

    record = store.append_deferred("同步操作", "角色甲", "同步左鍵－成功")

    assert store.close() is True
    reloaded = SyncOperationRecordStore(active, daily)
    assert [item.record_id for item in reloaded.records()] == [
        record.record_id
    ]
    assert record.record_id in reloaded.daily_files()[0].read_text(
        encoding="utf-8"
    )
    assert reloaded.close() is True


def test_pending_journal_recovers_interrupted_deferred_flush(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "services.sync_operation_record_store.DEFERRED_FLUSH_SECONDS",
        60,
    )
    active = tmp_path / "active.json"
    daily = tmp_path / "daily"
    interrupted = SyncOperationRecordStore(active, daily)

    record = interrupted.append_deferred(
        "同步操作",
        "角色甲",
        "同步左鍵－成功",
    )
    with interrupted._lock:
        interrupted._cancel_flush_timer_locked()
        interrupted._closed = True

    recovered = SyncOperationRecordStore(active, daily)

    assert [item.record_id for item in recovered.records()] == [
        record.record_id
    ]
    assert record.record_id in recovered.daily_files()[0].read_text(
        encoding="utf-8"
    )
    assert not recovered.pending_path.exists()
    assert recovered.close() is True


def test_three_keyboard_and_mouse_actions_finish_without_per_role_rewrite(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "services.sync_operation_record_store.DEFERRED_FLUSH_SECONDS",
        60,
    )
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
    )
    daily_batches = []
    save_calls = []
    monkeypatch.setattr(
        store,
        "_append_daily_records",
        lambda items: daily_batches.append(items)
        or (tmp_path / "daily.txt",),
    )
    monkeypatch.setattr(
        store,
        "_save_records",
        lambda _items: save_calls.append(True),
    )
    windows = tuple(
        WindowInfo(
            handle=index,
            title="Adobe Flash Player 11",
            visible=True,
            minimized=False,
            rect=(0, 0, 916, 629),
            process_id=1000 + index,
            window_class="ShockwaveFlash",
            launch_fingerprint=f"{index:064x}",
        )
        for index in range(1, 15)
    )

    class WindowBackend:
        def list_windows(self):
            return list(windows)

        def foreground_handle(self):
            return 1

    class MessageBackend:
        def __init__(self):
            self.keys = []
            self.pointers = []

        def is_window(self, _handle):
            return True

        def probe_responsive(self, _handle, _timeout_ms):
            return True

        def send_virtual_key(self, handle, virtual_key):
            self.keys.append((handle, virtual_key))
            return True

        def send_key_chord(self, handle, virtual_keys):
            self.keys.append((handle, virtual_keys))
            return True

        def send_pointer(self, handle, x, y, event):
            self.pointers.append((handle, x, y, event))
            return True

    messages = MessageBackend()

    def record(fingerprint, operation, outcome):
        store.append_deferred(
            "同步操作",
            fingerprint,
            f"{operation}－{outcome}",
        )

    keyboard = WindowsInputSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=WindowBackend(),
        message_backend=messages,
        allowed_fingerprints=(
            window.launch_fingerprint for window in windows
        ),
        role_operation_callback=record,
        target_windows_provider=lambda: windows,
    )
    pointer = WindowsPointerSyncController(
        expected_windows=14,
        title_keywords=("Adobe Flash Player",),
        window_backend=WindowBackend(),
        message_backend=messages,
        role_operation_callback=record,
        target_windows_provider=lambda: windows,
    )
    fingerprints = tuple(
        window.launch_fingerprint for window in windows
    )
    keyboard.set_controller_fingerprint(fingerprints[0])
    pointer.set_allowed_fingerprints(fingerprints)
    pointer.set_controller_fingerprint(fingerprints[0])
    try:
        keyboard_results = [
            keyboard.send_approved_key(
                "ESC",
                policy=WindowInputPolicy.ALL,
                execute=True,
                exclude_foreground=True,
                source_handle=1,
            )
            for _index in range(3)
        ]
        pointer_results = [
            pointer.send_click(
                source_handle=1,
                x_ratio=0.5,
                y_ratio=0.5,
                policy=WindowInputPolicy.ALL,
                include_source=False,
            )
            for _index in range(3)
        ]

        assert all(result.passed for result in keyboard_results)
        assert all(result.passed for result in pointer_results)
        assert len(messages.keys) == 39
        assert len(messages.pointers) == 78
        assert len(store.records()) == 78
        assert daily_batches == []
        assert save_calls == []
        assert store.flush() is True
        assert len(daily_batches) == 1
        assert len(daily_batches[0]) == 78
        assert save_calls == [True]
    finally:
        assert keyboard.close() is True
        assert pointer.close() is True
        assert store.close() is True


def test_background_persistence_never_holds_the_hot_path_state_lock(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "services.sync_operation_record_store.DEFERRED_FLUSH_SECONDS",
        60,
    )
    store = SyncOperationRecordStore(
        tmp_path / "active.json",
        tmp_path / "daily",
    )
    persistence_started = Event()
    release_persistence = Event()

    def blocked_daily_write(items):
        persistence_started.set()
        assert release_persistence.wait(1)
        return (tmp_path / "daily.txt",)

    monkeypatch.setattr(store, "_append_daily_records", blocked_daily_write)
    monkeypatch.setattr(store, "_save_records", lambda _items: None)
    store.append_deferred("同步操作", "角色甲", "第一次")
    background = Thread(target=store._flush_pending_once)
    background.start()
    assert persistence_started.wait(0.5)

    queued = Event()

    def queue_next_record():
        store.append_deferred("同步操作", "角色乙", "第二次")
        queued.set()

    hot_path = Thread(target=queue_next_record)
    hot_path.start()
    try:
        assert queued.wait(0.25)
    finally:
        release_persistence.set()
        hot_path.join(1)
        background.join(1)

    assert len(store.records()) == 2
    assert store.close() is True


def test_busy_pending_journal_keeps_record_in_memory_and_retries(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "services.sync_operation_record_store.DEFERRED_FLUSH_SECONDS",
        60,
    )
    active = tmp_path / "active.json"
    daily = tmp_path / "daily"
    store = SyncOperationRecordStore(active, daily)
    original_journal_append = store._append_pending_journal
    monkeypatch.setattr(
        store,
        "_append_pending_journal",
        lambda _item: (_ for _ in ()).throw(OSError("busy")),
    )

    record = store.append_deferred(
        "同步操作",
        "角色甲",
        "同步左鍵－成功",
    )

    assert [item.record_id for item in store.records()] == [record.record_id]
    assert store.persistence_failure == "record_pending_journal_write_failed"

    monkeypatch.setattr(
        store,
        "_append_pending_journal",
        original_journal_append,
    )
    assert store.flush() is True
    assert store.persistence_failure is None
    assert record.record_id in active.read_text(encoding="utf-8")
    assert record.record_id in store.daily_files()[0].read_text(
        encoding="utf-8"
    )
    assert store.close() is True
