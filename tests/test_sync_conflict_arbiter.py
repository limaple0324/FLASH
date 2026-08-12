import threading
import time

from services.sync_conflict_arbiter import SyncConflictArbiter


def test_first_different_operation_wins_and_conflict_is_recorded():
    conflicts = []
    arbiter = SyncConflictArbiter(
        clock=lambda: 12.5,
        on_conflict=conflicts.append,
    )
    first = arbiter.try_begin("角色甲", "key:B")

    second = arbiter.try_begin("角色甲", "key:C")

    assert first is not None
    assert second is None
    assert conflicts[0].target_id == "角色甲"
    assert conflicts[0].active_operation == "key:B"
    assert conflicts[0].skipped_operation == "key:C"
    assert conflicts[0].occurred_at == 12.5


def test_different_roles_and_same_operations_are_not_conflicts():
    conflicts = []
    arbiter = SyncConflictArbiter(on_conflict=conflicts.append)
    first = arbiter.try_begin("角色甲", "key:B")
    other = arbiter.try_begin("角色乙", "key:C")
    acquired = []

    def acquire_same():
        lease = arbiter.try_begin("角色甲", "key:B")
        acquired.append(lease)

    thread = threading.Thread(target=acquire_same)
    thread.start()

    assert first is not None
    assert other is not None
    assert acquired == []
    assert conflicts == []
    first.release()
    thread.join(1.0)
    assert len(acquired) == 1
    assert acquired[0] is not None
    acquired[0].release()
    other.release()
    assert arbiter.try_begin("角色甲", "key:C") is not None


def test_same_operation_queue_preserves_arrival_order_and_runs_twice():
    conflicts = []
    arbiter = SyncConflictArbiter(on_conflict=conflicts.append)
    first = arbiter.try_begin("角色甲", "key:B")
    order = []

    def wait_for_turn(value):
        lease = arbiter.try_begin("角色甲", "key:B")
        order.append(value)
        lease.release()

    second = threading.Thread(target=wait_for_turn, args=(2,))
    third = threading.Thread(target=wait_for_turn, args=(3,))
    second.start()
    deadline = time.monotonic() + 1.0
    while arbiter.waiting_count("角色甲") < 1 and time.monotonic() < deadline:
        time.sleep(0.001)
    third.start()
    first.release()
    second.join(1.0)
    third.join(1.0)

    assert order == [2, 3]
    assert conflicts == []
