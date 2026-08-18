import time

from services.game_time_timed_click_service import (
    DAY_MS,
    DEFAULT_TIMED_CLICK_INTERVAL_MS,
    DEFAULT_TIMED_CLICK_LEAD_MS,
    DEFAULT_TIMED_CLICK_REPEAT_COUNT,
    GameTimeTimedClickService,
    TAIPEI_UTC_OFFSET_MS,
    TimedClickPressReceipt,
    TimedClickTarget,
    parse_target_time_ms,
)
from services.game_operation_gate import GameOperationGate
from services.server_clock import (
    ServerClock,
    ServerTimeSample,
    ServerTimeSourceIdentity,
)


FINGERPRINT = "a" * 64


class Scheduler:
    def __init__(self):
        self.calls = []
        self.cancelled = []

    def schedule(self, delay, callback):
        token = [delay, callback]
        self.calls.append(token)
        return token

    def cancel(self, token):
        self.cancelled.append(token)
        if token in self.calls:
            self.calls.remove(token)

    def fire_next(self):
        token = min(self.calls, key=lambda item: item[0])
        self.calls.remove(token)
        return token[1]()


class Clock:
    def __init__(self):
        self.nanoseconds = 0

    def read(self):
        return self.nanoseconds


class Backend:
    def __init__(self):
        self.target = TimedClickTarget(FINGERPRINT, 0.25, 0.75, "120古")
        self.presses = []
        self.releases = []

    def capture_target(self, _allowed):
        return self.target

    def press(self, target, allowed):
        if target.fingerprint not in allowed:
            return None
        receipt = TimedClickPressReceipt(
            len(self.presses) + 1,
            target.x_ratio,
            target.y_ratio,
        )
        self.presses.append(receipt)
        return receipt

    def release(self, receipt):
        self.releases.append(receipt)
        return True


def make_service():
    scheduler = Scheduler()
    clock = Clock()
    backend = Backend()
    service = GameTimeTimedClickService(
        backend,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        allowed_fingerprints_provider=lambda: (FINGERPRINT,),
        wall_clock_ns=clock.read,
        localtime=time.gmtime,
    )
    return service, scheduler, clock, backend


def test_legacy_target_time_formats_are_parsed_without_guessing():
    assert parse_target_time_ms("2137") == 77_820_000
    assert parse_target_time_ms("21:37") == 77_820_000
    assert parse_target_time_ms("21:37:01.2") == 77_821_200
    assert parse_target_time_ms("213701250") == 77_821_250
    assert parse_target_time_ms("24:00") is None
    assert parse_target_time_ms("文字") is None


def test_timed_click_uses_one_captured_role_and_repeats_daily_with_new_defaults():
    service, scheduler, clock, backend = make_service()

    assert service.capture_target().success is True
    armed = service.arm("00:00:01")
    assert armed.success is True
    assert armed.snapshot.auto_update is True
    assert armed.snapshot.lead_ms == DEFAULT_TIMED_CLICK_LEAD_MS
    assert armed.snapshot.repeat_count == DEFAULT_TIMED_CLICK_REPEAT_COUNT
    assert armed.snapshot.repeat_interval_ms == DEFAULT_TIMED_CLICK_INTERVAL_MS

    clock.nanoseconds = 1_000_000_000
    assert scheduler.fire_next().action == "fire"
    assert len(backend.presses) == 0
    scheduler.fire_next()
    assert len(backend.presses) == 1
    assert backend.releases == []
    scheduler.fire_next()
    assert backend.releases == backend.presses
    for _ in range(4):
        scheduler.fire_next()

    assert len(backend.presses) == 3
    assert backend.releases == backend.presses
    assert service.snapshot().enabled is True
    assert service.snapshot().status == "定時按下：今日已完成 3 次，等待明日。"
    assert len(scheduler.calls) == 1

    clock.nanoseconds += DAY_MS * 1_000_000
    assert scheduler.fire_next().action == "fire"
    for _ in range(6):
        scheduler.fire_next()

    assert len(backend.presses) == 6
    assert backend.releases == backend.presses
    assert service.snapshot().enabled is True


def test_signed_timing_correction_can_advance_or_delay_the_daily_click():
    cases = (
        (100, 28_799_900_000_000),
        (-100, 28_800_100_000_000),
    )
    for correction_ms, clock_ns in cases:
        service, scheduler, clock, backend = make_service()
        clock.nanoseconds = clock_ns
        assert service.capture_target().success is True
        armed = service.arm(
            "08:00:00.000",
            lead_ms=correction_ms,
            repeat_count=1,
            repeat_interval_ms=0,
        )

        assert armed.success is True
        assert armed.snapshot.lead_ms == correction_ms
        assert scheduler.fire_next().action == "fire"
        scheduler.fire_next()
        scheduler.fire_next()
        assert len(backend.presses) == 1
        assert backend.releases == backend.presses


