import threading
import time

import pytest

from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from main import build_services
from services.app_context import AppContext
from services.group_selection_service import GroupSelectionService
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
    IdentityTransactionClosedError,
    IdentityTransactionStageError,
)
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService


def _activity() -> ActivityDefinition:
    return ActivityDefinition(
        activity_id="guard",
        name="守紀",
        activity_type=ActivityType.DAILY,
        reset_rule=ResetRule.DAILY_MIDNIGHT,
        max_completions=16,
    )


def _service(
    initial_state: WorkspaceState | None = None,
    *,
    coordinator: IdentityDataTransactionCoordinator | None = None,
) -> WorkspaceService:
    return WorkspaceService(
        coordinator or IdentityDataTransactionCoordinator(),
        initial_state,
    )


def test_service_starts_empty_and_can_use_a_known_initial_state():
    empty = _service()
    initial = WorkspaceState(next_step="選擇組別")
    restored = _service(initial)

    assert empty.state == WorkspaceState()
    assert empty.snapshot() is empty.state
    assert restored.state is initial
    assert restored.snapshot() is initial


def test_service_updates_only_the_requested_workspace_field():
    group = CharacterGroup(group_id="14-windows", name="14支")
    activity = _activity()
    service = _service()

    first = service.set_current_group(group)
    second = service.set_current_activity(activity)
    third = service.set_next_step("完成下一個角色")

    assert first.current_group is group
    assert first.current_activity is None
    assert second.current_group is group
    assert second.current_activity is activity
    assert third == WorkspaceState(group, activity, "完成下一個角色")


def test_service_can_clear_one_field_without_changing_the_others():
    group = CharacterGroup(group_id="dimension", name="魔心次元組")
    service = _service(WorkspaceState(group, _activity(), "繼續守紀"))

    state = service.set_current_activity(None)

    assert state.current_group is group
    assert state.current_activity is None
    assert state.next_step == "繼續守紀"


def test_service_clear_returns_to_an_empty_workspace():
    service = _service(WorkspaceState(next_step="選擇組別"))

    cleared = service.clear()

    assert cleared == WorkspaceState()
    assert service.state is cleared


def test_service_keeps_the_previous_state_when_an_update_is_invalid():
    initial = WorkspaceState(next_step="選擇組別")
    service = _service(initial)

    with pytest.raises(ValueError):
        service.set_next_step("   ")

    assert service.state is initial


def test_service_rejects_an_invalid_initial_state():
    with pytest.raises(TypeError):
        WorkspaceService(IdentityDataTransactionCoordinator(), object())


def test_service_notifies_subscribers_only_after_a_real_valid_change():
    service = _service()
    notifications = []

    def listener() -> None:
        notifications.append(service.snapshot())

    service.subscribe(listener)
    service.subscribe(listener)

    service.set_next_step("選擇組別")
    service.set_next_step("選擇組別")
    with pytest.raises(ValueError):
        service.set_next_step("   ")
    service.clear()

    assert notifications == [
        WorkspaceState(next_step="選擇組別"),
        WorkspaceState(),
    ]

    service.unsubscribe(listener)
    service.set_next_step("稍後再選")

    assert len(notifications) == 2


def test_service_rejects_a_non_callable_listener():
    service = _service()

    with pytest.raises(TypeError, match="listener"):
        service.subscribe(object())


def test_group_transaction_preserves_activity_and_next_step_changed_during_prepare():
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(coordinator=coordinator)
    group = CharacterGroup(group_id="group-a", name="甲組")
    activity = _activity()

    def prepare(transaction):
        service.stage_set_current_group(transaction, group)
        service.set_current_activity(activity)
        service.set_next_step("交易中更新")

    coordinator.execute(prepare)

    assert service.state == WorkspaceState(group, activity, "交易中更新")


def test_public_snapshot_waits_for_active_group_publication() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(coordinator=coordinator)
    group = CharacterGroup(group_id="group-a", name="甲組")
    transaction_entered = threading.Event()
    release_transaction = threading.Event()
    reader_started = threading.Event()
    reader_finished = threading.Event()
    observed = []

    def prepare(transaction) -> None:
        service.stage_set_current_group(transaction, group)
        transaction_entered.set()
        assert release_transaction.wait(2)

    def read_snapshot() -> None:
        reader_started.set()
        observed.append(service.snapshot())
        reader_finished.set()

    transaction_thread = threading.Thread(
        target=lambda: coordinator.execute(prepare)
    )
    reader_thread = threading.Thread(target=read_snapshot)
    transaction_thread.start()
    assert transaction_entered.wait(1)
    reader_thread.start()
    assert reader_started.wait(1)
    assert reader_finished.wait(0.05) is False
    release_transaction.set()
    transaction_thread.join(2)
    reader_thread.join(2)

    assert transaction_thread.is_alive() is False
    assert reader_thread.is_alive() is False
    assert observed == [WorkspaceState(current_group=group)]


