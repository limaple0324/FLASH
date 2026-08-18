from services.server_clock import (
    PROTOCOL_VERSION,
    ServerClock,
    ServerTimeSample,
    ServerTimeSourceIdentity,
)


IDENTITY = ServerTimeSourceIdentity(
    handle=100,
    process_id=200,
    thread_id=300,
    lifecycle=400,
    fingerprint="1f383b186a886c54a70800bdabfdc5c7a986fee50d63a089e7bfd17557b5b8d0",
)


def sample(server_now_ms=1_000_000, sequence=1, identity=IDENTITY):
    return ServerTimeSample(
        PROTOCOL_VERSION,
        identity,
        server_now_ms,
        123.0,
        sequence,
    )


def test_uncalibrated_clock_has_no_time_and_first_sample_calibrates_once():
    tick = [10_000_000_000]
    clock = ServerClock(monotonic_ns=lambda: tick[0], source_validator=lambda _: True)
    assert clock.now_ms() is None
    assert clock.calibrate_once(sample()) is True
    assert clock.calibration_count == 1
    tick[0] += 1_000_000_000
    assert clock.now_ms() == 1_001_000


def test_later_samples_cannot_replace_base_and_wall_clock_is_irrelevant():
    tick = [10_000_000_000]
    clock = ServerClock(monotonic_ns=lambda: tick[0], source_validator=lambda _: True)
    assert clock.calibrate_once(sample(2_000_000)) is True
    assert clock.calibrate_once(sample(9_000_000, sequence=2)) is False
    tick[0] += 2_000_000_000
    assert clock.now_ms() == 2_002_000
    assert clock.snapshot().calibration_count == 1


def test_invalid_values_and_identity_are_rejected():
    clock = ServerClock(source_validator=lambda identity: identity.process_id == 200)
    assert clock.calibrate_once(sample(float("nan"))) is False
    assert clock.calibrate_once(sample(-1)) is False
    assert clock.calibrate_once(sample(identity=ServerTimeSourceIdentity(
        handle=100,
        process_id=201,
        thread_id=300,
        lifecycle=400,
        fingerprint=IDENTITY.fingerprint,
    ))) is False


def test_bridge_disconnect_does_not_clear_calibration():
    tick = [1_000_000_000]
    clock = ServerClock(monotonic_ns=lambda: tick[0], source_validator=lambda _: True)
    assert clock.calibrate_once(sample(5_000)) is True
    tick[0] += 3_000_000_000
    assert clock.now_ms() == 8_000

