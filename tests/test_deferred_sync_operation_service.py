import time

from services.deferred_sync_operation_service import (
    DeferredSyncOperationService,
)


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_pending_operations_survive_restart_and_keep_original_order(tmp_path):
    state_path = tmp_path / "deferred.json"
    first = DeferredSyncOperationService(state_path=state_path)
    first.enqueue(
        "角色甲",
        "key:B",
        kind="keyboard",
        payload={"key": "B"},
    )
    first.enqueue(
        "角色甲",
        "key:C",
        kind="keyboard",
        payload={"key": "C"},
    )

    delivered = []
    restarted = DeferredSyncOperationService(state_path=state_path)
    restarted.register_handler(
        "keyboard",
        lambda target, payload: (
            delivered.append((target, payload["key"])) or True
        ),
    )
    restarted.process_ready(
        reconnecting_targets=(),
        failed_targets=(),
        ready_targets=("角色甲",),
    )

    assert wait_until(lambda: restarted.pending() == 0)
    assert delivered == [("角色甲", "B"), ("角色甲", "C")]


def test_pending_has_no_expiry_and_is_not_removed_when_not_ready(tmp_path):
    service = DeferredSyncOperationService(
        state_path=tmp_path / "deferred.json"
    )
    service.enqueue(
        "角色甲",
        "key:B",
        kind="keyboard",
        payload={"key": "B"},
    )

    for _ in range(10):
        service.process_ready(
            reconnecting_targets=("角色甲",),
            failed_targets=(),
            ready_targets=(),
        )

    assert service.pending("角色甲") == 1
    assert service.failures() == ()


def test_unsafe_first_operation_permanently_cancels_all_later_items():
    failures = []
    service = DeferredSyncOperationService(on_failure=failures.append)
    service.register_handler("keyboard", lambda _target, _payload: False)
    for key in ("B", "C", "W"):
        service.enqueue(
            "角色甲",
            f"key:{key}",
            kind="keyboard",
            payload={"key": key},
        )

    service.process_ready(
        reconnecting_targets=(),
        failed_targets=(),
        ready_targets=("角色甲",),
    )

    assert wait_until(lambda: service.pending() == 0)
    assert [item.failure_code for item in failures] == [
        "operation_screen_not_safe",
        "stopped_after_unsafe_operation",
        "stopped_after_unsafe_operation",
    ]


def test_failed_reconnect_keeps_group_paused_for_exact_role_restart():
    delivered = []
    failures = []
    service = DeferredSyncOperationService(on_failure=failures.append)
    service.enqueue(
        "角色甲",
        "key:B",
        lambda: delivered.append("B") or True,
    )

    service.process_ready(
        reconnecting_targets=(),
        failed_targets=("角色甲",),
        ready_targets=("角色甲",),
    )

    assert delivered == []
    assert service.pending() == 1
    assert failures == []