def test_nonidentity_update_waits_for_group_commit_and_preserves_both() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(coordinator=coordinator)
    group = CharacterGroup(group_id="group-a", name="甲組")
    transaction_entered = threading.Event()
    release_transaction = threading.Event()
    writer_finished = threading.Event()

    def prepare(transaction) -> None:
        service.stage_set_current_group(transaction, group)
        transaction_entered.set()
        assert release_transaction.wait(2)

    transaction_thread = threading.Thread(
        target=lambda: coordinator.execute(prepare)
    )
    writer_thread = threading.Thread(
        target=lambda: (
            service.set_next_step("交易後步驟"),
            writer_finished.set(),
        )
    )
    transaction_thread.start()
    assert transaction_entered.wait(1)
    writer_thread.start()
    assert writer_finished.wait(0.05) is False
    release_transaction.set()
    transaction_thread.join(2)
    writer_thread.join(2)

    assert transaction_thread.is_alive() is False
    assert writer_thread.is_alive() is False
    assert service.state == WorkspaceState(
        current_group=group,
        next_step="交易後步驟",
    )


def test_current_group_publication_memory_hooks_reject_external_callers() -> None:
    service = _service()
    group = CharacterGroup(group_id="group-a", name="甲組")

    with pytest.raises(IdentityTransactionStageError, match="does not own"):
        service.current_group_for_publication()
    with pytest.raises(IdentityTransactionStageError, match="does not own"):
        service.install_current_group_from_publication(group)
    with pytest.raises(IdentityTransactionStageError, match="does not own"):
        service.restore_current_group_from_publication(group)

    assert service.state == WorkspaceState()


def test_failed_group_publication_and_waiting_nonidentity_update_lose_neither(
    monkeypatch,
) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    original_group = CharacterGroup(group_id="group-old", name="舊組")
    candidate_group = CharacterGroup(group_id="group-new", name="新組")
    service = _service(
        WorkspaceState(current_group=original_group),
        coordinator=coordinator,
    )
    transaction_entered = threading.Event()
    release_transaction = threading.Event()
    writer_finished = threading.Event()
    transaction_errors = []
    original_replace = service._replace_current_group
    replace_calls = 0

    def fail_first_replace(group) -> None:
        nonlocal replace_calls
        replace_calls += 1
        original_replace(group)
        if replace_calls == 1:
            raise OSError("workspace publication interrupted")

    monkeypatch.setattr(service, "_replace_current_group", fail_first_replace)

    def prepare(transaction) -> None:
        service.stage_set_current_group(transaction, candidate_group)
        transaction_entered.set()
        assert release_transaction.wait(2)

    def publish() -> None:
        try:
            coordinator.execute(prepare)
        except BaseException as error:
            transaction_errors.append(error)

    def update_next_step() -> None:
        service.set_next_step("回復後步驟")
        writer_finished.set()

    transaction_thread = threading.Thread(target=publish)
    writer_thread = threading.Thread(target=update_next_step)
    transaction_thread.start()
    assert transaction_entered.wait(1)
    writer_thread.start()
    assert writer_finished.wait(0.05) is False
    release_transaction.set()
    transaction_thread.join(2)
    writer_thread.join(2)

    assert len(transaction_errors) == 1
    assert isinstance(transaction_errors[0], OSError)
    assert str(transaction_errors[0]) == "workspace publication interrupted"
    assert service.state == WorkspaceState(
        current_group=original_group,
        next_step="回復後步驟",
    )


def test_listener_subscription_changes_do_not_corrupt_active_notification() -> None:
    service = _service()
    first_entered = threading.Event()
    release_first = threading.Event()
    calls = []

    def first_listener() -> None:
        calls.append("first")
        first_entered.set()
        assert release_first.wait(2)

    def second_listener() -> None:
        calls.append("second")

    def third_listener() -> None:
        calls.append("third")

    service.subscribe(first_listener)
    service.subscribe(second_listener)
    notify_thread = threading.Thread(
        target=lambda: service.set_next_step("第一輪")
    )
    notify_thread.start()
    assert first_entered.wait(1)

    service.unsubscribe(second_listener)
    service.subscribe(third_listener)
    release_first.set()
    notify_thread.join(2)

    assert notify_thread.is_alive() is False
    assert calls == ["first", "second"]

    service.set_next_step("第二輪")
    assert calls == ["first", "second", "first", "third"]


