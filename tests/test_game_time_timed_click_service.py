import time

from services.game_time_timed_click_service import (
    GameTimeTimedClickService,
    TimedClickPressReceipt,
    TimedClickTarget,
    parse_target_time_ms,
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
        token[1]()


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


def test_timed_click_uses_one_captured_role_and_exact_legacy_defaults():
    service, scheduler, clock, backend = make_service()

    assert service.capture_target().success is True
    armed = service.arm("00:00:01")
    assert armed.success is True
    assert armed.snapshot.auto_update is True
    assert armed.snapshot.lead_ms == 120
    assert armed.snapshot.repeat_count == 2
    assert armed.snapshot.repeat_interval_ms == 250

    scheduler.fire_next()
    clock.nanoseconds = 880_000_000
    scheduler.fire_next()
    while scheduler.calls:
        scheduler.fire_next()

    assert len(backend.presses) == 2
    assert backend.releases == backend.presses
    assert service.snapshot().status == "定時按下：已完成 2 次"


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
