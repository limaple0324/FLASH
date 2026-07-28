import threading
import time

from services.game_operation_gate import GameOperationGate


def test_gate_serializes_different_game_operations():
    gate = GameOperationGate()
    first_entered = threading.Event()
    allow_first_release = threading.Event()
    order = []

    def first():
        lease = gate.acquire("同步")
        assert lease is not None
        order.append("同步開始")
        first_entered.set()
        allow_first_release.wait(1.0)
        order.append("同步結束")
        lease.release()

    def second():
        first_entered.wait(1.0)
        lease = gate.acquire("重連")
        assert lease is not None
        order.append("重連開始")
        lease.release()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(1.0)
    time.sleep(0.02)
    assert gate.snapshot().active_operation == "同步"
    allow_first_release.set()
    first_thread.join(1.0)
    second_thread.join(1.0)

    assert order == ["同步開始", "同步結束", "重連開始"]
    assert gate.snapshot().active_operation is None


def test_closing_gate_rejects_waiting_work_and_waits_for_active_batch():
    gate = GameOperationGate()
    active = gate.acquire("目前批次")
    assert active is not None
    waiting_result = []

    waiter = threading.Thread(
        target=lambda: waiting_result.append(gate.acquire("後到操作"))
    )
    waiter.start()
    while gate.snapshot().waiting_operations < 1:
        time.sleep(0.005)

    close_result = []
    closer = threading.Thread(
        target=lambda: close_result.append(gate.close_and_wait(1.0))
    )
    closer.start()
    waiter.join(1.0)
    assert waiting_result == [None]
    assert closer.is_alive()

    active.release()
    closer.join(1.0)
    assert close_result == [True]
    assert gate.snapshot().open is False
    assert gate.acquire("禁止的新操作") is None

    gate.reopen()
    lease = gate.acquire("新組別操作", timeout_seconds=0)
    assert lease is not None
    lease.release()


def test_gate_lease_releases_after_fault_injection():
    gate = GameOperationGate()

    try:
        with gate.acquire("故障注入"):
            raise RuntimeError("fault injection")
    except RuntimeError:
        pass

    assert gate.snapshot().active_operation is None
    lease = gate.acquire("故障後操作", timeout_seconds=0)
    assert lease is not None
    lease.release()


def test_close_timeout_restores_previous_open_state():
    gate = GameOperationGate()
    active = gate.acquire("尚未完成")
    assert active is not None

    assert gate.close_and_wait(0) is False
    snapshot = gate.snapshot()
    assert snapshot.open is True
    assert snapshot.active_operation == "尚未完成"

    active.release()
    next_lease = gate.acquire("原組別後續操作", timeout_seconds=0)
    assert next_lease is not None
    next_lease.release()