def test_workspace_read_fails_promptly_after_shutdown_starts() -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(coordinator=coordinator)
    transaction_entered = threading.Event()
    release_transaction = threading.Event()
    close_results = []

    def prepare(_transaction) -> None:
        transaction_entered.set()
        assert release_transaction.wait(2)

    transaction_thread = threading.Thread(
        target=lambda: coordinator.execute(prepare)
    )
    close_thread = threading.Thread(
        target=lambda: close_results.append(coordinator.close_and_wait(2))
    )
    transaction_thread.start()
    assert transaction_entered.wait(1)
    close_thread.start()
    deadline = time.monotonic() + 1
    while not coordinator.is_closing and time.monotonic() < deadline:
        time.sleep(0.005)

    started = time.monotonic()
    with pytest.raises(IdentityTransactionClosedError):
        service.snapshot()
    assert time.monotonic() - started < 0.5

    release_transaction.set()
    transaction_thread.join(2)
    close_thread.join(2)
    assert close_results == [True]


def test_outer_transaction_notifies_once_only_after_successful_group_commit():
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(coordinator=coordinator)
    group = CharacterGroup(group_id="group-a", name="甲組")
    notifications = []
    service.subscribe(lambda: notifications.append(service.snapshot()))

    changed = coordinator.execute(
        lambda transaction: service.stage_set_current_group(transaction, group)
    )

    assert changed is True
    assert notifications == []
    service.notify_current_group_committed(changed)
    assert notifications == [WorkspaceState(current_group=group)]


def test_outer_transaction_does_not_notify_when_group_is_unchanged():
    coordinator = IdentityDataTransactionCoordinator()
    group = CharacterGroup(group_id="group-a", name="甲組")
    service = _service(WorkspaceState(current_group=group), coordinator=coordinator)
    notifications = []
    service.subscribe(lambda: notifications.append(service.snapshot()))

    changed = coordinator.execute(
        lambda transaction: service.stage_set_current_group(transaction, group)
    )
    service.notify_current_group_committed(changed)

    assert changed is False
    assert notifications == []


def test_listener_exception_happens_after_group_state_is_committed():
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(coordinator=coordinator)
    group = CharacterGroup(group_id="group-a", name="甲組")

    def fail_listener() -> None:
        raise RuntimeError("listener failed")

    service.subscribe(fail_listener)

    with pytest.raises(RuntimeError, match="listener failed"):
        service.set_current_group(group)

    assert service.state.current_group is group


def test_group_publish_failure_restores_state_and_never_notifies(
    monkeypatch,
):
    coordinator = IdentityDataTransactionCoordinator()
    initial = WorkspaceState(
        current_activity=_activity(),
        next_step="原步驟",
    )
    service = _service(initial, coordinator=coordinator)
    notifications = []
    service.subscribe(lambda: notifications.append(service.snapshot()))
    original_replace = service._replace_current_group
    calls = 0

    def fail_first(group):
        nonlocal calls
        calls += 1
        original_replace(group)
        if calls == 1:
            raise OSError("workspace publish interrupted")

    monkeypatch.setattr(service, "_replace_current_group", fail_first)

    with pytest.raises(OSError, match="workspace publish interrupted"):
        service.set_current_group(CharacterGroup(group_id="group-a", name="甲組"))

    assert service.state == initial
    assert notifications == []


def test_workspace_access_is_rejected_after_identity_transaction_shutdown():
    coordinator = IdentityDataTransactionCoordinator()
    service = _service(
        WorkspaceState(current_activity=_activity(), next_step="待清除"),
        coordinator=coordinator,
    )
    assert coordinator.close_and_wait()

    with pytest.raises(IdentityTransactionClosedError):
        service.snapshot()
    with pytest.raises(IdentityTransactionClosedError):
        service.clear()


def test_build_services_registers_clean_sp3_workspace_without_legacy_groups(
    tmp_path,
):
    build_services(root=tmp_path)

    service = AppContext.get(WorkspaceService)
    groups = AppContext.get(GroupSelectionService)

    assert isinstance(service, WorkspaceService)
    assert service.snapshot() == WorkspaceState(next_step="選擇組別")
    assert groups.choices() == ()