def test_group_identity_change_fails_closed_without_sending_input():
    scheduler = Scheduler()
    backend = Backend()
    allowed = [FINGERPRINT]
    service = GameTimeTimedClickService(
        backend,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        allowed_fingerprints_provider=lambda: tuple(allowed),
        wall_clock_ns=lambda: 0,
        localtime=time.gmtime,
    )
    service.capture_target()
    allowed[:] = ["b" * 64]

    result = service.arm("00:00:01")

    assert result.success is False
    assert result.failure_code == "target_not_in_current_group"
    assert backend.presses == []
    assert scheduler.calls == []


def test_stop_cancels_pending_work_and_forgets_captured_target():
    service, scheduler, _clock, backend = make_service()
    service.capture_target()
    service.arm("00:00:01")

    service.stop()

    assert scheduler.calls == []
    assert service.snapshot().target is None
    assert backend.presses == []


def test_timed_click_fails_without_sending_when_reconnect_owns_gate():
    scheduler = Scheduler()
    backend = Backend()
    gate = GameOperationGate()
    active = gate.acquire("智慧重連")
    assert active is not None
    service = GameTimeTimedClickService(
        backend,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        allowed_fingerprints_provider=lambda: (FINGERPRINT,),
        wall_clock_ns=lambda: 0,
        localtime=time.gmtime,
        operation_gate=gate,
    )
    service.capture_target()
    service._press_once(service.snapshot().target)

    assert backend.presses == []
    assert "其他遊戲操作" in service.snapshot().status
    active.release()


def test_one_timed_press_reports_fourteen_synchronized_windows():
    scheduler = Scheduler()
    synchronized_handles = tuple(range(1, 15))

    class FourteenWindowBackend(Backend):
        def press(self, target, allowed):
            if target.fingerprint not in allowed:
                return None
            receipt = TimedClickPressReceipt(
                synchronized_handles[0],
                target.x_ratio,
                target.y_ratio,
                synchronized_handles,
            )
            self.presses.append(receipt)
            return receipt

    group_backend = FourteenWindowBackend()
    service = GameTimeTimedClickService(
        group_backend,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        allowed_fingerprints_provider=lambda: (FINGERPRINT,),
        wall_clock_ns=lambda: 0,
        localtime=time.gmtime,
    )

    service._press_once(group_backend.target)

    assert len(group_backend.presses) == 1
    assert group_backend.presses[0].handles == synchronized_handles
    assert service.snapshot().status == (
        "定時按下：已連點 1 次（同步 14 個視窗）"
    )


def test_server_clock_source_never_falls_back_to_system_time_or_manual_offset():
    scheduler = Scheduler()
    backend = Backend()
    monotonic = [10_000_000_000]
    identity = ServerTimeSourceIdentity(1, 2, 3, 4, FINGERPRINT)
    server_clock = ServerClock(
        monotonic_ns=lambda: monotonic[0],
        source_validator=lambda value: value == identity,
    )
    service = GameTimeTimedClickService(
        backend,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        allowed_fingerprints_provider=lambda: (FINGERPRINT,),
        wall_clock_ns=lambda: 99_999_999_999,
        localtime=time.gmtime,
        server_clock=server_clock,
    )

    service.configure_game_time(offset_ms=60_000, auto_update=True)
    assert service.snapshot().source == "遊戲伺服器時間"
    assert service.snapshot().current_time_ms is None
    assert service.snapshot().current_time_text == "尚未校正"
    service.capture_target()
    assert service.arm("00:00:01").failure_code == "server_time_uncalibrated"

    assert server_clock.calibrate_once(
        ServerTimeSample(1, identity, 86_401_234, 7, 1)
    ) is True
    assert service.snapshot().offset_ms == 0
    assert service.snapshot().current_time_ms == TAIPEI_UTC_OFFSET_MS + 1_234
    assert service.snapshot().current_time_text == "08:00:01.234"
    monotonic[0] += 1_000_000_000
    assert service.snapshot().current_time_ms == TAIPEI_UTC_OFFSET_MS + 2_234
    assert service.snapshot().current_time_text == "08:00:02.234"

    armed = service.arm("08:00:03.234")
    assert armed.success is True
    monotonic[0] += 1_000_000_000
    assert scheduler.fire_next().action == "fire"
    for _ in range(6):
        scheduler.fire_next()

    assert len(backend.presses) == 3
    assert backend.releases == backend.presses
    assert service.snapshot().enabled is True


def test_server_clock_taipei_conversion_crosses_midnight_without_recalibration():
    scheduler = Scheduler()
    backend = Backend()
    monotonic = [10_000_000_000]
    identity = ServerTimeSourceIdentity(1, 2, 3, 4, FINGERPRINT)
    server_clock = ServerClock(
        monotonic_ns=lambda: monotonic[0],
        source_validator=lambda value: value == identity,
    )
    service = GameTimeTimedClickService(
        backend,
        schedule=scheduler.schedule,
        cancel=scheduler.cancel,
        allowed_fingerprints_provider=lambda: (FINGERPRINT,),
        server_clock=server_clock,
    )

    assert server_clock.calibrate_once(
        ServerTimeSample(1, identity, 57_599_999, 7, 1)
    ) is True
    assert service.snapshot().current_time_text == "23:59:59.999"

    monotonic[0] += 1_000_000

    assert service.snapshot().current_time_text == "00:00:00.000"
    assert server_clock.calibration_count == 1
